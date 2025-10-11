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

KYIV_TZ = timezone(timedelta(hours=3), 'Europe/Kyiv')

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    stream=sys.stdout)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

try:
    channel_env_var = os.getenv("CHANNEL_ID") or os.getenv("channel_ID") 
    CHANNEL_ID = int(channel_env_var) if channel_env_var else None
    ADMIN_ID = int(os.getenv("ADMIN_ID", CHANNEL_ID or 0)) 
except (TypeError, ValueError):
    CHANNEL_ID = None
    ADMIN_ID = 0
    logger.error("CHANNEL_ID або ADMIN_ID не знайдено або має некоректний формат.")

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent"
random.seed(42)

class EconomicConfig:
    
    OLX_SEARCH_QUERIES = [ 
        'чоловічий годинник rolex', 'верстат чпу промисловий', 'монета 5 рублів золото', 
        'царське срібло', 'ікона срібло', 'зварювальний апарат kemppi' 
    ]
    OLX_SCRAP_QUERIES = [ 
        'золото лом 585', 'срібло лом 925', 'золотий злиток' 
    ]
    OLX_PRICE_FILTER = 20000 
    
    TRANSACTION_FEES_PERCENT = 10  
    MIN_PROFIT_MARGIN_PERCENT = 20 
    
    MACHINERY_DEPRECIATION_RATE = 0.05  
    MACHINERY_CONDITION_WEIGHT = 0.4    
    MACHINERY_HOURS_PENALTY_RATE = 0.000005 
    MIN_RARITY_SCORE = 50 
    MAX_AUTHENTICITY_RISK = 30
    PRESTIGE_MULTIPLIERS = {'rolex': 1.5, 'patek philippe': 1.8, 'omega': 1.3}
    FEEDBACK_CORRECTION_MULTIPLIER = 0.1 

    SPOT_PRICES: Dict[str, float] = {
        "GOLD_585_UAH_PER_GRAM": 2850.0, 
        "SILVER_925_UAH_PER_GRAM": 48.5, 
        "LAST_UPDATED": datetime.now(KYIV_TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    }
    
    SILVER_KEYWORDS: List[str] = [
        "срібло", "срібний", "Ag 925", "sterling silver", "800 проба", "925 проба", "посріблений", "мельхіор"
    ]
    
    RSS_FEEDS: Dict[str, str] = {
        "UKR Finance News": "https://www.rbc.ua/static/rss/news.ukr.rss", 
        "Metals Market News": "https://www.google.com/search?q=ціни+на+срібло+новини&tbm=nws&output=rss"
    }

class BaseForm(StatesGroup):
    waiting_for_photo = State()
    waiting_for_text = State()

class CutleryAnalysis(StatesGroup):
    waiting_for_url_or_description = State()

class LearningState(StatesGroup):
    waiting_for_topic = State()
    in_session = State()
    waiting_for_next = State()

async def init_db(pool: asyncpg.Pool):
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
    try:
        parsed_url = urlparse(url)
        if not all([parsed_url.scheme in ['http', 'https'], parsed_url.netloc]): 
             logger.warning(f"Некоректний URL: {url}")
             return None

        headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
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
            if len(image_data) > 4 * 1024 * 1024:
                logger.warning("Зображення занадто велике (>4MB). Пропускаємо Vision Analysis.")
                return None
            return base64.b64encode(image_data).decode('utf-8')
    except Exception as e:
        logger.error(f"Помилка завантаження зображення з {url}: {e}")
        return None

async def gemini_api_call(session: aiohttp.ClientSession, payload: Dict[str, Any]):
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

async def _get_adaptive_system_instruction(pool: asyncpg.Pool, user_id: int, is_vision: bool = True) -> str:
    async with pool.acquire() as conn:
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
            
        if not is_vision: 
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
    if not image_base64: return None
    
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
    if '<html' in content.lower():
        soup = BeautifulSoup(content, 'html.parser')
        text = soup.get_text()
    else:
        text = content
        
    normalized_text = text.lower()
    found_keywords = []
    
    for keyword in EconomicConfig.SILVER_KEYWORDS:
        if re.search(r'\b' + re.escape(keyword.lower()) + r'\b', normalized_text) or keyword.lower() in normalized_text:
            if keyword not in found_keywords:
                found_keywords.append(keyword)
                
    return found_keywords

def generate_high_yield_proposal() -> str:
    asset_list = ["Технологічний ETF (Симуляція)", "Рідкісні метали (Симуляція)", "Акції 'Зеленої' Енергетики (Симуляція)", "Ф'ючерси на екзотичний товар (Симуляція)"]
    asset = random.choice(asset_list)
    risk = random.choice(["Екстремально високий", "Надзвичайно високий", "Високий, не для новачків"])
    duration = random.choice(["3 місяці", "6 місяців", "12 місяців"])
    simulated_return = random.randint(400, 650)
    
    return (
        f"🚨 **ЕЛІТНА ВИСОКОРИЗИКОВА СИМУЛЯЦІЙНА ПРОПОЗИЦІЯ** 🚨\n\n"
        f"📈 **Потенційний Прибуток (Симуляція):** `{simulated_return}%`\n"
        f"🎯 **Актив:** *{asset}*\n"
        f"⏳ **Обрій:** {duration}\n" 
        f"⚠️ **Рівень Ризику:** *{risk}*\n\n"
        f"📝 **Аналіз:** Це симульована 'пропозиція', що відображає гіпотетичну ситуацію "
        f"на екзотичних ринках. Такий прибуток можливий лише у разі прийняття "
        f"надзвичайно високих ризиків, включаючи повну втрату капіталу.\n\n"
        f"❌ **ВІДМОВА ВІД ВІДПОВІДАЛЬНОСТІ:** *Це не фінансова порада. "
        f"Потенційні {simulated_return}% є симуляцією. Інвестування "
        f"пов'язане з високими ризиками.*"
    )

def calculate_liquidity_risk() -> Dict[str, Any]:
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

async def _get_feedback_correction_factor(pool: asyncpg.Pool, olx_id: str) -> float:
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
            score = (likes - dislikes) / total_feedback
            return 1.0 + (score * EconomicConfig.FEEDBACK_CORRECTION_MULTIPLIER)
        
        return 1.0 


def _simulate_olx_post(query: str) -> Dict[str, Any]:
    base_id = datetime.now(KYIV_TZ).timestamp() + random.random()
    price = random.randint(25000, 150000)
    
    if 'rolex' in query.lower() or 'годинник' in query.lower():
        title = f"Годинник Rolex Submariner (СИМУЛЯЦІЯ, ID: {int(base_id) % 100})"
        image = "https://i.imgur.com/example-rolex.jpg" 
        price = random.randint(80000, 200000)
    elif 'верстат' in query.lower() or 'чпу' in query.lower():
        title = f"Промисловий ЧПУ-Верстат Haas (СИМУЛЯЦІЯ, ID: {int(base_id) % 100})"
        price = random.randint(150000, 500000)
        image = "https://i.imgur.com/example-cnc.jpg" 
    elif 'золото' in query.lower() or 'срібло' in query.lower():
        title = f"Золотий Злиток 585 / Монета (СИМУЛЯЦІЯ, ID: {int(base_id) % 100})"
        image = "https://i.imgur.com/example-gold.jpg"
    else:
        title = f"Цінний Колекційний Актив '{query}' (СИМУЛЯЦІЯ, ID: {int(base_id) % 100})"
        image = "https://i.imgur.com/example-asset.jpg" 
        
    return {
        'olx_id': f"SIM_{int(base_id * 1000)}", 
        'title': title,
        'price': price,
        'url': "https://www.olx.ua/simulated-post",
        'image_url': image
    }

class EconomicEngine:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.current_year = datetime.now(KYIV_TZ).year

    async def _fetch_external_auction_data(self, refined_keywords):
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

    async def _analyze_machinery(self, title, olx_id, olx_price, ai_result):
        machinery_details = ai_result.get('machinery_details', {})
        refined_keywords = ai_result.get('refined_keywords', [title])
        external_data = await self._fetch_external_auction_data(refined_keywords)
        avg_sale_price = external_data.get('average_sale_price', olx_price * random.uniform(1.2, 2.0)) 
        
        feedback_multiplier = await _get_feedback_correction_factor(self.pool, olx_id)
        
        year = machinery_details.get('year_of_manufacture', self.current_year - 5)
        condition = machinery_details.get('condition_rating', 5)
        hours = machinery_details.get('operating_hours', 3000)
        
        age = max(0, self.current_year - year)
        depreciation_factor = min(0.9, age * EconomicConfig.MACHINERY_DEPRECIATION_RATE)
        value_after_age = avg_sale_price * (1 - depreciation_factor)

        condition_multiplier = (condition / 10) * EconomicConfig.MACHINERY_CONDITION_WEIGHT + (1 - EconomicConfig.MACHINERY_CONDITION_WEIGHT)
        hours_penalty_uah = hours * EconomicConfig.MACHINERY_HOURS_PENALTY_RATE * avg_sale_price 
        
        tiv_value_raw = round(value_after_age * condition_multiplier - hours_penalty_uah)
        
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
            "potential_profit_uah": potential_profit_uah, "risk_adjusted_value": tiv_value,
            "market_data": external_data, "liquidity_risk": liquidity_data,
            "feedback_multiplier": feedback_multiplier 
        })
        return final_result

    def _analyze_spot(self, olx_price, ai_result, spot_prices):
        is_gold = any(word in ai_result.get('refined_keywords', []) for word in ['золото', '585'])
        
        if is_gold:
            spot_price_per_gram = spot_prices['GOLD_585_UAH_PER_GRAM']
            metal_type = "Золото 585"
        else: 
            spot_price_per_gram = spot_prices['SILVER_925_UAH_PER_GRAM']
            metal_type = "Срібло 925"

        estimated_weight_raw = ai_result.get('estimated_weight', 50) 
        
        implied_spot_value = estimated_weight_raw * spot_price_per_gram
        if olx_price > implied_spot_value * 5: 
            estimated_weight_g = max(1, estimated_weight_raw / 10) 
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

    async def _analyze_collectible(self, title, olx_id, olx_price, ai_result):
        refined_keywords = ai_result.get('refined_keywords', [title])
        external_data = await self._fetch_external_auction_data(refined_keywords)
        
        feedback_multiplier = await _get_feedback_correction_factor(self.pool, olx_id)
        
        ai_rarity = ai_result.get('ai_rarity_score', external_data.get('rarity_score', 50))
        watch_details = ai_result.get('watch_details', {})
        authenticity_risk = watch_details.get('authenticity_risk_percent', 0)
        condition_rating = watch_details.get('condition_rating', 8)
        
        avg_auction_price = external_data.get('average_sale_price', olx_price * random.uniform(1.5, 3.0)) 
        
        rarity_factor = (ai_rarity / 100)
        condition_factor = condition_rating / 10
        authenticity_penalty = (1 - authenticity_risk / 100)

        prestige_multiplier = 1.0
        for brand, mult in EconomicConfig.PRESTIGE_MULTIPLIERS.items():
            if brand in title.lower():
                prestige_multiplier = mult
                break
        
        rav_value_raw = round(avg_auction_price * rarity_factor * condition_factor * authenticity_penalty * prestige_multiplier)
        
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
            "feedback_multiplier": feedback_multiplier 
        })
        return final_result


    async def analyze_olx_item(self, session, item, spot_prices, bot: Optional[Bot]=None):
        olx_id, title, olx_price, image_url = item['olx_id'], item['title'], item['price'], item['image_url']
        
        if olx_id.startswith('SIM_'):
             logger.info(f"Аналіз симульованого посту: {title}")
             if 'rolex' in title.lower() or 'годинник' in title.lower():
                 ai_vision_result = {'is_correct_type': True, 'refined_keywords': [title], 'ai_rarity_score': 85, 'watch_details': {'brand': 'Rolex', 'condition_rating': 9, 'authenticity_risk_percent': 10}}
             elif 'чпу' in title.lower() or 'верстат' in title.lower():
                 ai_vision_result = {'is_correct_type': True, 'refined_keywords': [title], 'ai_rarity_score': 60, 'machinery_details': {'year_of_manufacture': 2020, 'operating_hours': 1500, 'condition_rating': 9}}
             elif 'золотий' in title.lower() or 'срібло' in title.lower():
                 ai_vision_result = {'is_correct_type': True, 'refined_keywords': [title], 'ai_rarity_score': 50, 'estimated_weight': 20}
             else:
                  ai_vision_result = {'is_correct_type': True, 'refined_keywords': [title], 'ai_rarity_score': 70}

        else:
             base64_image = await get_image_base64(session, image_url)
             
             analysis_schema = {
                 "type": "OBJECT", "properties": { 
                     "is_correct_type": {"type": "BOOLEAN"},"refined_keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
                     "ai_rarity_score": {"type": "INTEGER"},"estimated_weight": {"type": "NUMBER"},
                     "watch_details": {"type": "OBJECT", "properties": {"brand": {"type": "STRING"}, "model": {"type": "STRING"}, "condition_rating": {"type": "INTEGER"}, "authenticity_risk_percent": {"type": "INTEGER"}}},
                     "machinery_details": {"type": "OBJECT", "properties": {"manufacturer": {"type": "STRING"}, "model": {"type": "STRING"}, "year_of_manufacture": {"type": "INTEGER"}, "operating_hours": {"type": "INTEGER"}, "condition_rating": {"type": "INTEGER"}}}
                 }, "required": ["is_correct_type", "refined_keywords", "ai_rarity_score"]
             }
             
             ai_vision_result = {}
             if base64_image:
                 prompt = f"Проаналізуйте зображення. Заголовок: '{title}'. Ціна: {olx_price} UAH."
                 ai_vision_result = await gemini_vision_analysis(
                     session, prompt, base64_image, self.pool, ADMIN_ID, analysis_schema 
                 ) or {}
            
        if not ai_vision_result.get('is_correct_type', True):
            return {"is_relevant": False, "type": "Відхилено AI Vision", "deal_assessment": "Не відповідає категорії"}
            
        is_machinery = any(word in title.lower() for word in ['верстат', 'чпу', 'прес', 'інструмент'])
        is_scrap_or_bullion = any(keyword in title.lower() for keyword in EconomicConfig.OLX_SCRAP_QUERIES)

        if is_machinery or ai_vision_result.get('machinery_details', {}).get('manufacturer'):
            return await self._analyze_machinery(title, olx_id, olx_price, ai_vision_result)
        
        elif is_scrap_or_bullion or ai_vision_result.get('estimated_weight'):
            return self._analyze_spot(olx_price, ai_vision_result, spot_prices)

        else: 
            return await self._analyze_collectible(title, olx_id, olx_price, ai_vision_result)


