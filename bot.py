import logging
import json
import os
import asyncio
import re
import sys
import time
import random
from datetime import datetime
from io import BytesIO

# Third-party libraries
import requests
import cv2
import numpy as np
from aiohttp import web
from fake_useragent import UserAgent
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# --- ⚙️ CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8509179556:AAFWu5bGnGDNShzmynZE2fHZKYo3BYmKhqE")

def get_env_int(key, default):
    try:
        val = os.getenv(key, str(default))
        return int(val)
    except ValueError:
        return int(default)

ADMIN_ID = get_env_int("ADMIN_CHAT_ID", 8184456641)
CHANNEL_ID = get_env_int("CHANNEL_ID", -1003680291028)
PORT = get_env_int("PORT", 8080)

# File Paths
DATA_DIR = "data"
IMAGES_DIR = os.path.join(DATA_DIR, "images")
SOURCES_FILE = os.path.join(DATA_DIR, "sources.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")

# Ensure directories exist
os.makedirs(IMAGES_DIR, exist_ok=True)

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("CollectorPro")

# --- 💾 DATABASE MODULE (JSON) ---
class JsonDatabase:
    _lock = asyncio.Lock()

    @staticmethod
    async def load(filepath, default_factory=list):
        async with JsonDatabase._lock:
            if not os.path.exists(filepath):
                return default_factory()
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"DB Load Error ({filepath}): {e}")
                return default_factory()

    @staticmethod
    async def save(filepath, data):
        async with JsonDatabase._lock:
            try:
                temp_path = filepath + ".tmp"
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                os.replace(temp_path, filepath)
            except Exception as e:
                logger.error(f"DB Save Error ({filepath}): {e}")

# --- 👁 COMPUTER VISION MODULE (OpenCV) ---
class ComputerVision:
    def __init__(self):
        self.orb = cv2.ORB_create(nfeatures=2000)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    def load_image_from_bytes(self, image_bytes):
        try:
            nparr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            return img
        except Exception as e:
            logger.error(f"CV Decode Error: {e}")
            return None

    def compare(self, img1, img2):
        if img1 is None or img2 is None: return 0

        try:
            # 1. Geometry (ORB)
            gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR_GRAY)
            gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR_GRAY)

            kp1, des1 = self.orb.detectAndCompute(gray1, None)
            kp2, des2 = self.orb.detectAndCompute(gray2, None)

            geo_score = 0
            if des1 is not None and des2 is not None and len(des1) > 0 and len(des2) > 0:
                matches = self.bf.match(des1, des2)
                matches = sorted(matches, key=lambda x: x.distance)
                good_matches = [m for m in matches if m.distance < 60]
                geo_score = min(100, (len(good_matches) / 25) * 100)

            # 2. Color (Histogram)
            h_bins = 50
            s_bins = 60
            histSize = [h_bins, s_bins]
            ranges = [0, 180, 0, 256]
            channels = [0, 1]

            hsv_base = cv2.cvtColor(img1, cv2.COLOR_BGR2HSV)
            hsv_test = cv2.cvtColor(img2, cv2.COLOR_BGR2HSV)

            hist_base = cv2.calcHist([hsv_base], channels, None, histSize, ranges, accumulate=False)
            cv2.normalize(hist_base, hist_base, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)

            hist_test = cv2.calcHist([hsv_test], channels, None, histSize, ranges, accumulate=False)
            cv2.normalize(hist_test, hist_test, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)

            color_score = cv2.compareHist(hist_base, hist_test, cv2.HISTCMP_CORREL) * 100
            color_score = max(0, color_score)

            # Weighted Average
            final_score = (geo_score * 0.7) + (color_score * 0.3)
            return final_score

        except Exception as e:
            logger.error(f"CV Comparison Error: {e}")
            return 0

cv_engine = ComputerVision()

