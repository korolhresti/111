# -*- coding: utf-8 -*-
"""
COLLECTOR BOT PRO - ENTERPRISE EDITION v12.7
--------------------------------------------
Full Functional: YOLO v8, ORB+HSV Matching, Market Price Analytics,
Web Dashboard, Automated Cleanup, Multi-target Monitoring.
--------------------------------------------
"""

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
import traceback
import sys
import gc
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Union, Tuple

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
    WebAppInfo,
    BotCommand
)
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    CallbackQueryHandler, 
    MessageHandler, 
    ContextTypes, 
    filters,
    ConversationHandler,
    Application
)
from telegram.error import TelegramError, NetworkError

# =============================================================================
# 1. ГЛОБАЛЬНА КОНФІГУРАЦІЯ ТА ЛОГУВАННЯ
# =============================================================================

# Налаштування ENV
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID")
PORT = int(os.getenv("PORT", "10000"))

# Ієрархія файлової системи
BASE_DIR = pathlib.Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "db_vault"
IMAGES_DIR = DATA_DIR / "visual_assets"
TARGETS_DIR = IMAGES_DIR / "references"
ADS_DIR = IMAGES_DIR / "scanned_ads"
TEMP_DIR = DATA_DIR / "temporary_cache"
LOGS_DIR = DATA_DIR / "system_logs"