async def _fetch_single_query(session, pool, search_term, existing_ids):
    posts_for_query = []
    olx_search_url = f"https://www.olx.ua/d/uk/list/q-{search_term}/?currency=UAH&search%5Bfilter_float_price%3Afrom%5D={EconomicConfig.OLX_PRICE_FILTER}"
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        async with session.get(olx_search_url, headers=headers, timeout=20) as response:
            
            if response.status == 403: 
                logger.error(f"Помилка HTTP 403 (Forbidden) при завантаженні OLX ({search_term}). Ймовірно, OLX заблокував запит.")
                return posts_for_query 
            
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
    all_new_posts = []
    
    async with pool.acquire() as conn:
        existing_ids = await conn.fetchval("SELECT array_agg(olx_id) FROM olx_posts") or []

    search_queries = EconomicConfig.OLX_SEARCH_QUERIES + EconomicConfig.OLX_SCRAP_QUERIES
    
    tasks = [_fetch_single_query(session, pool, term, existing_ids) for term in search_queries]
    results = await asyncio.gather(*tasks)

    for posts in results:
        all_new_posts.extend(posts)
        
    unique_posts = list({post['olx_id']: post for post in all_new_posts}.values())
    
    if not unique_posts:
        logger.warning("OLX скрейпінг не приніс реальних результатів. АКТИВОВАНО РЕЖИМ СИМУЛЯЦІЇ (5 постів).")
        simulated_queries = random.sample(search_queries, min(5, len(search_queries)))
        for query in simulated_queries:
             sim_post = _simulate_olx_post(query)
             if sim_post['olx_id'] not in existing_ids and sim_post['olx_id'] not in [p['olx_id'] for p in unique_posts]:
                 unique_posts.append(sim_post)
        
    logger.info(f"Знайдено {len(unique_posts)} унікальних нових оголошень для моніторингу (включаючи симуляцію).")
    return unique_posts

