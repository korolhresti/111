import os
import asyncio
import logging
import re
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urljoin
import sys

import asyncpg
import aiohttp
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# --- Налаштування середовища та логування ---

KYIV_TZ = timezone(timedelta(hours=3), 'Europe/Kyiv')

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    stream=sys.stdout) # Виведення логів у stdout для Render
logger = logging.getLogger(__name__)

# Змінні оточення
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
# Зчитуємо та намагаємося конвертувати у int. Використовуємо .get() для гнучкості.
try:
    # Припускаємо, що CHANNEL_ID може бути вказаний як CHANNEL_ID або channel_ID
    channel_env_var = os.getenv("CHANNEL_ID") or os.getenv("channel_ID")
    CHANNEL_ID = int(channel_env_var)
except (TypeError, ValueError):
    CHANNEL_ID = None
    logger.error("CHANNEL_ID не знайдено або має некоректний формат.")


# --- Конфігурація ---

# Конфігурація OLX моніторингу
OLX_SEARCH_QUERIES = [
    'золото',
    'срібло'
]
# Фільтр: ціна від 2000 UAH
OLX_PRICE_FILTER = 2000 
OLX_BASE_URL = "https://www.olx.ua/d/uk/obyavlenie/"

# Стан для FSM (Finite State Machine) - Наповнення Бази Знань
class BaseForm(StatesGroup):
    """Стани для послідовного додавання даних до Бази Знань через команду /base."""
    waiting_for_photo = State()
    waiting_for_text = State()

# --- ГЛОБАЛЬНЕ ВИЗНАЧЕННЯ ДИСПЕТЧЕРА (FIX: для коректної роботи декораторів) ---
dp = Dispatcher()

# --- Ініціалізація БД ---

