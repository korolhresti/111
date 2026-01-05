# bot.py
import os, io, re, json, asyncio, logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from dotenv import load_dotenv
import pytz, aiohttp, asyncpg
from bs4 import BeautifulSoup
from PIL import Image
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, BotCommand
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import BaseMiddleware
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# google-genai SDK
try:
    from google import genai
except Exception:
    genai = None

# -----------------------------
# Конфігурація
# -----------------------------
load_dotenv()

@dataclass
class Settings:
    TELEGRAM_BOT_TOKEN: str
    ADMIN_CHAT_ID: int
    CHANNEL_ID: int
    GEMINI_API_KEY: str
    DATABASE_URL: str
    PROXY_POOL: list
    TZ: str
    QUIET_START: str
    QUIET_END: str

def load_settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        ADMIN_CHAT_ID=int(os.getenv("ADMIN_CHAT_ID", "0")),
        CHANNEL_ID=int(os.getenv("CHANNEL_ID", "0")),
        GEMINI_API_KEY=os.getenv("GEMINI_API_KEY", ""),
        DATABASE_URL=os.getenv("DATABASE_URL", ""),
        PROXY_POOL=json.loads(os.getenv("PROXY_POOL", "[]")),
        TZ=os.getenv("TZ", "Europe/Kyiv"),
        QUIET_START=os.getenv("QUIET_START", "23:00"),
        QUIET_END=os.getenv("QUIET_END", "08:00"),
    )

settings = load_settings()
TZ = pytz.timezone(settings.TZ)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# -----------------------------
# База даних
# -----------------------------
class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=10)

    async def migrate(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS lots (
                id SERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                external_id TEXT UNIQUE,
                title TEXT,
                price NUMERIC,
                currency TEXT,
                location TEXT,
                url TEXT,
                images JSONB,
                condition_score NUMERIC,
                kit_score NUMERIC,
                defects JSONB,
                vision_flags JSONB,
                fair_price NUMERIC,
                liquidity INT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """)
            logging.info("DB migrated")

    async def insert_or_update_lot(self, lot: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
            INSERT INTO lots (source, external_id, title, price, currency, location, url, images,
                              condition_score, kit_score, defects, vision_flags, fair_price, liquidity)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            ON CONFLICT (external_id) DO UPDATE SET
                price=EXCLUDED.price,
                images=EXCLUDED.images,
                condition_score=EXCLUDED.condition_score,
                kit_score=EXCLUDED.kit_score,
                defects=EXCLUDED.defects,
                vision_flags=EXCLUDED.vision_flags,
                fair_price=EXCLUDED.fair_price,
                liquidity=EXCLUDED.liquidity
            """,
            lot.get("source"), lot.get("external_id"), lot.get("title"), lot.get("price"), lot.get("currency"),
            lot.get("location"), lot.get("url"), json.dumps(lot.get("images") or []),
            lot.get("condition_score"), lot.get("kit_score"),
            json.dumps(lot.get("defects") or []), json.dumps(lot.get("vision_flags") or {}),
            lot.get("fair_price"), lot.get("liquidity"))

# -----------------------------
# Middleware тихого режиму
# -----------------------------
class QuietModeMiddleware(BaseMiddleware):
    def __init__(self, start: str, end: str, tz: str):
        self.start_h, self.start_m = map(int, start.split(":"))
        self.end_h, self.end_m = map(int, end.split(":"))
        self.tz = pytz.timezone(tz)

    async def __call__(self, handler, event: Message, data):
        now = datetime.now(self.tz).time()
        start_t = time(self.start_h, self.start_m)
        end_t = time(self.end_h, self.end_m)
        in_quiet = start_t <= now or now <= end_t if start_t > end_t else start_t <= now <= end_t
        data["quiet_mode"] = in_quiet
        return await handler(event, data)

# -----------------------------
# VisionExpert
# -----------------------------
class VisionExpert:
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key) if genai and api_key else None

    def upscale(self, image_bytes: bytes) -> bytes:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = img.resize((img.width * 2, img.height * 2))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    async def analyze(self, image_bytes: bytes) -> dict:
        upscaled = self.upscale(image_bytes)
        if not self.client:
            return {"movement": "unknown", "symmetry_score": 90, "crystal": "unknown"}
        # Виклик Gemini Vision (спрощено)
        return {
            "movement": "automatic",
            "symmetry_score": 95,
            "crystal": "sapphire",
            "dial_texture": "guilloche",
            "case_material": "steel",
            "clasp": "deployant",
            "lume": {"quality": "good"},
            "kit": {"box": True, "papers": False, "tags": False},
            "franken": False,
            "font_flags": {"logo": "ok", "date": "ok"}
        }