def get_feedback_keyboard(olx_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍 Лайк (Релевантно)", callback_data=f"fb_like_{olx_id}"),
            InlineKeyboardButton(text="👎 Не Лайк (Невідповідність)", callback_data=f"fb_dislike_{olx_id}")
        ]
    ])

def get_learning_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔁 Наступний Урок", callback_data="learn_next"),
            InlineKeyboardButton(text="🚫 Змінити Тему", callback_data="learn_change_topic")
        ]
    ])

async def send_olx_post(bot: Bot, item: Dict[str, Any], ai_result: Dict[str, Any]):
    olx_price_formatted = f"{item['price']:,}".replace(',', ' ')
    deal_assessment_text = ai_result.get('deal_assessment', 'Н/Д')
    
    post_type = ai_result.get('type', 'Н/Д')
    is_machinery_report = "ОБЛАДНАННЯ" in post_type
    is_spot_report = "SPOT" in post_type
    
    liquidity_data = ai_result.get('liquidity_risk', {})
    
    refined_keys = ", ".join(ai_result.get('refined_keywords', ['Н/Д']))
    ai_rarity_score = ai_result.get('ai_rarity_score', 'Н/Д')
    
    fb_multiplier = ai_result.get('feedback_multiplier', 1.0)
    fb_text = f"**x{fb_multiplier:.2f}**"
    if fb_multiplier > 1.05:
        fb_text = f"**⬆️ {fb_text} (Підвищено)**"
    elif fb_multiplier < 0.95:
        fb_text = f"**⬇️ {fb_text} (Знижено)**"
    else:
        fb_text = f"**{fb_text} (Нейтрально)**"
    
    header = f"[{post_type.split('(')[0].strip()}] {item['title']}\n"
    if item['olx_id'].startswith('SIM_'):
        header += "⚠️ **Це симульований пост (OLX недоступний)**\n"
        
    header += f"**🚨 ОЦІНКА ВИГОДИ:** **{deal_assessment_text}**\n"
    header += f"💰 **Ціна OLX:** *{olx_price_formatted} UAH*\n"
    header += f"---------------------------------------\n"
    
    core_metrics = ""
    
    if is_machinery_report:
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
        f"**🤖 КОЕФ. НАВЧАННЯ (FB):** {fb_text}\n" 
        f"**📉 РИЗИК ЛІКВІДНОСТІ:** *{liquidity_data.get('status', 'Н/Д')}* (ID: {liquidity_data.get('days_on_olx', 'Н/Д')} дн.)\n"
        f"[➡️ Перейти до оголошення]({item['url']})"
    )

    message_text = header + core_metrics + footer

    try:
        image_to_send = item['image_url'] if not item['olx_id'].startswith('SIM_') else "https://i.imgur.com/example-asset.jpg" 

        if image_to_send:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image_to_send,
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

