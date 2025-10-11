import os
import asyncio
import logging
import re
import random
import sys
import json
import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urljoin
from typing import Dict, Any, List, Optional

import asyncpg
import aiohttp
import feedparser
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# --- 1. НАЛАШТУВАННЯ СЕРЕДОВИЩА ТА ЛОГУВАННЯ ---

KYIV_TZ = timezone(timedelta(hours=3), 'Europe/Kyiv')

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    stream=sys.stdout)
logger = logging.getLogger(__name__)

# Змінні оточення
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

try:
    # Централізований спосіб отримання ID
    channel_env_var = os.getenv("CHANNEL_ID") or os.getenv("channel_ID") 
    CHANNEL_ID = int(channel_env_var) if channel_env_var else None
    ADMIN_ID = int(os.getenv("ADMIN_ID", CHANNEL_ID or 0)) # ADMIN_ID за замовчуванням дорівнює CHANNEL_ID
except (TypeError, ValueError):
    CHANNEL_ID = None
    ADMIN_ID = 0
    logger.error("CHANNEL_ID або ADMIN_ID не знайдено або має некоректний формат.")

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent"
# Фіксований seed для симуляції стабільних випадкових даних
random.seed(42)

# --- 2. КОНФІГУРАЦІЯ ЕКОНОМІЧНОЇ ПЛАТФОРМИ (V10) ---
class EconomicConfig:
    """Централізована конфігурація для професійної платформи V10."""
    
    # --- OLX МОНІТОРИНГ ТА ФІЛЬТРИ ---
    OLX_SEARCH_QUERIES = [ # Колекційні/цінні предмети
        'чоловічий годинник rolex', 'верстат чпу промисловий', 'монета 5 рублів золото', 
        'царське срібло', 'ікона срібло', 'зварювальний апарат kemppi' 
    ]
    OLX_SCRAP_QUERIES = [ # Лом/бульйон
        'золото лом 585', 'срібло лом 925', 'золотий злиток' 
    ]
    OLX_PRICE_FILTER = 20000 # Збільшений поріг для V10
    
    # --- ЕКОНОМІЧНІ КОНСТАНТИ ---
    TRANSACTION_FEES_PERCENT = 10  # Умовна комісія перепродажу
    MIN_PROFIT_MARGIN_PERCENT = 20 # Мінімальна маржа для вигідної угоди
    
    # --- TIV МОДЕЛЬ (ОБЛАДНАННЯ) ---
    MACHINERY_DEPRECIATION_RATE = 0.05  
    MACHINERY_CONDITION_WEIGHT = 0.4    
    MACHINERY_HOURS_PENALTY_RATE = 0.000005 

    # --- RAV МОДЕЛЬ (ГОДИННИКИ/КОЛЕКЦІЇ) ---
    MIN_RARITY_SCORE = 50 
    MAX_AUTHENTICITY_RISK = 30
    PRESTIGE_MULTIPLIERS = {'rolex': 1.5, 'patek philippe': 1.8, 'omega': 1.3}
    # НОВЕ: Коефіцієнт корекції від зворотного зв'язку
    FEEDBACK_CORRECTION_MULTIPLIER = 0.1 # Вплив зворотного зв'язку на ціну

    # --- SPOT ЦІНИ (Фіксовані на добу - для імітації) ---
    SPOT_PRICES: Dict[str, float] = {
        "GOLD_585_UAH_PER_GRAM": 2850.0, 
        "SILVER_925_UAH_PER_GRAM": 48.5, 
        "LAST_UPDATED": datetime.now(KYIV_TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    }
    
    # --- СРІБЛО ТА МЕТАЛИ ---
    SILVER_KEYWORDS: List[str] = [
        "срібло", "срібний", "Ag 925", "sterling silver", "800 проба", "925 проба", "посріблений", "мельхіор"
    ]
    
    # --- RSS ФІНАНСОВІ СТРІЧКИ ---
    RSS_FEEDS: Dict[str, str] = {
        "Financial News": "https://feeds.finance.ua/ua/news/rss", 
        "Metals Market News": "https://www.google.com/search?q=ціни+на+срібло+новини&tbm=nws&output=rss"
    }

# --- 3. FSM СТАНИ (ОБ'ЄДНАНІ) ---
class BaseForm(StatesGroup):
    """База Знань Користувача."""
    waiting_for_photo = State()
    waiting_for_text = State()

class CutleryAnalysis(StatesGroup):
    """Аналіз столових приладів на срібло."""
    waiting_for_url_or_description = State()

class LearningState(StatesGroup):
    """Стани для інтерактивного навчання та становлення колекціонером."""
    waiting_for_topic = State()
    in_session = State()
    waiting_for_next = State()

# --- 4. УТИЛІТИ ТА VISION АНАЛІЗ ---

async def init_db(pool: asyncpg.Pool):
    """Створює необхідні таблиці в базі даних."""
    logger.info("Підключення та ініціалізація БД...")
    
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS olx_posts (
            id SERIAL PRIMARY KEY,
            olx_id TEXT UNIQUE,
            title TEXT,
            price INTEGER,
            published_at TIMESTAMP WITH TIME ZONE,
            ai_analysis_json JSONB,
            is_relevant BOOLEAN
        );
    """)
    # ... (Інші таблиці user_base, user_feedback, users залишаються незмінними)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS user_base (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            title TEXT,
            image_url TEXT,
            keywords TEXT,
            estimated_value_text TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
    """)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS user_feedback (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            olx_id TEXT,
            is_like BOOLEAN,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
    """)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            joined_at TIMESTAMP WITH TIME ZONE
        );
    """)
    logger.info("Ініціалізація БД завершена.")


