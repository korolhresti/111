
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
import sys
import re
import gc
import traceback
import warnings
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple, Union
from collections import deque

import numpy as np
import aiohttp
import aiofiles
from aiohttp import web
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from skimage.metrics import structural_similarity as ssim

import torch
import torch.nn as nn
import torch.nn.functional as F
from ultralytics import YOLO
from torchvision import models, transforms
from PIL import Image

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, 
    ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand,
    InputMediaPhoto, WebAppInfo, MenuButtonWebApp
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, 
    MessageHandler, ContextTypes, filters, Application, 
    ConversationHandler, PicklePersistence
)
os.environ['YOLO_CONFIG_DIR'] = '/tmp/Ultralytics'
# =============================================================================
# [1] СИСТЕМНА АРХІТЕКТУРА ТА ШЛЯХИ (Enterprise Structure)
# =============================================================================

warnings.filterwarnings("ignore")

# Налаштування оточення
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))
DEBUG = os.getenv("DEBUG", "False") == "True"

# Файлова система
BASE_DIR = pathlib.Path(__file__).parent.resolve()
CORE_DIR = BASE_DIR / "omni_vault"
DB_DIR = CORE_DIR / "database"
AI_DIR = CORE_DIR / "neural_models"
MEDIA_DIR = CORE_DIR / "media"
LOGS_DIR = CORE_DIR / "logs"
CACHE_DIR = CORE_DIR / "cache"

# --- ВИПРАВЛЕНИЙ БЛОК СТВОРЕННЯ ПАПОК ---
for folder in [DB_DIR, MEDIA_DIR, LOGS_DIR, CACHE_DIR]:
    # parents=True дозволяє створювати вкладені шляхи одночасно
    folder.mkdir(parents=True, exist_ok=True)

# Створення підпапки для таргетів тепер пройде успішно
(MEDIA_DIR / "targets").mkdir(parents=True, exist_ok=True)

class STORAGE:
    TARGETS = DB_DIR / "targets.json"
    SOURCES = DB_DIR / "sources.json"
    HISTORY = DB_DIR / "history.json"
    INTEL = DB_DIR / "market_intelligence.json"
    DEALS = DB_DIR / "super_deals.json"
    WEIGHTS = DB_DIR / "feedback_loop.json"
    RUNTIME_LOG = LOGS_DIR / "omni_runtime.log"

# Протоколювання
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.FileHandler(STORAGE.RUNTIME_LOG), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("OmniAI")

# Конфігурація AI
AI_PARAMS = {
    "yolo_model": "yolov8n.pt", # Nano версія для Render (RAM friendly)
    "sift_features": 8000,
    "ssim_weight": 0.3,
    "sift_weight": 0.4,
    "semantic_weight": 0.3,
    "match_threshold": 0.78,
    "super_deal_threshold": 0.60, # -40% знижка
    "scan_interval": 900,
    "user_agent_rotation": True
}

# =============================================================================
# [2] РОЗШИРЕНА БАЗА ДАНИХ (Async JSON Engine)
# =============================================================================

class OmniDB:
    _locks = {}

    @classmethod
    async def get_lock(cls, key):
        if key not in cls._locks:
            cls._locks[key] = asyncio.Lock()
        return cls._locks[key]

    @classmethod
    async def load(cls, path: pathlib.Path, default: Any = None) -> Any:
        async with await cls.get_lock(str(path)):
            if not path.exists(): return default if default is not None else []
            try:
                async with aiofiles.open(path, mode='r', encoding='utf-8') as f:
                    content = await f.read()
                    return json.loads(content) if content else (default if default is not None else [])
            except Exception as e:
                logger.error(f"DB Read Error {path.name}: {e}")
                return default if default is not None else []

    @classmethod
    async def save(cls, path: pathlib.Path, data: Any):
        async with await cls.get_lock(str(path)):
            try:
                async with aiofiles.open(path, mode='w', encoding='utf-8') as f:
                    await f.write(json.dumps(data, indent=4, ensure_ascii=False))
            except Exception as e:
                logger.error(f"DB Write Error {path.name}: {e}")

# =============================================================================
# [3] НЕЙРОННИЙ ДВИГУН (Computer Vision & Machine Learning)
# =============================================================================

