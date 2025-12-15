import asyncio
import logging
import io
import os
import requests
import hashlib 
import asyncpg # Додано для роботи з базою даних Neon
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bs4 import BeautifulSoup
import google.generativeai as genai
from PIL import Image
from dotenv import load_dotenv

# --- 1. ЗАВАНТАЖЕННЯ ЗМІННИХ СЕРЕДОВИЩА ---
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
DATABASE_URL = os.getenv("DATABASE_URL") # Додано змінну для БД

try:
    ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID"))
except (TypeError, ValueError):
    logging.error("ADMIN_CHAT_ID не знайдено або це не число!")
    ADMIN_ID = 0

# --- 2. НАЛАШТУВАННЯ GEMINI ---
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    logging.error("GEMINI_API_KEY не знайдено в .env")

# --- 3. НАЛАШТУВАННЯ БОТА ТА БД ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Глобальна змінна для пулу з'єднань БД
db_pool = None

# ----------------------------------------------------
# --- ФУНКЦІЇ БАЗИ ДАНИХ (DB) ---
# ----------------------------------------------------

async def create_tables():
    """Створює таблиці, якщо вони не існують."""
    global db_pool
    if not db_pool:
        return False
    
    # Використовуйте вашу фактичну SQL-схему. Це приклад.
    SQL_SCHEMA = """
    CREATE TABLE IF NOT EXISTS processed_photos (
        photo_hash VARCHAR(64) PRIMARY KEY,
        timestamp TIMESTAMPTZ DEFAULT NOW(),
        search_query TEXT
    );
    """
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(SQL_SCHEMA)
        return True
    except Exception as e:
        logging.error(f"Помилка створення таблиць: {e}")
        return False

async def init_db_pool():
    """Ініціалізує пул з'єднань з базою даних."""
    global db_pool
    if not DATABASE_URL:
        logging.error("DATABASE_URL не знайдено. База даних буде недоступна.")
        return False

    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL, 
            min_size=1, 
            max_size=10,
            timeout=30 # Додано таймаут
        )
        if await create_tables():
            logging.info("Neon DB pool and tables initialized successfully.")
            return True
        return False
    except Exception as e:
        logging.error(f"Помилка підключення до Neon DB: {e}")
        return False

async def close_db_pool():
    """Закриває пул з'єднань при зупинці бота."""
    global db_pool
    if db_pool:
        logging.info("Closing Neon DB pool...")
        await db_pool.close()
        logging.info("Neon DB pool closed.")


# ----------------------------------------------------
# --- ФУНКЦІЇ БОТА ---
# ----------------------------------------------------

# --- ФУНКЦІЯ: OLX PARSER ---
def search_olx(query):
    # ... (Ваша функція search_olx залишається без змін) ...
    """Шукає товар на OLX за запитом і повертає список словників."""
    search_query = query.replace(" ", "-")
    url = f"https://www.olx.ua/uk/list/q-{search_query}/"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        listings = []
        
        # Пошук карток (може потребувати оновлення селекторів, якщо OLX змінить дизайн)
        cards = soup.find_all('div', {'data-cy': 'l-card'})

        for card in cards[:5]:
            try:
                title_tag = card.find('h6')
                price_tag = card.find('p', {'data-testid': 'ad-price'})
                link_tag = card.find('a', href=True)

                if title_tag and link_tag:
                    title = title_tag.text.strip()
                    price = price_tag.text.strip() if price_tag else "Ціна не вказана"
                    link = link_tag['href']
                    if not link.startswith("http"):
                        link = f"https://www.olx.ua{link}"

                    listings.append({"title": title, "price": price, "link": link})
            except Exception:
                continue
        return listings
    except Exception as e:
        logging.error(f"Помилка парсингу OLX: {e}")
        return []

# --- ФУНКЦІЯ: GEMINI VISION ---
async def identify_image(photo_bytes):
    # ... (Ваша функція identify_image залишається без змін) ...
    """Розпізнає товар на фото."""
    try:
        image = Image.open(io.BytesIO(photo_bytes))
        prompt = (
            "Ти помічник для пошуку товарів. Подивись на це фото. "
            "Що саме тут зображено? Напиши ТІЛЬКИ назву предмета для пошукового запиту "
            "на сайті оголошень (OLX). Мова: Українська. "
            "Приклад відповіді: 'Відеокарта RTX 3060', 'Червоний диван', 'Iphone 13'. "
            "Нічого зайвого, тільки 2-4 ключових слова."
        )
        response = await asyncio.to_thread(model.generate_content, [prompt, image])
        return response.text.strip()
    except Exception as e:
        logging.error(f"Gemini Error: {e}")
        return None