async def fetch_page_content(session: aiohttp.ClientSession, url: str) -> str | None:
    """Безпечно завантажує вміст веб-сторінки."""
    try:
        # Додана перевірка на коректність URL
        parsed_url = urlparse(url)
        if not all([parsed_url.scheme in ['http', 'https'], parsed_url.netloc]): 
             logger.warning(f"Некоректний URL: {url}")
             return None

        # Професійний User-Agent
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (TIV_RAV_Bot/9.0)'}
        async with session.get(url, headers=headers, timeout=10) as response:
            if response.status != 200: 
                logger.warning(f"Помилка HTTP {response.status} при завантаженні {url}")
                return None
            return await response.text()
    except aiohttp.ClientError as e:
        logger.error(f"Помилка aiohttp при завантаженні {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"Невідома помилка при завантаженні {url}: {e}")
        return None

async def get_image_base64(session, url, bot: Optional[Bot]=None, file_id=None):
    """Завантажує зображення за URL або file_id (Telegram) та повертає Base64."""
    if file_id and bot:
        try:
            tg_file = await bot.get_file(file_id)
            url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"
        except Exception as e:
            logger.error(f"Помилка отримання file_path Telegram: {e}")
            return None
    
    if not url: return None

    try:
        async with session.get(url, timeout=15) as response:
            response.raise_for_status()
            image_data = await response.read()
            # Обмеження розміру до 4MB (Gemini-Flash API ліміт)
            if len(image_data) > 4 * 1024 * 1024:
                logger.warning("Зображення занадто велике (>4MB). Пропускаємо Vision Analysis.")
                return None
            return base64.b64encode(image_data).decode('utf-8')
    except Exception as e:
        logger.error(f"Помилка завантаження зображення з {url}: {e}")
        return None

async def gemini_api_call(session: aiohttp.ClientSession, payload: Dict[str, Any]):
    """Узагальнена функція виклику Gemini API з експоненційним відступом."""
    if not GEMINI_API_KEY:
        logger.warning("Gemini API Key відсутній. Пропускаємо API-виклик.")
        return None
    
    max_retries = 3
    delay = 1 

    for attempt in range(max_retries):
        try:
            async with session.post(f"{GEMINI_API_URL}?key={GEMINI_API_KEY}", json=payload, timeout=30) as response:
                if response.status == 200:
                    result = await response.json()
                    # Gemini повертає JSON як текстовий рядок
                    json_str = result['candidates'][0]['content']['parts'][0]['text']
                    return json.loads(json_str)
                
                logger.warning(f"Gemini API помилка {response.status}. Спроба {attempt+1}/{max_retries}.")
                await asyncio.sleep(delay)
                delay *= 2 
        except Exception as e:
            logger.error(f"Критична помилка виклику Gemini API: {e}. Спроба {attempt+1}/{max_retries}.")
            await asyncio.sleep(delay)
            delay *= 2
            
    return None

# --- НОВА ЛОГІКА: АДАПТИВНА ІНСТРУКЦІЯ ДЛЯ GEMINI ---
async def _get_adaptive_system_instruction(pool: asyncpg.Pool, user_id: int, is_vision: bool = True) -> str:
    """Формує адаптивну системну інструкцію для Gemini на основі даних користувача та агрегованого фідбеку."""
    
    async with pool.acquire() as conn:
        # 1. Дані з Бази Знань користувача
        user_base_records = await conn.fetch("""
            SELECT title, keywords, estimated_value_text FROM user_base 
            WHERE user_id = $1 ORDER BY created_at DESC LIMIT 5
        """, user_id)
        
        user_context = "Наступні еталонні предмети додано користувачем:\n"
        if user_base_records:
            for rec in user_base_records:
                user_context += f"- **{rec['title']}** (Ключові слова: {rec['keywords']}. Оцінка: {rec['estimated_value_text']})\n"
        else:
            user_context += "- Дані відсутні.\n"
            
        # 2. Агрегований Фідбек (найбільш "спірні" пости)
        if not is_vision: # Агрегований фідбек потрібен для навчання, а не для Vision
             disputed_posts = await conn.fetch("""
                SELECT 
                    olx_posts.title,
                    SUM(CASE WHEN user_feedback.is_like = TRUE THEN 1 ELSE 0 END) AS likes,
                    SUM(CASE WHEN user_feedback.is_like = FALSE THEN 1 ELSE 0 END) AS dislikes
                FROM olx_posts
                JOIN user_feedback ON olx_posts.olx_id = user_feedback.olx_id
                GROUP BY olx_posts.title
                HAVING SUM(CASE WHEN user_feedback.is_like = TRUE THEN 1 ELSE 0 END) > 0 AND 
                       SUM(CASE WHEN user_feedback.is_like = FALSE THEN 1 ELSE 0 END) > 0
                ORDER BY ABS(likes - dislikes) ASC, (likes + dislikes) DESC LIMIT 3;
            """)
             
             disputed_context = "\nНайбільш спірні предмети, які потребують покращеного аналізу:\n"
             if disputed_posts:
                 for post in disputed_posts:
                     disputed_context += f"- **{post['title']}** (Лайки: {post['likes']}, Дизлайки: {post['dislikes']})\n"
             
        
    base_instruction = "Ви — досвідчений AI-експерт, що володіє моделями TIV/RAV для аналізу інвестиційних активів."
    
    if is_vision:
        return f"{base_instruction} Ваша мета — точно класифікувати актив на основі зображення та заголовка. Зверніть увагу на:\n{user_context}"
    else:
        return f"{base_instruction} Ваша мета — навчати користувача складним аспектам інвестиційного колекціонування. Використовуйте знання про спірні активи для наголошення на ключових ризиках: \n{disputed_context}"


async def gemini_vision_analysis(session, prompt, image_base64, pool: asyncpg.Pool, user_id: int, response_schema):
    """Викликає Gemini Vision API для аналізу зображення та повертає структурований JSON."""
    if not image_base64: return None
    
    # Використовуємо адаптивну інструкцію
    system_instruction = await _get_adaptive_system_instruction(pool, user_id, is_vision=True)
    
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": "image/jpeg", 
                            "data": image_base64
                        }
                    }
                ]
            }
        ],
        "config": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
        "systemInstruction": {"parts": [{"text": system_instruction}]}
    }
    return await gemini_api_call(session, payload)

async def generate_collector_lesson(session: aiohttp.ClientSession, topic: str, pool: asyncpg.Pool, user_id: int, difficulty: str = "intermediate") -> Optional[Dict[str, Any]]:
    """Генерує структурований урок та вікторину для навчання."""
    
    # Використовуємо адаптивну інструкцію для навчального модуля
    system_prompt = await _get_adaptive_system_instruction(pool, user_id, is_vision=False)
    
    lesson_schema = {
        "type": "OBJECT",
        "properties": {
            "lesson_title": {"type": "STRING", "description": "Назва уроку."},
            "content": {"type": "STRING", "description": "Детальний, навчальний текст уроку, з розбивкою на параграфи (використовуйте **жирний** текст для термінів)."},
            "quiz_question": {"type": "STRING", "description": "Одне питання для вікторини."},
            "quiz_answer": {"type": "STRING", "description": "Коротка, правильна відповідь на питання."},
            "quiz_hint": {"type": "STRING", "description": "Підказка для користувача, якщо він помилиться."}
        },
        "required": ["lesson_title", "content", "quiz_question", "quiz_answer", "quiz_hint"]
    }
    
    prompt = f"Згенеруй інтерактивний урок на тему '{topic}' для рівня '{difficulty}'. Включи ключові терміни, ризики та поради щодо оцінки. Створи одне питання для вікторини з відповіддю та підказкою."
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": lesson_schema
        },
        "systemInstruction": {"parts": [{"text": system_prompt}]}
    }

    result = await gemini_api_call(session, payload)
    return result

def analyze_for_silver(content: str) -> List[str]:
    """Аналізує текст на наявність срібних ключових слів."""
    if '<html' in content.lower():
        soup = BeautifulSoup(content, 'html.parser')
        text = soup.get_text()
    else:
        text = content
        
    normalized_text = text.lower()
    found_keywords = []
    
    for keyword in EconomicConfig.SILVER_KEYWORDS:
        # Пошук як по цілому слову, так і по фрагменту (для проб)
        if re.search(r'\b' + re.escape(keyword.lower()) + r'\b', normalized_text) or keyword.lower() in normalized_text:
            if keyword not in found_keywords:
                found_keywords.append(keyword)
                
    return found_keywords

def generate_high_yield_proposal() -> str:
    """Симулює генерацію високоприбуткової 'пропозиції' (400%+)."""
    
    asset_list = ["Технологічний ETF (Симуляція)", "Рідкісні метали (Симуляція)", "Акції 'Зеленої' Енергетики (Симуляція)", "Ф'ючерси на екзотичний товар (Симуляція)"]
    asset = random.choice(asset_list)
    risk = random.choice(["Екстремально високий", "Надзвичайно високий", "Високий, не для новачків"])
    duration = random.choice(["3 місяці", "6 місяців", "12 місяців"])
    simulated_return = random.randint(400, 650)
    
    return (
        f"🚨 **ЕЛІТНА ВИСОКОРИЗИКОВА СИМУЛЯЦІЙНА ПРОПОЗИЦІЯ** 🚨\n\n"
        f"📈 **Потенційний Прибуток (Симуляція):** `{simulated_return}%`\n"
        f"🎯 **Актив:** *{asset}*\n"
        f"⏳ **Обрій:** {duration}\n" # Виправлено f"⏳ **Обрій:** *duration*
        f"⚠️ **Рівень Ризику:** *{risk}*\n\n"
        f"📝 **Аналіз:** Це симульована 'пропозиція', що відображає гіпотетичну ситуацію "
        f"на екзотичних ринках. Такий прибуток можливий лише у разі прийняття "
        f"надзвичайно високих ризиків, включаючи повну втрату капіталу.\n\n"
        f"❌ **ВІДМОВА ВІД ВІДПОВІДАЛЬНОСТІ:** *Це не фінансова порада. "
        f"Потенційні {simulated_return}% є симуляцією. Інвестування "
        f"пов'язане з високими ризиками.*"
    )

def calculate_liquidity_risk() -> Dict[str, Any]:
    """Симуляція оцінки ризику ліквідності."""
    days_on_olx = random.randint(1, 100)
    risk_score = min(99, int(days_on_olx * 0.8))
    
    if days_on_olx > 60:
        status = "Високий (Тривалий час на ринку)"
    elif days_on_olx > 20:
        status = "Середній (У межах норми)"
    else:
        status = "Низький (Свіже оголошення)"
        
    return {
        "days_on_olx": days_on_olx,
        "risk_score": risk_score,
        "status": status
    }

