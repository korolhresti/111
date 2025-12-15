import asyncio
import logging
import io
import os
import requests
import hashlib 
import asyncpg 

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
DATABASE_URL = os.getenv("DATABASE_URL") 

try:
    ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID"))
except (TypeError, ValueError):
    logging.error("ADMIN_CHAT_ID не знайдено або це не число!")
    ADMIN_ID = 0

# --- ФУНКЦІЯ ПРИ ЗАПУСКУ ---
async def on_startup(bot: Bot):
    """Ця функція спрацьовує один раз при старті бота."""
    try:
        # ДОДАЙТЕ ЦЕЙ РЯДОК: Скидає всі активні Polling/Webhook сесії
        await bot.delete_webhook(drop_pending_updates=True) 
        
        # Конвертуємо ID каналу в int, якщо він в форматі "-100..."
        chat_id = int(CHANNEL_ID) if str(CHANNEL_ID).startswith("-100") else CHANNEL_ID
        await bot.send_message(chat_id, "🤖 **NEON BOT ONLINE**\\nПривіт! Я готовий до роботи.")
        logging.info("Startup message sent to channel and admin.")
    except Exception as e:
        # ... (ваш існуючий код обробки помилок)
        logging.error(f"Не вдалося відправити привіт: {e}") 



# --- 2. НАЛАШТУВАННЯ GEMINI ТА БОТА ---
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    logging.error("GEMINI_API_KEY не знайдено в .env")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
db_pool = None 
processing_queue = False # Запобіжник для уникнення одночасного запуску черги

# --- 3. СТВОРЕННЯ FSM-СТАНІВ ---
class SearchStates(StatesGroup):
    """Стани для управління процесом пошуку."""
    awaiting_query_confirmation = State()
    awaiting_new_query = State()


# --- ФУНКЦІЯ: ІНІЦІАЛІЗАЦІЯ БАЗИ ДАНИХ ---
async def init_db_pool():
    """Створює пул з'єднань, ініціалізує таблиці кешу та черги."""
    global db_pool
    if not DATABASE_URL:
        logging.warning("DATABASE_URL не знайдено. Кешування та черга не працюватимуть.")
        return False
        
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        async with db_pool.acquire() as conn:
            # Таблиця для кешування (щоб не постити повторно)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS posted_items (
                    id SERIAL PRIMARY KEY,
                    photo_hash TEXT UNIQUE NOT NULL,
                    search_query TEXT,
                    posted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Таблиця для черги (відкладений пошук)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS search_queue (
                    id SERIAL PRIMARY KEY,
                    file_id TEXT NOT NULL,
                    search_query TEXT NOT NULL,
                    added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
        logging.info("Neon DB pool and tables initialized successfully.")
        return True
    except Exception as e:
        logging.error(f"Помилка підключення або ініціалізації Neon DB: {e}")
        db_pool = None
        return False

# --- ФУНКЦІЯ: ПЕРЕВІРКА ТА ЗБЕРІГАННЯ КЕШУ ---
async def check_and_save_cache(photo_data, search_query, commit_if_new):
    """
    Перевіряє кеш. Якщо commit_if_new=True, зберігає запис.
    Повертає True, якщо знайдено в кеші (дублікат).
    """
    if not db_pool:
        return False, None 

    photo_hash = hashlib.sha256(photo_data).hexdigest()

    async with db_pool.acquire() as conn:
        # 1. Перевірка наявності в кеші
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM posted_items WHERE photo_hash = $1)",
            photo_hash
        )

        if exists:
            return True, photo_hash # Дублікат знайдено
        
        if commit_if_new:
            # 2. Зберігання нового лота (після успішної публікації)
            await conn.execute(
                "INSERT INTO posted_items (photo_hash, search_query) VALUES ($1, $2)",
                photo_hash, search_query
            )
            return False, photo_hash
            
        return False, photo_hash # Новий лот, але не зберігаємо