# --- ОБРОБНИК ФОТО ---
@dp.message(F.photo)
async def handle_photo(message: types.Message):
    # ... (Ваша функція handle_photo залишається без змін) ...
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Ви не адміністратор.")
        return

    status_msg = await message.answer("👾 **NEON BASE:** Сканую об'єкт...", parse_mode=ParseMode.MARKDOWN)

    try:
        # Завантаження фото в пам'ять
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        photo_bytes = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=photo_bytes)
        photo_data = photo_bytes.getvalue()
        
        # Перевірка на дублікат (якщо DB підключена)
        # photo_hash = hashlib.sha256(photo_data).hexdigest()
        # if db_pool:
        #    async with db_pool.acquire() as conn:
        #        exists = await conn.fetchval("SELECT photo_hash FROM processed_photos WHERE photo_hash = $1", photo_hash)
        #        if exists:
        #            await status_msg.edit_text("⚠️ **Помилка:** Це фото вже було оброблено.")
        #            return


        # Розпізнавання через AI
        search_query = await identify_image(photo_data)
        if not search_query:
            await status_msg.edit_text("❌ Gemini не зміг розпізнати об'єкт.")
            return

        await status_msg.edit_text(f"👁 **Розпізнано:** `{search_query}`\n📡 Підключаюсь до OLX...")

        # Пошук на OLX
        items = await asyncio.to_thread(search_olx, search_query)
        if not items:
            await status_msg.edit_text(f"⚠️ На OLX нічого не знайдено за запитом: **{search_query}**")
            return

        # Формування посту
        caption = f"💠 **RENDER FINDER**\n\n"
        caption += f"🔎 Лот: **{search_query}**\n"
        caption += f"➖➖➖➖➖➖➖➖➖➖\n"
        for i, item in enumerate(items, 1):
            caption += f"{i}. [{item['title']}]({item['link']})\n🏷 **{item['price']}**\n\n"
        caption += f"➖➖➖➖➖➖➖➖➖➖\n#render #neon #finder"

        # Публікація в канал
        await bot.send_photo(chat_id=CHANNEL_ID, photo=photo.file_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
        await status_msg.edit_text(f"✅ **Опубліковано!**")

    except Exception as e:
        logging.error(f"Critical Error: {e}")
        await status_msg.edit_text("❌ Сталася критична помилка. Перевірте логи.")


# ----------------------------------------------------
# --- ФУНКЦІЇ ЗАПУСКУ/ЗУПИНКИ ---
# ----------------------------------------------------

# --- СТАРТОВА КОМАНДА ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Привіт, Адмін! Кидай фото, я готовий працювати.")
    else:
        await message.answer("Я приватний бот.")


# --- ФУНКЦІЯ ПРИ ЗАПУСКУ ---
async def on_startup(bot: Bot):
    """Ця функція спрацьовує один раз при старті бота, ініціалізує БД та надсилає вітання."""
    
    # --- КРОК 1: ФІКС КОНФЛІКТУ ---
    try:
        # ЦЕ ПОВИННО БУТИ ПЕРШИМ: Скидаємо всі активні Polling/Webhook сесії для уникнення TelegramConflictError
        await bot.delete_webhook(drop_pending_updates=True) 
        logging.info("Old Telegram sessions cleared. Conflict error fixed.")
    except Exception as e:
        logging.error(f"Не вдалося скинути вебхуки: {e}")
        # Продовжуємо роботу, навіть якщо скидання не вдалося
        
    # --- КРОК 2: ІНІЦІАЛІЗАЦІЯ БД ---
    db_connected = await init_db_pool()
    db_status_text = "✅ Neon DB Online" if db_connected else "❌ Neon DB Offline (Кешування недоступне)"
    
    # --- КРОК 3: ВІТАННЯ ---
    channel_startup_message = (
        "🤖 **NEON RENDER FINDER ONLINE**\n"
        f"Системи завантажені: Gemini Vision, OLX Parser.\n"
        f"Статус БД: **{db_status_text}**\n\n"
        "Очікую нові лоти від Адміністратора. ✨"
    )

    try:
        chat_id = int(CHANNEL_ID) if str(CHANNEL_ID).startswith("-100") else CHANNEL_ID
        
        await bot.send_message(
            chat_id=chat_id, 
            text=channel_startup_message,
            parse_mode=ParseMode.MARKDOWN
        )
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"✅ **Система запущена.** {db_status_text}",
            parse_mode=ParseMode.MARKDOWN
        )
        logging.info("Startup message sent to channel and admin.")
    except Exception as e:
        logging.error(f"Не вдалося відправити повідомлення про запуск: {e}")


# --- MAIN ---
async def main():
    # Реєструємо функцію запуску (виконується перед start_polling)
    dp.startup.register(on_startup)
    
    # Реєструємо функцію зупинки (виконується при зупинці/перезапуску)
    dp.shutdown.register(close_db_pool) # Для коректного закриття DB
    
    # Починаємо слухати оновлення
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())