# --- НОВА ЛОГІКА: ОТРИМАННЯ КОЕФІЦІЄНТА НАВЧАННЯ З ФІДБЕКУ ---
async def _get_feedback_correction_factor(pool: asyncpg.Pool, olx_id: str) -> float:
    """Визначає коефіцієнт корекції на основі агрегованого зворотного зв'язку по конкретному посту."""
    async with pool.acquire() as conn:
        record = await conn.fetchrow("""
            SELECT 
                SUM(CASE WHEN is_like = TRUE THEN 1 ELSE 0 END) AS likes,
                SUM(CASE WHEN is_like = FALSE THEN 1 ELSE 0 END) AS dislikes
            FROM user_feedback
            WHERE olx_id = $1
        """, olx_id)
        
        if record and (record['likes'] + record['dislikes']) > 0:
            likes = record['likes'] or 0
            dislikes = record['dislikes'] or 0
            total_feedback = likes + dislikes
            
            # Корекція: від +1 до -1. Якщо багато лайків: +1. Якщо багато дизлайків: -1.
            # Наприклад, (5 лайків, 0 дизлайків) -> (5-0)/5 = +1.
            # (0 лайків, 5 дизлайків) -> (0-5)/5 = -1.
            # (5 лайків, 5 дизлайків) -> (5-5)/10 = 0.
            # (1 лайк, 2 дизлайки) -> (1-2)/3 = -0.33
            score = (likes - dislikes) / total_feedback
            
            # Застосовуємо його до глобального множника впливу
            # Коефіцієнт буде від (1 - 0.1) до (1 + 0.1)
            return 1.0 + (score * EconomicConfig.FEEDBACK_CORRECTION_MULTIPLIER)
        
        return 1.0 # Якщо фідбеку немає, коефіцієнт = 1.0


# --- 5. ЕКОНОМІЧНЕ ЯДРО З TIV/RAV/SPOT МОДЕЛЯМИ ---

class EconomicEngine:
    """Ядро економічного аналізу, інтегроване з Gemini Vision V10."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.current_year = datetime.now(KYIV_TZ).year

    # ... (Методи _fetch_external_auction_data, _analyze_machinery, _analyze_spot, _analyze_collectible залишаються незмінними)
    # З метою уникнення надмірності коду, ці методи залишаються як у попередньому запиті

    # --- 5.1. Утиліти для Крос-Референсу ---

    async def _fetch_external_auction_data(self, refined_keywords):
        """Імітує пошук схожих товарів у Зовнішній Базі Аукціонних Цін (BCA)."""
        keyword_seed = hash("".join(refined_keywords)) % 10000
        random.seed(keyword_seed) 
        
        is_machinery_context = any(word in "".join(refined_keywords).lower() for word in ['верстат', 'чпу', 'прес'])
        
        if is_machinery_context:
            avg_sale_price = random.randint(50000, 300000) 
            market_depth = random.randint(5, 50)
        else:
            avg_sale_price = random.randint(25000, 150000)
            market_depth = random.randint(1, 25)

        rarity_score = random.randint(EconomicConfig.MIN_RARITY_SCORE, 99)
        
        return {
            "source": "BCA Auction Data Simulation",
            "rarity_score": rarity_score,
            "average_sale_price": avg_sale_price,
            "market_depth": market_depth 
        }

    # --- 5.2. Аналіз TIV (Промислове обладнання) ---

    async def _analyze_machinery(self, title, olx_id, olx_price, ai_result):
        """Аналіз професійного обладнання з моделлю TIV."""
        machinery_details = ai_result.get('machinery_details', {})
        refined_keywords = ai_result.get('refined_keywords', [title])
        external_data = await self._fetch_external_auction_data(refined_keywords)
        avg_sale_price = external_data.get('average_sale_price', olx_price * random.uniform(1.2, 2.0)) 
        
        # --- НОВЕ: КОЕФІЦІЄНТ КОРЕКЦІЇ ВІД ФІДБЕКУ ---
        feedback_multiplier = await _get_feedback_correction_factor(self.pool, olx_id)
        
        # Вилучення даних з AI
        year = machinery_details.get('year_of_manufacture', self.current_year - 5)
        condition = machinery_details.get('condition_rating', 5)
        hours = machinery_details.get('operating_hours', 3000)
        
        # Розрахунок TIV
        age = max(0, self.current_year - year)
        depreciation_factor = min(0.9, age * EconomicConfig.MACHINERY_DEPRECIATION_RATE)
        value_after_age = avg_sale_price * (1 - depreciation_factor)

        condition_multiplier = (condition / 10) * EconomicConfig.MACHINERY_CONDITION_WEIGHT + (1 - EconomicConfig.MACHINERY_CONDITION_WEIGHT)
        hours_penalty_uah = hours * EconomicConfig.MACHINERY_HOURS_PENALTY_RATE * avg_sale_price 
        
        tiv_value_raw = round(value_after_age * condition_multiplier - hours_penalty_uah)
        
        # ЗАСТОСУВАННЯ КОЕФІЦІЄНТА
        tiv_value = round(tiv_value_raw * feedback_multiplier)
        tiv_value = max(EconomicConfig.OLX_PRICE_FILTER, tiv_value) 

        potential_profit_uah = round(tiv_value - olx_price - (olx_price * EconomicConfig.TRANSACTION_FEES_PERCENT / 100))
        potential_profit_percent = (potential_profit_uah / olx_price) * 100 if olx_price > 0 else 0
        
        is_relevant = potential_profit_percent > EconomicConfig.MIN_PROFIT_MARGIN_PERCENT
        deal_assessment = f"🔥 ВИГІДНА УГОДА ({potential_profit_percent:.1f}% маржа)" if is_relevant else f"Ринкова Ціна ({potential_profit_percent:.1f}% маржа)"
        
        liquidity_data = calculate_liquidity_risk()
            
        final_result = ai_result.copy()
        final_result.update({
            "is_relevant": is_relevant, "type": "ПРОФЕСІЙНЕ ОБЛАДНАННЯ (TIV Model)",
            "estimated_value": tiv_value, "deal_assessment": deal_assessment,
            "potential_profit_uah": potential_profit_uah, "risk_adjusted_value": tiv_value, # TIV = RAV для обладнання
            "market_data": external_data, "liquidity_risk": liquidity_data,
            "feedback_multiplier": feedback_multiplier # Додаємо для звіту
        })
        return final_result
    
    # --- 5.3. Аналіз SPOT (Лом/Бульйон) ---

    def _analyze_spot(self, olx_price, ai_result, spot_prices):
        """Аналіз металевого лому/бульйону з моделлю Spot Price."""
        
        is_gold = any(word in ai_result.get('refined_keywords', []) for word in ['золото', '585'])
        
        if is_gold:
            spot_price_per_gram = spot_prices['GOLD_585_UAH_PER_GRAM']
            metal_type = "Золото 585"
        else: # Припускаємо Срібло 925
            spot_price_per_gram = spot_prices['SILVER_925_UAH_PER_GRAM']
            metal_type = "Срібло 925"

        # Імітація евристики ваги: якщо ціна в рази перевищує очікувану, зменшуємо вагу
        estimated_weight_raw = ai_result.get('estimated_weight', 50) # Імітація ваги від AI
        
        # Евристика: Вартість "колекційної" складової
        implied_spot_value = estimated_weight_raw * spot_price_per_gram
        if olx_price > implied_spot_value * 5: # Якщо ціна в 5 разів вища за Spot, це колекційна цінність, а не лом
            estimated_weight_g = max(1, estimated_weight_raw / 10) # Припускаємо, що це 1/10 ваги у чистому металі (наприклад, позолота, частина ціни це антикваріат)
        else:
            estimated_weight_g = estimated_weight_raw

        calculated_spot_value = round(estimated_weight_g * spot_price_per_gram)
        
        premium_discount_percent = ((olx_price - calculated_spot_value) / calculated_spot_value) * 100 if calculated_spot_value > 0 else 100
        
        if premium_discount_percent < -1 * EconomicConfig.MIN_PROFIT_MARGIN_PERCENT:
            is_relevant = True
            deal_assessment = f"✅ ЗНИЖКА {abs(premium_discount_percent):.1f}% (Нижче Spot Value)"
        else:
            is_relevant = False
            deal_assessment = f"❌ ПРЕМІЯ {premium_discount_percent:.1f}% (Вище Spot Value)"
            
        liquidity_data = calculate_liquidity_risk()

        final_result = ai_result.copy()
        final_result.update({
            "is_relevant": is_relevant, "type": f"МЕТАЛ (SPOT: {metal_type})",
            "estimated_weight_g": estimated_weight_g, "spot_price_per_gram": spot_price_per_gram,
            "calculated_spot_value": calculated_spot_value, "premium_discount_percent": premium_discount_percent,
            "deal_assessment": deal_assessment, "liquidity_risk": liquidity_data
        })
        return final_result

    # --- 5.4. Аналіз RAV (Колекційні предмети/Годинники) ---

    async def _analyze_collectible(self, title, olx_id, olx_price, ai_result):
        """Аналіз колекційних предметів (RAV Model)."""
        refined_keywords = ai_result.get('refined_keywords', [title])
        external_data = await self._fetch_external_auction_data(refined_keywords)
        
        # --- НОВЕ: КОЕФІЦІЄНТ КОРЕКЦІЇ ВІД ФІДБЕКУ ---
        feedback_multiplier = await _get_feedback_correction_factor(self.pool, olx_id)
        
        # Дані з AI Vision
        ai_rarity = ai_result.get('ai_rarity_score', external_data.get('rarity_score', 50))
        watch_details = ai_result.get('watch_details', {})
        authenticity_risk = watch_details.get('authenticity_risk_percent', 0)
        condition_rating = watch_details.get('condition_rating', 8)
        
        # 1. Базова вартість (Аукціонна)
        avg_auction_price = external_data.get('average_sale_price', olx_price * random.uniform(1.5, 3.0)) 
        
        # 2. Фактори RAV
        rarity_factor = (ai_rarity / 100)
        condition_factor = condition_rating / 10
        authenticity_penalty = (1 - authenticity_risk / 100)

        prestige_multiplier = 1.0
        for brand, mult in EconomicConfig.PRESTIGE_MULTIPLIERS.items():
            if brand in title.lower():
                prestige_multiplier = mult
                break
        
        # 3. Розрахунок RAV
        rav_value_raw = round(avg_auction_price * rarity_factor * condition_factor * authenticity_penalty * prestige_multiplier)
        
        # ЗАСТОСУВАННЯ КОЕФІЦІЄНТА
        rav_value = round(rav_value_raw * feedback_multiplier)

        potential_profit_uah = round(rav_value - olx_price - (rav_value * EconomicConfig.TRANSACTION_FEES_PERCENT / 100))
        potential_profit_percent = (potential_profit_uah / olx_price) * 100 if olx_price > 0 else 0

        is_relevant = potential_profit_percent > EconomicConfig.MIN_PROFIT_MARGIN_PERCENT
        deal_assessment = f"🔥 ВИГІДНА УГОДА ({potential_profit_percent:.1f}% маржа)" if is_relevant else f"Ринкова Ціна ({potential_profit_percent:.1f}% маржа)"
        
        liquidity_data = calculate_liquidity_risk()

        final_result = ai_result.copy()
        final_result.update({
            "is_relevant": is_relevant, "type": "КОЛЕКЦІЙНИЙ ПРЕДМЕТ (RAV Model)",
            "estimated_value": rav_value, "deal_assessment": deal_assessment,
            "potential_profit_uah": potential_profit_uah, "risk_adjusted_value": rav_value,
            "market_data": external_data, "liquidity_risk": liquidity_data,
            "feedback_multiplier": feedback_multiplier # Додаємо для звіту
        })
        return final_result

    # --- 5.5. Головна функція аналізу OLX ---

    async def analyze_olx_item(self, session, item, spot_prices, bot: Optional[Bot]=None):
        """Визначає тип об'єкта, виконує Gemini Vision та запускає економічний аналіз."""
        olx_id, title, olx_price, image_url = item['olx_id'], item['title'], item['price'], item['image_url']
        
        is_machinery = any(word in title.lower() for word in ['верстат', 'чпу', 'прес', 'інструмент'])
        is_scrap_or_bullion = any(keyword in title.lower() for keyword in EconomicConfig.OLX_SCRAP_QUERIES)

        base64_image = await get_image_base64(session, image_url)
        
        # 1. СТРУКТУРА JSON ДЛЯ VISION
        analysis_schema = {
            "type": "OBJECT",
            "properties": {
                "is_correct_type": {"type": "BOOLEAN", "description": "Чи це дійсно цінний актив (а не сміття)"},
                "refined_keywords": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Ультра-спеціалізовані ключові слова для крос-референсу"},
                "ai_rarity_score": {"type": "INTEGER", "description": "Оцінка рідкісності від 1 до 100"},
                "estimated_weight": {"type": "NUMBER", "description": "Оціночна вага металу в грамах (для лому)"},
                "watch_details": {"type": "OBJECT", "properties": {"brand": {"type": "STRING"}, "model": {"type": "STRING"}, "condition_rating": {"type": "INTEGER", "description": "1-10"}, "authenticity_risk_percent": {"type": "INTEGER", "description": "Ризик підробки 0-100%"}}},
                "machinery_details": {"type": "OBJECT", "properties": {"manufacturer": {"type": "STRING"}, "model": {"type": "STRING"}, "year_of_manufacture": {"type": "INTEGER"}, "operating_hours": {"type": "INTEGER"}, "condition_rating": {"type": "INTEGER", "description": "1-10"}}}
            },
            "required": ["is_correct_type", "refined_keywords", "ai_rarity_score"]
        }
        
        # 2. ВИКЛИК GEMINI (Використовуємо Admin ID як user_id для загального OLX моніторингу)
        ai_vision_result = {}
        if base64_image:
            prompt = f"Проаналізуйте зображення. Заголовок: '{title}'. Ціна: {olx_price} UAH."
            # Тут використовуємо Admin ID, оскільки це фоновий воркер, а не запит від конкретного користувача
            ai_vision_result = await gemini_vision_analysis(
                session, prompt, base64_image, self.pool, ADMIN_ID, analysis_schema 
            ) or {}
            
        if not ai_vision_result.get('is_correct_type', True):
            return {"is_relevant": False, "type": "Відхилено AI Vision", "deal_assessment": "Не відповідає категорії"}
            
        # 3. ФІНАЛЬНА ОЦІНКА ТА ДИСПЕТЧЕРИЗАЦІЯ
        
        # TIV
        if is_machinery or ai_vision_result.get('machinery_details', {}).get('manufacturer'):
            return await self._analyze_machinery(title, olx_id, olx_price, ai_vision_result)
        
        # SPOT (Лом/Бульйон)
        elif is_scrap_or_bullion or ai_vision_result.get('estimated_weight'):
            return self._analyze_spot(olx_price, ai_vision_result, spot_prices)

        # RAV (Колекційні предмети, Годинники)
        else: 
            return await self._analyze_collectible(title, olx_id, olx_price, ai_vision_result)