class OmniVision:
    """Гібридна система аналізу: YOLO + SIFT + SSIM + ResNet Embeddings."""
    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.sift = cv2.SIFT_create(nfeatures=AI_PARAMS["sift_features"])
        self.bf = cv2.BFMatcher(cv2.NORM_L2, crossCheck=True)
        self._yolo = None
        
        # Модель семантичних векторів
        logger.info(f"🧠 Loading ResNet Semantic Engine on {self.device}...")
        self.resnet = models.resnet18(pretrained=True).to(self.device).eval()
        self.transform = transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])

    async def get_yolo(self):
        if self._yolo is None:
            self._yolo = YOLO(AI_PARAMS["yolo_model"])
        return self._yolo

    def get_embedding(self, img_path: str) -> torch.Tensor:
        try:
            img = Image.open(img_path).convert('RGB')
            tensor = self.transform(img).unsqueeze(0).to(self.device)
            with torch.no_grad():
                return self.resnet(tensor)
        except:
            return torch.zeros((1, 1000)).to(self.device)

    def analyze_deep_match(self, ref_path: str, ad_path: str) -> Dict[str, float]:
        try:
            img_ref = cv2.imread(ref_path)
            img_ad = cv2.imread(ad_path)
            if img_ref is None or img_ad is None: return {"score": 0.0}

            # Приводимо до одного розміру для порівняння
            gray_ref = cv2.cvtColor(img_ref, cv2.COLOR_BGR2GRAY)
            gray_ad = cv2.resize(cv2.cvtColor(img_ad, cv2.COLOR_BGR2GRAY), (gray_ref.shape[1], gray_ref.shape[0]))
            
            # SSIM (Геометрія)
            score_ssim, _ = ssim(gray_ref, gray_ad, full=True)

            # SIFT (Деталі)
            kp1, des1 = self.sift.detectAndCompute(gray_ref, None)
            kp2, des2 = self.sift.detectAndCompute(gray_ad, None)
            score_sift = 0.0
            if des1 is not None and des2 is not None:
                matches = self.bf.match(des1, des2)
                score_sift = len(matches) / max(len(kp1), len(kp2), 1)
                score_sift = min(1.0, score_sift * 10)

            # Semantic (ResNet)
            emb1 = self.get_embedding(ref_path)
            emb2 = self.get_embedding(ad_path)
            score_cosine = F.cosine_similarity(emb1, emb2).item()

            final = (score_ssim * 0.3) + (score_sift * 0.4) + (score_cosine * 0.3)
            return {"score": float(final), "details": score_sift, "struct": score_ssim, "semantic": score_cosine}
        except Exception as e:
            logger.error(f"AI Analysis Error: {e}")
            return {"score": 0.0}

# =============================================================================
# [4] СКАНЕР ТА МЕРЕЖЕВИЙ АНАЛІЗ (Multi-Source Agent)
# =============================================================================

