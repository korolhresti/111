import os
import asyncio
import logging
import re
import aiohttp
import asyncpg
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# --- 1. НАЛАШТУВАННЯ ---
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_CHAT_ID")

# Логування
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("WatchExpert")

# Ініціалізація бота
bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- 2. РОБОТА З БАЗОЮ ДАНИХ ---

async def init_db(pool):
    """Створює таблиці, якщо їх немає"""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS empress_watches (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE,
                price_usd NUMERIC,
                last_updated TIMESTAMP DEFAULT NOW()
            );
        """)
    logger.info("✅ Таблиці в базі даних перевірено/створено")

# --- 3. ФУНКЦІЯ СИНХРОНІЗАЦІЇ З EMPRESS.CC ---

async def sync_empress_data(pool):
    """Парсить сайт та оновлює базу даних"""
    url = "https://empress.cc/collections/all"
    logger.info("🔄 Початок збору даних з Empress.cc...")
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=30) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    items = soup.select(".grid-product__content")
                    
                    async with pool.acquire() as conn:
                        for item in items:
                            try:
                                name = item.select_one(".grid-product__title").text.strip()
                                price_text = item.select_one(".grid-product__price").text
                                # Витягуємо лише цифри для ціни
                                price = float(re.sub(r"[^\d.]", "", price_text))
                                
                                await conn.execute("""
                                    INSERT INTO empress_watches (name, price_usd)
                                    VALUES ($1, $2)
                                    ON CONFLICT (name) DO UPDATE SET price_usd = $2, last_updated = NOW()
                                """, name, price)
                            except Exception:
                                continue
                    logger.info("✅ Синхронізація з Empress завершена успішно")
        except Exception as e:
            logger.error(f"❌ Помилка при парсингу: {e}")

# --- 4. ОБРОБНИКИ КОМАНД ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message, db_pool: asyncpg.Pool):
    """Обробка команди /start"""
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM empress_watches")
    
    welcome_text = (
        f"👋 <b>Привіт! Система Watch-Expert AI активована.</b>\n\n"
        f"📊 У базі даних зараз: <code>{count}</code> записів годинників.\n"
        f"🔎 Бот працює у фоновому режимі та моніторить оновлення."
    )
    await message.answer(welcome_text)

# --- 5. ЗАПУСК ТА ПРИВІТАННЯ В КАНАЛ ---

async def on_startup(bot: Bot, pool: asyncpg.Pool):
    """Виконується при запуску бота"""
    # Створюємо таблиці
    await init_db(pool)
    
    # Запускаємо першу синхронізацію
    asyncio.create_task(sync_empress_data(pool))
    
    # Повідомлення в канал
    startup_msg = (
        "🤖 <b>Watch-Expert AI Online</b>\n"
        "✨ Система моніторингу запущена на Render.\n"
        "📡 База даних Neon підключена.\n"
        "🕔 Статус: Очікування нових лотів..."
    )
    try:
        await bot.send_message(CHANNEL_ID, startup_msg)
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, "✅ Бот успішно перевантажений та працює.")
    except Exception as e:
        logger.error(f"Не вдалося надіслати привітання: {e}")

async def main():
    # Створення пулу з'єднань з БД
    pool = await asyncpg.create_pool(DATABASE_URL)
    
    # Додаємо пул у context диспетчера
    dp["db_pool"] = pool
    
    # Виконуємо дії при старті
    await on_startup(bot, pool)
    
    # Видаляємо старі оновлення (запобігає конфліктам)
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Старт поллінгу
    logger.info("🚀 Бот починає слухати повідомлення...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот вимкнений")