# --- 6. OLX ТА POSTING ЛОГІКА ---

async def _fetch_single_query(session, pool, search_term, existing_ids):
    """Внутрішня функція для сканування однієї пошукової фрази."""
    posts_for_query = []
    # Використовуємо лише один пошуковий запит
    olx_search_url = f"https://www.olx.ua/d/uk/list/q-{search_term}/?currency=UAH&search%5Bfilter_float_price%3Afrom%5D={EconomicConfig.OLX_PRICE_FILTER}"
    
    # ... (Логіка скрейпінгу OLX)
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (TIV_RAV_Bot/9.0)'}
        async with session.get(olx_search_url, headers=headers, timeout=20) as response:
            response.raise_for_status()
            html = await response.text()
    except Exception as e:
        logger.error(f"Помилка при завантаженні OLX ({search_term}): {e}")
        return posts_for_query

    soup = BeautifulSoup(html, 'lxml')
    items = soup.find_all('div', {'data-cy': re.compile(r'l-card')})

    for item in items:
        olx_url_tag = item.find('a', {'data-cy': 'listing-ad-link'})
        if not olx_url_tag: continue
            
        full_url = urljoin(olx_search_url, olx_url_tag.get('href'))
        match = re.search(r'-ID(\d+)\.html', full_url)
        olx_id = match.group(1) if match else None
        
        if not olx_id or olx_id in existing_ids: continue

        title = item.find('h6').text.strip() if item.find('h6') else 'N/A'
        price_text = item.find('p', {'data-testid': 'price'}).text.strip() if item.find('p', {'data-testid': 'price'}) else '0 UAH'
        price_match = re.search(r'([\d\s]+)', price_text)
        price_uah = int("".join(price_match.group(1).split())) if price_match else 0
        
        img_tag = item.find('img')
        image_url = img_tag.get('src') if img_tag and 'src' in img_tag.attrs else None
        
        if price_uah < EconomicConfig.OLX_PRICE_FILTER: continue

        posts_for_query.append({
            'olx_id': olx_id,
            'title': title,
            'price': price_uah,
            'url': full_url,
            'image_url': image_url
        })
        
    return posts_for_query