# --- 🕸 SCRAPER MODULE ---
class ScraperEngine:
    def __init__(self):
        self.ua = UserAgent()

    def get_headers(self):
        return {
            'User-Agent': self.ua.random,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7',
            'Referer': 'https://www.google.com/'
        }

    def download_image_bytes(self, url):
        if not url: return None
        try:
            if not url.startswith('http'):
                if os.path.exists(url):
                    with open(url, 'rb') as f:
                        return f.read()
                return None
            
            resp = requests.get(url, headers=self.get_headers(), timeout=15)
            if resp.status_code == 200:
                return resp.content
        except Exception as e:
            logger.warning(f"Download Failed ({url}): {e}")
        return None

    def parse_empress_cc(self):
        url = "https://empress.cc/collections/all"
        results = []
        logger.info("📡 Starting Empress.cc sync...")
        try:
            resp = requests.get(url, headers=self.get_headers(), timeout=20)
            soup = BeautifulSoup(resp.text, 'lxml')
            products = soup.select('.grid-product__content, .product-card, .grid-view-item')
            
            for p in products[:15]:
                try:
                    title_el = p.select_one('.grid-product__title, .product-card__title, .grid-view-item__title')
                    price_el = p.select_one('.grid-product__price, .price-item--regular')
                    link_el = p.select_one('a')
                    img_el = p.select_one('img')

                    if not title_el or not link_el or not img_el: continue

                    title = title_el.get_text(strip=True)
                    price = price_el.get_text(strip=True) if price_el else "N/A"
                    link = "https://empress.cc" + link_el['href']
                    
                    img_src = img_el.get('data-src') or img_el.get('src')
                    if img_src:
                        img_src = "https:" + img_src if img_src.startswith('//') else img_src
                        img_src = re.sub(r'_\d+x\d+', '', img_src).split('?')[0]

                    results.append({
                        "title": title, "url": link, "image_url": img_src,
                        "price": price, "source": "Empress.cc"
                    })
                except: continue
        except Exception as e:
            logger.error(f"Empress Parse Error: {e}")
        return results

    def search_olx(self, query):
        clean_query = re.sub(r'[^\w\s]', '', query).strip().replace(' ', '-')
        url = f"https://www.olx.ua/uk/list/q-{clean_query}/?search%5Bphotos%5D=1"
        results = []
        try:
            resp = requests.get(url, headers=self.get_headers(), timeout=15)
            soup = BeautifulSoup(resp.text, 'lxml')
            cards = soup.find_all('div', {'data-cy': 'l-card'})
            
            for card in cards[:10]:
                try:
                    link_tag = card.find('a', href=True)
                    if not link_tag: continue
                    
                    full_url = link_tag['href']
                    if not full_url.startswith('http'): full_url = f"https://www.olx.ua{full_url}"
                    
                    if 'promoted' in str(card).lower(): continue

                    title = card.find('h6').text.strip()
                    price_div = card.find('p', {'data-testid': 'ad-price'})
                    price = price_div.text.strip() if price_div else "?"
                    
                    img_tag = card.find('img')
                    img_url = img_tag.get('src') or img_tag.get('data-src')
                    if not img_url: continue

                    is_replica = any(w in title.lower() for w in ['копия', 'реплика', 'copy', 'replica', 'aaa'])

                    results.append({
                        "title": title, "url": full_url, "price": price,
                        "image_url": img_url, "is_replica": is_replica
                    })
                except: continue
        except Exception as e:
            logger.error(f"OLX Search Error: {e}")
        return results

scraper = ScraperEngine()

