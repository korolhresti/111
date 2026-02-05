import os
import io
import asyncio
import logging
import json
import re
import random
import time
from datetime import datetime
from typing import List, Dict, Optional, Any

import aiohttp
import asyncpg
import numpy as np
from PIL import Image
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# --- Telegram Imports ---
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# --- AI & ML Imports ---
import google.generativeai as genai
from sentence_transformers import SentenceTransformer, util

# Налаштування логування
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
log = logging.getLogger("WatchExpert")

# Завантаження змінних середовища
load_dotenv()

# --- КОНФІГУРАЦІЯ ---
BOT_TOKEN = os.getenv("TELEGRAMTOKEN", "")
DB_DSN = os.getenv("DATABASEURL", "postgresql://user:password@host/dbname")
GEMINI_KEY = os.getenv("GEMINIAPIKEY", "")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")  # ID каналу
ADMIN_IDS = [int(x) for x in os.getenv("ADMINUSERID", "0").split(",") if x.strip().isdigit()]

# --- Ініціалізація Gemini ---
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
    ai_model = genai.GenerativeModel('gemini-1.5-flash')
else:
    ai_model = None
    log.error("Gemini API Key missing!")

# --- Глобальні змінні для ML моделі ---
img_model = None  # Тут буде CLIP модель

# --- СТАНИ ДІАЛОГУ ---
MENU = range(1)

# --- КЛАВІАТУРА ---
KB_MAIN = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📸 Оцінити годинник"), KeyboardButton("🔍 Пошук SUPER DEAL")],
        [KeyboardButton("🔄 Оновити базу Empress"), KeyboardButton("⚙️ Статус системи")]
    ],
    resize_keyboard=True
)

# ==============================================================================
# 🧠 МОДУЛЬ ML (ЛОКАЛЬНИЙ ПОШУК)
# ==============================================================================

class LocalVisionSearch:
    def __init__(self):
        self.model = None
        
    def load_model(self):
        if self.model is None:
            log.info("Завантаження локальної ML моделі (CLIP)...")
            try:
                self.model = SentenceTransformer('clip-ViT-B-32')
                log.info("ML модель завантажено успішно.")
            except Exception as e:
                log.error(f"Помилка завантаження ML моделі: {e}")

    def get_embedding(self, image: Image.Image) -> Optional[np.ndarray]:
        if not self.model:
            self.load_model()
        try:
            return self.model.encode(image)
        except Exception as e:
            log.error(f"Помилка генерації ембеддінга: {e}")
            return None

    def calculate_similarity(self, emb1: np.ndarray, emb2: np.ndarray) -> float:
        return float(util.cos_sim(emb1, emb2)[0][0])

vision_engine = LocalVisionSearch()

# ==============================================================================
# 🗄️ БАЗА ДАНИХ
# ==============================================================================

class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(dsn=self.dsn)
        await self.create_tables()

    async def create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS empress_watches (
                    id SERIAL PRIMARY KEY,
                    title TEXT UNIQUE,
                    price_usd NUMERIC,
                    url TEXT,
                    collection TEXT,
                    image_url TEXT,
                    embedding FLOAT[],
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS deals (
                    id SERIAL PRIMARY KEY,
                    source TEXT,
                    title TEXT,
                    price_usd NUMERIC,
                    url TEXT UNIQUE,
                    is_super_deal BOOLEAN DEFAULT FALSE,
                    found_at TIMESTAMP DEFAULT NOW()
                )
            """)

    async def save_empress_watch(self, data: Dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO empress_watches (title, price_usd, url, collection, image_url, embedding)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (title) DO UPDATE 
                SET price_usd = $2, embedding = $6, updated_at = NOW()
            """, data['title'], data['price'], data['url'], data['collection'], data['image_url'], data['embedding'])

    async def find_closest_match(self, query_embedding: List[float], limit=3):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT title, price_usd, url, image_url, embedding FROM empress_watches")
            
        results = []
        q_vec = np.array(query_embedding)
        
        for row in rows:
            if row['embedding']:
                db_vec = np.array(row['embedding'])
                sim = vision_engine.calculate_similarity(q_vec, db_vec)
                results.append({**dict(row), 'similarity': sim})
        
        results.sort(key=lambda x: x['similarity'], reverse=True)
        return results[:limit]

# ==============================================================================
# 🌐 СКРЕПЕРИ (Empress & OLX)
# ==============================================================================