class MarketAgent:
    def __init__(self):
        self.is_running = False
        self.ua = UserAgent()
        self.session = None
        self.history = deque(maxlen=5000)

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self.session

    async def update_intel(self, name: str, price: float):
        intel = await OmniDB.load(STORAGE.INTEL, {})
        if name not in intel: intel[name] = []
        intel[name].append({"p": price, "t": time.time()})
        intel[name] = intel[name][-100:] # Тримаємо останні 100 цін
        await OmniDB.save(STORAGE.INTEL, intel)

    async def get_benchmark(self, name: str) -> Optional[float]:
        intel = await OmniDB.load(STORAGE.INTEL, {})
        prices = [x['p'] for x in intel.get(name, [])]
        if len(prices) < 5: return None
        return float(np.median(prices))

    async def run_monitoring(self, context: ContextTypes.DEFAULT_TYPE):
        self.is_running = True
        logger.info("📡 Market Agent: DEPLOYED")
        
        while self.is_running:
            targets = await OmniDB.load(STORAGE.TARGETS)
            sources = await OmniDB.load(STORAGE.SOURCES, [
                {
                    "name": "OLX UA",
                    "url": "https://www.olx.ua/d/uk/list/q-{q}/",
                    "c_card": "div[data-cy='l-card']",
                    "c_title": "h6", "c_price": "p[data-testid='ad-price']", "c_img": "img"
                }
            ])

            for target in targets:
                if not self.is_running: break
                logger.info(f"🔎 Scanning for: {target['name']}")
                
                for src in sources:
                    try:
                        await self.scan_engine(target, src, context)
                        await asyncio.sleep(random.randint(5, 15))
                    except Exception as e:
                        logger.error(f"Scraper error {src['name']}: {e}")
                
            await asyncio.sleep(AI_PARAMS["scan_interval"])

    async def scan_engine(self, target, src, context):
        session = await self.get_session()
        url = src['url'].format(q=target['name'].replace(" ", "-"))
        
        async with session.get(url, headers={"User-Agent": self.ua.random}) as r:
            if r.status != 200: return
            soup = BeautifulSoup(await r.text(), "lxml")
            
        cards = soup.select(src['c_card'])
        history_data = await OmniDB.load(STORAGE.HISTORY, [])
        seen_urls = {x['u'] for x in history_data}

        for card in cards[:15]:
            try:
                title = card.select_one(src['c_title']).text.strip()
                price_raw = card.select_one(src['c_price']).text
                price = float(re.sub(r'[^\d]', '', price_raw))
                
                link = card.select_one("a")['href']
                ad_url = link if link.startswith("http") else f"https://olx.ua{link}"
                
                if ad_url in seen_urls: continue

                img_tag = card.select_one(src['c_img'])
                img_url = img_tag.get("src") or img_tag.get("data-src")
                
                # Завантаження для нейро-аналізу
                tmp_p = CACHE_DIR / f"check_{secrets.token_hex(4)}.jpg"
                async with session.get(img_url) as ir:
                    async with aiofiles.open(tmp_p, "wb") as f:
                        await f.write(await ir.read())

                analysis = vision.analyze_deep_match(target['path'], str(tmp_p))
                
                if analysis['score'] >= AI_PARAMS["match_threshold"]:
                    await self.notify_admin(target, title, price, ad_url, analysis, context)

                history_data.append({"u": ad_url, "ts": time.time()})
                await OmniDB.save(STORAGE.HISTORY, history_data[-2000:])
                if tmp_p.exists(): tmp_p.unlink()
                
            except: continue

    async def notify_admin(self, target, title, price, url, analysis, context):
        await self.update_intel(target['name'], price)
        bench = await self.get_benchmark(target['name'])
        
        is_deal = bench and price < bench * AI_PARAMS["super_deal_threshold"]
        
        msg = (
            f"{'🔥 **SUPER DEAL FOUND** 🔥' if is_deal else '✅ **AI MATCH**'}\n\n"
            f"📦 **Ціль:** {target['name']}\n"
            f"🏷 **Знайдено:** {title}\n"
            f"💰 **Ціна:** {int(price)} грн\n"
            f"📊 **AI Confidence:** {analysis['score']:.1%}\n"
            f"📉 **Медіана ринку:** {int(bench) if bench else '---'} грн\n"
            f"🔍 **Деталізація:** {analysis['details']:.2f}\n\n"
            f"🔗 [ПЕРЕЙТИ ДО ОГОЛОШЕННЯ]({url})"
        )
        
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode="Markdown")

agent = MarketAgent()

# =============================================================================
# [5] UI/UX ТЕЛЕГРАМ ІНТЕРФЕЙС (Omni Control)
# =============================================================================