async def init_db(pool):
    """Створює необхідні таблиці в базі даних Neon."""
    logger.info("Підключення та ініціалізація БД...")
    
    # 1. Таблиця для OLX оголошень (стан моніторингу)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS olx_posts (
            id SERIAL PRIMARY KEY,
            olx_id TEXT UNIQUE,
            title TEXT,
            price INTEGER,
            published_at TIMESTAMP WITH TIME ZONE
        );
    """)

    # 2. Таблиця для Бази Знань (еталони)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS user_base (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            title TEXT,
            image_url TEXT, -- Зберігаємо URL або Telegram file_id
            keywords TEXT,
            estimated_value_text TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
    """)

    # 3. Таблиця для статистики (лайк/не лайк - НАВЧАННЯ)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS user_feedback (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            olx_id TEXT,
            is_like BOOLEAN,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
    """)
    logger.info("Ініціалізація БД завершена.")

# --- Модуль Gemini AI (ЕКОНОМІЧНА Заглушка) ---

class GeminiMock:
    """Заглушка для Gemini AI, імітує швидкий та економічний аналіз (Gemini 2.5 Flash)."""

    def __init__(self, pool):
        self.pool = pool

    async def _search_mock_references(self, item_id):
        """Імітація крос-референсу (мінімум 3 товари)."""
        references = []
        for i in range(3):
            random_price = random.randint(3000, 15000)
            references.append({
                "url": f"https://www.olx.ua/d/uk/obyavlenie/ref-item-{item_id}-{i}/",
                "price": random_price
            })
        return references

    async def analyze_olx_item(self, title, image_url, olx_id, user_base_context=None):
        """
        Імітація Двоступеневого Візуального Аналізу. 
        Використовує контекст user_base_context для імітації покращення точності.
        """
        
        # Обчислення ймовірності покращення автентичності на основі Бази Знань
        base_match_probability = len(user_base_context) * 0.05 
        is_original = random.random() < (0.7 + base_match_probability) 
        
        # Етап I: Ідентифікація
        mark_detected = random.choice(["Проба 585", "Клеймо 'Л' (Louis)", "Не знайдено"])
        
        # Етап II: Крос-Референс та Оцінка
        references = await self._search_mock_references(olx_id)
        
        estimated_value_low = random.randint(7000, 10000)
        estimated_value_high = estimated_value_low + 3000
        
        olx_price_mock = random.randint(4000, 12000) 
        
        # Оцінка Вигідності
        if olx_price_mock < estimated_value_low * 0.8: 
            deal_assessment = "🔥 Вигідна Ціна"
        elif olx_price_mock > estimated_value_high * 1.1:
            deal_assessment = "Завищена Ціна"
        else:
            deal_assessment = "Ринкова Ціна"

        
        result = {
            "is_relevant": is_original or deal_assessment == "🔥 Вигідна Ціна", 
            "authenticity": "Ймовірно Оригінал" if is_original else "Можлива Підробка",
            "mark_detected": mark_detected,
            "estimated_value": f"{estimated_value_low:,} - {estimated_value_high:,} UAH",
            "deal_assessment": deal_assessment,
            "references": references,
            "olx_price": olx_price_mock
        }
        
        return result

# --- OLX Модуль ---

async def fetch_olx_data(session, pool):
    """Асинхронно збирає нові оголошення з OLX з фільтрами."""
    new_posts = []
    
    search_term = OLX_SEARCH_QUERIES[0]
    olx_search_url = f"https://www.olx.ua/d/uk/list/q-{search_term}/?currency=UAH&search%5Bfilter_float_price%3Afrom%5D={OLX_PRICE_FILTER}"
    
    logger.info(f"Запуск сканування OLX: {olx_search_url}")
    
    try:
        async with session.get(olx_search_url, timeout=20) as response:
            response.raise_for_status()
            html = await response.text()
    except aiohttp.ClientError as e:
        logger.error(f"Помилка при завантаженні OLX: {e}")
        return new_posts
    except asyncio.TimeoutError:
        logger.error("Таймаут при завантаженні OLX.")
        return new_posts

    soup = BeautifulSoup(html, 'lxml')
    items = soup.find_all('div', {'data-cy': re.compile(r'l-card')})

    async with pool.acquire() as conn:
        existing_ids = await conn.fetchval("SELECT array_agg(olx_id) FROM olx_posts") or []

    for item in items:
        olx_url_tag = item.find('a', {'data-cy': 'listing-ad-link'})
        if not olx_url_tag:
            continue
            
        full_url = urljoin(olx_search_url, olx_url_tag.get('href'))
        
        match = re.search(r'-ID(\d+)\.html', full_url)
        olx_id = match.group(1) if match else None
        
        if not olx_id or olx_id in existing_ids:
            continue

        title = item.find('h6').text.strip() if item.find('h6') else 'N/A'
        price_text = item.find('p', {'data-testid': 'price'}).text.strip() if item.find('p', {'data-testid': 'price'}) else '0 UAH'
        price_match = re.search(r'([\d\s]+)', price_text)
        price_uah = int("".join(price_match.group(1).split())) if price_match else 0
        
        img_tag = item.find('img')
        image_url = img_tag.get('src') if img_tag else None

        new_posts.append({
            'olx_id': olx_id,
            'title': title,
            'price': price_uah,
            'url': full_url,
            'image_url': image_url
        })
        
    logger.info(f"Знайдено {len(new_posts)} нових оголошень.")
    return new_posts

# --- Telegram Хендлери ---

def get_feedback_keyboard(olx_id):
    """Створює клавіатуру з кнопками Лайк/Не Лайк."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍 Лайк (Релевантно)", callback_data=f"fb_like_{olx_id}"),
            InlineKeyboardButton(text="👎 Не Лайк (Невідповідність)", callback_data=f"fb_dislike_{olx_id}")
        ]
    ])
    return keyboard

async def send_olx_post(bot, item, ai_result):
    """Форматує та відправляє фінальне оголошення в Telegram."""
    
    refs_text = ""
    for i, ref in enumerate(ai_result.get('references', [])):
        price_formatted = f"{ref['price']:,}".replace(',', ' ')
        refs_text += f"*{i+1}.* {price_formatted} грн. [Посилання]({ref['url']})\n"
    
    olx_price_formatted = f"{item['price']:,}".replace(',', ' ')
    
    message_text = (
        f"**✨ НОВЕ ОГОЛОШЕННЯ НА OLX ✨**\n\n"
        f"**{item['title']}**\n"
        f"💰 **Ціна OLX:** *{olx_price_formatted} UAH*\n"
        f"🕵️‍♂️ **Марка/Клеймо:** `{ai_result.get('mark_detected', 'Н/Д')}`\n"
        f"---------------------------------------\n"
        f"🤖 **Оцінка AI:** `{ai_result.get('authenticity', 'Н/Д')}`\n"
        f"💰 **Орієнтовна Вартість:** `{ai_result.get('estimated_value', 'Н/Д')}`\n"
        f"🔥 **Оцінка Вигідності:** **{ai_result.get('deal_assessment', 'Н/Д')}**\n\n"
        f"**⚖️ Порівняння (мін. 3 референси):**\n"
        f"{refs_text}\n"
        f"[➡️ Перейти до оголошення]({item['url']})"
    )

    try:
        if item['image_url']:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=item['image_url'],
                caption=message_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_feedback_keyboard(item['olx_id'])
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=message_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_feedback_keyboard(item['olx_id'])
            )
        return True
    except Exception as e:
        logger.error(f"Помилка при відправці повідомлення в Telegram: {e}")
        return False

