# ==============================================================================
# Watch-Expert AI Pro v7.0 [Final Release]
# Core: Gemini Vision | Empress Collector | OLX Deep Scan | AsyncPG | Telegram
# ==============================================================================

import os
import sys
import json
import asyncio
import logging
import random
import re
import aiohttp
import asyncpg
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image, ImageEnhance, ImageFilter
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
from google import genai

# --- CONFIGURATION ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_CHAT_ID")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(BASE_DIR, "empress_images")
os.makedirs(IMG_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("WatchExpert_v7")

# --- AI CLIENT ---
ai_client = genai.Client(api_key=GEMINI_API_KEY)
AI_MODEL = "gemini-1.5-flash"

# --- DATABASE LAYER ---
async def create_pool():
    return await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)

async def init_schema(pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS empress_watches (
                id SERIAL PRIMARY KEY,
                name TEXT,
                collection TEXT,
                price NUMERIC,
                currency TEXT DEFAULT 'USD',
                image_path TEXT,
                features JSONB,
                last_updated TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS olx_archive (
                id SERIAL PRIMARY KEY,
                url TEXT UNIQUE,
                title TEXT,
                price NUMERIC,
                ai_verdict JSONB,
                status TEXT,
                detected_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS system_logs (
                id SERIAL PRIMARY KEY,
                event TEXT,
                level TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

# --- UTILITIES ---
async def upscale_image(image_bytes: bytes) -> bytes:
    loop = asyncio.get_running_loop()
    def process():
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
        img = img.filter(ImageFilter.SHARPEN)
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.2)
        bio = BytesIO()
        img.save(bio, format="JPEG", quality=95)
        return bio.getvalue()
    return await loop.run_in_executor(None, process)

async def get_currency_rate():
    # Placeholder for NBU/Bank API
    return 41.50

async def fetch_html(session, url):
    try:
        async with session.get(url, timeout=20) as resp:
            if resp.status == 200: return await resp.text()
    except Exception: return None

# --- MODULE 1: AI CORE ---
def build_prompt():
    return """
    ACT AS: Horology Expert.
    TASK: Analyze watch image.
    RETURN JSON ONLY:
    {
        "brand": "String",
        "model": "String",
        "authenticity": "ORIGINAL" | "SUSPICIOUS" | "FAKE",
        "confidence": 0-100,
        "mechanism": "QUARTZ" | "MECHANICAL" | "AUTOMATIC",
        "defects": ["scratch_glass", "worn_strap", "dial_misalignment"],
        "estimated_market_price_usd": Number,
        "liquidity_rating": 1-10,
        "is_male": true/false
    }
    """

async def analyze_image_ai(image_bytes: bytes):
    try:
        response = ai_client.models.generate_content(
            model=AI_MODEL,
            contents=[{"mime_type": "image/jpeg", "data": image_bytes}, build_prompt()]
        )
        clean = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception as e:
        logger.error(f"AI Analysis Error: {e}")
        return None

# --- MODULE 2: EMPRESS COLLECTOR ---
EMPRESS_URL = "https://empress.cc"

async def sync_empress_all(pool):
    logger.info("🔄 Empress.cc Sync Started")
    async with aiohttp.ClientSession() as session:
        html = await fetch_html(session, f"{EMPRESS_URL}/collections")
        if not html: return
        soup = BeautifulSoup(html, "html.parser")
        collections = list(set([a['href'] for a in soup.select("a[href^='/collections/']") if 'all' not in a['href']]))

        for col_path in collections:
            page = 1
            while True:
                url = f"{EMPRESS_URL}{col_path}?page={page}"
                c_html = await fetch_html(session, url)
                if not c_html: break
                
                c_soup = BeautifulSoup(c_html, "html.parser")
                products = c_soup.select(".grid-product__content")
                if not products: break

                for p in products:
                    try:
                        title = p.select_one(".grid-product__title").text.strip()
                        price_tag = p.select_one(".grid-product__price")
                        price = float(re.sub(r"[^\d.]", "", price_tag.text)) if price_tag else 0
                        img_tag = p.select_one("img")
                        
                        if img_tag:
                            img_src = img_tag['src']
                            if img_src.startswith("//"): img_src = "https:" + img_src
                            
                            fname = f"{re.sub(r'[^a-zA-Z0-9]', '', title)[:20]}_{random.randint(1000,9999)}.jpg"
                            fpath = os.path.join(IMG_DIR, fname)
                            
                            if not os.path.exists(fpath):
                                async with session.get(img_src) as i_resp:
                                    if i_resp.status == 200:
                                        with open(fpath, "wb") as f:
                                            f.write(await i_resp.read())

                            async with pool.acquire() as conn:
                                await conn.execute("""
                                    INSERT INTO empress_watches (name, collection, price, image_path)
                                    VALUES ($1, $2, $3, $4)
                                    ON CONFLICT DO NOTHING
                                """, title, col_path, price, fpath)
                    except Exception: continue
                page += 1
                await asyncio.sleep(1)

# --- MODULE 3: OLX SCANNER ---
USER_AGENTS = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0"]

async def scan_olx_v7(pool, bot: Bot):
    logger.info("📡 OLX Scanner v7.0 Active")
    queries = ["годинник", "часы", "seiko", "tissot", "orient"]
    
    while True:
        async with aiohttp.ClientSession() as session:
            for query in queries:
                url = f"https://www.olx.ua/uk/list/q-{query}/?search%5Bfilter_float_price%3Afrom%5D=2000"
                try:
                    headers = {"User-Agent": random.choice(USER_AGENTS)}
                    async with session.get(url, headers=headers) as resp:
                        if resp.status != 200: continue
                        html = await resp.text()

                    soup = BeautifulSoup(html, "html.parser")
                    cards = soup.select("div[data-cy='l-card']")

                    for card in cards[:3]:
                        link_tag = card.select_one("a")
                        if not link_tag: continue
                        link = link_tag['href']
                        if not link.startswith("http"): link = f"https://www.olx.ua{link}"

                        async with pool.acquire() as conn:
                            exists = await conn.fetchval("SELECT 1 FROM olx_archive WHERE url=$1", link)
                            if exists: continue

                        title = card.select_one("h6").text.strip()
                        price_txt = card.select_one("[data-testid='ad-price']")
                        price = float(re.sub(r"[^\d]", "", price_txt.text)) if price_txt else 0
                        
                        img_tag = card.select_one("img")
                        img_url = img_tag.get('src') or img_tag.get('data-src')

                        ai_result = {}
                        is_deal = False
                        est_uah = 0

                        if img_url:
                            async with session.get(img_url) as img_resp:
                                if img_resp.status == 200:
                                    img_bytes = await img_resp.read()
                                    ai_result = await analyze_image_ai(img_bytes)

                        if ai_result:
                            usd_rate = await get_currency_rate()
                            est_uah = ai_result.get("estimated_market_price_usd", 0) * usd_rate
                            if est_uah > 0:
                                is_deal = (est_uah / 3) - price > 3000

                        if is_deal and ai_result.get("authenticity") == "ORIGINAL":
                            await bot.send_message(
                                CHANNEL_ID,
                                f"🚨 <b>SUPER DEAL FOUND!</b>\n\n⌚ <b>{title}</b>\n💰 Price: {price} UAH\n📉 Est. Market: {int(est_uah)} UAH\n💎 AI: {ai_result.get('brand')} ({ai_result.get('authenticity')})\n👉 {link}",
                                parse_mode=ParseMode.HTML
                            )

                        async with pool.acquire() as conn:
                            await conn.execute(
                                "INSERT INTO olx_archive (url, title, price, ai_verdict, status) VALUES ($1, $2, $3, $4, 'PROCESSED')",
                                link, title, price, json.dumps(ai_result)
                            )
                        await asyncio.sleep(random.randint(3, 7))

                except Exception as e:
                    logger.error(f"Scan Loop Error: {e}")
                
                await asyncio.sleep(random.randint(20, 40))
        await asyncio.sleep(300)

# --- MODULE 4: TELEGRAM BOT ---
bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Scan Photo", callback_data="scan_mode")
    kb.button(text="📊 Stats", callback_data="stats")
    await message.answer("⌚ <b>Watch-Expert AI v7.0</b>\nSystem is Online.", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "stats")
async def cb_stats(callback: types.CallbackQuery, db_pool: asyncpg.Pool):
    async with db_pool.acquire() as conn:
        scanned = await conn.fetchval("SELECT COUNT(*) FROM olx_archive")
        empress = await conn.fetchval("SELECT COUNT(*) FROM empress_watches")
    await callback.message.edit_text(f"📊 <b>System Stats</b>\n\n📡 OLX Scanned: {scanned}\n📚 Empress DB: {empress}")

@dp.message(F.photo)
async def handle_photo(message: types.Message, db_pool: asyncpg.Pool):
    status_msg = await message.answer("⏳ <b>Processing:</b> Upscaling & Deep Vision Analysis...")
    
    file_id = message.photo[-1].file_id
    f = await bot.get_file(file_id)
    io_obj = BytesIO()
    await bot.download_file(f.file_path, io_obj)
    high_res_bytes = await upscale_image(io_obj.getvalue())

    ai_data = await analyze_image_ai(high_res_bytes)
    if not ai_data:
        await status_msg.edit_text("❌ AI Analysis Failed.")
        return

    async with db_pool.acquire() as conn:
        refs = await conn.fetch("""
            SELECT name, price FROM empress_watches 
            WHERE name ILIKE $1 ORDER BY price DESC LIMIT 3
        """, f"%{ai_data.get('brand', 'xxx')}%")

    rate = await get_currency_rate()
    est_uah = ai_data.get('estimated_market_price_usd', 0) * rate
    
    report = (
        f"🕵️ <b>EXPERT REPORT</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🏷 <b>Brand:</b> {ai_data.get('brand')} {ai_data.get('model')}\n"
        f"⚙️ <b>Mechanism:</b> {ai_data.get('mechanism')}\n"
        f"💎 <b>Authenticity:</b> {ai_data.get('authenticity')} ({ai_data.get('confidence')}%) \n"
        f"⚠️ <b>Defects:</b> {', '.join(ai_data.get('defects', []))}\n"
        f"━━━━━━━━━━━━━━\n"
        f"💵 <b>Est. Market:</b> ${ai_data.get('estimated_market_price_usd')} (~{int(est_uah)} UAH)\n"
        f"🌊 <b>Liquidity:</b> {ai_data.get('liquidity_rating')}/10\n"
    )

    if refs:
        report += "\n📚 <b>Empress.cc Reference:</b>\n"
        for r in refs:
            report += f"• {r['name']} (${r['price']})\n"

    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Search Chrono24", url=f"https://www.chrono24.com/search/index.htm?query={ai_data.get('brand')}+{ai_data.get('model')}")
    
    await status_msg.edit_text(report, reply_markup=kb.as_markup())

# --- MODULE 5: HEALTH & REPORTS ---
async def health_check_loop(bot: Bot):
    while True:
        try:
            await bot.get_me()
            logger.info("✅ Health Check OK")
        except Exception as e:
            logger.error(f"Health Check Failed: {e}")
        await asyncio.sleep(43200)

async def generate_weekly_report(pool, bot: Bot):
    while True:
        now = datetime.now()
        if now.weekday() == 6 and now.hour == 20:
             async with pool.acquire() as conn:
                 count = await conn.fetchval("SELECT COUNT(*) FROM olx_archive WHERE detected_at > NOW() - INTERVAL '7 days'")
                 if count > 0:
                    await bot.send_message(ADMIN_ID, f"📄 <b>Weekly Report:</b>\nProcessed {count} items.")
        await asyncio.sleep(3600)

# --- ENTRY POINT ---
async def main():
    pool = await create_pool()
    await init_schema(pool)
    
    # Start Background Workers
    asyncio.create_task(sync_empress_all(pool))
    asyncio.create_task(scan_olx_v7(pool, bot))
    asyncio.create_task(health_check_loop(bot))
    asyncio.create_task(generate_weekly_report(pool, bot))

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, db_pool=pool)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("System Shutdown.")