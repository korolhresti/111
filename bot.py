import os
import sys
import json
import time
import asyncio
import random
import logging
import re
from datetime import datetime
from io import BytesIO

import requests
import cv2
import numpy as np
from aiohttp import web
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ===================== CONFIG =====================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
PORT = int(os.getenv("PORT", "8080"))

SIMILARITY_THRESHOLD = 0.80
SCRAPE_INTERVAL = int(os.getenv("DEFAULTSCRAPEINTERVAL", "300"))
BATCH_SIZE = int(os.getenv("BATCHSIZEPER_CYCLE", "5"))
DEDUPE_WINDOW = int(os.getenv("DEDUPE_WINDOW", "86400"))

DATA_DIR = "data"
IMAGES_DIR = os.path.join(DATA_DIR, "images")
TARGETS_FILE = os.path.join(DATA_DIR, "targets.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
SOURCES_FILE = os.path.join(DATA_DIR, "sources.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("CollectorBotPro")

# ===================== DB =====================
class JsonDB:
    _lock = asyncio.Lock()

    @staticmethod
    async def load(path, default):
        async with JsonDB._lock:
            if not os.path.exists(path):
                return default
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                return default

    @staticmethod
    async def save(path, data):
        async with JsonDB._lock:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)

# ===================== CV ENGINE =====================
class CVEngine:
    def __init__(self):
        self.orb = cv2.ORB_create(3500)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    def load_img(self, b):
        try:
            return cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
        except:
            return None

    def compare(self, a, b):
        if a is None or b is None:
            return 0.0

        try:
            g1 = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
            g2 = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)

            kp1, d1 = self.orb.detectAndCompute(g1, None)
            kp2, d2 = self.orb.detectAndCompute(g2, None)

            geo = 0.0
            if d1 is not None and d2 is not None:
                matches = self.matcher.match(d1, d2)
                good = [m for m in matches if m.distance < 55]
                geo = min(1.0, len(good) / 45)

            hsv1 = cv2.cvtColor(a, cv2.COLOR_BGR2HSV)
            hsv2 = cv2.cvtColor(b, cv2.COLOR_BGR2HSV)

            h1 = cv2.calcHist([hsv1], [0, 1], None, [50, 60], [0, 180, 0, 256])
            h2 = cv2.calcHist([hsv2], [0, 1], None, [50, 60], [0, 180, 0, 256])
            cv2.normalize(h1, h1)
            cv2.normalize(h2, h2)

            color = max(0, cv2.compareHist(h1, h2, cv2.HISTCMP_CORREL))
            return geo * 0.7 + color * 0.3
        except:
            return 0.0

cv_engine = CVEngine()