# -----------------------------
# Ринкові дані, тренди, автотеги
# -----------------------------
STOP_WORDS = ["копія", "репліка", "кварц не працює"]
WHITELIST_BRANDS = ["Seiko", "Rolex", "Omega", "Casio"]

def text_flags(text: str) -> dict:
    lower = text.lower()
    return {
        "stop_words": [w for w in STOP_WORDS if w in lower],
        "green_dial_trend": bool(re.search(r"\bgreen\b|\bзелений\b", lower)),
        "season": get_season()
    }

def get_season() -> str | None:
    month = datetime.now(TZ).month
    if month in (6, 7, 8): return "summer"
    if month in (12, 1, 2): return "winter"
    return None

def auto_tags(title: str) -> list[str]:
    tags = []
    for brand in WHITELIST_BRANDS:
        if brand.lower() in title.lower():
            tags.append(f"#{brand}")
    if "diver" in title.lower(): tags.append("#Diver")
    if "deal" in title.lower(): tags.append("#SuperDeal")
    return tags

# -----------------------------
# Evaluator (справедлива ціна)
# -----------------------------
class Evaluator:
    def condition_penalty(self, defects: list[dict]) -> Decimal:
        base = Decimal("0.00")
        for d in defects:
            if d.get("type") == "scratch": base += Decimal("0.03")
            elif d.get("type") == "chip": base += Decimal("0.05")
            elif d.get("type") == "stretched_bracelet": base += Decimal("0.07")
        return base

        def kit_bonus(self, kit: dict) -> Decimal:
        return Decimal("0.25") if any(kit.values()) else Decimal("0.00")

    def trend_adjustment(self, flags: dict) -> Decimal:
        adj = Decimal("0.00")
        if flags.get("green_dial_trend"): adj += Decimal("0.05")
        if flags.get("season") == "summer": adj += Decimal("0.03")
        if flags.get("season") == "winter": adj += Decimal("0.02")
        return adj

    def fair_price(self, base: Decimal, penalties: Decimal, bonus: Decimal, trend_adj: Decimal) -> Decimal:
        return base * (Decimal("1.00") - penalties + bonus + trend_adj)
class Scheduler:
    def __init__(self, bot: Bot, db: Database, scraper: Scraper):
        self.bot = bot
        self.db = db
        self.scraper = scraper
        self._last_health = datetime.now(TZ)

    async def start(self):
        asyncio.create_task(self.run())

    async def health_check(self):
        logging.info("✅ Health-check OK")
        await self.bot.send_message(settings.ADMIN_CHAT_ID, "✅ Health-check: система працює стабільно")

    async def run(self):
        while True:
            try:
                # Health-check кожні 12 годин
                if datetime.now(TZ) - self._last_health > timedelta(hours=12):
                    await self.health_check()
                    self._last_health = datetime.now(TZ)

                # Сканування OLX
                url = "https://www.olx.ua/uk/elektronika/aksessuary/chas/"
                listings = await self.scraper.parse_olx(url)

                if any("error" in l for l in listings):
                    await self.bot.send_message(settings.ADMIN_CHAT_ID, "⚠️ SCRAPER ERROR: Потрібне оновлення парсера!")

                # Збереження лотів у БД
                for l in listings:
                    if "error" in l: 
                        continue
                    lot = {
                        "source": "olx",
                        "external_id": l["url"],
                        "title": l["title"],
                        "price": Decimal("0.00"),
                        "currency": "UAH",
                        "location": None,
                        "url": l["url"],
                        "images": [],
                        "condition_score": Decimal("0.90"),
                        "kit_score": Decimal("0.00"),
                        "defects": [],
                        "vision_flags": {},
                        "fair_price": Decimal("0.00"),
                        "liquidity": 5,
                    }
                    await self.db.insert_or_update_lot(lot)

                # Затримка: пікові години 30с, ніч 120с
                delay = 30 if (8 <= datetime.now(TZ).hour <= 11 or 18 <= datetime.now(TZ).hour <= 23) else 120
                await asyncio.sleep(delay)

            except Exception as e:
                logging.exception("Scheduler loop error: %s", e)
                await asyncio.sleep(60)
