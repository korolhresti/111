import os
import asyncio
import asyncpg
import aiohttp
import hashlib
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from google import genai
from PIL import Image
import io

# ========== CONFIG ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # @channel or -100xxx
USD = 38.0

# ========== CLIENTS ==========
bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
gemini = genai.Client(api_key=GEMINI_API_KEY)

# ========== UTILS ==========

async def fetchb(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.read()

def img_hash(b):
    return hashlib.md5(b).hexdigest()

def lens(img_url):
    # placeholder — in prod replace with SerpAPI / Vision API
    return True

def ai_risk(image_bytes):
    prompt = """
You are a luxury watch authentication expert.
Return JSON:
{
 "authentic": true/false,
 "liquidity": 1-10
}
"""
    img = Image.open(io.BytesIO(image_bytes))
    res = gemini.models.generate_content(
        model="gemini-2.0-flash",
        contents=[prompt, img]
    )
    return eval(res.text)

def embed(image_bytes):
    res = gemini.models.embed_content(
        model="models/embedding-001",
        content=image_bytes
    )
    return res["embedding"]

# ========== DB INIT ==========

async def init_db(db):
    async with db.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS empress_watches(
            id SERIAL PRIMARY KEY,
            name TEXT,
            price_usd FLOAT,
            image_url TEXT,
            embedding vector(768)
        );
        """)
        await c.execute("""
        CREATE TABLE IF NOT EXISTS olx_ads(
            id SERIAL PRIMARY KEY,
            title TEXT,
            price_uah FLOAT,
            url TEXT,
            image_url TEXT,
            embedding vector(768)
        );
        """)
        await c.execute("""
        CREATE TABLE IF NOT EXISTS portfolio(
            id SERIAL PRIMARY KEY,
            olx_id INT,
            empress_id INT,
            buy_price FLOAT,
            market_price FLOAT,
            profit FLOAT,
            ts TIMESTAMP DEFAULT now()
        );
        """)

# ========== DEAL ENGINE ==========

async def deals(db):
    async with db.acquire() as c:
        rows = await c.fetch("""
        SELECT *
        FROM (
            SELECT 
                o.id as oid,
                o.title,
                o.price_uah,
                o.url,
                o.image_url,
                e.id as eid,
                e.price_usd,
                1 - (e.embedding <=> o.embedding) AS sim
            FROM olx_ads o
            CROSS JOIN empress_watches e
        ) s
        ORDER BY sim DESC
        LIMIT 50
        """)

        for r in rows:
            olx_usd = r["price_uah"] / USD
            disc = 1 - (olx_usd / r["price_usd"])

            if r["sim"] < 0.85 or disc < 0.5:
                continue

            if not lens(r["image_url"]):
                continue

            img = await fetchb(r["image_url"])
            ai = ai_risk(img)

            if not ai["authentic"] or ai["liquidity"] < 6:
                continue

            await c.execute("""
            INSERT INTO portfolio(olx_id, empress_id, buy_price, market_price, profit)
            VALUES($1,$2,$3,$4,$5)
            """,
            r["oid"],
            r["eid"],
            olx_usd,
            r["price_usd"],
            r["price_usd"] - olx_usd)

            await bot.send_message(
                CHANNEL_ID,
                f"🔥 <b>SUPER DEAL</b>\n"
                f"{r['title']}\n"
                f"OLX ${olx_usd:.0f}\n"
                f"Market ${r['price_usd']}\n"
                f"Profit {int(disc*100)}%\n"
                f"{r['url']}"
            )

# ========== TELEGRAM ==========

@dp.message(F.photo)
async def photo_handler(m: Message):
    photo = m.photo[-1]
    b = await bot.download(photo)
    data = b.read()
    vec = embed(data)

    await m.answer("📸 Photo analyzed.\nVector size: " + str(len(vec)))

@dp.message(F.text == "/start")
async def start(m: Message):
    await m.answer("🤖 Watch-Expert AI Pro is running")

# ========== MAIN LOOP ==========

async def main():
    db = await asyncpg.create_pool(DATABASE_URL)
    await init_db(db)

    asyncio.create_task(dp.start_polling(bot))

    while True:
        try:
            await deals(db)
        except Exception as e:
            print("DEALS ERROR:", e)
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