class EmpressScraper:
    BASE_URL = "https://empress.cc"
    
    async def scrape_collection(self, collection_url: str, db: Database):
        log.info(f"Початок парсингу Empress: {collection_url}")
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(collection_url) as resp:
                    if resp.status != 200: return 0
                    html = await resp.text()
            except Exception as e:
                log.error(f"Помилка доступу до {collection_url}: {e}")
                return 0

            soup = BeautifulSoup(html, 'lxml')
            products = soup.select('.grid-product__content')
            
            count = 0
            for prod in products:
                try:
                    title_elem = prod.select_one('.grid-product__title')
                    price_elem = prod.select_one('.grid-product__price')
                    img_elem = prod.select_one('img')
                    link_elem = prod.select_one('.grid-product__link')

                    if not (title_elem and price_elem and img_elem): continue

                    title = title_elem.get_text(strip=True)
                    price_str = price_elem.get_text(strip=True).replace('$', '').replace(',', '')
                    price = float(re.findall(r"[\d\.]+", price_str)[0])
                    
                    img_url = "https:" + img_elem['src'] if img_elem['src'].startswith('//') else img_elem['src']
                    img_url = re.sub(r'_\d+x\d+', '', img_url) 
                    prod_url = self.BASE_URL + link_elem['href']

                    embedding_list = []
                    try:
                        async with session.get(img_url) as img_resp:
                            if img_resp.status == 200:
                                img_data = await img_resp.read()
                                image = Image.open(io.BytesIO(img_data)).convert("RGB")
                                vector = vision_engine.get_embedding(image)
                                if vector is not None:
                                    embedding_list = vector.tolist()
                    except Exception as e:
                        log.warning(f"Не вдалося обробити фото {title}: {e}")
                        continue

                    if embedding_list:
                        await db.save_empress_watch({
                            'title': title,
                            'price': price,
                            'url': prod_url,
                            'collection': collection_url,
                            'image_url': img_url,
                            'embedding': embedding_list
                        })
                        count += 1
                except Exception as e:
                    log.error(f"Помилка парсингу товару: {e}")
            return count

class OLXHunter:
    async def search_and_verify(self, query: str, reference_embedding: List[float], min_price: float, max_price: float):
        search_url = f"https://www.olx.ua/uk/list/q-{query.replace(' ', '-')}/"
        results = []
        log.info(f"OLX Пошук: {query}")
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(search_url) as resp:
                    if resp.status != 200: return []
                    html = await resp.text()
            except Exception:
                return []

            soup = BeautifulSoup(html, 'lxml')
            offers = soup.select('[data-cy="l-card"]')
            
            for offer in offers[:10]:
                try:
                    title_div = offer.select_one('h6')
                    if not title_div: continue
                    title = title_div.get_text(strip=True)
                    
                    link_tag = offer.select_one('a')
                    url = "https://www.olx.ua" + link_tag['href'] if link_tag else ""
                    
                    price_div = offer.select_one('[data-testid="ad-price"]')
                    if not price_div: continue
                    price_raw = price_div.get_text(strip=True).replace(' ', '').replace('грн.', '')
                    
                    try:
                        price_uah = float(re.findall(r'\d+', price_raw)[0])
                        price_usd = price_uah / 41.5
                    except: continue

                    if not (min_price * 0.2 <= price_usd <= max_price * 1.5): continue

                    img_tag = offer.select_one('img')
                    if not img_tag: continue
                    img_src = img_tag.get('src')
                    
                    similarity = 0.0
                    if img_src and reference_embedding:
                        async with session.get(img_src) as i_resp:
                            if i_resp.status == 200:
                                i_data = await i_resp.read()
                                olx_img = Image.open(io.BytesIO(i_data)).convert("RGB")
                                olx_emb = vision_engine.get_embedding(olx_img)
                                if olx_emb is not None:
                                    similarity = vision_engine.calculate_similarity(np.array(reference_embedding), olx_emb)

                    if similarity > 0.75:
                        results.append({
                            'title': title,
                            'price_usd': price_usd,
                            'url': url,
                            'similarity': similarity,
                            'img_src': img_src
                        })
                except Exception as e:
                    log.error(f"Помилка обробки лота OLX: {e}")
        return results

# ==============================================================================
# 🤖 BOT LOGIC
# ==============================================================================

