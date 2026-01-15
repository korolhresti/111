````python
import asyncio
import logging
import os
import sys
import json
import hashlib
from datetime import datetime, time
import pytz
from typing import List, Optional

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bs4 import BeautifulSoup
from PIL import Image
from dotenv import load_dotenv
from google import genai

# ================== CONFIG ==================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

KYIV_TZ = pytz.timezone("Europe/Kyiv")
QUIET_FROM = time(23, 0)
QUIET_TO = time(8, 0)

EMRESS_BASE = "https://empress.cc"
EMRESS_COLLECTIONS = [
    "swiss-vintage-watches",
    "dive-watches",
    "military-watches",
]

OLX_SEARCH = "https://www.olx.ua/d/uk/list/q-годинник/"

GEMINI_MODEL = "gemini-1.5-flash-latest"
gen_client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("WatchExpertBot")

# ================== BOT ==================
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ================== DB ==================
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS empress_watches (
    id SERIAL PRIMARY KEY,
    collection TEXT,
    title TEXT,
    price_usd NUMERIC,
    image_url TEXT,
    image_hash TEXT UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS olx_ads (
    id SERIAL PRIMARY KEY,
    title TEXT,
    price_uah NUMERIC,
    url TEXT,
    image_url TEXT,
    image_hash TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS matches (
    id SERIAL PRIMARY KEY,
    empress_id INT REFERENCES empress_watches(id),
    olx_id INT REFERENCES olx_ads(id),
    score INT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

async def create_pool():
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute(CREATE_SQL)
    return pool

# ================== UTILS ==================
def quiet_mode():
    now = datetime.now(KYIV_TZ).time()
    return now >= QUIET_FROM or now < QUIET_TO

def img_hash_from_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

async def fetch(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, timeout=30) as r:
        return await r.text()

async def fetch_bytes(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url, timeout=30) as r:
        return await r.read()

# ================== EMPRESS COLLECTOR ==================
async def collect_empress(pool):
    async with aiohttp.ClientSession() as session:
        for col in EMRESS_COLLECTIONS:
            html = await fetch(session, f"{EMRESS_BASE}/collections/{col}")
            soup = BeautifulSoup(html, "html.parser")
            cards = soup.select("a.grid-product__link")
            for a in cards:
                title = a.get_text(strip=True)
                img = a.select_one("img")
                price_el = a.select_one(".grid-product__price")
                if not img or not price_el:
                    continue
                img_url = img.get("src")
                price_txt = price_el.get_text(strip=True).replace("$", "").replace(",", "")
                try:
                    price = float(price_txt)
                except:
                    continue
                img_bytes = await fetch_bytes(session, img_url)
                ih = img_hash_from_bytes(img_bytes)
                async with pool.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO empress_watches(collection,title,price_usd,image_url,image_hash)
                           VALUES($1,$2,$3,$4,$5)
                           ON CONFLICT (image_hash) DO NOTHING""",
                        col, title, price, img_url, ih
                    )

# ================== OLX SCANNER ==================
async def scan_olx(pool):
    async with aiohttp.ClientSession() as session:
        html = await fetch(session, OLX_SEARCH)
        soup = BeautifulSoup(html, "html.parser")
        ads = soup.select("a.css-rc5s2u")
        for a in ads[:30]:
            title = a.get_text(strip=True)
            url = a.get("href")
            price_el = a.select_one(".css-8kqr5l")
            img = a.select_one("img")
            if not price_el or not img:
                continue
            price_txt = price_el.get_text(strip=True).replace("грн", "").replace(" ", "")
            try:
                price = float(price_txt)
            except:
                continue
            img_url = img.get("src")
            img_bytes = await fetch_bytes(session, img_url)
            ih = img_hash_from_bytes(img_bytes)
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO olx_ads(title,price_uah,url,image_url,image_hash)
                       VALUES($1,$2,$3,$4,$5)""",
                    title, price, url, img_url, ih
                )

# ================== MATCHING ==================
async def match(pool):
    async with pool.acquire() as conn:
        ems = await conn.fetch("SELECT id,image_hash,price_usd FROM empress_watches")
        olx = await conn.fetch("SELECT id,image_hash,price_uah,url,title FROM olx_ads")
        for e in ems:
            for o in olx:
                if e["image_hash"] == o["image_hash"]:
                    score = 100
                else:
                    score = 0
                if score >= 90:
                    await conn.execute(
                        "INSERT INTO matches(empress_id,olx_id,score) VALUES($1,$2,$3)",
                        e["id"], o["id"], score
                    )
                    if not quiet_mode():
                        await bot.send_message(
                            CHANNEL_ID,
                            f"🔥 <b>Супер-угода</b>\n{o['title']}\nЦіна: {o['price_uah']} грн\n{ o['url'] }"
                        )

# ================== AI ANALYSIS ==================
def build_prompt():
    return """Return JSON only:
{"brand":"","mechanism":"","authentic":true,"price_estimate_usd":0,"comment":""}"""

@dp.message(F.photo)
async def analyze_photo(message: types.Message, db_pool):
    file = await bot.get_file(message.photo[-1].file_id)
    path = file.file_path
    data = await bot.download_file(path)
    img_bytes = data.read()
    res = gen_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[{"mime_type": "image/jpeg", "data": img_bytes}, build_prompt()]
    )
    txt = res.text.replace("```json","").replace("```","").strip()
    try:
        js = json.loads(txt)
    except:
        js = {"comment": txt}
    await message.answer(f"🧠 AI:\n{json.dumps(js,ensure_ascii=False,indent=2)}")

# ================== COMMANDS ==================
@dp.message(CommandStart())
async def start(message: types.Message):
    await message.answer("Надішли фото годинника для аналізу.")

# ================== MAIN ==================
async def main():
    pool = await create_pool()
    dp["db"] = pool
    await bot.delete_webhook(drop_pending_updates=True)
    if ADMIN_ID:
        await bot.send_message(ADMIN_ID, "Бот запущено")
    asyncio.create_task(collect_empress(pool))
    asyncio.create_task(scan_olx(pool))
    asyncio.create_task(match(pool))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
````