# --- ФУНКЦІЯ: OLX PARSER (залишається без змін) ---
def search_olx(query):
    """Шукає товар на OLX за запитом і повертає список словників."""
    search_query = query.replace(" ", "-")
    url = f"https://www.olx.ua/uk/list/q-{search_query}/"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            logging.warning(f"OLX returned status code {response.status_code} for query: {query}")
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        listings = []
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

# --- ФУНКЦІЯ: GEMINI VISION (залишається без змін) ---
async def identify_image(photo_bytes):
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

# --- ГЕНЕРАЦІЯ КНОПОК ПІДТВЕРДЖЕННЯ ---
def get_confirmation_keyboard():
    """Створює клавіатуру для підтвердження, зміни запиту або додавання в чергу."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Підтвердити і шукати", callback_data="query_confirm")
    builder.button(text="✏️ Змінити запит", callback_data="query_edit")
    builder.button(text="➕ Додати в чергу", callback_data="query_queue")
    builder.adjust(2, 1) 
    return builder.as_markup()

# --- 4. ОСНОВНА ФУНКЦІЯ ПАРСИНГУ (уніфікована) ---
async def start_olx_parsing(admin_chat_id: int, search_query: str, photo_id: str, status_message_id: int = None):
    """Виконує повний цикл: завантаження, кеш-перевірка, парсинг, публікація, кеш-збереження."""
    
    # 1. Завантаження фото за file_id (потрібно для хешування)
    try:
        file_info = await bot.get_file(photo_id)
        photo_bytes = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=photo_bytes)
        photo_data = photo_bytes.getvalue()
    except Exception as e:
        logging.error(f"Помилка завантаження фото {photo_id}: {e}")
        if status_message_id:
             await bot.edit_message_text(chat_id=admin_chat_id, message_id=status_message_id, text="❌ Помилка завантаження фото.")
        return False
        
    # 2. Визначаємо, куди відправляти оновлення статусу
    if status_message_id:
        update_status = lambda text: bot.edit_message_text(text=text, chat_id=admin_chat_id, message_id=status_message_id, parse_mode=ParseMode.MARKDOWN)
    else:
        update_status = lambda text: bot.send_message(admin_chat_id, text=f"📥 **ЧЕРГА** | {text}", parse_mode=ParseMode.MARKDOWN)

    # 3. Кеш-перевірка (до парсингу OLX)
    is_duplicate, photo_hash = await check_and_save_cache(photo_data, search_query, commit_if_new=False) 
    
    if is_duplicate:
        await update_status(f"⚠️ **Дублікат знайдено!** Цей лот (Hash: `{photo_hash[:8]}...`) вже був опублікований.")
        return False

    await update_status(f"✅ **Кеш:** Новий лот.\n📡 Підключаюсь до OLX...")
    
    # 4. Пошук на OLX
    items = await asyncio.to_thread(search_olx, search_query)
    
    if not items:
        await update_status(f"⚠️ На OLX нічого не знайдено за запитом: **{search_query}**")
        return False

    # 5. Формування посту
    caption = f"💠 **RENDER FINDER**\n\n"
    caption += f"🔎 Лот: **{search_query}**\n"
    caption += f"➖➖➖➖➖➖➖➖➖➖\n"
    for i, item in enumerate(items, 1):
        caption += f"{i}. [{item['title']}]({item['link']})\n🏷 **{item['price']}**\n\n"
    caption += f"➖➖➖➖➖➖➖➖➖➖\n#render #neon #finder"

    # 6. Публікація в канал
    await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
    
    # 7. Збереження до кешу (після успішної публікації)
    await check_and_save_cache(photo_data, search_query, commit_if_new=True)

    await update_status(f"✅ **Опубліковано!**")
    return True

# --- 5. ОБРОБНИКИ ЧЕРГИ ТА FSM ---

# ОБРОБНИК ФОТО (Початок процесу)
@dp.message(F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Ви не адміністратор.")
        return

    status_msg = await message.answer("👾 **NEON BASE:** Сканую об'єкт...", parse_mode=ParseMode.MARKDOWN)

    try:
        # 1. Завантаження фото лише для розпізнавання
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        photo_bytes = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=photo_bytes)
        photo_data = photo_bytes.getvalue()

        # 2. Розпізнавання через AI
        search_query = await identify_image(photo_data)
        
        if not search_query:
            await status_msg.edit_text("❌ Gemini не зміг розпізнати об'єкт.")
            return

        # 3. Зберігання ID фото та запиту в FSM
        await state.update_data(
            photo_id=photo.file_id, 
            search_query=search_query, 
            status_message_id=status_msg.message_id
        )

        await status_msg.edit_text(
            f"👁 **Розпізнано:** `{search_query}`\n\n"
            "Підтвердіть дію:",
            reply_markup=get_confirmation_keyboard()
        )
        await state.set_state(SearchStates.awaiting_query_confirmation)

    except Exception as e:
        logging.error(f"Critical Error in handle_photo: {e}")
        await status_msg.edit_text("❌ Сталася критична помилка. Перевірте логи.")
        await state.clear()


# ОБРОБНИК: Кнопка "Додати в чергу"
@dp.callback_query(F.data == "query_queue", SearchStates.awaiting_query_confirmation)
async def callback_query_queue(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    search_query = data.get('search_query')
    photo_id = data.get('photo_id')

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO search_queue (file_id, search_query) VALUES ($1, $2)",
                    photo_id, search_query
                )
            await callback.message.edit_text(
                f"📥 **Додано в чергу!**\n"
                f"Запит `{search_query}` збережено.\n"
                "Ви можете запустити пошук командою `/run_queue`."
            )
        except Exception as e:
            await callback.message.edit_text(f"❌ Помилка при збереженні в чергу: {e}")
    else:
        await callback.message.edit_text("❌ База даних недоступна. Неможливо додати в чергу.")
        
    await state.clear()
    await callback.answer()


# ОБРОБНИК: Кнопка "Змінити запит"
@dp.callback_query(F.data == "query_edit", SearchStates.awaiting_query_confirmation)
async def callback_query_edit(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✏️ **Введіть новий пошуковий запит** (наприклад, 'Відеокарта 3060 12gb б/у'):"
    )
    await state.set_state(SearchStates.awaiting_new_query)
    await callback.answer()

# ОБРОБНИК: Введення нового запиту
@dp.message(SearchStates.awaiting_new_query)
async def handle_new_query(message: types.Message, state: FSMContext):
    if not message.text or len(message.text) < 3:
        await message.answer("Будь ласка, введіть коректний запит (мінімум 3 символи).")
        return
        
    new_query = message.text.strip()
    data = await state.get_data()
    
    await message.answer(
        f"✅ **Новий запит збережено:** `{new_query}`. Починаю пошук...",
        reply_markup=types.ReplyKeyboardRemove()
    )
    # Запускаємо парсинг з новим запитом
    await start_olx_parsing(
        admin_chat_id=message.chat.id, 
        search_query=new_query, 
        photo_id=data.get('photo_id'),
        status_message_id=data.get('status_message_id')
    )
    await state.clear()


# ОБРОБНИК: Кнопка "Підтвердити"
@dp.callback_query(F.data == "query_confirm", SearchStates.awaiting_query_confirmation)
async def callback_query_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    await callback.message.edit_text("✅ Запит підтверджено. Починаю пошук...")
    
    # Запускаємо парсинг
    await start_olx_parsing(
        admin_chat_id=callback.message.chat.id, 
        search_query=data.get('search_query'), 
        photo_id=data.get('photo_id'),
        status_message_id=data.get('status_message_id')
    )
    await state.clear()
    await callback.answer()

# --- 6. КОМАНДА ЗАПУСКУ ЧЕРГИ ---

# Головна функція, що обробляє чергу
async def process_queue(admin_chat_id: int):
    global processing_queue
    if not db_pool or processing_queue:
        await bot.send_message(admin_chat_id, "❌ **Черга вже запущена** або база даних недоступна.")
        return

    processing_queue = True
    status_msg = await bot.send_message(admin_chat_id, "⚙️ **Обробка черги:** Починаю роботу...")
    processed_count = 0
    
    try:
        async with db_pool.acquire() as conn:
            queue_items = await conn.fetch("SELECT id, file_id, search_query FROM search_queue ORDER BY added_at ASC")
            
            if not queue_items:
                await status_msg.edit_text("✅ **Черга порожня.** Роботу завершено.")
                return

            await status_msg.edit_text(f"⚙️ **Обробка черги:** Знайдено {len(queue_items)} лотів. Починаю...")

            for item in queue_items:
                await status_msg.edit_text(
                    f"⚙️ **Обробка черги:** Лот {processed_count + 1} з {len(queue_items)}.\n"
                    f"Запит: `{item['search_query']}`. Шукаю..."
                )
                
                is_success = await start_olx_parsing(
                    admin_chat_id=admin_chat_id, 
                    search_query=item['search_query'], 
                    photo_id=item['file_id']
                )

                if is_success:
                    # Видаляємо лот з черги лише у разі успішної публікації
                    await conn.execute("DELETE FROM search_queue WHERE id = $1", item['id'])
                    processed_count += 1
                
                await asyncio.sleep(2) # Затримка для зменшення навантаження на API

        await status_msg.edit_text(f"✅ **Обробка черги завершена.** Опубліковано: {processed_count} лотів.")

    except Exception as e:
        logging.error(f"Critical Error in process_queue: {e}")
        await status_msg.edit_text(f"❌ Критична помилка під час обробки черги: {e}")
    finally:
        processing_queue = False


@dp.message(F.text.lower() == '/run_queue')
async def cmd_run_queue(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Ви не адміністратор.")
        return
    
    # Запускаємо чергу як окрему асинхронну задачу
    asyncio.create_task(process_queue(message.chat.id))

# --- ОБРОБНИКИ СТАНУ СИСТЕМИ ---
@dp.message(CommandStart(magic=F.args == 'db_status'))
@dp.message(F.text.lower() == '/db_status')
async def cmd_db_status(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Ви не адміністратор.")
        return

    if not db_pool:
        await message.answer("⚠️ База даних Neon не підключена або недоступна.")
        return
    
    try:
        async with db_pool.acquire() as conn:
            # Отримуємо кількість записів
            count_posted = await conn.fetchval("SELECT COUNT(*) FROM posted_items")
            count_queue = await conn.fetchval("SELECT COUNT(*) FROM search_queue")
            
            status_message = (
                "📊 **СТАТУС NEON DB**\n"
                "➖➖➖➖➖➖➖➖➖➖\n"
                f"Статус: **✅ Підключено**\n"
                f"Кількість кешованих лотів: **{count_posted}**\n"
                f"Лотів у черзі: **{count_queue}**\n"
                "Кешування працює надійно."
            )
            await message.answer(status_message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await message.answer(f"❌ Помилка при запиті до бази даних: {e}")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Привіт, Адмін! Кидай фото, я готовий працювати.")
    else:
        await message.answer("Я приватний бот.")


# --- ФУНКЦІЯ ПРИ ЗАПУСКУ ---
async def on_startup(bot: Bot):
    """Ця функція спрацьовує один раз при старті бота, ініціалізує БД та надсилає вітання."""
    db_connected = await init_db_pool()
    
    db_status_text = "✅ Neon DB Online" if db_connected else "❌ Neon DB Offline (Кешування/Черга недоступні)"
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
            text=f"✅ **Система запущена.** {db_status_text}\nВикористовуйте `/db_status` та `/run_queue`.",
            parse_mode=ParseMode.MARKDOWN
        )
        logging.info("Startup message sent to channel and admin.")
    except Exception as e:
        logging.error(f"Не вдалося відправити привіт в канал або адміну: {e}")

# --- MAIN ---
async def main():
    dp.startup.register(on_startup)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())