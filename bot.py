```python
import os
import re
import json
import time
import random
import logging
import asyncio
import asyncpg
import aiohttp
from io import BytesIO
from datetime import datetime
from typing import List, Dict, Any, Optional

from PIL import Image, ImageEnhance, ImageFilter
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/watchdb")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0)
ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID") or 0)
IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "empress_images")
os.makedirs(IMG_DIR, exist_ok=True)

EMPRESS_BASE = "https://empress.cc"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.1 Safari/605.1.15"
]
PROXIES = [p for p in (os.getenv("HTTP_PROXIES") or "").split(",") if p]
SCAN_CONCURRENCY = int(os.getenv("SCAN_CONCURRENCY", "4"))
RATE_API = os.getenv("CURRENCY_API_URL", "")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("WatchExpert_v7")

# -------------------------
# Database
# -------------------------
async def create_pool():
    return await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)

async def init_schema(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS empress_collections (
                id SERIAL PRIMARY KEY,
                path TEXT UNIQUE,
                title TEXT,
                fetched_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS empress_watches (
                id SERIAL PRIMARY KEY,
                empress_id TEXT UNIQUE,
                name TEXT,
                collection TEXT,
                price NUMERIC,
                currency TEXT,
                image_path TEXT,
                details JSONB,
                created_at TIMESTAMP DEFAULT NOW(),
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

# -------------------------
# Utilities
# -------------------------
def choose_proxy() -> Optional[str]:
    return random.choice(PROXIES) if PROXIES else None

def sanitize_filename(s: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_\-\.]', '_', s)[:120]

async def fetch_text(session: aiohttp.ClientSession, url: str, headers=None, proxy=None, timeout=20) -> Optional[str]:
    try:
        async with session.get(url, headers=headers, proxy=proxy, timeout=timeout) as r:
            if r.status == 200:
                return await r.text()
            logger.debug("fetch_text non-200 %s %s", url, r.status)
    except Exception as e:
        logger.debug("fetch_text error %s %s", url, e)
    return None

async def fetch_bytes(session: aiohttp.ClientSession, url: str, headers=None, proxy=None, timeout=30) -> Optional[bytes]:
    try:
        async with session.get(url, headers=headers, proxy=proxy, timeout=timeout) as r:
            if r.status == 200:
                return await r.read()
            logger.debug("fetch_bytes non-200 %s %s", url, r.status)
    except Exception as e:
        logger.debug("fetch_bytes error %s %s", url, e)
    return None

async def save_image(bytes_data: bytes, name_hint: str) -> str:
    fname = sanitize_filename(f"{name_hint}_{int(time.time())}_{random.randint(1000,9999)}.jpg")
    path = os.path.join(IMG_DIR, fname)
    try:
        with open(path, "wb") as f:
            f.write(bytes_data)
        return path
    except Exception as e:
        logger.error("save_image error: %s", e)
        return ""

async def upscale_image_local(image_bytes: bytes) -> bytes:
    loop = asyncio.get_running_loop()
    def proc():
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
        img = img.filter(ImageFilter.SHARPEN)
        img = ImageEnhance.Contrast(img).enhance(1.12)
        bio = BytesIO()
        img.save(bio, format="JPEG", quality=92)
        return bio.getvalue()
    return await loop.run_in_executor(None, proc)

async def get_currency_rate() -> float:
    try:
        if RATE_API:
            async with aiohttp.ClientSession() as s:
                async with s.get(RATE_API, timeout=8) as r:
                    if r.status == 200:
                        data = await r.json()
                        return float(data.get("rate", 41.5))
    except Exception:
        pass
    return 41.5

# -------------------------
# AI placeholder
# -------------------------
async def analyze_image_ai(image_bytes: bytes) -> Dict[str, Any]:
    return {
        "brand": "Unknown",
        "model": "",
        "authenticity": "SUSPICIOUS",
        "confidence": 50,
        "mechanism": "AUTOMATIC",
        "defects": [],
        "estimated_market_price_usd": 0,
        "liquidity_rating": 5,
        "is_male": True
    }

# -------------------------
# Empress scraper (robust)
# -------------------------
async def list_collections(session: aiohttp.ClientSession) -> List[Dict[str, str]]:
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    html = await fetch_text(session, f"{EMPRESS_BASE}/collections", headers=headers, proxy=choose_proxy())
    if not html:
        logger.warning("list_collections: no html from collections page")
        return []
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()
    for a in soup.select("a[href^='/collections/']"):
        href = a.get("href")
        if not href:
            continue
        if href in seen:
            continue
        seen.add(href)
        title = a.get_text(strip=True) or href.split("/")[-1]
        links.append({"path": href, "title": title})
    logger.info("Found %d collections", len(links))
    return links

def extract_img_src(img_tag) -> Optional[str]:
    if not img_tag:
        return None
    for attr in ("src", "data-src", "data-srcset", "srcset", "data-lazy-src"):
        val = img_tag.get(attr)
        if val:
            if "," in val:
                parts = [p.strip() for p in val.split(",") if p.strip()]
                last = parts[-1]
                url = last.split(" ")[0]
            else:
                url = val
            if url.startswith("//"):
                url = "https:" + url
            return url
    return None

def parse_price_text(price_txt: str) -> float:
    if not price_txt:
        return 0.0
    txt = price_txt.replace("\u00A0", "").replace(",", ".")
    num = re.sub(r"[^\d\.]", "", txt)
    try:
        return float(num) if num else 0.0
    except Exception:
        return 0.0

async def fetch_collection_products(session: aiohttp.ClientSession, col_path: str) -> List[Dict[str, Any]]:
    page = 1
    results = []
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    while True:
        url = f"{EMPRESS_BASE}{col_path}?page={page}"
        html = await fetch_text(session, url, headers=headers, proxy=choose_proxy())
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        products = soup.select(".grid-product__content, .product-card, .product-item, .product-grid-item")
        if not products:
            ld = []
            for script in soup.select("script[type='application/ld+json']"):
                try:
                    ld.append(json.loads(script.string or "{}"))
                except Exception:
                    continue
            if ld:
                for item in ld:
                    if isinstance(item, dict) and item.get("@type") in ("Product",):
                        title = item.get("name", "Unknown")
                        price = 0.0
                        offers = item.get("offers") or {}
                        price = float(offers.get("price", 0)) if offers else 0.0
                        img = item.get("image")
                        results.append({"title": title, "price": price, "img": img, "url": item.get("url")})
            break
        for p in products:
            try:
                title_tag = p.select_one(".grid-product__title, .product-title, .card-title, h3, h2")
                price_tag = p.select_one(".grid-product__price, .price, .card-price, .product-price")
                img_tag = p.select_one("img")
                link_tag = p.select_one("a[href]")
                title = title_tag.get_text(strip=True) if title_tag else "Unknown"
                price_txt = price_tag.get_text(strip=True) if price_tag else ""
                price = parse_price_text(price_txt)
                img_src = extract_img_src(img_tag)
                product_url = None
                if link_tag:
                    href = link_tag.get("href")
                    if href:
                        product_url = href if href.startswith("http") else EMPRESS_BASE + href
                results.append({
                    "title": title,
                    "price": price,
                    "img": img_src,
                    "url": product_url
                })
            except Exception:
                continue
        page += 1
        await asyncio.sleep(0.4)
    return results

async def fetch_product_details(session: aiohttp.ClientSession, product_url: str) -> Dict[str, Any]:
    if not product_url:
        return {}
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    html = await fetch_text(session, product_url, headers=headers, proxy=choose_proxy())
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    details: Dict[str, Any] = {}
    desc = soup.select_one(".product-single__description, .description, .product-description")
    if desc:
        details["description"] = desc.get_text(strip=True)
    specs = {}
    for row in soup.select(".specs tr, .product-specs li, .product-attributes li"):
        try:
            k = row.select_one("th, .spec-key, .attr-key") or row.select_one("b")
            v = row.select_one("td, .spec-value, .attr-value")
            if k and v:
                specs[k.get_text(strip=True)] = v.get_text(strip=True)
        except Exception:
            continue
    if specs:
        details["specs"] = specs
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string or "{}")
            if isinstance(data, dict) and data.get("@type") == "Product":
                details.setdefault("jsonld", data)
                break
        except Exception:
            continue
    return details

async def sync_empress_all(pool: asyncpg.Pool):
    logger.info("Empress sync started")
    async with aiohttp.ClientSession() as session:
        collections = await list_collections(session)
        if not collections:
            logger.warning("No collections found")
            return
        async with pool.acquire() as conn:
            for col in collections:
                await conn.execute("""
                    INSERT INTO empress_collections (path, title) VALUES ($1, $2)
                    ON CONFLICT (path) DO UPDATE SET title = EXCLUDED.title, fetched_at = NOW()
                """, col["path"], col["title"])
        sem = asyncio.Semaphore(SCAN_CONCURRENCY)
        async def process_collection(col):
            async with sem:
                try:
                    products = await fetch_collection_products(session, col["path"])
                    logger.info("Collection %s: %d products", col["path"], len(products))
                    for p in products:
                        img_path = ""
                        if p.get("img"):
                            b = await fetch_bytes(session, p["img"], headers={"User-Agent": random.choice(USER_AGENTS)}, proxy=choose_proxy())
                            if b:
                                try:
                                    up = await upscale_image_local(b)
                                    img_path = await save_image(up, p["title"])
                                except Exception:
                                    img_path = await save_image(b, p["title"])
                        details = {}
                        if p.get("url"):
                            details = await fetch_product_details(session, p["url"])
                        empress_id = p.get("url") or f"{col['path']}#{p['title']}"
                        async with pool.acquire() as conn:
                            await conn.execute("""
                                INSERT INTO empress_watches (empress_id, name, collection, price, currency, image_path, details)
                                VALUES ($1,$2,$3,$4,$5,$6,$7)
                                ON CONFLICT (empress_id) DO UPDATE
                                  SET name=EXCLUDED.name, price=EXCLUDED.price, image_path=EXCLUDED.image_path, details=EXCLUDED.details, last_updated=NOW()
                            """, empress_id, p["title"], col["title"], p["price"], "USD", img_path, json.dumps({"source_url": p.get("url"), **(details or {})}))
                except Exception as e:
                    logger.error("process_collection error %s %s", col.get("path"), e)
        tasks = [process_collection(c) for c in collections]
        await asyncio.gather(*tasks)
    logger.info("Empress sync completed")

# -------------------------
# OLX scanner (kept robust)
# -------------------------
async def scan_olx(pool: asyncpg.Pool, bot: Bot):
    queries = ["годинник", "часы", "seiko", "tissot", "orient"]
    base_sleep = 30
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                for q in queries:
                    url = f"https://www.olx.ua/uk/list/q-{q}/"
                    headers = {"User-Agent": random.choice(USER_AGENTS)}
                    html = await fetch_text(session, url, headers=headers, proxy=choose_proxy())
                    if not html:
                        continue
                    soup = BeautifulSoup(html, "html.parser")
                    cards = soup.select("div[data-cy='l-card'], .offer-wrapper, .css-1sw7q4x, .css-1bbgabe")
                    for card in cards[:6]:
                        try:
                            link_tag = card.select_one("a[href]")
                            if not link_tag:
                                continue
                            link = link_tag.get("href")
                            if not link.startswith("http"):
                                link = "https://www.olx.ua" + link
                            async with pool.acquire() as conn:
                                exists = await conn.fetchval("SELECT 1 FROM olx_archive WHERE url=$1", link)
                                if exists:
                                    continue
                            title_el = card.select_one("h6") or card.select_one(".offer-title") or card.select_one(".css-1bbgabe")
                            title = title_el.get_text(strip=True) if title_el else "No title"
                            price_tag = card.select_one("[data-testid='ad-price']") or card.select_one(".price")
                            price = parse_price_text(price_tag.get_text()) if price_tag else 0.0
                            img_tag = card.select_one("img")
                            img_url = extract_img_src(img_tag)
                            ai_result = {}
                            if img_url:
                                b = await fetch_bytes(session, img_url, headers=headers, proxy=choose_proxy())
                                if b:
                                    ai_result = await analyze_image_ai(b)
                            is_deal = False
                            est_uah = 0
                            if ai_result:
                                usd = ai_result.get("estimated_market_price_usd", 0)
                                rate = await get_currency_rate()
                                est_uah = usd * rate
                                is_deal = (est_uah / 3) - price > 3000
                            if is_deal and ai_result.get("authenticity") == "ORIGINAL":
                                try:
                                    await bot.send_message(
                                        CHANNEL_ID,
                                        f"🚨 <b>SUPER DEAL</b>\n{title}\nPrice: {price} UAH\nEst Market: {int(est_uah)} UAH\nAI: {ai_result.get('brand')} ({ai_result.get('authenticity')})\n{link}",
                                        parse_mode=ParseMode.HTML
                                    )
                                except Exception:
                                    pass
                            async with pool.acquire() as conn:
                                await conn.execute(
                                    "INSERT INTO olx_archive (url, title, price, ai_verdict, status) VALUES ($1,$2,$3,$4,$5)",
                                    link, title, price, json.dumps(ai_result), "PROCESSED"
                                )
                        except Exception:
                            continue
                    await asyncio.sleep(random.randint(4, 12))
            await asyncio.sleep(base_sleep + random.randint(0, 60))
        except Exception as e:
            logger.error("OLX scanner error: %s", e)
            await asyncio.sleep(60)

# -------------------------
# Telegram bot
# -------------------------
bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

@dp.message.register(CommandStart())
async def cmd_start(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Scan Photo", callback_data="scan_mode")
    kb.button(text="📊 Stats", callback_data="stats")
    kb.button(text="⚙️ Admin", callback_data="admin")
    await message.answer("⌚ <b>Watch-Expert AI v7.0</b>\nSystem Online.", reply_markup=kb.as_markup())

@dp.callback_query.register(F.data == "stats")
async def cb_stats(callback: types.CallbackQuery):
    pool = dp.data.get("db_pool")
    scanned = empress = 0
    if pool:
        async with pool.acquire() as conn:
            scanned = await conn.fetchval("SELECT COUNT(*) FROM olx_archive")
            empress = await conn.fetchval("SELECT COUNT(*) FROM empress_watches")
    await callback.message.edit_text(f"📊 <b>System Stats</b>\n\nOLX Scanned: {scanned}\nEmpress DB: {empress}")

@dp.callback_query.register(F.data == "admin")
async def cb_admin(callback: types.CallbackQuery):
    await callback.message.answer("Admin commands: /sync_empress /report /health")

@dp.message.register(Command("sync_empress"))
async def cmd_sync_empress(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("Unauthorized")
        return
    await message.reply("Starting Empress sync...")
    pool = dp.data.get("db_pool")
    if pool:
        asyncio.create_task(sync_empress_all(pool))

@dp.message.register(Command("report"))
async def cmd_report(message: types.Message):
    pool = dp.data.get("db_pool")
    count = 0
    if pool:
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM empress_watches")
    await message.reply(f"Empress items: {count}")

@dp.message.register(Command("health"))
async def cmd_health(message: types.Message):
    try:
        me = await bot.get_me()
        await message.reply(f"Bot OK: @{me.username}")
    except Exception as e:
        await message.reply(f"Health error: {e}")

@dp.message.register(F.photo)
async def handle_photo(message: types.Message):
    status = await message.answer("⏳ Processing image...")
    file_id = message.photo[-1].file_id
    f = await bot.get_file(file_id)
    io = BytesIO()
    await bot.download_file(f.file_path, io)
    img_bytes = io.getvalue()
    up = await upscale_image_local(img_bytes)
    ai = await analyze_image_ai(up)
    if not ai:
        await status.edit_text("❌ AI analysis failed.")
        return
    rate = await get_currency_rate()
    est_uah = int(ai.get("estimated_market_price_usd", 0) * rate)
    report = (
        f"🕵️ <b>EXPERT REPORT</b>\n"
        f"Brand: {ai.get('brand')} {ai.get('model')}\n"
        f"Mechanism: {ai.get('mechanism')}\n"
        f"Authenticity: {ai.get('authenticity')} ({ai.get('confidence')}%)\n"
        f"Defects: {', '.join(ai.get('defects', []))}\n"
        f"Est Market: ${ai.get('estimated_market_price_usd')} (~{est_uah} UAH)\n"
        f"Liquidity: {ai.get('liquidity_rating')}/10\n"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Chrono24", url=f"https://www.chrono24.com/search/index.htm?query={ai.get('brand')}+{ai.get('model')}")
    await status.edit_text(report, reply_markup=kb.as_markup())

# -------------------------
# Health & Reports
# -------------------------
async def health_check_loop(bot: Bot):
    while True:
        try:
            await bot.get_me()
            logger.info("Health OK")
        except Exception as e:
            logger.error("Health failed: %s", e)
        await asyncio.sleep(3600)

async def weekly_report_loop(pool: asyncpg.Pool, bot: Bot):
    while True:
        now = datetime.utcnow()
        if now.weekday() == 6 and 19 <= now.hour <= 20:
            async with pool.acquire() as conn:
                count = await conn.fetchval("SELECT COUNT(*) FROM olx_archive WHERE detected_at > NOW() - INTERVAL '7 days'")
                if count and ADMIN_ID:
                    await bot.send_message(ADMIN_ID, f"📄 Weekly Report: processed {count} items.")
        await asyncio.sleep(1800)

# -------------------------
# Entry point
# -------------------------
async def main():
    pool = await create_pool()
    await init_schema(pool)
    dp.data["db_pool"] = pool
    asyncio.create_task(sync_empress_all(pool))
    asyncio.create_task(scan_olx(pool, bot))
    asyncio.create_task(health_check_loop(bot))
    asyncio.create_task(weekly_report_loop(pool, bot))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown")
```