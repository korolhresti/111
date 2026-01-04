import asyncio
import logging
import os
import sys
import json
from datetime import datetime, time
import pytz

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder

from google import genai
from PIL import Image
import asyncpg
import aiohttp
from dotenv import load_dotenv

# --- 1. КОНФІГУРАЦІЯ ---
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = os.getenv("ADMIN_CHAT_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")

KYIV_TZ = pytz.timezone("Europe/Kyiv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("WatchExpertBot")

# --- НОВИЙ GEMINI КЛІЄНТ ---
client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL = "gemini-1.5-flash-latest"

# --- 2. ІНІЦІАЛІЗАЦІЯ БОТА ---
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# --- 3. БАЗА ДАНИХ ---
async def create_db_pool():
    return await asyncpg.create_pool(DATABASE_URL)


async def init_db(pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS watches (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                image_file_id TEXT,

                brand TEXT,
                mechanism_type TEXT,
                glass_type TEXT,
                case_material TEXT,
                symmetry_score INT,
                is_frankenstein BOOLEAN,

                liquidity_score INT,
                price_estimate_usd NUMERIC,
                currency_rate NUMERIC,

                full_ai_report TEXT,
                tags TEXT[],

                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        logger.info("Database schema initialized.")


# --- 4. ДОПОМІЖНІ ФУНКЦІЇ ---
async def get_usd_rate():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.privatbank.ua/p24api/pubinfo?exchange&coursid=5") as resp:
                data = await resp.json(content_type=None)
                for row in data:
                    if row["ccy"] == "USD":
                        return float(row["sale"])
    except Exception as e:
        logger.error(f"Currency API failed: {e}")
        return 42.0


def is_quiet_mode():
    now = datetime.now(KYIV_TZ).time()
    return now >= time(23, 0) or now < time(8, 0)


async def send_admin_alert(text: str):
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, text)
        except:
            pass


# --- 5. PROMPT GPT ---
def build_expert_prompt(usd_rate):
    return f"""
You are Watch-Expert AI Pro v4.1. Perform deep multimodal evaluation.
Return ONLY VALID JSON.

Analysis includes:
- Mechanism type
- Symmetry
- Glass type
- Dial texture
- Case material
- Lume quality
- Clasp type
- Font authenticity
- Liquidity score (1–10)
- Price estimate

JSON format:
{{
  "brand": "",
  "mechanism_type": "",
  "glass_type": "",
  "case_material": "",
  "symmetry_score": 0,
  "is_frankenstein": false,
  "liquidity_score": 0,
  "price_usd_min": 0,
  "price_usd_max": 0,
  "tags": [],
  "human_readable_report_ua": ""
}}

USD rate: {usd_rate}
"""


# --- 6. HANDLERS ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 Вітаю, {message.from_user.full_name}!\n"
        "Надішліть фото годинника — і я проведу глибоку експертизу."
    )


@dp.message(F.photo)
async def handle_photo(message: types.Message, db_pool):
    user_id = message.from_user.id
    status_msg = await message.answer("⏳ Аналізую фото...")

    temp_path = f"img_{message.photo[-1].file_id}.jpg"

    try:
        # 1. Завантаження фото
        file_info = await bot.get_file(message.photo[-1].file_id)
        await bot.download_file(file_info.file_path, temp_path)

        with open(temp_path, "rb") as f:
            img_bytes = f.read()

        usd_rate = await get_usd_rate()
        prompt = build_expert_prompt(usd_rate)

        # --- 2. НОВИЙ GEMINI ЗАПИТ ---
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                {"mime_type": "image/jpeg", "data": img_bytes},
                prompt
            ]
        )

        raw_text = response.text.strip()
        raw_text = raw_text.replace("```json", "").replace("```", "").strip()

        try:
            data = json.loads(raw_text)
        except:
            logger.error("Invalid JSON from AI")
            data = {
                "human_readable_report_ua": raw_text,
                "price_usd_min": 0,
                "tags": []
            }

        # 3. ЗБЕРЕЖЕННЯ В БД
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO watches (
                    user_id, username, image_file_id,
                    brand, mechanism_type, glass_type, case_material,
                    symmetry_score, is_frankenstein, liquidity_score,
                    price_estimate_usd, currency_rate, full_ai_report, tags
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            """,
            user_id, message.from_user.username, message.photo[-1].file_id,
            data.get("brand"), data.get("mechanism_type"), data.get("glass_type"), data.get("case_material"),
            data.get("symmetry_score", 100), data.get("is_frankenstein", False),
            data.get("liquidity_score", 5), data.get("price_usd_min", 0),
            usd_rate, raw_text, data.get("tags", []))

        # 4. ВІДПОВІДЬ КОРИСТУВАЧУ
        report = data.get("human_readable_report_ua", "Звіт не сформовано.")

        if data.get("price_usd_min", 0):
            price_uah = int(data["price_usd_min"] * usd_rate)
            report += f"\n\n💵 Орієнтовна ціна: {price_uah:,}₴"

        kb = InlineKeyboardBuilder()
        kb.button(text="📢 В канал", callback_data="pub_wait")
        kb.button(text="🔍 Chrono24", url="https://www.chrono24.com/")

        await status_msg.edit_text(report, reply_markup=kb.as_markup())

    except Exception as e:
        logger.error(f"ERROR: {e}")
        await status_msg.edit_text("❌ Помилка при аналізі.")
        await send_admin_alert(f"AI Error: {e}")

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@dp.callback_query(F.data == "pub_wait")
async def cb_publish(callback: types.CallbackQuery):
    await callback.answer("Функція 'Публікація' скоро буде доступна.", show_alert=True)


# --- 7. MAIN ---
async def main():
    logger.info("Starting Watch-Expert AI Pro v4.1")

    try:
        db_pool = await create_db_pool()
        await init_db(db_pool)
        logger.info("Database OK")
    except Exception as e:
        logger.critical(f"DB FAIL: {e}")
        sys.exit(1)

    dp["db_pool"] = db_pool

    await bot.delete_webhook(drop_pending_updates=True)
    await send_admin_alert("🚀 Бот перезавантажено на сервері.")

    await dp.start_polling(bot, db_pool=db_pool)


if __name__ == "__main__":
    asyncio.run(main())