async def fetch_olx_data(session, pool):
    """Асинхронно збирає нові оголошення з OLX, ітеруючи всі запити."""
    all_new_posts = []
    
    async with pool.acquire() as conn:
        existing_ids = await conn.fetchval("SELECT array_agg(olx_id) FROM olx_posts") or []

    search_queries = EconomicConfig.OLX_SEARCH_QUERIES + EconomicConfig.OLX_SCRAP_QUERIES
    
    # Використовуємо asyncio.gather для паралельного виконання запитів OLX
    tasks = [_fetch_single_query(session, pool, term, existing_ids) for term in search_queries]
    results = await asyncio.gather(*tasks)

    for posts in results:
        all_new_posts.extend(posts)
        
    # Видалення дублікатів (може бути, якщо товар потрапив у різні категорії пошуку)
    unique_posts = list({post['olx_id']: post for post in all_new_posts}.values())
        
    logger.info(f"Знайдено {len(unique_posts)} унікальних нових оголошень для моніторингу.")
    return unique_posts


def get_feedback_keyboard(olx_id):
    """Створює клавіатуру з кнопками Лайк/Не Лайк."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍 Лайк (Релевантно)", callback_data=f"fb_like_{olx_id}"),
            InlineKeyboardButton(text="👎 Не Лайк (Невідповідність)", callback_data=f"fb_dislike_{olx_id}")
        ]
    ])

def get_learning_keyboard():
    """Створює клавіатуру для навчання."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔁 Наступний Урок", callback_data="learn_next"),
            InlineKeyboardButton(text="🚫 Змінити Тему", callback_data="learn_change_topic")
        ]
    ])

async def send_olx_post(bot: Bot, item: Dict[str, Any], ai_result: Dict[str, Any]):
    """Форматує та відправляє професійний економічний звіт в Telegram."""
    
    olx_price_formatted = f"{item['price']:,}".replace(',', ' ')
    deal_assessment_text = ai_result.get('deal_assessment', 'Н/Д')
    
    post_type = ai_result.get('type', 'Н/Д')
    is_machinery_report = "ОБЛАДНАННЯ" in post_type
    is_spot_report = "SPOT" in post_type
    
    liquidity_data = ai_result.get('liquidity_risk', {})
    
    refined_keys = ", ".join(ai_result.get('refined_keywords', ['Н/Д']))
    ai_rarity_score = ai_result.get('ai_rarity_score', 'Н/Д')
    
    # НОВЕ: Коефіцієнт фідбеку
    fb_multiplier = ai_result.get('feedback_multiplier', 1.0)
    fb_text = f"**x{fb_multiplier:.2f}**"
    if fb_multiplier > 1.05:
        fb_text = f"**⬆️ {fb_text} (Підвищено)**"
    elif fb_multiplier < 0.95:
        fb_text = f"**⬇️ {fb_text} (Знижено)**"
    else:
        fb_text = f"**{fb_text} (Нейтрально)**"
    
    # --- СТРУКТУРА ЗВІТУ ---
    
    header = f"[{post_type.split('(')[0].strip()}] {item['title']}\n"
    header += f"**🚨 ОЦІНКА ВИГОДИ:** **{deal_assessment_text}**\n"
    header += f"💰 **Ціна OLX:** *{olx_price_formatted} UAH*\n"
    header += f"---------------------------------------\n"
    
    core_metrics = ""
    
    if is_machinery_report:
        # --- ПРОФЕСІЙНИЙ ЗВІТ ДЛЯ ОБЛАДНАННЯ (TIV) ---
        machinery_details = ai_result.get('machinery_details', {})
        tiv_formatted = f"{ai_result.get('risk_adjusted_value', 0):,}".replace(',', ' ')
        profit_uah_formatted = f"{ai_result.get('potential_profit_uah', 0):,}".replace(',', ' ')
        
        core_metrics = (
            f"**⚙️ ТЕХНІЧНІ ДАНІ (AI Vision)**\n"
            f"**Виробник/Модель:** `{machinery_details.get('manufacturer', 'Н/Д')} / {machinery_details.get('model', 'Н/Д')}`\n"
            f"**Рік/Напрацювання:** `{machinery_details.get('year_of_manufacture', 'Н/Д')} / {machinery_details.get('operating_hours', 'Н/Д')} год.`\n"
            f"**Візуальний Стан (1-10):** `{machinery_details.get('condition_rating', 'Н/Д')}/10`\n"
            f"---------------------------------------\n"
            f"**📈 ФІНАНСОВИЙ АНАЛІЗ (TIV)**\n"
            f"**TIV (Інв. Вартість):** `{tiv_formatted} UAH`\n"
            f"**Прогнозований Прибуток:** `{profit_uah_formatted} UAH`\n"
        )
    
    elif is_spot_report:
        # --- ЗВІТ ДЛЯ ЛОМУ/БУЛЬЙОНУ (SPOT) ---
        calculated_spot_value_formatted = f"{ai_result.get('calculated_spot_value', 0):,}".replace(',', ' ')
        premium_discount = ai_result.get('premium_discount_percent', 0)
        spot_price_per_gram = ai_result.get('spot_price_per_gram', 'Н/Д')
        estimated_weight_g = ai_result.get('estimated_weight_g', 'Н/Д')

        core_metrics = (
            f"**⚖️ МЕТАЛЕВИЙ АНАЛІЗ (SPOT)**\n"
            f"**Оціночна Вага (AI):** `{estimated_weight_g:.2f} г`\n"
            f"**Поточна Spot Ціна:** `{spot_price_per_gram:.2f} UAH/г`\n"
            f"**Справедлива Spot Value:** `{calculated_spot_value_formatted} UAH`\n"
            f"**Премія/Знижка:** `{premium_discount:.1f}%`\n"
        )

    else:
        # --- ЗВІТ ДЛЯ КОЛЕКЦІЙНИХ ПРЕДМЕТІВ (RAV) ---
        rav_formatted = f"{ai_result.get('risk_adjusted_value', 0):,}".replace(',', ' ')
        profit_uah_formatted = f"{ai_result.get('potential_profit_uah', 0):,}".replace(',', ' ')
        market_depth = ai_result.get('market_data', {}).get('market_depth', 'Н/Д')

        core_metrics = (
            f"🖼️ **КОЛЕКЦІЙНИЙ АНАЛІЗ (RAV)**\n"
            f"**RAV (Ризикована Вартість):** `{rav_formatted} UAH`\n"
            f"**Прогнозований Прибуток:** `{profit_uah_formatted} UAH`\n"
            f"**Rarity Score (AI/BCA):** `{ai_rarity_score}`\n"
            f"**Глибина Ринку (BCA):** `{market_depth} продажів`\n"
        )
        
    footer = (
        f"---------------------------------------\n"
        f"**🧠 AI KEYWORDS:** *{refined_keys}*\n"
        f"**🤖 КОЕФ. НАВЧАННЯ (FB):** {fb_text}\n" # Додано коефіцієнт
        f"**📉 РИЗИК ЛІКВІДНОСТІ:** *{liquidity_data.get('status', 'Н/Д')}* (ID: {liquidity_data.get('days_on_olx', 'Н/Д')} дн.)\n"
        f"[➡️ Перейти до оголошення]({item['url']})"
    )

    message_text = header + core_metrics + footer

    # --- ВІДПРАВКА ---
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


# --- 7. ХЕНДЛЕРИ КОМАНД ТА FSM ---

dp = Dispatcher()

