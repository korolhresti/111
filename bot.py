import asyncio
import logging
import os
import sys
import json
import re
from datetime import datetime, time
import pytz

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

import google.generativeai as genai
from PIL import Image
import asyncpg
import aiohttp
from dotenv import load_dotenv

# --- 1. КОНФІГУРАЦІЯ ТА БЕЗПЕКА ---
load_dotenv()

# Отримуємо змінні (Render автоматично підставить їх з Environment Variables)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = os.getenv("ADMIN_CHAT_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")

# Часовий пояс для режиму "Тиша"
KYIV_TZ = pytz.timezone('Europe/Kyiv')

# Налаштування логування (важливо для Render logs)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("WatchExpertBot")

# Ініціалізація Gemini (Vision Model)
genai.configure(api_key=GEMINI_API_KEY)
# Використовуємо 'flash' для швидкості або 'pro' для максимальної деталізації (як в ТЗ)
model = genai.GenerativeModel('gemini-1.5-flash') 

# Ініціалізація бота
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- 2. БАЗА ДАНИХ (ВІДПОВІДАЄ WEB-DASHBOARD) ---
async def create_db_pool():
    return await asyncpg.create_pool(DATABASE_URL)

async def init_db(pool):
    async with pool.acquire() as conn:
        # Створюємо розширену таблицю для зберігання всіх параметрів ТЗ 4.1
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS watches (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                image_file_id TEXT,
                
                -- Deep Vision Data
                brand TEXT,
                mechanism_type TEXT, -- Quartz/Automatic
                glass_type TEXT,     -- Sapphire/Mineral
                case_material TEXT,
                symmetry_score INT,  -- % симетрії
                is_frankenstein BOOLEAN,
                
                -- Financials
                liquidity_score INT, -- 1-10
                price_estimate_usd NUMERIC,
                currency_rate NUMERIC,
                
                -- Meta
                full_ai_report TEXT,
                tags TEXT[],
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        logger.info("Database schema initialized.")

# --- 3. ДОПОМІЖНІ ФУНКЦІЇ (UX & MARKET DATA) ---

async def get_usd_rate():
    """Отримує актуальний курс USD/UAH (ПриватБанк API) для розрахунків."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get('https://api.privatbank.ua/p24api/pubinfo?exchange&coursid=5') as resp:
                data = await resp.json(content_type=None)
                for item in data:
                    if item['ccy'] == 'USD':
                        return float(item['sale'])
    except Exception as e:
        logger.error(f"Currency API failed: {e}. Using fallback rate.")
        return 42.0 # Fallback

def is_quiet_mode():
    """Перевіряє, чи зараз ніч (23:00 - 08:00) за Києвом."""
    now = datetime.now(KYIV_TZ).time()
    # Якщо час більше 23:00 АБО менше 08:00
    return now >= time(23, 0) or now < time(8, 0)

async def send_admin_alert(text: str):
    """Система самодіагностики: відправка критичних помилок адміну."""
    if ADMIN_ID:
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=f"🚨 <b>SYSTEM ALERT:</b>\n{text}")
        except Exception:
            pass

# --- 4. ЯДРО AI (DEEP VISION PROMPT) ---
def build_expert_prompt(usd_rate):
    """
    Промпт, що реалізує ТЗ 4.1. Змушує модель працювати як JSON-API.
    """
    return f"""
    You are "Watch-Expert AI Pro" (v4.1). Perform a deep visual analysis of this watch.
    Current USD/UAH Rate: {usd_rate}.

    ANALYZE THESE 10 POINTS (Deep Vision):
    1. Mechanism (Quartz vs Automatic via visual cues).
    2. Symmetry (Dial alignment, logo position).
    3. Glass (Sapphire vs Mineral reflections).
    4. Dial Texture (Guilloche vs Print).
    5. Lume quality.
    6. Case Material (Steel, Titanium, Plated).
    7. Clasp Type.
    8. Font Analysis (Consistency).
    9. Liquidity Score (1-10).
    10. Brand Trends.

    OUTPUT FORMAT:
    Return a strictly valid JSON object ONLY. No markdown, no "json" tags, just the raw object.
    Structure:
    {{
        "brand": "String (e.g. Seiko)",
        "mechanism_type": "String (Quartz/Automatic)",
        "glass_type": "String",
        "case_material": "String",
        "symmetry_score": Integer (0-100),
        "is_frankenstein": Boolean,
        "liquidity_score": Integer (1-10),
        "price_usd_min": Integer,
        "price_usd_max": Integer,
        "tags": ["#Tag1", "#Tag2"],
        "human_readable_report_ua": "A detailed analysis in Ukrainian with emojis using the structure from the requirements."
    }}
    """

# --- 5. ОБРОБНИКИ ПОДІЙ (HANDLERS) ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 Вітаю, {message.from_user.full_name}!\n"
        "Я — <b>Watch-Expert AI Pro v4.1</b>.\n\n"
        "📸 <b>Надішліть фото годинника</b> для:\n"
        "• Перевірки на 'Франкенштейна'\n"
        "• Визначення механізму та скла\n"
        "• Оцінки ринкової вартості (USD/UAH)"
    )

@dp.message(F.photo)
async def handle_photo(message: types.Message, db_pool):
    user_id = message.from_user.id
    
    # Режим тиші (UX)
    if is_quiet_mode():
        await message.answer("🌙 <i>Прийнято. Зараз режим 'Тиша', аналіз може зайняти трохи більше часу.</i>")

    status_msg = await message.answer("⏳ <b>AI Vision+ сканує зображення...</b>\n<i>(Перевірка симетрії, калібру, шрифтів)</i>")
    
    temp_path = f"temp_{message.photo[-1].file_id}.jpg"
    
    try:
        # 1. Завантаження фото
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        await bot.download_file(file_info.file_path, temp_path)
        
        # 2. Отримання даних
        usd_rate = await get_usd_rate()
        img = Image.open(temp_path)
        
        # 3. AI Аналіз
        prompt = build_expert_prompt(usd_rate)
        response = await asyncio.to_thread(model.generate_content, [prompt, img])
        
        # 4. Обробка відповіді (JSON Cleaning)
        raw_text = response.text.replace('```json', '').replace('```', '').strip()
        
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            # Fallback якщо модель повернула текст, а не JSON
            logger.error("AI returned invalid JSON. Using raw text.")
            data = {
                "human_readable_report_ua": raw_text,
                "liquidity_score": 0,
                "price_usd_min": 0,
                "tags": []
            }

        report_text = data.get("human_readable_report_ua", "Звіт не згенеровано.")
        
        # 5. Збереження в PostgreSQL
        async with db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO watches (
                    user_id, username, image_file_id, 
                    brand, mechanism_type, glass_type, case_material,
                    symmetry_score, is_frankenstein, liquidity_score,
                    price_estimate_usd, currency_rate, full_ai_report, tags
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ''', 
            user_id, message.from_user.username, photo.file_id,
            data.get("brand"), data.get("mechanism_type"), data.get("glass_type"), data.get("case_material"),
            data.get("symmetry_score", 100), data.get("is_frankenstein", False), data.get("liquidity_score", 5),
            data.get("price_usd_min", 0), usd_rate, report_text, data.get("tags", []))

        # 6. Відповідь користувачу
        # Додаємо ціну в гривнях, якщо є ціна в доларах
        price_usd = data.get("price_usd_min", 0)
        if price_usd:
            price_uah = int(price_usd * usd_rate)
            report_text += f"\n\n💱 <b>Курс:</b> {price_uah:,} UAH (по {usd_rate})"

        # Кнопки дій
        kb = InlineKeyboardBuilder()
        kb.button(text="📢 В канал", callback_data=f"pub_wait")
        kb.button(text="🔍 Chrono24", url="https://www.chrono24.com/")
        
        await status_msg.edit_text(report_text, reply_markup=kb.as_markup())

    except Exception as e:
        logger.error(f"Critical Error: {e}")
        await status_msg.edit_text("❌ Виникла помилка при обробці. Інформацію передано розробнику.")
        await send_admin_alert(f"User {user_id} error: {e}")
        
    finally:
        # Прибирання сміття
        if os.path.exists(temp_path):
            os.remove(temp_path)

@dp.callback_query(F.data == "pub_wait")
async def cb_publish(callback: types.CallbackQuery):
    await callback.answer("Функція 'Публікація' додасть лот в чергу модерації.", show_alert=True)

# --- 6. ЗАПУСК (MAIN) ---

async def main():
    # Health-check при старті
    logger.info("Starting Watch-Expert AI Pro v4.1...")
    
    # Підключення до БД
    try:
        db_pool = await create_db_pool()
        await init_db(db_pool)
        logger.info("✅ Database connected.")
    except Exception as e:
        logger.critical(f"❌ DB Connection failed: {e}")
        sys.exit(1)

    # Ін'єкція залежностей
    dp["db_pool"] = db_pool

    # Видалення вебхуків (обов'язково для polling)
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Сповіщення адміна про деплой
    await send_admin_alert("🚀 <b>DEPLOY SUCCESSFUL:</b> Бот перезавантажено на сервері.")
    
    # Старт
    await dp.start_polling(bot, db_pool=db_pool)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")