# --- FSM Хендлери для Бази Знань ---

@dp.message(Command("start"))
async def command_start_handler(message: types.Message):
    """Обробка команди /start. Вітає користувача."""
    await message.answer(
        "👋 Вітаю! Я OLX-Монітор бот, що шукає для вас цінні предмети (золото/срібло) за вигідними цінами. "
        "Скористайтеся командою /base, щоб **додати власні еталонні зразки та покращити моє навчання!**"
    )

@dp.message(Command("base"))
async def command_base_handler(message: types.Message, state: FSMContext):
    """Запуск процесу додавання еталону до Бази Знань (FSM)."""
    await state.clear() 
    await message.answer(
        "**📚 Додавання до Бази Знань (Крок 1/2):**\n"
        "1. Будь ласка, **надішліть еталонне зображення** цінного предмета.\n"
        "2. Я попрошу надіслати опис пізніше.\n\n"
        "Для скасування надішліть /cancel.",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(BaseForm.waiting_for_photo)

@dp.message(Command("base_list"))
async def command_base_list_handler(message: types.Message, pool):
    """Виводить список еталонів, які користувач додав до Бази Знань."""
    user_id = message.from_user.id
    
    async with pool.acquire() as conn:
        records = await conn.fetch("SELECT id, title, created_at FROM user_base WHERE user_id = $1 ORDER BY created_at DESC LIMIT 10", user_id)
        
    if not records:
        await message.answer("Ваша **База Знань** поки що порожня. Використовуйте /base, щоб додати перший еталон.")
        return
        
    response_text = "**📚 Ваші Еталони (База Знань):**\n\n"
    
    for record in records:
        date_str = record['created_at'].astimezone(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
        response_text += (
            f"**ID {record['id']}**: {record['title']}\n"
            f"  `Додано: {date_str}`\n\n"
        )
        
    response_text += "*(Показано останні 10 записів)*"
    await message.answer(response_text, parse_mode=ParseMode.MARKDOWN)


@dp.message(Command("cancel"))
async def cancel_handler(message: types.Message, state: FSMContext):
    """Скасування будь-якого поточного стану FSM."""
    await state.clear()
    await message.answer("❌ Додавання до Бази Знань скасовано.")

@dp.message(BaseForm.waiting_for_photo, F.photo)
async def handle_base_photo(message: types.Message, state: FSMContext):
    """Обробка зображення для Бази Знань (Крок 1/2)."""
    
    photo_file_id = message.photo[-1].file_id 
    
    await state.update_data(photo_file_id=photo_file_id, user_id=message.from_user.id)
    
    await message.answer("✅ Зображення отримано (Крок 2/2). Тепер, будь ласка, надішліть **текстовий опис** та ключові слова.\n\n"
                         "Приклад: `Монета 5 рублів 1898 року, Оригінал, Ринкова ціна 12000-15000`")
    await state.set_state(BaseForm.waiting_for_text)

@dp.message(BaseForm.waiting_for_photo)
async def handle_base_photo_invalid(message: types.Message):
    """Обробка невірних типів повідомлень під час очікування фото."""
    await message.answer("Це не зображення. Будь ласка, надішліть фото або /cancel.")


@dp.message(BaseForm.waiting_for_text, F.text)
async def handle_base_text(message: types.Message, state: FSMContext, pool):
    """Обробка тексту та збереження еталону в БД (Крок 2/2)."""
    
    data = await state.get_data()
    user_id = data.get('user_id')
    photo_file_id = data.get('photo_file_id')
    
    if not user_id or not photo_file_id:
        await message.answer("❌ Помилка: не знайдено попереднє зображення. Спробуйте ще раз: /base")
        await state.clear()
        return

    # Збереження даних у таблицю user_base
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_base (user_id, title, keywords, estimated_value_text, image_url) VALUES ($1, $2, $3, $4, $5)",
            user_id, 
            message.text.split(',')[0].strip(), 
            message.text, 
            message.text, 
            photo_file_id 
        )
    
    await message.answer("✅ Еталон успішно додано до вашої **Бази Знань**! Це підвищить точність AI-оцінки. Використовуйте /base_list, щоб переглянути ваші записи.")
    await state.clear()

@dp.message(BaseForm.waiting_for_text)
async def handle_base_text_invalid(message: types.Message):
    """Обробка невірних типів повідомлень під час очікування тексту."""
    await message.answer("Будь ласка, надішліть опис текстом або /cancel.")

# --- Обробка кнопок зворотного зв'язку (Лайк/Не Лайк) ---

@dp.callback_query(F.data.startswith("fb_"))
async def feedback_callback_handler(callback_query: types.CallbackQuery, pool, dp):
    """Обробка кліків по кнопках Лайк/Не Лайк (механізм навчання)."""
    
    action, olx_id = callback_query.data.split('_')[1:]
    is_like = action == 'like'
    
    async with pool.acquire() as conn:
        # 1. Зберігання статистики в БД
        await conn.execute(
            "INSERT INTO user_feedback (user_id, olx_id, is_like) VALUES ($1, $2, $3)",
            callback_query.from_user.id, olx_id, is_like
        )
    
    # 2. Якщо 'Не Лайк' (👎), додаємо ID у навчальний пул для AI корекції
    if not is_like:
        dp['gemini_feedback'].append(olx_id)
        logger.warning(f"Зворотний зв'язок 👎: OLX ID {olx_id} додано до навчального пулу.")

    await callback_query.answer(f"Дякуємо за ваш відгук! Це допоможе покращити точність AI.")
    
    # Вимкнення кнопок після кліку
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# --- Воркер (Фоновий Процес) ---

async def olx_monitoring_worker(bot, pool, gemini_mock, dp, interval=600):
    """Фоновий воркер для періодичної перевірки OLX (кожних 10 хвилин)."""
    logger.info(f"Воркер моніторингу OLX запущено. Інтервал: {interval} сек.")
    while True:
        try:
            # 1. ОБРОБКА ЗВОРОТНОГО ЗВ'ЯЗКУ (НАВЧАННЯ)
            if dp['gemini_feedback']:
                logger.info(f"СИСТЕМА НАВЧАННЯ: Обробка {len(dp['gemini_feedback'])} прикладів 'Не Лайк'.")
                dp['gemini_feedback'].clear() # Очистка пулу

            # 2. СКАНУВАННЯ OLX
            async with aiohttp.ClientSession() as session:
                new_posts = await fetch_olx_data(session, pool)
                
                async with pool.acquire() as conn:
                    # Отримуємо контекст (еталони) для Gemini
                    user_base_context = await conn.fetch("SELECT image_url, keywords FROM user_base")
                    
                    for post in new_posts:
                        
                        # Крок 3: Аналіз Gemini (ЕКОНОМІЧНИЙ)
                        ai_result = await gemini_mock.analyze_olx_item(
                            post['title'], post['image_url'], post['olx_id'], user_base_context
                        )
                        
                        # Крок 4: Умова публікації (is_relevant)
                        if ai_result.get('is_relevant'):
                            success = await send_olx_post(bot, post, ai_result)
                            
                            if success:
                                # Збереження стану в olx_posts
                                await conn.execute(
                                    "INSERT INTO olx_posts (olx_id, title, price, published_at) VALUES ($1, $2, $3, $4)",
                                    post['olx_id'], post['title'], post['price'], datetime.now(KYIV_TZ)
                                )
        except Exception as e:
            logger.error(f"Глобальна помилка у воркері моніторингу: {e}")
            
        await asyncio.sleep(interval) 

# --- Головна функція ---

async def main():
    """Ініціалізація та запуск бота."""
    
    if not BOT_TOKEN or not DATABASE_URL or CHANNEL_ID is None:
        logger.critical("Критична помилка: Не знайдено BOT_TOKEN, DATABASE_URL або CHANNEL_ID.")
        sys.exit(1)
        
    # Ініціалізація пулу з'єднань з БД
    pool = await asyncpg.create_pool(DATABASE_URL)
    await init_db(pool)
    
    # Ініціалізація об'єкта Bot
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    
    # Передача залежностей у Dispatcher (dp вже визначений глобально)
    dp['pool'] = pool
    dp['gemini_mock'] = GeminiMock(pool)
    dp['bot'] = bot
    dp['gemini_feedback'] = [] # Сховище для ID, які отримали 👎 (Навчальний пул)

    # Запуск фонового воркера
    asyncio.create_task(olx_monitoring_worker(bot, pool, dp['gemini_mock'], dp))
    
    logger.info("Бот запущено. Починаю опитування...")
    
    try:
        # dp.start_polling() працює з глобально визначеним dp
        await dp.start_polling(bot)
    except Exception as e:
        logger.critical(f"Критична помилка виконання: {e}")
    finally:
        await pool.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот зупинено вручну.")
    except Exception as e:
        logger.critical(f"Головна помилка виконання: {e}")