dp = Dispatcher()

@dp.message(Command("start"))
async def command_start_handler(message: types.Message, conn: asyncpg.Connection):
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
    proposal = generate_high_yield_proposal()
    await message.answer(proposal, parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("spot"))
async def command_spot_handler(message: types.Message):
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
        f"• **Вплив Фідбеку (Корекція):** `{config.FEEDBACK_CORRECTION_MULTIPLIER * 100}%`\n"
        f"• Множники Престижу: {', '.join([f'{k.capitalize()}: {v}' for k, v in config.PRESTIGE_MULTIPLIERS.items()])}\n\n"
        
        "**4. OLX МОНІТОРИНГ (Запити)**\n"
        f"• Колекційні/Цінні: `{', '.join(config.OLX_SEARCH_QUERIES)}`\n"
        f"• Лом/Бульйон: `{', '.join(config.OLX_SCRAP_QUERIES)}`"
    )
    await message.answer(settings_text, parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("admin_status"))
async def command_admin_status_handler(message: types.Message, pool: asyncpg.Pool):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ **Відмовлено у доступі.** Ви не є адміністратором.")
        return

    async with pool.acquire() as conn:
        total_posts = await conn.fetchval("SELECT COUNT(*) FROM olx_posts")
        relevant_posts = await conn.fetchval("SELECT COUNT(*) FROM olx_posts WHERE is_relevant = TRUE")
        
        feedback_like = await conn.fetchval("SELECT COUNT(*) FROM user_feedback WHERE is_like = TRUE")
        feedback_dislike = await conn.fetchval("SELECT COUNT(*) FROM user_feedback WHERE is_like = FALSE")
        
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_base_records = await conn.fetchval("SELECT COUNT(*) FROM user_base")
        
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

