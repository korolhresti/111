import os
import cv2
import json
import time
import asyncio
import random
import hashlib
import logging
import shutil
import pathlib
import secrets
import string
import warnings
from datetime import datetime
from typing import List, Dict, Optional, Any

import numpy as np
import aiohttp
import aiofiles
from aiohttp import web
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

import torch
from ultralytics import YOLO

from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    ReplyKeyboardMarkup, 
    ReplyKeyboardRemove,
    WebAppInfo
)
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    CallbackQueryHandler, 
    MessageHandler, 
    ContextTypes, 
    filters,
    ConversationManager
)

# Вимикаємо зайві попередження
warnings.filterwarnings("ignore", category=UserWarning)

# =============================================================================
# 1. КОНФІГУРАЦІЯ ТА ГЛОБАЛЬНІ НАЛАШТУВАННЯ
# =============================================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID")
PORT = int(os.getenv("PORT", "10000"))

BASE_DIR = pathlib.Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"
TARGETS_DIR = IMAGES_DIR / "targets"
TEMP_DIR = DATA_DIR / "temp"

# Створення ієрархії папок
for folder in [DATA_DIR, IMAGES_DIR, TARGETS_DIR, TEMP_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

# Файли бази даних
DB_TARGETS = DATA_DIR / "targets.json"
DB_HISTORY = DATA_DIR / "history.json"
DB_STATS = DATA_DIR / "price_stats.json"
DB_DEALS = DATA_DIR / "super_deals.json"
DB_SETTINGS = DATA_DIR / "settings.json"

# Константи алгоритмів
SIMILARITY_THRESHOLD = 0.82
ORB_FEATURES = 2000
SUPER_DEAL_DISCOUNT = 0.35
MIN_HISTORY_FOR_STATS = 5
SCAN_INTERVAL = 600  # 10 хвилин

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("CollectorBotPRO")

# =============================================================================
# 2. МОДЕЛЬ ДАНИХ ТА СИСТЕМА ЗБЕРЕЖЕННЯ
# =============================================================================

class Database:
    @staticmethod
    def load(file_path: pathlib.Path, default: Any) -> Any:
        if not file_path.exists():
            return default
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {file_path}: {e}")
            return default

    @staticmethod
    def save(file_path: pathlib.Path, data: Any):
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving {file_path}: {e}")

# =============================================================================
# 3. СИСТЕМА КОМП'ЮТЕРНОГО ЗОРУ (CV ENGINE)
# =============================================================================

class CVEngine:
    def __init__(self):
        self.orb = cv2.ORB_create(nfeatures=ORB_FEATURES)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        self.yolo = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

    def load_yolo(self):
        if self.yolo is None:
            logger.info(f"Loading YOLOv8n on {self.device}...")
            self.yolo = YOLO("yolov8n.pt")
            self.yolo.to(self.device)
        return self.yolo

    def get_image_fingerprint(self, image_path: str):
        img = cv2.imread(image_path)
        if img is None: return None
        
        # 1. Колірна гістограма (HSV)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        
        # 2. Ключові точки ORB
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        kp, des = self.orb.detectAndCompute(gray, None)
        
        return {"hist": hist, "des": des, "kp_len": len(kp)}

    def compare(self, path1: str, path2: str) -> float:
        f1 = self.get_image_fingerprint(path1)
        f2 = self.get_image_fingerprint(path2)
        
        if not f1 or not f2 or f1["des"] is None or f2["des"] is None:
            return 0.0

        # Hist Score (30%)
        hist_score = cv2.compareHist(f1["hist"], f2["hist"], cv2.HISTCMP_CORREL)
        hist_score = max(0, hist_score)

        # ORB Score (70%)
        matches = self.bf.match(f1["des"], f2["des"])
        matches = sorted(matches, key=lambda x: x.distance)
        good_matches = [m for m in matches if m.distance < 40]
        
        orb_score = len(good_matches) / max(f1["kp_len"], f2["kp_len"], 1)
        orb_score = min(1.0, orb_score * 5) # Підсилення сигналу

        final_score = (hist_score * 0.3) + (orb_score * 0.7)
        return float(final_score)

engine = CVEngine()

# =============================================================================
# 4. МОДУЛЬ ПАРСИНГУ ТА МОНІТОРИНГУ
# =============================================================================

class OLXScanner:
    def __init__(self):
        self.ua = UserAgent()
        self.running = False

    async def get_page(self, session, url):
        headers = {"User-Agent": self.ua.random}
        async with session.get(url, headers=headers, timeout=20) as response:
            if response.status == 200:
                return await response.text()
            return None

    async def scan_item(self, target: Dict, context: ContextTypes.DEFAULT_TYPE):
        query = target["title"].replace(" ", "-")
        url = f"https://www.olx.ua/d/uk/list/q-{query}/?search%5Bfilter_enum_state%5D%5B0%5D=used"
        
        async with aiohttp.ClientSession() as session:
            html = await self.get_page(session, url)
            if not html: return

            soup = BeautifulSoup(html, "lxml")
            cards = soup.select("div[data-cy='l-card']")
            
            history = Database.load(DB_HISTORY, [])
            seen_urls = {h['url'] for h in history}
            
            for card in cards:
                try:
                    link_el = card.select_one("a")
                    if not link_el: continue
                    ad_url = "https://www.olx.ua" + link_el['href'].split('#')[0]
                    
                    if ad_url in seen_urls: continue
                    
                    title = card.select_one("h6").text.strip()
                    price_text = card.select_one("p[data-testid='ad-price']").text
                    price = int("".join(filter(str.isdigit, price_text))) if any(c.isdigit() for c in price_text) else 0
                    
                    img_el = card.select_one("img")
                    img_url = img_el.get("src") or img_el.get("data-src")
                    
                    if not img_url: continue

                    # Завантаження та порівняння фото
                    temp_path = TEMP_DIR / f"check_{secrets.token_hex(4)}.jpg"
                    async with session.get(img_url) as img_resp:
                        if img_resp.status == 200:
                            content = await img_resp.read()
                            async with aiofiles.open(temp_path, mode='wb') as f:
                                await f.write(content)
                    
                    score = engine.compare(str(target["image_path"]), str(temp_path))
                    
                    # Оновлення статистики цін
                    self._update_stats(target["title"], price)
                    
                    if score >= SIMILARITY_THRESHOLD:
                        avg_p = self._get_avg(target["title"])
                        deal_marker = ""
                        if avg_p and price < avg_p * (1 - SUPER_DEAL_DISCOUNT):
                            deal_marker = "\n🔥 **СУПЕР ЦІНА (DEAL)**"
                        
                        msg = (
                            f"✅ **Знайдено збіг!** ({score:.2%})\n"
                            f"📦 **Товар:** {title}\n"
                            f"💰 **Ціна:** {price} грн\n"
                            f"📊 **Сер. ціна:** {int(avg_p) if avg_p else '---'} грн{deal_marker}\n\n"
                            f"🔗 [Переглянути оголошення]({ad_url})"
                        )
                        
                        await context.bot.send_message(
                            chat_id=CHANNEL_ID or ADMIN_ID,
                            text=msg,
                            parse_mode="Markdown"
                        )
                        
                    history.append({"url": ad_url, "timestamp": time.time()})
                    Database.save(DB_HISTORY, history[-1000:])
                    if temp_path.exists(): temp_path.unlink()
                    
                except Exception as e:
                    logger.error(f"Error parsing card: {e}")
                await asyncio.sleep(1)

    def _update_stats(self, title, price):
        if price <= 0: return
        stats = Database.load(DB_STATS, {})
        data = stats.get(title, [])
        data.append(price)
        stats[title] = data[-100:]
        Database.save(DB_STATS, stats)

    def _get_avg(self, title):
        stats = Database.load(DB_STATS, {}).get(title, [])
        if len(stats) < MIN_HISTORY_FOR_STATS: return None
        return sum(stats) / len(stats)

scanner = OLXScanner()

# =============================================================================
# 5. TELEGRAM ІНТЕРФЕЙС (BOT LOGIC)
# =============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"User {user.id} started the bot")
    
    keyboard = [
        [InlineKeyboardButton("🔍 Мої цілі", callback_data="view_targets"), 
         InlineKeyboardButton("➕ Додати нову", callback_data="add_new")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats"),
         InlineKeyboardButton("⚙️ Налаштування", callback_data="settings")],
        [InlineKeyboardButton("▶️ Запустити сканер", callback_data="start_scan"),
         InlineKeyboardButton("⏹ Зупинити", callback_data="stop_scan")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        f"👋 Вітаю, {user.first_name}!\n\n"
        f"Я — **CollectorBot PRO**.\n"
        f"Я вмію шукати антикваріат та рідкісні речі на OLX за допомогою штучного інтелекту.\n\n"
        f"Статус сканера: {'🟢 Активний' if scanner.running else '🔴 Зупинений'}"
    )
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

async def button_tap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "add_new":
        context.user_data["mode"] = "waiting_photo"
        await query.edit_message_text("📸 Будь ласка, надішліть **ФОТО** предмета-еталона.")
    
    elif data == "start_scan":
        if not scanner.running:
            scanner.running = True
            asyncio.create_task(background_monitor(context))
            await query.edit_message_text("🚀 Сканер запущено! Перевірка кожні 10 хвилин.")
        else:
            await query.edit_message_text("✅ Сканер вже працює.")
            
    elif data == "stop_scan":
        scanner.running = False
        await query.edit_message_text("🛑 Сканер зупинено.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    
    if mode == "waiting_photo" and update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        file_id = secrets.token_hex(8)
        file_path = TARGETS_DIR / f"{file_id}.jpg"
        await photo_file.download_to_drive(file_path)
        
        # Використання YOLO для пре-аналізу
        try:
            model = engine.load_yolo()
            results = model(str(file_path))
            # Можна додати логіку автоматичного іменування за класом YOLO
        except: pass

        context.user_data["tmp_path"] = str(file_path)
        context.user_data["mode"] = "waiting_title"
        await update.message.reply_text("✅ Фото отримано! Тепер введіть **НАЗВУ** для пошуку на OLX:")

    elif mode == "waiting_title" and update.message.text:
        title = update.message.text
        targets = Database.load(DB_TARGETS, [])
        
        new_target = {
            "id": secrets.token_hex(4),
            "title": title,
            "image_path": context.user_data["tmp_path"],
            "created_at": datetime.now().isoformat()
        }
        
        targets.append(new_target)
        Database.save(DB_TARGETS, targets)
        context.user_data.clear()
        await update.message.reply_text(f"🥳 Цілі '{title}' успішно додана!")

async def background_monitor(context):
    while scanner.running:
        logger.info("Starting global scan cycle...")
        targets = Database.load(DB_TARGETS, [])
        for t in targets:
            if not scanner.running: break
            await scanner.scan_item(t, context)
        await asyncio.sleep(SCAN_INTERVAL)

# =============================================================================
# 6. WEB АДМІНІСТРУВАННЯ ТА СЕРВЕР
# =============================================================================

async def handle_web_root(request):
    targets = Database.load(DB_TARGETS, [])
    html = f"""
    <html>
        <head><title>CollectorBot Admin</title>
        <style>body{{font-family:sans-serif; padding:20px; background:#f4f4f4;}}
        .card{{background:#fff; padding:15px; margin:10px; border-radius:8px; box-shadow:0 2px 5px rgba(0,0,0,0.1);}}
        img{{max-width:100px; border-radius:4px;}}</style></head>
        <body>
            <h1>💎 CollectorBot PRO Control Panel</h1>
            <p>Статус сканера: {'<b style="color:green">ACTIVE</b>' if scanner.running else '<b style="color:red">OFFLINE</b>'}</p>
            <h2>Активні цілі ({len(targets)})</h2>
            <div style="display:flex; flex-wrap:wrap;">
    """
    for t in targets:
        html += f"""
        <div class="card">
            <h3>{t['title']}</h3>
            <p>ID: {t['id']}</p>
            <p>Додано: {t['created_at'][:10]}</p>
        </div>
        """
    html += "</div></body></html>"
    return web.Response(text=html, content_type='text/html')

async def start_web_server():
    app_web = web.Application()
    app_web.router.add_get('/', handle_web_root)
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web Admin Panel available on port {PORT}")

# =============================================================================
# 7. ЗАПУСК
# =============================================================================

def main():
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found!")
        return

    application = ApplicationBuilder().token(TOKEN).build()

    # Реєстрація обробників
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_tap))
    application.add_handler(MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, handle_message))

    # Запуск фонового веб-сервера
    loop = asyncio.get_event_loop()
    loop.create_task(start_web_server())

    print("--- CollectorBot PRO Started ---")
    application.run_polling()

if __name__ == "__main__":
    main()