# ===================== SCRAPER =====================
class Scraper:
    def __init__(self):
        self.ua = UserAgent()

    def headers(self):
        return {
            "User-Agent": self.ua.random,
            "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8",
        }

    def download_img(self, url):
        try:
            if url.startswith("http"):
                r = requests.get(url, headers=self.headers(), timeout=15)
                if r.status_code == 200:
                    return r.content
            else:
                with open(url, "rb") as f:
                    return f.read()
        except:
            return None

    def scan_empress(self):
        page = 1
        results = []
        while True:
            url = f"https://empress.cc/collections/all?page={page}"
            r = requests.get(url, headers=self.headers(), timeout=20)
            soup = BeautifulSoup(r.text, "lxml")
            cards = soup.select("a.grid-product__link")
            if not cards:
                break
            for c in cards:
                img = c.find("img")
                if not img:
                    continue
                src = img.get("data-src") or img.get("src")
                src = src.split("?")[0]
                results.append({
                    "id": f"empress_{hash(c['href'])}",
                    "title": c.get_text(strip=True),
                    "image_url": "https:" + src if src.startswith("//") else src,
                    "price": "N/A",
                    "url": "https://empress.cc" + c["href"],
                    "source": "empress.cc",
                    "created": time.time(),
                })
            page += 1
            time.sleep(random.uniform(0.8, 1.5))
        return results

    def search_olx(self, query):
        q = re.sub(r"[^\w\s]", "", query).strip().replace(" ", "-")
        url = f"https://www.olx.ua/uk/list/q-{q}/?search%5Bphotos%5D=1"
        r = requests.get(url, headers=self.headers(), timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
        ads = []
        for c in soup.find_all("div", {"data-cy": "l-card"}):
            if "promoted" in str(c).lower():
                continue
            a = c.find("a", href=True)
            img = c.find("img")
            if not a or not img:
                continue
            title = c.find("h6").get_text(strip=True)
            ads.append({
                "title": title,
                "url": a["href"],
                "image_url": img.get("src") or img.get("data-src"),
                "price": c.find("p", {"data-testid": "ad-price"}).get_text(strip=True)
                if c.find("p", {"data-testid": "ad-price"}) else "?",
                "replica": any(x in title.lower() for x in ["копия", "реплика", "replica", "aaa"]),
            })
        return ads

scraper = Scraper()

# ===================== BOT UI =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    kb = [
        [InlineKeyboardButton("🌐 Sync Empress", callback_data="sync")],
        [InlineKeyboardButton("📸 Add Photo Target", callback_data="photo")],
        [InlineKeyboardButton("📋 List Targets", callback_data="list")],
        [InlineKeyboardButton("🗑 Clear All", callback_data="clear")],
    ]
    await update.message.reply_text(
        "🖥 CollectorBot Pro Admin Panel",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "sync":
        await q.message.reply_text("⏳ Syncing empress.cc ...")
        items = await asyncio.to_thread(scraper.scan_empress)
        targets = await JsonDB.load(TARGETS_FILE, [])
        added = 0
        for i in items:
            if not any(t["url"] == i["url"] for t in targets):
                targets.append(i)
                added += 1
        await JsonDB.save(TARGETS_FILE, targets)
        await q.message.reply_text(f"✅ Added {added} items")

    if q.data == "list":
        t = await JsonDB.load(TARGETS_FILE, [])
        msg = "\n".join([f"{i+1}. {x['title']}" for i, x in enumerate(t[:25])])
        await q.message.reply_text(msg or "Empty")

    if q.data == "clear":
        await JsonDB.save(TARGETS_FILE, [])
        await JsonDB.save(HISTORY_FILE, {})
        await q.message.reply_text("🗑 Cleared")

async def add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    p = update.message.photo[-1]
    f = await context.bot.get_file(p.file_id)
    path = os.path.join(IMAGES_DIR, f"{p.file_id}.jpg")
    await f.download_to_drive(path)
    targets = await JsonDB.load(TARGETS_FILE, [])
    targets.append({
        "id": f"manual_{p.file_id}",
        "title": "Manual Item",
        "image_url": path,
        "price": "N/A",
        "url": path,
        "source": "manual",
        "created": time.time(),
    })
    await JsonDB.save(TARGETS_FILE, targets)
    await update.message.reply_text("✅ Photo target added")

# ===================== MONITOR LOOP =====================
async def monitor(context: ContextTypes.DEFAULT_TYPE):
    targets = await JsonDB.load(TARGETS_FILE, [])
    history = await JsonDB.load(HISTORY_FILE, {})
    if not targets:
        return

    batch = random.sample(targets, min(BATCH_SIZE, len(targets)))
    for t in batch:
        tb = scraper.download_img(t["image_url"])
        ti = cv_engine.load_img(tb) if tb else None

        queries = [
            t["title"],
            f"{t['title']} б.у",
            f"{t['title']} used",
        ]

        for q in queries:
            ads = await asyncio.to_thread(scraper.search_olx, q)
            for ad in ads:
                if ad["url"] in history and time.time() - history[ad["url"]] < DEDUPE_WINDOW:
                    continue

                ab = scraper.download_img(ad["image_url"])
                ai = cv_engine.load_img(ab) if ab else None
                score = cv_engine.compare(ti, ai)

                if score >= SIMILARITY_THRESHOLD:
                    status = "⚠️ Replica" if ad["replica"] else "✅ Original"
                    text = (
                        f"🚨 MATCH FOUND\n"
                        f"🔍 Target: {t['title']}\n"
                        f"📦 Found: {ad['title']}\n"
                        f"💵 Price: {ad['price']}\n"
                        f"📊 Similarity: {int(score*100)}%\n"
                        f"{status}\n"
                        f"{ad['url']}"
                    )
                    await context.bot.send_message(CHANNEL_ID, text)
                    history[ad["url"]] = time.time()
                    await JsonDB.save(HISTORY_FILE, history)

                await asyncio.sleep(random.uniform(1.5, 3.5))

# ===================== WEB =====================
async def health(req):
    return web.Response(text="OK")

async def web_server():
    app = web.Application()
    app.router.add_get("/", health)
    r = web.AppRunner(app)
    await r.setup()
    s = web.TCPSite(r, "0.0.0.0", PORT)
    await s.start()

async def post_init(app: Application):
    await web_server()
    try:
        await app.bot.send_message(ADMIN_ID, "🤖 CollectorBot Pro Started")
    except:
        pass

# ===================== MAIN =====================
def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.PHOTO, add_photo))
    if app.job_queue:
        app.job_queue.run_repeating(monitor, interval=SCRAPE_INTERVAL, first=30)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
                                  