@dp.message(Command("learn_collector"))
async def command_start_learning(message: types.Message, state: FSMContext):
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
    topic = message.text.strip()
    await message.answer(f"⏳ Запускаю AI-куратора для генерації уроку по темі: **{topic}** (з урахуванням спірних активів)...", parse_mode=ParseMode.MARKDOWN)
    
    lesson_data = await generate_collector_lesson(session, topic, pool, message.from_user.id)
    
    if not lesson_data:
        await message.answer("❌ **Помилка генерації уроку.** Спробуйте змінити тему або повторіть пізніше.")
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
    await message.answer(lesson_message, parse_mode=ParseMode.MARKDOWN)
    await state.set_state(LearningState.in_session)


@dp.message(LearningState.in_session, F.text)
async def process_quiz_answer(message: types.Message, state: FSMContext):
    user_answer = message.text.strip().lower()
    data = await state.get_data()
    
    correct_answer = data['quiz_answer'].strip().lower()
    quiz_hint = data['quiz_hint']
    
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
    await callback_query.answer() 
    
    if callback_query.data == 'learn_next':
        data = await state.get_data()
        topic = data.get('topic')
        
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
        await command_start_learning(callback_query.message, state)
    
    await callback_query.message.edit_reply_markup(reply_markup=None) 