class WatchBot:
    def __init__(self):
        self.db = Database(DB_DSN)
        self.empress = EmpressScraper()
        self.olx = OLXHunter()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "👋 Привіт! Я Watch-Expert AI Pro v1.\n"
            "Я допоможу визначити вартість годинника, перевірити його по базі Empress "
            "та знайти вигідні пропозиції на OLX.\n\n"
            "Надішліть фото годинника або оберіть дію в меню.",
            reply_markup=KB_MAIN
        )
        return MENU

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.message.from_user
        log.info(f"Отримано фото від {user.first_name}")

        status_msg = await update.message.reply_text("🧠 AI аналізує зображення...")
        
        photo_file = await update.message.photo[-1].get_file()
        img_bytes = await photo_file.download_as_bytearray()
        user_image = Image.open(io.BytesIO(img_bytes)).convert("RGB")

        user_embedding = vision_engine.get_embedding(user_image)
        if user_embedding is None:
            await status_msg.edit_text("❌ Помилка обробки зображення (Vision AI).")
            return MENU

        await status_msg.edit_text("🔍 Порівнюю з еталонною базою Empress...")
        matches = await self.db.find_closest_match(user_embedding.tolist(), limit=1)
        
        reference_info = "Не знайдено в базі Empress."
        ref_price = 0
        ref_title = ""
        
        if matches:
            top_match = matches[0]
            if top_match['similarity'] > 0.8:
                ref_price = float(top_match['price_usd'])
                ref_title = top_match['title']
                reference_info = (
                    f"✅ **Знайдено відповідність!**\n"
                    f"Модель: {ref_title}\n"
                    f"Ціна Empress: **${ref_price}**\n"
                    f"Схожість: {top_match['similarity']*100:.1f}%"
                )
            else:
                reference_info = f"⚠️ Прямої відповідності в Empress не знайдено (найближче: {top_match['similarity']*100:.1f}%)."

        await status_msg.edit_text("📝 Генерую експертний звіт...")
        
        prompt = """
        Ти експерт з годинників. Проаналізуй це зображення.
        Виведи JSON з полями:
        - brand (string)
        - model (string, if visible)
        - estimated_year (string)
        - condition (string)
        - authenticity_check (string: notes on signs of fake)
        - liquidity_score (1-10)
        - search_keywords (string: best keywords for OLX search)
        """
        
        try:
            ai_response = await asyncio.to_thread(
                ai_model.generate_content, 
                [prompt, user_image]
            )
            text_resp = ai_response.text.replace('```json', '').replace('```', '')
            analysis = json.loads(text_resp)
        except Exception as e:
            log.error(f"Gemini Error: {e}")
            analysis = {"brand": "Unknown", "search_keywords": "годинник"}

        olx_deals = []
        if analysis.get('search_keywords'):
            await status_msg.edit_text(f"🦅 Шукаю лоти на OLX за запитом: '{analysis['search_keywords']}'...")
            olx_deals = await self.olx.search_and_verify(
                analysis['search_keywords'], 
                user_embedding.tolist(),
                min_price=ref_price * 0.1 if ref_price else 10,
                max_price=ref_price * 1.2 if ref_price else 10000
            )

        report = (
            f"🕵️‍♂️ **ЗВІТ AI-ЕКСПЕРТА**\n\n"
            f"🏷 **Бренд:** {analysis.get('brand')}\n"
            f"⏱ **Рік:** {analysis.get('estimated_year')}\n"
            f"💎 **Стан:** {analysis.get('condition')}\n"
            f"📈 **Ліквідність:** {analysis.get('liquidity_score')}/10\n\n"
            f"🛡 **Перевірка:** {analysis.get('authenticity_check')}\n\n"
            f"🏛 **Еталон (Empress):**\n{reference_info}\n"
        )

        if olx_deals:
            report += "\n🇺🇦 **Знайдено на OLX (схожі візуально):**\n"
            for deal in olx_deals:
                profit_icon = "🔥 SUPER DEAL" if (ref_price > 0 and deal['price_usd'] < ref_price * 0.6) else ""
                report += f"- [{deal['title']}]({deal['url']}) - **${deal['price_usd']:.0f}** {profit_icon}\n"
                
                if profit_icon and CHANNEL_ID:
                    await self.post_to_channel(context, deal, ref_price, analysis['brand'])
        else:
            report += "\n😞 На OLX ідентичних лотів візуально не підтверджено."

        await update.message.reply_text(report, parse_mode="Markdown", disable_web_page_preview=True)
        return MENU

    async def post_to_channel(self, context, deal, ref_price, brand):
        percent_off = ((ref_price - deal['price_usd']) / ref_price) * 100
        text = (
            f"🚨 **SUPER DEAL DETECTED!**\n\n"
            f"🕰 **{brand}**\n"
            f"💰 Ціна OLX: **${deal['price_usd']:.0f}**\n"
            f"🏛 Ринкова (Empress): **${ref_price:.0f}**\n"
            f"📉 Вигода: **{percent_off:.0f}%**\n\n"
            f"👉 [Переглянути оголошення]({deal['url']})"
        )
        try:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Помилка посту в канал: {e}")

    async def update_empress_db(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message.from_user.id not in ADMIN_IDS:
            await update.message.reply_text("Тільки для адмінів.")
            return MENU
            
        await update.message.reply_text("🔄 Починаю оновлення бази Empress... Це може зайняти хвилини.")
        url = "https://empress.cc/collections/swiss-vintage-watches"
        count = await self.empress.scrape_collection(url, self.db)
        await update.message.reply_text(f"✅ Оновлення завершено. Додано/Оновлено годинників: {count}")
        return MENU

    async def status_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            async with self.db.pool.acquire() as conn:
                count = await conn.fetchval("SELECT COUNT(*) FROM empress_watches")
            
            msg = (
                f"⚙️ **Статус системи**\n"
                f"📚 База Empress: {count} записів\n"
                f"🧠 ML Модель: {'Завантажена' if vision_engine.model else 'Не завантажена'}\n"
                f"👁 Gemini AI: {'Підключено' if ai_model else 'Відключено'}"
            )
        except Exception as e:
            msg = f"Помилка отримання статусу: {e}"
        await update.message.reply_text(msg)
        return MENU

# --- ФУНКЦІЯ ПОВІДОМЛЕННЯ ПРИ ЗАПУСКУ ---
async def post_init(application: Application):
    """Виконується після успішного запуску бота."""
    log.info("Бот запущено. Відправка повідомлення в канал...")
    
    welcome_message = (
        "🚀 **Watch-Expert AI Pro v1 ЗАПУЩЕНО!**\n\n"
        "Система готова до роботи. Доступні команди:\n"
        "📸 **Оцінити годинник** - Надішліть фото боту\n"
        "🔍 **Пошук SUPER DEAL** - Автоматичний аналіз при оцінці\n"
        "🔄 **Оновити базу Empress** - (Адмін) Парсинг еталонів\n"
        "⚙️ **Статус системи** - Перевірка підключень\n\n"
        "🤖 Бот очікує фотографії..."
    )
    
    if CHANNEL_ID:
        try:
            await application.bot.send_message(chat_id=CHANNEL_ID, text=welcome_message, parse_mode="Markdown")
            log.info("Вітання відправлено в канал.")
        except Exception as e:
            log.error(f"Не вдалося відправити вітання в канал: {e}")
    else:
        log.warning("CHANNEL_ID не встановлено, вітання не відправлено.")

# --- MAIN SETUP ---

async def main():
    bot_instance = WatchBot()
    await bot_instance.db.connect()
    
    # Спроба завантажити ML модель при старті (блокуюче)
    # Якщо мало RAM, це може вбити процес. Можна закоментувати.
    # vision_engine.load_model()
    
    # Додаємо post_init для відправки повідомлення при старті
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", bot_instance.start)],
        states={
            MENU: [
                MessageHandler(filters.Regex("^📸 Оцінити годинник$"), lambda u,c: u.message.reply_text("Надішліть фото годинника.")),
                MessageHandler(filters.Regex("^🔍 Пошук SUPER DEAL$"), lambda u,c: u.message.reply_text("Ця функція працює автоматично при аналізі фото.")),
                MessageHandler(filters.Regex("^🔄 Оновити базу Empress$"), bot_instance.update_empress_db),
                MessageHandler(filters.Regex("^⚙️ Статус системи$"), bot_instance.status_check),
                MessageHandler(filters.PHOTO, bot_instance.handle_photo)
            ],
        },
        fallbacks=[CommandHandler("start", bot_instance.start)]
    )

    application.add_handler(conv_handler)
    
    log.info("Початок поллінгу...")
    await application.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