@dp.message(Command("start"))
async def command_start_handler(message: types.Message, conn: asyncpg.Connection):
    """Обробляє команду /start та додає користувача в БД."""
    username = message.from_user.username or message.from_user.full_name
    try:
        await conn.execute('INSERT INTO users (user_id, username, joined_at) VALUES ($1, $2, $3) ON CONFLICT (user_id) DO UPDATE SET username = $2',
                           message.from_user.id, username, datetime.now(KYIV_TZ))
    except Exception as e:
        logger.error(f"Помилка додавання користувача: {e}")

    welcome_message = (
        "💎 **Ласкаво просимо на Професійну Аналітичну Платформу V10!** 🛠️\n\n"
        "Я - Ваш AI-помічник для пошуку високоцінних активів та інвестиційних угод.\n"
        "**Система V10 Адаптивно Навчається** на Вашому зворотньому зв'язку та еталонах Бази Знань.\n\n"
        "• **TIV Model:** Аналіз промислового обладнання.\n"
        "• **RAV Model:** Аналіз колекційних предметів (годинники, монети).\n\n"
        "**📚 НАВЧАЛЬНИЙ МОДУЛЬ (CollectorLearning)**\n"
        "• **/learn_collector:** Почніть свій шлях до становлення експертом-колекціонером!\n\n"
        "**⚙️ ІНШІ КОМАНДИ**\n"
        "• **/spot:** Перевірити поточні ринкові ціни.\n"
        "• **/settings:** Переглянути поточну конфігурацію системи.\n"
        "• **/analyze_cutlery:** Аналіз на вміст срібла.\n"
        "• **/base:** Додати еталонний зразок для навчання AI Vision."
    )
    await message.answer(welcome_message, parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("proposals"))
async def command_proposals_handler(message: types.Message):
    """Обробляє команду /proposals (високоризикові симуляції)."""
    proposal = generate_high_yield_proposal()
    await message.answer(proposal, parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("spot"))
async def command_spot_handler(message: types.Message):
    """Обробляє команду /spot для перевірки поточних ринкових цін."""
    spot_prices = EconomicConfig.SPOT_PRICES
    updated_at = datetime.fromtimestamp(spot_prices['LAST_UPDATED'], KYIV_TZ).strftime("%d.%m.%Y %H:%M")
    
    response = (
        "📊 **ПОТОЧНІ РИНКОВІ SPOT ЦІНИ (Симуляція)**\n\n"
        f"**🥇 Золото 585:** `{spot_prices['GOLD_585_UAH_PER_GRAM']:.2f} UAH/грам`\n"
        f"**🥈 Срібло 925:** `{spot_prices['SILVER_925_UAH_PER_GRAM']:.2f} UAH/грам`\n\n"
        f"*[Дані оновлено: {updated_at} (Київ)]*"
    )
    await message.answer(response, parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("settings"))
async def command_settings_handler(message: types.Message):
    """Обробляє команду /settings для перегляду конфігурації системи."""
    config = EconomicConfig
    
    settings_text = (
        "⚙️ **НАЛАШТУВАННЯ ПЛАТФОРМИ V10**\n\n"
        "**1. ЕКОНОМІЧНІ КОНСТАНТИ**\n"
        f"• Мінімальна Маржа: `{config.MIN_PROFIT_MARGIN_PERCENT}%`\n"
        f"• Комісія Перепродажу (Умовна): `{config.TRANSACTION_FEES_PERCENT}%`\n"
        f"• Мінімальний Пошуковий Поріг: `{config.OLX_PRICE_FILTER} UAH`\n\n"
        
        "**2. МОДЕЛЬ TIV (Обладнання)**\n"
        f"• Щорічна Амортизація: `{config.MACHINERY_DEPRECIATION_RATE * 100}%`\n"
        f"• Вага Стану у TIV: `{config.MACHINERY_CONDITION_WEIGHT * 100}%`\n\n"
        
        "**3. МОДЕЛЬ RAV (Колекційні)**\n"
        f"• Мінімальний Rarity Score: `{config.MIN_RARITY_SCORE}`\n"
        f"• Максимальний Ризик Автентичності: `{config.MAX_AUTHENTICITY_RISK}%`\n"
        f"• **Вплив Фідбеку (Корекція):** `{config.FEEDBACK_CORRECTION_MULTIPLIER * 100}%`\n" # Додано
        f"• Множники Престижу: {', '.join([f'{k.capitalize()}: {v}' for k, v in config.PRESTIGE_MULTIPLIERS.items()])}\n\n"
        
        "**4. OLX МОНІТОРИНГ (Запити)**\n"
        f"• Колекційні/Цінні: `{', '.join(config.OLX_SEARCH_QUERIES)}`\n"
        f"• Лом/Бульйон: `{', '.join(config.OLX_SCRAP_QUERIES)}`"
    )
    await message.answer(settings_text, parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("admin_status"))