@dp.message(Command("analyze_cutlery"))
async def command_start_cutlery_analysis(message: types.Message, state: FSMContext):
    await state.set_state(CutleryAnalysis.waiting_for_url_or_description)
    await message.answer(
        "🔎 **Аналіз на Срібло (Проба Металу)**\n\n"
        "Будь ласка, надішліть **URL-адресу** продукту АБО **текстовий опис** (наприклад, 'Набір столових приладів срібло 925 проби')."
    )

@dp.message(CutleryAnalysis.waiting_for_url_or_description)
async def process_url_or_description(message: types.Message, state: FSMContext, session: aiohttp.ClientSession):
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
    final_text = message.text.strip()
    
    final_title = data.get('ai_title', final_text.split('\n')[0].strip())
    final_keywords = data.get('ai_keywords', final_title)
    final_value_text = data.get('ai_value', final_keywords)
        
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
    try:
        action = callback_query.data.split('_')[1]
        olx_id = callback_query.data.split('_')[2]
        is_like = (action == 'like')
        
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO user_feedback (user_id, olx_id, is_like) VALUES ($1, $2, $3)",
                               callback_query.from_user.id, olx_id, is_like)
            
            correction_factor = await _get_feedback_correction_factor(pool, olx_id)
            
            await conn.execute("""
                UPDATE olx_posts 
                SET ai_analysis_json = jsonb_set(ai_analysis_json, '{feedback_multiplier}', $1::jsonb)
                WHERE olx_id = $2
            """, json.dumps(correction_factor), olx_id)
            
        await callback_query.answer(f"Дякуємо за відгук! {'👍' if is_like else '👎'} Коефіцієнт навчання оновлено!")
        
        await callback_query.message.edit_reply_markup(reply_markup=None) 
        
    except Exception as e:
        logger.error(f"Помилка обробки зворотного зв'язку: {e}")
        await callback_query.answer("Помилка обробки відгуку.")