for folder in [DATA_DIR, IMAGES_DIR, TARGETS_DIR, ADS_DIR, TEMP_DIR, LOGS_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

# Шляхи до баз даних JSON
class DB:
    TARGETS = DATA_DIR / "targets_v12.json"
    HISTORY = DATA_DIR / "scan_history.json"
    STATS = DATA_DIR / "market_prices.json"
    SETTINGS = DATA_DIR / "bot_settings.json"
    LOGS = LOGS_DIR / "runtime.log"

# Налаштування логера (розширене)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | [%(levelname)s] | %(message)s',
    handlers=[
        logging.FileHandler(DB.LOGS),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("CollectorEngine")

# Глобальні параметри системи
SYSTEM_CONFIG = {
    "orb_features": 3500,
    "similarity_threshold": 0.82,
    "super_deal_percent": 0.35,
    "min_samples_for_market": 5,
    "scan_cooldown": 900,  # 15 хвилин
    "max_history_records": 5000,
    "yolo_model_name": "yolov8n.pt",
    "image_resize": (640, 640)
}

# =============================================================================
# 2. МЕНЕДЖЕР ПАМ'ЯТІ ТА ФАЙЛІВ
# =============================================================================

class StorageManager:
    """Клас для безпечної роботи з JSON та очищенням дисків."""
    
    @staticmethod
    async def load_json(path: pathlib.Path, default: Any = None) -> Any:
        if default is None: default = []
        if not path.exists(): return default
        try:
            async with aiofiles.open(path, mode='r', encoding='utf-8') as f:
                content = await f.read()
                return json.loads(content)
        except Exception as e:
            logger.error(f"Failed to load JSON {path.name}: {e}")
            return default

    @staticmethod
    async def save_json(path: pathlib.Path, data: Any):
        try:
            async with aiofiles.open(path, mode='w', encoding='utf-8') as f:
                await f.write(json.dumps(data, indent=4, ensure_ascii=False))
        except Exception as e:
            logger.error(f"Failed to save JSON {path.name}: {e}")

    @staticmethod
    def cleanup_temp():
        """Видаляє старі фото з папки temp."""
        try:
            for file in TEMP_DIR.glob("*.jpg"):
                if time.time() - file.stat().st_mtime > 3600: # 1 година
                    file.unlink()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

# =============================================================================
# 3. CORE AI ENGINE (YOLO + COMPUTER VISION)
# =============================================================================

class AIEngine:
    """Ядро системи: розпізнавання об'єктів та порівняння зображень."""
    
    def __init__(self):
        self.orb = cv2.ORB_create(nfeatures=SYSTEM_CONFIG["orb_features"])
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        self._yolo = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

    async def get_yolo(self):
        if self._yolo is None:
            logger.info(f"🚀 Initializing YOLO v8 on {self.device}...")
            self._yolo = YOLO(SYSTEM_CONFIG["yolo_model_name"])
            self._yolo.to(self.device)
        return self._yolo

    def get_fingerprint(self, img_path: str) -> Optional[Dict]:
        """Створює комбінований цифровий відбиток зображення."""
        img = cv2.imread(img_path)
        if img is None: return None
        
        # Підготовка
        img = cv2.resize(img, SYSTEM_CONFIG["image_resize"])
        
        # 1. HSV Histogram (Аналіз кольору - Part 3 original)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        
        # 2. ORB Features (Геометрія - Part 3 original)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        kp, des = self.orb.detectAndCompute(gray, None)
        
        return {
            "hist": hist, 
            "des": des, 
            "kp_count": len(kp)
        }

    def compare(self, fp_ref: Dict, fp_ad: Dict) -> float:
        """Порівнює два відбитки з ваговими коефіцієнтами."""
        if not fp_ref or not fp_ad: return 0.0
        if fp_ref["des"] is None or fp_ad["des"] is None: return 0.0

        # Колірний скор (Correlation)
        color_score = cv2.compareHist(fp_ref["hist"], fp_ad["hist"], cv2.HISTCMP_CORREL)
        color_score = max(0, color_score)

        # Скор особливостей (Feature Matching)
        matches = self.bf.match(fp_ref["des"], fp_ad["des"])
        good_matches = [m for m in matches if m.distance < 48]
        
        feature_score = len(good_matches) / max(fp_ref["kp_count"], fp_ad["kp_count"], 1)
        # Нормалізація: якщо більше 20% точок збіглися, це дуже високий показник
        feature_score = min(1.0, feature_score * 5.0) 

        # Фінальна вага: 40% колір, 60% форма
        return (color_score * 0.4) + (feature_score * 0.6)

# =============================================================================
# 4. MARKET SCRAPER (OLX ANALYTICS)
# =============================================================================

class OLXScraper:
    """Модуль збору даних та цінового аналізу."""
    
    def __init__(self, ai_engine: AIEngine):
        self.ai = ai_engine
        self.ua = UserAgent()
        self._session = None
        self.is_monitoring = False

    async def get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self.ua.random},
                connector=aiohttp.TCPConnector(ssl=False)
            )
        return self._session

    def clean_price(self, raw_price: str) -> int:
        """Перетворює рядок '1 200 грн' у число 1200."""
        return int("".join(filter(str.isdigit, raw_price))) if any(c.isdigit() for c in raw_price) else 0

    async def run_scan_cycle(self, context: ContextTypes.DEFAULT_TYPE):
        """Головний цикл моніторингу."""
        self.is_monitoring = True
        logger.info("📡 Scanning Cycle Started")
        
        while self.is_monitoring:
            targets = await StorageManager.load_json(DB.TARGETS)
            if not targets:
                logger.info("No targets found. Sleeping...")
                await asyncio.sleep(300)
                continue

            for target in targets:
                if not self.is_monitoring: break
                await self.process_item(target, context)
                # Рандомна пауза щоб OLX не банив IP
                await asyncio.sleep(random.randint(15, 30))
                
            StorageManager.cleanup_temp()
            await asyncio.sleep(SYSTEM_CONFIG["scan_cooldown"])

    async def process_item(self, target: Dict, context: ContextTypes.DEFAULT_TYPE):
        """Обробка конкретної цілі."""
        logger.info(f"🔎 Checking OLX for: {target['title']}")
        
        # Формування URL (з фільтром used як в оригіналі)
        q = target['title'].replace(" ", "-")
        url = f"https://www.olx.ua/d/uk/list/q-{q}/?search%5Bfilter_enum_state%5D%5B0%5D=used"
        
        try:
            session = await self.get_session()
            async with session.get(url, timeout=20) as resp:
                if resp.status != 200: return
                soup = BeautifulSoup(await resp.text(), "lxml")
                
            cards = soup.select("div[data-cy='l-card']")
            history = await StorageManager.load_json(DB.HISTORY)
            seen_urls = {h['u'] for h in history}
            
            # Створюємо відбиток еталона
            ref_fp = self.ai.get_fingerprint(target['image_path'])
            if not ref_fp: return

            for card in cards:
                try:
                    link_el = card.select_one("a")
                    if not link_el: continue
                    ad_url = "https://www.olx.ua" + link_el['href'].split('#')[0]
                    
                    if ad_url in seen_urls: continue
                    
                    # Парсинг даних картки
                    ad_title = card.select_one("h6").text.strip()
                    price_txt = card.select_one("p[data-testid='ad-price']").text
                    price = self.clean_price(price_txt)
                    img_url = card.select_one("img").get("src") or card.select_one("img").get("data-src")
                    
                    if not img_url: continue

                    # Завантаження фото оголошення для порівняння
                    tmp_file = TEMP_DIR / f"ad_{secrets.token_hex(4)}.jpg"
                    async with session.get(img_url) as i_resp:
                        if i_resp.status == 200:
                            async with aiofiles.open(tmp_file, mode='wb') as f:
                                await f.write(await i_resp.read())

                    # AI Порівняння
                    ad_fp = self.ai.get_fingerprint(str(tmp_file))
                    score = self.ai.compare(ref_fp, ad_fp)
                    
                    # Аналітика ціни (Original Part 2)
                    await self._update_market_stats(target['title'], price)
                    market_avg = await self._calculate_market_avg(target['title'])
                    
                    if score >= SYSTEM_CONFIG["similarity_threshold"]:
                        is_super_deal = market_avg and price < market_avg * (1 - SYSTEM_CONFIG["super_deal_percent"])
                        
                        notification = (
                            f"🌟 **ЗНАЙДЕНО ВІДПОВІДНІСТЬ!**\n\n"
                            f"📦 **Ціль:** {target['title']}\n"
                            f"🏷 **Знайдено:** {ad_title}\n"
                            f"💰 **Ціна:** {price} грн\n"
                            f"📊 **Схожість:** {score:.1%}\n"
                            f"{'🔥 СУПЕР ЦІНА (НИЖЧЕ РИНКУ!)' if is_super_deal else ''}\n\n"
                            f"🔗 [ПЕРЕГЛЯНУТИ НА OLX]({ad_url})"
                        )
                        
                        await context.bot.send_message(
                            chat_id=CHANNEL_ID or ADMIN_ID,
                            text=notification,
                            parse_mode="Markdown"
                        )

                    # Оновлення історії
                    history.append({"u": ad_url, "t": time.time()})
                    await StorageManager.save_json(DB.HISTORY, history[-SYSTEM_CONFIG["max_history_records"]:])
                    
                    if tmp_file.exists(): tmp_file.unlink()
                    
                except Exception as e:
                    logger.error(f"Error parsing OLX card: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"Global Scraper Error: {e}")

    async def _update_market_stats(self, key: str, price: int):
        if price <= 10: return
        stats = await StorageManager.load_json(DB.STATS, default={})
        p_list = stats.get(key, [])
        p_list.append(price)
        stats[key] = p_list[-100:] # Тримаємо останні 100 цін
        await StorageManager.save_json(DB.STATS, stats)

    async def _calculate_market_avg(self, key: str) -> Optional[float]:
        stats = await StorageManager.load_json(DB.STATS, default={})
        prices = stats.get(key, [])
        if len(prices) < SYSTEM_CONFIG["min_samples_for_market"]: return None
        # Медіана стійкіша до викидів
        return float(np.median(prices))

# =============================================================================
# 5. TELEGRAM INTERFACE (UI & COMMANDS)
# =============================================================================

class CollectorBotUI:
    """Керування логікою бота та командами."""
    
    def __init__(self, scraper: OLXScraper):
        self.scraper = scraper

    async def start_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Головне меню."""
        kb = [
            [InlineKeyboardButton("📋 Мої цілі", callback_data="list"), 
             InlineKeyboardButton("➕ Нова ціль", callback_data="add")],
            [InlineKeyboardButton("🚀 Запустити", callback_data="run"), 
             InlineKeyboardButton("🛑 Зупинити", callback_data="stop")],
            [InlineKeyboardButton("📊 Статистика", callback_data="stats")]
        ]
        
        txt = (
            "💎 **CollectorBot PRO v12.7**\n"
            "--- Система готова до пошуку ---\n\n"
            f"Статус моніторингу: {'🟢 ВКЛ' if self.scraper.is_monitoring else '🔴 ВИКЛ'}\n"
            f"Об'єктів у базі: {len(await StorageManager.load_json(DB.TARGETS))}"
        )
        
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    async def callback_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == "add":
            context.user_data["step"] = "wait_photo"
            await query.edit_message_text("📸 Надішліть **ФОТО** предмета (еталон):")
            
        elif query.data == "run":
            if not self.scraper.is_monitoring:
                asyncio.create_task(self.scraper.run_scan_cycle(context))
                await query.edit_message_text("🚀 Сканер активований! Перевірка почалася.")
            else:
                await query.edit_message_text("✅ Сканер вже працює.")
                
        elif query.data == "stop":
            self.scraper.is_monitoring = False
            await query.edit_message_text("🛑 Сканер буде зупинено після завершення поточної перевірки.")

        elif query.data == "list":
            targets = await StorageManager.load_json(DB.TARGETS)
            if not targets:
                await query.edit_message_text("База цілей порожня.")
                return
            msg = "📌 **Ваші об'єкти для пошуку:**\n" + "\n".join([f"- {t['title']}" for t in targets])
            await query.edit_message_text(msg, parse_mode="Markdown")

    async def message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        step = context.user_data.get("step")
        
        if step == "wait_photo" and update.message.photo:
            file = await update.message.photo[-1].get_file()
            t_id = secrets.token_hex(6)
            save_path = TARGETS_DIR / f"ref_{t_id}.jpg"
            await file.download_to_drive(save_path)
            
            # Виклик YOLO як в оригінальній частині 1
            try:
                yolo = await self.scraper.ai.get_yolo()
                yolo(str(save_path), imgsz=320, conf=0.3)
            except Exception as e:
                logger.warning(f"YOLO pass failed: {e}")

            context.user_data["tmp_path"] = str(save_path)
            context.user_data["step"] = "wait_title"
            await update.message.reply_text("✅ Фото отримано. Тепер введіть **НАЗВУ** товару для OLX:")

        elif step == "wait_title" and update.message.text:
            title = update.message.text
            targets = await StorageManager.load_json(DB.TARGETS)
            
            targets.append({
                "id": secrets.token_hex(4),
                "title": title,
                "image_path": context.user_data["tmp_path"],
                "added": datetime.now().strftime("%d.%m.%Y %H:%M")
            })
            
            await StorageManager.save_json(DB.TARGETS, targets)
            context.user_data.clear()
            await update.message.reply_text(f"🎯 Ціль '{title}' успішно збережена!")

# =============================================================================
# 6. ВЕБ-ІНТЕРФЕЙС ТА АДАПТАЦІЯ (RENDER.COM)
# =============================================================================

async def web_admin(request):
    """HTML сторінка для моніторингу статусу."""
    targets = await StorageManager.load_json(DB.TARGETS)
    stats = await StorageManager.load_json(DB.STATS, {})
    
    rows = ""
    for t in targets:
        avg = np.median(stats.get(t['title'], [0]))
        rows += f"<tr><td>{t['title']}</td><td>{t['added']}</td><td>{avg} грн</td></tr>"
        
    html = f"""
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"><title>Admin Panel</title>
    <style>body{{font-family:sans-serif;background:#1a1a1a;color:#fff;padding:20px;}}
    table{{width:100%; border-collapse:collapse;}} th,td{{padding:10px; border:1px solid #444; text-align:left;}}
    th{{background:#333;}} .status{{color:#0f0;}}</style></head>
    <body>
        <h1>💎 CollectorBot PRO Status</h1>
        <p>Active Targets: {len(targets)} | System Time: {datetime.now().strftime('%H:%M:%S')}</p>
        <table><thead><tr><th>Назва</th><th>Дата додавання</th><th>Сер. ринок</th></tr></thead>
        <tbody>{rows}</tbody></table>
    </body></html>
    """
    return web.Response(text=html, content_type='text/html')

async def run_server():
    app_web = web.Application()
    app_web.router.add_get('/', web_admin)
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Web Admin started on port {PORT}")

# =============================================================================
# 7. ГОЛОВНИЙ ЗАПУСК (MAIN)
# =============================================================================

def main():
    if not TOKEN:
        logger.critical("❌ FATAL: TELEGRAM_BOT_TOKEN NOT FOUND!")
        sys.exit(1)

    # Ініціалізація Core
    ai_engine = AIEngine()
    scraper = OLXScraper(ai_engine)
    ui = CollectorBotUI(scraper)

    # Побудова бота
    application = ApplicationBuilder().token(TOKEN).build()

    # Реєстрація хендлерів
    application.add_handler(CommandHandler("start", ui.start_handler))
    application.add_handler(CallbackQueryHandler(ui.callback_router))
    application.add_handler(MessageHandler(filters.PHOTO, ui.message_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ui.message_handler))

    # Запуск асинхронного Веб-сервера (для Render)
    loop = asyncio.get_event_loop()
    loop.create_task(run_server())

    # Постійне очищення сміття (GC)
    async def memory_cleaner():
        while True:
            gc.collect()
            await asyncio.sleep(1800)
    loop.create_task(memory_cleaner())

    print("--- CollectorBot PRO v12.7 is Online ---")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.error(f"CRITICAL CRASH: {traceback.format_exc()}")
