# ==============================================================================
# Watch-Expert AI Pro v7.1 [Final Stable]
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
from datetime import datetime
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

# --- AI SETUP ---
ai_client = genai.Client(api_key=GEMINI_API_KEY)
AI_MODEL = "gemini-1.5-flash"

# --- DB POOL ---
async def create_pool():
    return await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)

async def init_schema(pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS empress_watches (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE,
                collection TEXT,
                price NUMERIC,
                image_path TEXT,
                last_updated TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS olx_archive (
                id SERIAL PRIMARY KEY,
                url TEXT UNIQUE,
                title TEXT,
                price NUMERIC,
                ai_verdict JSONB,
                status TEXT DEFAULT 'PENDING',
                detected_at TIMESTAMP DEFAULT NOW()
            );
        """)

# --- UTILS ---
async def get_currency_rate():
    return 41.50 # Актуальний курс USD/UAH

async def fetch_html(session, url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with session.get(url, headers=headers, timeout=20) as resp:
            if resp.status == 200: return await resp.text()
    except: return None

# --- MODULE 1: AI CORE ---
async def analyze_image_ai(image_bytes: bytes):
    prompt = """Analyze watch image. Return JSON: {"brand": "String", "model": "String", "authenticity": "ORIGINAL"|"FAKE", "confidence": 0-100, "estimated_market_price_usd": Number}"""
    try:
        response = ai_client.models.generate_content(
            model=AI_MODEL,
            contents=[{"mime_type": "image/jpeg", "data": image_bytes}, prompt]
        )
        clean = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except: return None

# --- MODULE 2: EMPRESS SYNC ---
async def sync_empress_task(pool):
    logger.info("🔄 Starting immediate Empress.cc data collection...")
    async with aiohttp.ClientSession() as session:
        html = await fetch_html(session, "https://empress.cc/collections")
        if not html: return
        soup = BeautifulSoup(html, "html.parser")
        collections = list(set([a['href'] for a in soup.select("a[href^='/collections/']") if 'all' not in a['href']]))
        
        for col in collections:
            page = 1
            while page <= 3:
                url = f"https://empress.cc{col}?page={page}"
                content = await fetch_html(session, url)
                if not content: break
                c_soup = BeautifulSoup(content, "html.parser")
                items = c_soup.select(".grid-product__content")
                if not items: break
                
                async with pool.acquire() as conn:
                    for item in items:
                        try:
                            name = item.select_one(".grid-product__title").text.strip()
                            price_raw = item.select_one(".grid-product__price").text
                            price = float(re.sub(r"[^\d.]", "", price_raw))
                            await conn.execute("""
                                INSERT INTO empress_watches (name, collection, price)
                                VALUES ($1, $2, $3) ON CONFLICT (name) DO UPDATE SET price = $3
                            """, name, col, price)
                        except: continue
                page += 1
                await asyncio.sleep(0.5)
    logger.info("✅ Empress DB Synchronization Complete")

# --- MODULE 3: OLX SCANNER ---
async def olx_scanner_task(pool, bot):
    queries = ["seiko", "tissot", "orient", "годинник"]
    while True:
        async with aiohttp.ClientSession() as session:
            for q in queries:
                url = f"https://www.olx.ua/uk/list/q-{q}/?search%5Bfilter_float_price%3Afrom%5D=2000"
                html = await fetch_html(session, url)
                if not html: continue
                
                soup = BeautifulSoup(html, "html.parser")
                cards = soup.select("div[data-cy='l-card']")
                for card in cards[:5]:
                    try:
                        link = "https://www.olx.ua" + card.select_one("a")['href']
                        async with pool.acquire() as conn:
                            if await conn.fetchval("SELECT 1 FROM olx_archive WHERE url=$1", link): continue
                            
                        title = card.select_one("h6").text.strip()
                        price_text = card.select_one("[data-testid='ad-price']").text
                        price = float(re.sub(r"[^\d]", "", price_text))
                        
                        img_url = card.select_one("img").get('src')
                        ai_data = {}
                        if img_url:
                            async with session.get(img_url) as r:
                                if r.status == 200: ai_data = await analyze_image_ai(await r.read())
                        
                        async with pool.acquire() as conn:
                            await conn.execute("""
                                INSERT INTO olx_archive (url, title, price, ai_verdict, status)
                                VALUES ($1, $2, $3, $4, 'PENDING')
                            """, link, title, price, json.dumps(ai_data))
                    except: continue
                await asyncio.sleep(10)
        await asyncio.sleep(600)

# --- MODULE 4: POSTING MANAGER ---
async def poster_task(pool, bot):
    """Що 10 хвилин публікує знайдені лоти в канал"""
    while True:
        await asyncio.sleep(600) # 10 хвилин
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM olx_archive WHERE status = 'PENDING' LIMIT 5")
            for row in rows:
                ai = json.loads(row['ai_verdict']) if row['ai_verdict'] else {}
                brand = ai.get('brand', 'Unknown')
                
                # Пошук співпадіння в базі Empress
                ref = await conn.fetchrow("SELECT * FROM empress_watches WHERE name ILIKE $1", f"%{brand}%")
                
                msg = f"🔔 <b>НОВИЙ ЛОТ ЗНАЙДЕНО</b>\n\n"
                msg += f"⌚ {row['title']}\n"
                msg += f"💰 Ціна OLX: {row['price']} UAH\n"
                
                if ref:
                    msg += f"🏛 <b>Знайдено в Empress.cc:</b>\n"
                    msg += f"└ Модель: {ref['name']}\n"
                    msg += f"└ Ціна сайту: ${ref['price']} (~{int(ref['price'] * 41.5)} UAH)\n"
                    msg += f"🔥 <b>Вигода: {int((ref['price'] * 41.5) - row['price'])} UAH</b>\n"
                
                msg += f"\n🔗 <a href='{row['url']}'>Переглянути на OLX</a>"
                
                try:
                    await bot.send_message(CHANNEL_ID, msg)
                    await conn.execute("UPDATE olx_archive SET status = 'POSTED' WHERE id = $1", row['id'])
                    await asyncio.sleep(2)
                except: continue

# --- BOT HANDLERS ---
bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: types.Message, db_pool):
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM empress_watches")
    
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика бази", callback_data="stats")
    await message.answer(
        f"⌚ <b>Watch-Expert AI v7.1</b>\n\n"
        f"База даних Empress: <code>{count}</code> моделей.\n"
        f"Система автоматично сканує OLX та Empress.",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data == "stats")
async def cb_stats(callback: types.CallbackQuery, db_pool):
    async with db_pool.acquire() as conn:
        empress_count = await conn.fetchval("SELECT COUNT(*) FROM empress_watches")
        olx_count = await conn.fetchval("SELECT COUNT(*) FROM olx_archive")
    await callback.message.answer(f"📊 <b>Статистика:</b>\n- В базі Empress: {empress_count}\n- Оброблено оголошень: {olx_count}")

# --- MAIN ---
async def main():
    pool = await create_pool()
    await init_schema(pool)
    dp['db_pool'] = pool

    # Background tasks
    asyncio.create_task(sync_empress_task(pool)) # Заповнення бази при старті
    asyncio.create_task(olx_scanner_task(pool, bot))
    asyncio.create_task(poster_task(pool, bot))

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())