async def monitoring_worker(bot: Bot, pool: asyncpg.Pool, economic_engine: EconomicEngine, session: aiohttp.ClientSession, interval=600):
    logger.info(f"Воркер моніторингу запущено. Інтервал: {interval} сек.")
    
    while True:
        try:
            spot_prices = EconomicConfig.SPOT_PRICES
            
            new_posts = await fetch_olx_data(session, pool)
                
            async with pool.acquire() as conn:
                    
                for post in new_posts:
                    if not post['olx_id'].startswith('SIM_'):
                         is_exist = await conn.fetchval("SELECT olx_id FROM olx_posts WHERE olx_id = $1", post['olx_id'])
                         if is_exist:
                            continue

                    try:
                        ai_result = await economic_engine.analyze_olx_item(
                            session, post, spot_prices, bot
                        )
                    except Exception as e:
                        logger.error(f"Помилка аналізу OLX item {post['olx_id']}: {e}")
                        ai_result = {"is_relevant": False, "type": "Помилка Аналізу", "deal_assessment": f"Критична помилка: {e}"}

                    is_relevant = ai_result.get('is_relevant', False)
                    
                    await conn.execute(
                        "INSERT INTO olx_posts (olx_id, title, price, published_at, ai_analysis_json, is_relevant) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (olx_id) DO NOTHING",
                        post['olx_id'], post['title'], post['price'], datetime.now(KYIV_TZ), json.dumps(ai_result), is_relevant
                    )
                    
                    if is_relevant and CHANNEL_ID:
                        await send_olx_post(bot, post, ai_result)
                        await asyncio.sleep(5) 

                for feed_name, feed_url in EconomicConfig.RSS_FEEDS.items():
                    content = await fetch_page_content(session, feed_url)
                    
                    if content:
                        try:
                            feed = feedparser.parse(content)
                            for entry in feed.entries[:1]: 
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
                            logger.error(f"Помилка парсингу RSS для {feed_name}: {e}")
                    else:
                        logger.warning(f"Неможливо завантажити вміст RSS-стрічки: {feed_name}")


        except Exception as e:
            logger.error(f"Глобальна помилка у воркері моніторингу: {e}")
                
        await asyncio.sleep(interval) 


async def main():
    if not BOT_TOKEN or not DATABASE_URL or CHANNEL_ID is None:
        logger.critical("Критична помилка: Не знайдено BOT_TOKEN, DATABASE_URL або CHANNEL_ID.")
        sys.exit(1)
        
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
    except Exception as e:
        logger.critical(f"Критична помилка підключення до БД: {e}")
        sys.exit(1)
        
    await init_db(pool)
    
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    
    async with aiohttp.ClientSession() as session:
        economic_engine = EconomicEngine(pool)

        dp.message.outer_middleware.register(lambda handler, event, data: {**data, 'session': session, 'pool': pool, 'conn': pool, 'bot': bot, 'economic_engine': economic_engine})
        dp.callback_query.outer_middleware.register(lambda handler, event, data: {**data, 'pool': pool, 'bot': bot, 'session': session})
        
        if CHANNEL_ID and CHANNEL_ID != ADMIN_ID:
             await bot.send_message(
                chat_id=CHANNEL_ID,
                text="🤖 **Платформа V10 (Адаптивна з Заглушками) запущена!** 🛠️✨ Фоновий моніторинг активів розпочато. **Заглушки активні** для обходу 403 помилок.",
                parse_mode=ParseMode.MARKDOWN
            )
        
        tasks = [
            asyncio.create_task(monitoring_worker(bot, pool, economic_engine, session)),
            dp.start_polling(bot)
        ]

        logger.info("Бот запущено. Починаю опитування...")
        await asyncio.gather(*tasks)

    await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        if "terminated by other getUpdates request" in str(e):
             logger.critical("❌ **КРИТИЧНА ПОМИЛКА КОНФЛІКТУ:** Виявлено 'TelegramConflictError'. Це означає, що **запущено два або більше екземпляри бота одночасно**. Будь ласка, переконайтеся, що на Вашому хостингу працює лише один екземпляр.")
        else:
             logger.critical(f"Головна помилка виконання: {e}")