# --- 🤖 BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Access Denied.")
        return

    stats = len(await JsonDatabase.load(SOURCES_FILE))
    kb = [
        [InlineKeyboardButton("📸 Add Photo Sample", callback_data="add_photo")],
        [InlineKeyboardButton("🌐 Sync Empress", callback_data="sync_web")],
        [InlineKeyboardButton(f"📋 List Targets ({stats})", callback_data="list")],
        [InlineKeyboardButton("🛑 Clear Database", callback_data="clear")]
    ]
    
    await update.message.reply_text(
        f"🖥 **Collector Pro Panel**\n\nStatus: ✅ Active\nTargets: {stats}\nEngine: CV + Web Scraping",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "add_photo":
        await query.message.reply_text("📤 Send me a photo of the item to track.")

    elif data == "sync_web":
        await query.message.reply_text("⏳ Syncing Empress.cc...")
        items = await asyncio.to_thread(scraper.parse_empress_cc)
        if items:
            db = await JsonDatabase.load(SOURCES_FILE)
            added = 0
            for item in items:
                if not any(x['url'] == item['url'] for x in db):
                    db.append(item)
                    added += 1
            await JsonDatabase.save(SOURCES_FILE, db)
            await query.message.reply_text(f"✅ Added {added} new items.")
        else:
            await query.message.reply_text("❌ No items found.")

    elif data == "list":
        sources = await JsonDatabase.load(SOURCES_FILE)
        text = "📋 **Active Targets:**\n\n" + "\n".join([f"{i}. {s['title']}" for i, s in enumerate(sources[:10], 1)])
        if len(sources) > 10: text += f"\n...and {len(sources)-10} more."
        await query.message.reply_text(text or "Empty.", parse_mode=ParseMode.MARKDOWN)

    elif data == "clear":
        await JsonDatabase.save(SOURCES_FILE, [])
        await JsonDatabase.save(HISTORY_FILE, [])
        await query.message.reply_text("🗑 Database cleared.")

async def handle_photo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    filename = f"{int(time.time())}_{photo.file_id[:5]}.jpg"
    path = os.path.join(IMAGES_DIR, filename)
    await file.download_to_drive(path)
    
    new_item = {
        "title": f"Manual Item {filename}", "url": "local_upload",
        "image_url": path, "price": "N/A", "source": "User Upload"
    }
    
    db = await JsonDatabase.load(SOURCES_FILE)
    db.append(new_item)
    await JsonDatabase.save(SOURCES_FILE, db)
    await update.message.reply_text("✅ Photo added! Tracking started.")

# --- 🔄 MONITORING LOOP ---
async def monitoring_loop(context: ContextTypes.DEFAULT_TYPE):
    sources = await JsonDatabase.load(SOURCES_FILE)
    history = await JsonDatabase.load(HISTORY_FILE)
    
    if not sources: return
    logger.info(f"🔎 Scanning {len(sources)} items...")
    
    batch = random.sample(sources, min(len(sources), 5))

    for target in batch:
        target_bytes = await asyncio.to_thread(scraper.download_image_bytes, target['image_url'])
        target_cv = cv_engine.load_image_from_bytes(target_bytes)
        if target_cv is None: continue

        olx_results = await asyncio.to_thread(scraper.search_olx, target['title'])
        
        for item in olx_results:
            if item['url'] in history: continue

            item_bytes = await asyncio.to_thread(scraper.download_image_bytes, item['image_url'])
            item_cv = cv_engine.load_image_from_bytes(item_bytes)

            similarity = await asyncio.to_thread(cv_engine.compare, target_cv, item_cv)
            
            if similarity > 20.0:
                logger.info(f"MATCH: {similarity:.1f}% -> {item['title']}")
                status = "⚠️ REPLICA" if item['is_replica'] else "✅ PROBABLY ORIGINAL"
                
                caption = (
                    f"🚨 **MATCH FOUND!**\n\n"
                    f"🔍 **Target:** {target['title']}\n"
                    f"📦 **Found:** {item['title']}\n"
                    f"💵 **Price:** {item['price']}\n"
                    f"🛡 **Status:** {status}\n"
                    f"📊 **Similarity:** {similarity:.1f}%\n"
                    f"🔗 [OLX Link]({item['url']})"
                )
                try:
                    await context.bot.send_photo(CHANNEL_ID, item['image_url'], caption=caption, parse_mode=ParseMode.MARKDOWN)
                    history.append(item['url'])
                    await JsonDatabase.save(HISTORY_FILE, history[-1000:])
                except Exception as e:
                    logger.error(f"Telegram Error: {e}")
            await asyncio.sleep(2)

# --- 🌍 WEB SERVER ---
async def health_check(request):
    return web.Response(text="Bot is Alive", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌍 Web Server on port {PORT}")

# --- 🚀 STARTUP ---
async def post_init(application: Application):
    await start_web_server()
    try:
        await application.bot.send_message(CHANNEL_ID, "🤖 **CollectorBot Pro v4.0** Started!")
    except: pass

def main():
    # Corrected Initialization for v20.x to avoid AttributeError
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_upload))

    if application.job_queue:
        application.job_queue.run_repeating(monitoring_loop, interval=300, first=20)

    print("🚀 Bot Polling Started...")
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