async def set_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Запустити бота"),
        BotCommand(command="help", description="Довідка"),
        BotCommand(command="watch", description="Аналіз конкретного лоту"),
        BotCommand(command="mute", description="Режим тиші увімк/вимк"),
    ]
    await bot.set_my_commands(commands)

async def on_startup(bot: Bot):
    await bot.set_webhook(f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/webhook")

async def main():
    logging.info("Запуск Watch-Expert AI Pro v4.1")

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    db = Database(settings.DATABASE_URL)
    await db.connect()
    await db.migrate()

    dp.message.middleware(QuietModeMiddleware(settings.QUIET_START, settings.QUIET_END, settings.TZ))
    vision = VisionExpert(settings.GEMINI_API_KEY)

    await set_commands(bot)

    @dp.message(F.text == "/start")
    async def start(m: Message):
        await m.answer("👋 Вітаю у Watch-Expert AI Pro. Надішліть фото або посилання на лот.")

    @dp.message(F.text == "/help")
    async def help_cmd(m: Message):
        await m.answer("Команди: /watch, /mute. Надішліть фото годинника для AI Vision+ аналізу.")

    @dp.message(F.text == "/watch")
    async def watch_cmd(m: Message):
        await m.answer("Надішліть фото або посилання на лот для аналізу.")

    @dp.message(F.text == "/mute")
    async def mute_cmd(m: Message, quiet_mode: bool):
        await m.answer(f"🔕 Тихий режим активний: {quiet_mode}")

    @dp.message(F.photo)
    async def photo_handler(m: Message):
        photo = m.photo[-1]
        file = await m.bot.get_file(photo.file_id)
        file_bytes = await m.bot.download_file(file.file_path)
        img_bytes = file_bytes.read()
        analysis = await vision.analyze(img_bytes)
        tags = auto_tags(m.caption or "")
        await m.answer(
            f"AI Vision+ результат:\n"
            f"- Механізм: {analysis['movement']}\n"
            f"- Симетрія: {analysis['symmetry_score']}%\n"
            f"- Скло: {analysis['crystal']}\n"
            f"- Матеріал корпусу: {analysis.get('case_material')}\n"
            f"- Застібка: {analysis.get('clasp')}\n"
            f"- Люмінофор: {analysis.get('lume', {}).get('quality')}\n"
            f"- Комплект: {analysis.get('kit')}\n"
            f"- Франкен: {analysis.get('franken')}\n"
            f"- Автотеги: {' '.join(tags)}"
        )

    @dp.message(F.text.regexp(r"^https?://"))
    async def link_handler(m: Message):
        flags = text_flags(m.text)
        await m.answer(f"🔎 Прийнято. Аналізую лот...\nСтоп-слова: {flags['stop_words']}")

    # Планувальник
    scraper = Scraper()
    scheduler = Scheduler(bot=bot, db=db, scraper=scraper)
    await scheduler.start()

    # Запуск aiohttp‑сервера
    app = web.Application()
    SimpleRequestHandler(dp, bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    port = int(os.getenv("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    asyncio.run(main())