async def command_admin_status_handler(message: types.Message, pool: asyncpg.Pool):
    """Адмін-статус: моніторинг БД та ефективності AI."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ **Відмовлено у доступі.** Ви не є адміністратором.")
        return

    async with pool.acquire() as conn:
        total_posts = await conn.fetchval("SELECT COUNT(*) FROM olx_posts")
        relevant_posts = await conn.fetchval("SELECT COUNT(*) FROM olx_posts WHERE is_relevant = TRUE")
        
        feedback_like = await conn.fetchval("SELECT COUNT(*) FROM user_feedback WHERE is_like = TRUE")
        feedback_dislike = await conn.fetchval("SELECT COUNT(*) FROM user_feedback WHERE is_like = FALSE")
        
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_base_records = await conn.fetchval("SELECT COUNT(*) FROM user_base") # Додано
        
        # НОВЕ: Середній коефіцієнт корекції
        avg_correction_factor = await conn.fetchval("""
            SELECT AVG( (ai_analysis_json->>'feedback_multiplier')::float ) 
            FROM olx_posts 
            WHERE ai_analysis_json ? 'feedback_multiplier'
        """)


    status_text = (
        "👑 **АДМІН-СТАТУС ПЛАТФОРМИ V10**\n\n"
        "**📊 МОНІТОРИНГ OLX/AI**\n"
        f"• Всього Проаналізовано Постів: `{total_posts}`\n"
        f"• Релевантних (Опубліковано): `{relevant_posts}` ({relevant_posts/total_posts * 100 if total_posts else 0:.1f}%) \n"
        f"• Сер. Коеф. Навчання (FB): `{avg_correction_factor:.3f}`\n\n"
        
        "**👍 ЗВОРОТНИЙ ЗВ'ЯЗОК (Для Навчання AI)**\n"
        f"• Лайків (Релевантність Підтверджена): `{feedback_like}`\n"
        f"• Дислайків (Невідповідність): `{feedback_dislike}`\n\n"
        
        "**👤 КОРИСТУВАЧІ ТА НАВЧАННЯ**\n"
        f"• Всього Користувачів: `{total_users}`\n"
        f"• Записів у Базі Знань: `{total_base_records}`"
    )
    await message.answer(status_text, parse_mode=ParseMode.MARKDOWN)

# --- НОВИЙ МОДУЛЬ НАВЧАННЯ (COLLECTOR LEARNING) ---

@dp.message(Command("learn_collector"))
async def command_start_learning(message: types.Message, state: FSMContext):
    """Початок інтерактивного навчання."""
    await state.clear()
    await state.set_state(LearningState.waiting_for_topic)
    
    suggested_topics = ['Оцінка рідкісних золотих монет', 'Ризики автентичності преміальних годинників', 'Оцінка зносу промислового обладнання']
    
    await message.answer(
        "📚 **КОЛЕКЦІОНЕР-ЕКСПЕРТ (Крок 1/3)**\n\n"
        "Напишіть, про яку тему інвестиційного колекціонування Ви б хотіли отримати детальний урок та вікторину:\n"
        f"Наприклад: `{suggested_topics[0]}`, `{suggested_topics[1]}` або `{suggested_topics[2]}`."
    )

@dp.message(LearningState.waiting_for_topic, F.text)
async def process_learning_topic(message: types.Message, state: FSMContext, session: aiohttp.ClientSession, pool: asyncpg.Pool):
    """Генерує та відправляє навчальний урок за темою."""
    topic = message.text.strip()
    await message.answer(f"⏳ Запускаю AI-куратора для генерації уроку по темі: **{topic}** (з урахуванням спірних активів)...", parse_mode=ParseMode.MARKDOWN)
    
    # Передаємо pool та user_id для адаптивної інструкції
    lesson_data = await generate_collector_lesson(session, topic, pool, message.from_user.id)
    
    if not lesson_data:
        await message.answer("❌ **Помилка генерації уроку.** Спробуйте змінити тему або повторіть пізніше.")
        await state.clear()
        return

    # Зберігаємо дані уроку для перевірки вікторини
    await state.update_data(
        topic=topic,
        quiz_answer=lesson_data['quiz_answer'],
        quiz_hint=lesson_data['quiz_hint']
    )
    
    lesson_message = (
        f"🎓 **УРОК: {lesson_data['lesson_title']}**\n\n"
        f"{lesson_data['content']}\n\n"
        f"---------------------------------------\n"
        f"❓ **ВІКТОРИНА:**\n"
        f"*{lesson_data['quiz_question']}* \n\n"
        f"Надішліть Вашу відповідь, щоб перевірити знання."
    )
    await message.answer(lesson_message, parse_mode=ParseMode.MARKDOWN)
    await state.set_state(LearningState.in_session)


@dp.message(LearningState.in_session, F.text)
async def process_quiz_answer(message: types.Message, state: FSMContext):
    """Перевіряє відповідь користувача на питання вікторини."""
    user_answer = message.text.strip().lower()
    data = await state.get_data()
    
    correct_answer = data['quiz_answer'].strip().lower()
    quiz_hint = data['quiz_hint']
    
    # Спрощена перевірка
    if correct_answer in user_answer or user_answer in correct_answer or user_answer == "так":
        feedback = (
            f"✅ **ПРАВИЛЬНО!** Ви чудово засвоїли матеріал по темі **{data['topic']}**.\n"
            f"**Правильна відповідь:** *{data['quiz_answer']}*\n\n"
            f"Ви на крок ближче до звання експерта!"
        )
    else:
        feedback = (
            f"❌ **НЕПРАВИЛЬНО.** Не хвилюйтесь, це складний матеріал.\n"
            f"**Підказка:** *{quiz_hint}*\n"
            f"**Правильна відповідь:** *{data['quiz_answer']}*\n\n"
            f"Спробуйте знову або перейдіть до наступного уроку."
        )
        
    await message.answer(feedback, parse_mode=ParseMode.MARKDOWN, reply_markup=get_learning_keyboard())
    await state.set_state(LearningState.waiting_for_next)

@dp.callback_query(LearningState.waiting_for_next, F.data.startswith('learn_'))
async def process_learning_callback(callback_query: types.CallbackQuery, state: FSMContext, pool: asyncpg.Pool, session: aiohttp.ClientSession):
    """Обробляє кнопки для продовження навчання."""
    await callback_query.answer() 
    
    if callback_query.data == 'learn_next':
        # Повторно запускаємо генерацію уроку з поточною темою
        data = await state.get_data()
        topic = data.get('topic')
        
        # Виклик генерації наступного уроку
        await callback_query.message.answer(f"⏳ Генерую наступний урок по темі: **{topic}**...", parse_mode=ParseMode.MARKDOWN)
        
        lesson_data = await generate_collector_lesson(session, topic, pool, callback_query.from_user.id)
        
        if not lesson_data:
            await callback_query.message.answer("❌ **Помилка генерації уроку.** Спробуйте змінити тему або повторіть пізніше.")
            await state.clear()
            return
            
        await state.update_data(
            topic=topic,
            quiz_answer=lesson_data['quiz_answer'],
            quiz_hint=lesson_data['quiz_hint']
        )
        
        lesson_message = (
            f"🎓 **УРОК: {lesson_data['lesson_title']}**\n\n"
            f"{lesson_data['content']}\n\n"
            f"---------------------------------------\n"
            f"❓ **ВІКТОРИНА:**\n"
            f"*{lesson_data['quiz_question']}* \n\n"
            f"Надішліть Вашу відповідь, щоб перевірити знання."
        )
        await callback_query.message.answer(lesson_message, parse_mode=ParseMode.MARKDOWN)
        await state.set_state(LearningState.in_session)

        
    elif callback_query.data == 'learn_change_topic':
        await state.clear()
        await command_start_learning(callback_query.message, state) # Повертаємось на початок
    
    await callback_query.message.edit_reply_markup(reply_markup=None) # Видаляємо кнопки

# (FSM handlers for /analyze_cutlery залишаються незмінними)

@dp.message(Command("analyze_cutlery"))
async def command_start_cutlery_analysis(message: types.Message, state: FSMContext):
    """Початок процесу аналізу столових приладів."""
    await state.set_state(CutleryAnalysis.waiting_for_url_or_description)
    await message.answer(
        "🔎 **Аналіз на Срібло (Проба Металу)**\n\n"
        "Будь ласка, надішліть **URL-адресу** продукту АБО **текстовий опис** (наприклад, 'Набір столових приладів срібло 925 проби')."
    )

@dp.message(CutleryAnalysis.waiting_for_url_or_description)
async def process_url_or_description(message: types.Message, state: FSMContext, session: aiohttp.ClientSession):
    """Обробляє введене посилання або опис та виконує аналіз срібла."""
    user_input = message.text.strip()
    await state.clear() 

    if re.match(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+', user_input):
        await message.answer(f"⏳ Запускаю симуляцію веб-скрейпінгу для посилання: `{user_input}`...", parse_mode=ParseMode.MARKDOWN)
        
        content = await fetch_page_content(session, user_input)
        if content:
            found_keywords = analyze_for_silver(content)
            if found_keywords:
                result_message = f"✅ **АНАЛІЗ ЗАВЕРШЕНО: СРІБЛО ВИЯВЛЕНО**\n\nЗнайдені ключові слова: *{', '.join(found_keywords)}*."
            else:
                result_message = "❌ **АНАЛІЗ ЗАВЕРШЕНО: СРІБЛО НЕ ВИЯВЛЕНО** (Ключові слова не знайдені)."
        else:
            result_message = "⚠️ **ПОМИЛКА Скрейпінгу**"
    
    else:
        await message.answer("✍️ Проводжу швидкий аналіз наданого тексту...")
        found_keywords = analyze_for_silver(user_input)
        if found_keywords:
            result_message = f"✅ **ТЕКСТОВИЙ АНАЛІЗ: ІМОВІРНО СРІБЛО**\n\nЗнайдені ключові слова: *{', '.join(found_keywords)}*."
        else:
            result_message = "❌ **ТЕКСТОВИЙ АНАЛІЗ: СРІБЛО НЕ ЗНАЙДЕНО**"

    await message.answer(result_message, parse_mode=ParseMode.MARKDOWN)

# (FSM handlers for /base. Змінено: передаємо pool та user_id у vision_analysis)

@dp.message(Command("base"))
async def command_base_handler(message: types.Message, state: FSMContext):
    await state.clear() 
    await message.answer("**📚 Додавання до Бази Знань (Крок 1/2):** Надішліть еталонне зображення цінного предмета. Це покращить роботу AI Vision спеціально для Вас.")
    await state.set_state(BaseForm.waiting_for_photo)

@dp.message(BaseForm.waiting_for_photo, F.photo)
async def handle_base_photo(message: types.Message, state: FSMContext, bot: Bot, session: aiohttp.ClientSession, pool: asyncpg.Pool):
    photo_file_id = message.photo[-1].file_id     
    base64_image = await get_image_base64(session, None, bot, photo_file_id)
    if not base64_image:
        await message.answer("❌ Не вдалося завантажити зображення або файл завеликий.")
        await state.clear()
        return
        
    analysis_schema = {"type": "OBJECT", "properties": {"title": {"type": "STRING"}, "keywords": {"type": "ARRAY", "items": {"type": "STRING"}}, "estimated_value_text": {"type": "STRING"}}}
    
    # Використовуємо _get_adaptive_system_instruction для Base
    prompt = "Проаналізуйте це еталонне зображення. Згенеруйте назву, ключові слова та діапазон вартості."
    ai_base_analysis = await gemini_vision_analysis(session, prompt, base64_image, pool, message.from_user.id, analysis_schema) or {}
    
    ai_keywords = ", ".join(ai_base_analysis.get('keywords', []))
    ai_title = ai_base_analysis.get('title', "Н/Д")
    ai_value = ai_base_analysis.get('estimated_value_text', "Н/Д")

    await state.update_data(photo_file_id=photo_file_id, user_id=message.from_user.id, ai_keywords=ai_keywords, ai_title=ai_title, ai_value=ai_value)
    
    base_text = (
        f"**✅ Зображення отримано (Крок 2/2).**\n\n"
        f"**🔥 AI Vision Згенерував:**\n"
        f"Назва: `{ai_title}`\n"
        f"Ключові Слова: `{ai_keywords}`\n"
        f"Оцінка: `{ai_value}`\n\n"
        f"Підтвердьте або відредагуйте цей опис (надішліть остаточний текст)."
    )
    await message.answer(base_text, parse_mode=ParseMode.MARKDOWN)
    await state.set_state(BaseForm.waiting_for_text)

@dp.message(BaseForm.waiting_for_text, F.text)
async def handle_base_text(message: types.Message, state: FSMContext, pool: asyncpg.Pool):
    data = await state.get_data()
    # Використовуємо текст користувача як остаточний опис
    final_text = message.text.strip()
    
    # Витягуємо дані з AI, якщо користувач не надав власних
    final_title = data.get('ai_title', final_text.split('\n')[0].strip())
    final_keywords = data.get('ai_keywords', final_title)
    final_value_text = data.get('ai_value', final_keywords)
        
    # Пріоритет: Якщо текст містить схожу інформацію, використовуємо його
    if len(final_text) > 50:
        lines = final_text.split('\n')
        final_title = lines[0].strip()
        final_keywords = final_text 
        final_value_text = final_text 
        
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_base (user_id, title, keywords, estimated_value_text, image_url) VALUES ($1, $2, $3, $4, $5)",
            data['user_id'], final_title, final_keywords, final_value_text, data['photo_file_id']
        )
    await message.answer(f"✅ Еталон `{final_title}` успішно додано до Бази Знань! Це зробить Ваші майбутні аналізи ще точнішими.")
    await state.clear()

@dp.callback_query(lambda c: c.data and c.data.startswith('fb_'))
async def process_callback_feedback(callback_query: types.CallbackQuery, pool: asyncpg.Pool):
    """Обробка зворотного зв'язку від користувачів."""
    try:
        action = callback_query.data.split('_')[1]
        olx_id = callback_query.data.split('_')[2]
        is_like = (action == 'like')
        
        async with pool.acquire() as conn:
            # Зберігаємо фідбек
            await conn.execute("INSERT INTO user_feedback (user_id, olx_id, is_like) VALUES ($1, $2, $3)",
                               callback_query.from_user.id, olx_id, is_like)
            
            # НОВЕ: Оновлюємо коефіцієнт корекції в самому olx_posts після фідбеку
            correction_factor = await _get_feedback_correction_factor(pool, olx_id)
            
            # Оновлюємо запис у olx_posts (це для відображення в адмін-статусі)
            await conn.execute("""
                UPDATE olx_posts 
                SET ai_analysis_json = jsonb_set(ai_analysis_json, '{feedback_multiplier}', $1::jsonb)
                WHERE olx_id = $2
            """, json.dumps(correction_factor), olx_id)
            
        await callback_query.answer(f"Дякуємо за відгук! {'👍' if is_like else '👎'} Коефіцієнт навчання оновлено!")
        
        # Видаляємо клавіатуру після натискання
        await callback_query.message.edit_reply_markup(reply_markup=None) 
        
    except Exception as e:
        logger.error(f"Помилка обробки зворотного зв'язку: {e}")
        await callback_query.answer("Помилка обробки відгуку.")