class OmniBot:
    def __init__(self):
        self.application = ApplicationBuilder().token(TOKEN).build()
        self._setup_handlers()

    def _setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("add_source", self.cmd_add_src))
        self.application.add_handler(CallbackQueryHandler(self.handle_ui))
        self.application.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        kb = [
            [InlineKeyboardButton("➕ Додати ціль (AI)", callback_data="ui_new")],
            [InlineKeyboardButton("📦 Список еталонів", callback_data="ui_list"),
             InlineKeyboardButton("🔌 Джерела", callback_data="ui_src")],
            [InlineKeyboardButton("▶️ СТАРТ AI", callback_data="sys_on"),
             InlineKeyboardButton("⏹ ЗУПИНКА", callback_data="sys_off")],
            
        ]
        await update.message.reply_text(
            "💎 **OmniAI Collector v25.0**\n"
            "Всі системи активовані. Очікую інструкцій.",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
        )

    async def cmd_add_src(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Формат: /add_source Назва|URL|Карточка|Заголовок|Ціна|Фото"""
        try:
            p = " ".join(context.args).split("|")
            new_s = {"name": p[0], "url": p[1], "c_card": p[2], "c_title": p[3], "c_price": p[4], "c_img": p[5]}
            srcs = await OmniDB.load(STORAGE.SOURCES, [])
            srcs.append(new_s)
            await OmniDB.save(STORAGE.SOURCES, srcs)
            await update.message.reply_text("✅ Нове джерело інтегровано в мережу.")
        except:
            await update.message.reply_text("Помилка. Формат: /add_source Name|URL|Card|Title|Price|Img")

    async def handle_ui(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()

        if q.data == "ui_new":
            context.user_data["state"] = "wait_img"
            await q.edit_message_text("📸 Надішліть **ФОТО ЕТАЛОНА** (AI проаналізує деталі):")
        
        elif q.data == "sys_on":
            if not agent.is_running:
                asyncio.create_task(agent.run_monitoring(context))
                await q.edit_message_text("🚀 **AI МОНІТОРИНГ ЗАПУЩЕНО.**")
            else:
                await q.edit_message_text("✅ Система вже працює.")
        
        elif q.data == "sys_off":
            agent.is_running = False
            await q.edit_message_text("🛑 **AI МОНІТОРИНГ ЗУПИНЕНО.**")

        elif q.data == "ui_list":
            t = await OmniDB.load(STORAGE.TARGETS)
            res = "\n".join([f"• {x['name']}" for x in t]) if t else "Список порожній."
            await q.edit_message_text(f"📦 **Ваші цілі:**\n{res}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]]))

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.user_data.get("st") == "img":
            file = await update.message.photo[-1].get_file()
            path = str(MEDIA_DIR / "targets" / f"ref_{secrets.token_hex(4)}.jpg")
            await file.download_to_drive(path)
            
            # Отримуємо результати YOLO
            y = await vision.get_yolo()
            results = y(path)
            detected_list = [y.names[int(c)] for c in results[0].boxes.cls]
            
            # Готуємо текст окремо, щоб уникнути конфліктів лапок у f-string
            if detected_list:
                ai_vision_text = ", ".join(detected_list)
            else:
                ai_vision_text = "Обʼєкт"  # Використовуємо модифікований апостроф (U+02BC) або просто текст
            
            context.user_data.update({"tmp": path, "st": "name"})
            
            # Використовуємо потрійні лапки для максимальної безпеки синтаксису
            response_text = f"""✅ Фото завантажено. 
AI бачить: {ai_vision_text}.
Тепер введіть НАЗВУ товару для пошуку:"""
            
            await update.message.reply_text(response_text)

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.user_data.get("state") == "wait_name":
            name = update.message.text
            t = await OmniDB.load(STORAGE.TARGETS)
            t.append({"name": name, "path": context.user_data["tmp_p"], "id": secrets.token_hex(4)})
            await OmniDB.save(STORAGE.TARGETS, t)
            context.user_data.clear()
            await update.message.reply_text(f"🎯 Ціль '{name}' додана до бази!")

# =============================================================================
# [6] WEB ADMIN DASHBOARD (AIOHTTP Server)
# =============================================================================

async def web_index(request):
    targets = await OmniDB.load(STORAGE.TARGETS)
    deals = await OmniDB.load(STORAGE.DEALS, [])
    
    html = f"""
    <html><head><title>OmniAI Dashboard</title>
    <style>
        body{{font-family:sans-serif; background:#0a0a0c; color:#eee; padding:30px;}}
        .card{{background:#16161a; padding:20px; border-radius:12px; margin:10px; border:1px solid #222;}}
        .green{{color:#4caf50;}} .red{{color:#f44336;}}
    </style></head>
    <body>
        <h1>💎 OmniAI Supreme Control</h1>
        <div class="card">Статус: <b class="{'green' if agent.is_running else 'red'}">{'АКТИВНИЙ' if agent.is_running else 'ПАУЗА'}</b></div>
        <div class="card"><h2>📦 Цільові товари ({len(targets)})</h2>
        {"<br>".join([x['name'] for x in targets])}</div>
    </body></html>
    """
    return web.Response(text=html, content_type='text/html')

async def start_web():
    app_web = web.Application()
    app_web.router.add_get('/', web_index)
    runner = web.AppRunner(app_web)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    logger.info(f"🌐 Web Admin Panel: http://0.0.0.0:{PORT}")

# =============================================================================
# [7] ТОЧКА ЗАПУСКУ (Main Loop)
# =============================================================================

def main():
    if not TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN не знайдено!")
        return

    omni_bot = OmniBot()
    
    loop = asyncio.get_event_loop()
    loop.create_task(start_web())
    
    # Garbage Collector
    async def cleanup():
        while True:
            gc.collect()
            await asyncio.sleep(3600)
    loop.create_task(cleanup())

    print("🚀 OMNI-AI v25.0 INITIALIZED AND ONLINE")
    omni_bot.application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    # Параметр drop_pending_updates видаляє всі старі повідомлення, 
    # які прийшли, поки бот був офлайн або конфліктував
    application.run_polling(drop_pending_updates=True)