# --- 8. РОБОЧИЙ ВОРКЕР (MONITORING WORKER) ---

async def monitoring_worker(bot: Bot, pool: asyncpg.Pool, economic_engine: EconomicEngine, session: aiohttp.ClientSession, interval=600):
    """Фоновий воркер: моніторинг OLX та RSS-стрічок."""
    logger.info(f"Воркер моніторингу запущено. Інтервал: {interval} сек.")
    
    while True:
        try:
            spot_prices = EconomicConfig.SPOT_PRICES # Фіксовані на добу
            
            # 1. СКАНУВАННЯ OLX
            new_posts = await fetch_olx_data(session, pool)
                
            async with pool.acquire() as conn:
                    
                for post in new_posts:
                    try:
                        ai_result = await economic_engine.analyze_olx_item(
                            session, post, spot_prices, bot
                        )
                    except Exception as e:
                        logger.error(f"Помилка аналізу OLX item {post['olx_id']}: {e}")
                        ai_result = {"is_relevant": False, "type": "Помилка Аналізу", "deal_assessment": f"Критична помилка: {e}"}

                    is_relevant = ai_result.get('is_relevant', False)
                    
                    # 2. ПУБЛІКАЦІЯ ТА ЗБЕРЕЖЕННЯ
                    await conn.execute(
                        "INSERT INTO olx_posts (olx_id, title, price, published_at, ai_analysis_json, is_relevant) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (olx_id) DO NOTHING",
                        post['olx_id'], post['title'], post['price'], datetime.now(KYIV_TZ), json.dumps(ai_result), is_relevant
                    )
                    
                    if is_relevant and CHANNEL_ID:
                        await send_olx_post(bot, post, ai_result)
                        # Затримка між публікаціями
                        await asyncio.sleep(5) 

                # 3. МОНІТОРИНГ RSS ФІНАНСОВИХ СТРІЧОК (Спрощена реалізація)
                for feed_name, feed_url in EconomicConfig.RSS_FEEDS.items():
                    content = await fetch_page_content(session, feed_url)
                    if content:
                        feed = feedparser.parse(content)
                        for entry in feed.entries[:1]: # Обробка 1 останньої
                            title = entry.get('title', 'Без заголовку')
                            link = entry.get('link', '#')
                            
                            is_silver_related = any(kw in (title).lower() for kw in EconomicConfig.SILVER_KEYWORDS)
                            
                            if is_silver_related and CHANNEL_ID:
                                post_text = f"🔔 **[ЕЛІТНИЙ АНАЛІЗ]** Знайдено новину, пов'язану зі сріблом/металами:\n\n📰 **{title}**\n[Читати більше]({link})"
                                await bot.send_message(
                                    chat_id=CHANNEL_ID, text=post_text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
                                )
                                logger.info(f"Опубліковано новину про срібло: {title}")
                                await asyncio.sleep(2)


        except Exception as e:
            logger.error(f"Глобальна помилка у воркері моніторингу: {e}")
                
        await asyncio.sleep(interval) 


# --- 9. ГОЛОВНА ФУНКЦІЯ ---

async def main():
    """Ініціалізація та запуск бота."""
    
    if not BOT_TOKEN or not DATABASE_URL or CHANNEL_ID is None:
        logger.critical("Критична помилка: Не знайдено BOT_TOKEN, DATABASE_URL або CHANNEL_ID.")
        # Використання змінних з .env для ініціалізації
        print(f"DEBUG: BOT_TOKEN={bool(BOT_TOKEN)}, DATABASE_URL={bool(DATABASE_URL)}, CHANNEL_ID={CHANNEL_ID}")
        sys.exit(1)
        
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
    except Exception as e:
        logger.critical(f"Критична помилка підключення до БД: {e}")
        sys.exit(1)
        
    await init_db(pool)
    
    # Використання DefaultBotProperties для чистоти коду
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    
    # Використання Context Managers для aiohttp.ClientSession
    async with aiohttp.ClientSession() as session:
        economic_engine = EconomicEngine(pool)

        # Реєстрація залежностей через Middleware
        # Передаємо session, pool, economic_engine, bot (якщо потрібно для FSM)
        dp.message.outer_middleware.register(lambda handler, event, data: {**data, 'session': session, 'pool': pool, 'conn': pool, 'bot': bot, 'economic_engine': economic_engine})
        dp.callback_query.outer_middleware.register(lambda handler, event, data: {**data, 'pool': pool, 'bot': bot, 'session': session}) # Додано session для callback
        
        # Перевірка та сповіщення про успішний запуск
        if CHANNEL_ID and CHANNEL_ID != ADMIN_ID: # Не дублювати сповіщення, якщо канал і адмін ID однакові
             await bot.send_message(
                chat_id=CHANNEL_ID,
                text="🤖 **Платформа V10 (Unified & Adaptive) запущена!** 🛠️✨ Фоновий моніторинг активів розпочато. **Впроваджено адаптивне навчання AI.**",
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Запуск воркера моніторингу та опитування
        tasks = [
            asyncio.create_task(monitoring_worker(bot, pool, economic_engine, session)),
            dp.start_polling(bot)
        ]

        logger.info("Бот запущено. Починаю опитування...")
        await asyncio.gather(*tasks)

    await pool.close()


if __name__ == "__main__":
    try:
        # Використання asyncio.run з try/except для коректної обробки
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот зупинено вручну (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Головна помилка виконання: {e}")