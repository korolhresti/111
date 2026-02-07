import logging
import json
import os
import asyncio
import re
import sys
import time
import random
from datetime import datetime
from io import BytesIO

# Сторонні бібліотеки
import requests
import cv2
import numpy as np
from aiohttp import web
from fake_useragent import UserAgent
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# --- ⚙️ КОНФІГУРАЦІЯ ТА ЗМІННІ СЕРЕДОВИЩА ---
# Використовуємо змінні середовища або дефолтні значення з вашого ТЗ
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8509179556:AAFWu5bGnGDNShzmynZE2fHZKYo3BYmKhqE")

def get_env_int(key, default):
    try:
        val = os.getenv(key, str(default))
        return int(val)
    except ValueError:
        return int(default)

ADMIN_ID = get_env_int("ADMIN_CHAT_ID", 8184456641)
CHANNEL_ID = get_env_int("CHANNEL_ID", -1003680291028)
PORT = get_env_int("PORT", 8080)

# Шляхи до файлів
DATA_DIR = "data"
IMAGES_DIR = os.path.join(DATA_DIR, "images")
SOURCES_FILE = os.path.join(DATA_DIR, "sources.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")

# Створення папок
os.makedirs(IMAGES_DIR, exist_ok=True)

# Налаштування логування
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
# Прибираємо зайвий шум від бібліотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("CollectorPro")

# --- 💾 МОДУЛЬ БАЗИ ДАНИХ (JSON) ---
class JsonDatabase:
    """Клас для надійного збереження даних у JSON файли"""
    _lock = asyncio.Lock()

    @staticmethod
    async def load(filepath, default_factory=list):
        async with JsonDatabase._lock:
            if not os.path.exists(filepath):
                return default_factory()
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"DB Load Error ({filepath}): {e}")
                return default_factory()

    @staticmethod
    async def save(filepath, data):
        async with JsonDatabase._lock:
            try:
                # Атомарний запис через тимчасовий файл
                temp_path = filepath + ".tmp"
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                os.replace(temp_path, filepath)
            except Exception as e:
                logger.error(f"DB Save Error ({filepath}): {e}")

# --- 👁 МОДУЛЬ КОМП'ЮТЕРНОГО ЗОРУ (OpenCV) ---
class ComputerVision:
    def __init__(self):
        # ORB детектор для пошуку ключових точок
        self.orb = cv2.ORB_create(nfeatures=2000)
        # BFMatcher для порівняння точок
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    def load_image_from_bytes(self, image_bytes):
        """Конвертує байти в зображення OpenCV"""
        try:
            nparr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            return img
        except Exception as e:
            logger.error(f"CV Decode Error: {e}")
            return None

    def compare(self, img1, img2):
        """
        Порівнює два зображення.
        Повертає оцінку схожості від 0 до 100.
        Використовує комбінацію співставлення точок (Geometry) та гістограм (Color).
        """
        if img1 is None or img2 is None: return 0

        try:
            # 1. Геометричний аналіз (ORB)
            gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR_GRAY)
            gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR_GRAY)

            kp1, des1 = self.orb.detectAndCompute(gray1, None)
            kp2, des2 = self.orb.detectAndCompute(gray2, None)

            geo_score = 0
            if des1 is not None and des2 is not None and len(des1) > 0 and len(des2) > 0:
                matches = self.bf.match(des1, des2)
                # Сортуємо за дистанцією (менше = краще)
                matches = sorted(matches, key=lambda x: x.distance)
                # Беремо лише хороші збіги (дистанція < 50)
                good_matches = [m for m in matches if m.distance < 60]
                
                # Евристика: 30 гарних точок - це дуже схоже
                geo_score = min(100, (len(good_matches) / 25) * 100)

            # 2. Колірний аналіз (Histogram)
            # Допомагає відсіяти предмети однакової форми, але різного кольору
            h_bins = 50
            s_bins = 60
            histSize = [h_bins, s_bins]
            h_ranges = [0, 180]
            s_ranges = [0, 256]
            ranges = h_ranges + s_ranges
            channels = [0, 1]

            hsv_base = cv2.cvtColor(img1, cv2.COLOR_BGR2HSV)
            hsv_test = cv2.cvtColor(img2, cv2.COLOR_BGR2HSV)

            hist_base = cv2.calcHist([hsv_base], channels, None, histSize, ranges, accumulate=False)
            cv2.normalize(hist_base, hist_base, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)

            hist_test = cv2.calcHist([hsv_test], channels, None, histSize, ranges, accumulate=False)
            cv2.normalize(hist_test, hist_test, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)

            # Метод кореляції (1.0 = ідеально)
            color_score = cv2.compareHist(hist_base, hist_test, cv2.HISTCMP_CORREL) * 100
            color_score = max(0, color_score)

            # Фінальна оцінка: 70% геометрія, 30% колір
            final_score = (geo_score * 0.7) + (color_score * 0.3)
            
            return final_score

        except Exception as e:
            logger.error(f"CV Comparison Error: {e}")
            return 0

cv_engine = ComputerVision()

# --- 🕸 МОДУЛЬ СКРЕЙПІНГУ ---
class ScraperEngine:
    def __init__(self):
        self.ua = UserAgent()

    def get_headers(self):
        return {
            'User-Agent': self.ua.random,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7',
            'Referer': 'https://www.google.com/'
        }

    def download_image_bytes(self, url):
        """Завантажує байти зображення з URL"""
        if not url: return None
        try:
            # Обробка локальних файлів
            if not url.startswith('http'):
                with open(url, 'rb') as f:
                    return f.read()
            
            # Обробка веб-URL
            resp = requests.get(url, headers=self.get_headers(), timeout=15)
            if resp.status_code == 200:
                return resp.content
        except Exception as e:
            logger.warning(f"Download Failed ({url}): {e}")
        return None

    def parse_empress_cc(self):
        """Парсинг сайту Empress.cc"""
        url = "https://empress.cc/collections/all"
        results = []
        logger.info("📡 Starting Empress.cc sync...")
        try:
            resp = requests.get(url, headers=self.get_headers(), timeout=20)
            soup = BeautifulSoup(resp.text, 'lxml')
            
            # Селектори можуть змінюватися, використовуємо кілька варіантів
            products = soup.select('.grid-product__content, .product-card, .grid-view-item')
            
            for p in products[:15]: # Ліміт для демо
                try:
                    title_el = p.select_one('.grid-product__title, .product-card__title, .grid-view-item__title')
                    price_el = p.select_one('.grid-product__price, .price-item--regular')
                    link_el = p.select_one('a')
                    img_el = p.select_one('img')

                    if not title_el or not link_el or not img_el: continue

                    title = title_el.get_text(strip=True)
                    price = price_el.get_text(strip=True) if price_el else "N/A"
                    link = "https://empress.cc" + link_el['href']
                    
                    # Отримання найкращої якості зображення
                    img_src = img_el.get('data-src') or img_el.get('src')
                    if img_src:
                        img_src = "https:" + img_src if img_src.startswith('//') else img_src
                        img_src = re.sub(r'_\d+x\d+', '', img_src) # Видаляємо розмір
                        img_src = img_src.split('?')[0]

                    results.append({
                        "title": title,
                        "url": link,
                        "image_url": img_src,
                        "price": price,
                        "source": "Empress.cc"
                    })
                except Exception as e:
                    continue
        except Exception as e:
            logger.error(f"Empress Parse Error: {e}")
        
        return results

    def search_olx(self, query):
        """Розумний пошук по OLX"""
        # Очищення запиту
        clean_query = re.sub(r'[^\w\s]', '', query).strip()
        clean_query = clean_query.replace(' ', '-')
        
        # URL з фільтрами: шукати в заголовках, тільки з фото
        url = f"https://www.olx.ua/uk/list/q-{clean_query}/?search%5Bphotos%5D=1"
        
        results = []
        try:
            resp = requests.get(url, headers=self.get_headers(), timeout=15)
            soup = BeautifulSoup(resp.text, 'lxml')
            
            cards = soup.find_all('div', {'data-cy': 'l-card'})
            
            for card in cards[:10]:
                try:
                    link_tag = card.find('a', href=True)
                    if not link_tag: continue
                    
                    href = link_tag['href']
                    full_url = href if href.startswith('http') else f"https://www.olx.ua{href}"
                    
                    # Ігноруємо рекламу (promoted)
                    if 'promoted' in str(card).lower(): continue

                    title = card.find('h6').text.strip()
                    
                    price_div = card.find('p', {'data-testid': 'ad-price'})
                    price = price_div.text.strip() if price_div else "?"
                    
                    img_tag = card.find('img')
                    img_url = img_tag.get('src') or img_tag.get('data-src')
                    
                    if not img_url: continue

                    # Аналіз тексту на репліку
                    is_replica = any(w in title.lower() for w in ['копия', 'реплика', 'copy', 'replica', 'aaa'])

                    results.append({
                        "title": title,
                        "url": full_url,
                        "price": price,
                        "image_url": img_url,
                        "is_replica": is_replica
                    })
                except: continue
        except Exception as e:
            logger.error(f"OLX Search Error: {e}")
            
        return results

scraper = ScraperEngine()

# --- 🤖 ЛОГІКА ТЕЛЕГРАМ БОТА ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Доступ заборонено.")
        return

    stats_sources = len(await JsonDatabase.load(SOURCES_FILE))
    
    kb = [
        [InlineKeyboardButton("📸 Додати фото-зразок", callback_data="add_photo")],
        [InlineKeyboardButton("🌐 Синхронізувати Empress", callback_data="sync_web")],
        [InlineKeyboardButton(f"📋 Список цілей ({stats_sources})", callback_data="list")],
        [InlineKeyboardButton("🛑 Очистити базу", callback_data="clear")]
    ]
    
    await update.message.reply_text(
        f"🖥 **Панель Колекціонера Pro**\n\n"
        f"Статус: ✅ Активний\n"
        f"Цілей в базі: {stats_sources}\n"
        f"Метод: Computer Vision + Web Scraping\n\n"
        f"Оберіть дію:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "add_photo":
        await query.message.reply_text("📤 Надішліть фотографію предмета, який ви хочете відслідковувати.")

    elif data == "sync_web":
        await query.message.reply_text("⏳ Починаю парсинг Empress.cc...")
        # Виконуємо в окремому потоці
        items = await asyncio.to_thread(scraper.parse_empress_cc)
        
        if items:
            current_db = await JsonDatabase.load(SOURCES_FILE)
            added = 0
            for item in items:
                # Уникаємо дублікатів по URL
                if not any(x['url'] == item['url'] for x in current_db):
                    current_db.append(item)
                    added += 1
            
            await JsonDatabase.save(SOURCES_FILE, current_db)
            await query.message.reply_text(f"✅ Успішно! Додано {added} нових позицій.")
        else:
            await query.message.reply_text("❌ Не вдалося отримати дані або нових товарів немає.")

    elif data == "list":
        sources = await JsonDatabase.load(SOURCES_FILE)
        if not sources:
            await query.message.reply_text("📭 База пуста.")
        else:
            text = "📋 **Активні цілі:**\n\n"
            for i, s in enumerate(sources[:10], 1):
                text += f"{i}. {s['title']} ({s['price']})\n"
            if len(sources) > 10:
                text += f"\n...і ще {len(sources)-10} предметів."
            await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    elif data == "clear":
        await JsonDatabase.save(SOURCES_FILE, [])
        await JsonDatabase.save(HISTORY_FILE, [])
        await query.message.reply_text("🗑 Базу даних та історію очищено.")

async def handle_photo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    
    # Зберігаємо локально
    filename = f"{int(time.time())}_{photo.file_id[:5]}.jpg"
    path = os.path.join(IMAGES_DIR, filename)
    await file.download_to_drive(path)
    
    # Додаємо в БД
    new_item = {
        "title": f"Manual Item {filename}",
        "url": "local_upload",
        "image_url": path, # Зберігаємо шлях до файлу
        "price": "N/A",
        "source": "User Upload"
    }
    
    db = await JsonDatabase.load(SOURCES_FILE)
    db.append(new_item)
    await JsonDatabase.save(SOURCES_FILE, db)
    
    await update.message.reply_text("✅ Фото прийнято в роботу! Пошук розпочнеться автоматично.")

# --- 🔄 ФОНОВИЙ МОНІТОРИНГ ---
async def monitoring_loop(context: ContextTypes.DEFAULT_TYPE):
    """
    Головна функція, яка періодично запускається JobQueue.
    Вона перебирає всі джерела та шукає відповідники.
    """
    sources = await JsonDatabase.load(SOURCES_FILE)
    history = await JsonDatabase.load(HISTORY_FILE)
    
    if not sources: return

    logger.info(f"🔎 Monitoring started for {len(sources)} items...")
    
    # Вибираємо випадкові 5 предметів для перевірки за один цикл (щоб не блокували)
    # Якщо база велика, перевіряти все одразу небезпечно
    batch = random.sample(sources, min(len(sources), 5))

    for target in batch:
        # 1. Завантажуємо еталонне фото
        target_img_bytes = await asyncio.to_thread(scraper.download_image_bytes, target['image_url'])
        target_cv_img = cv_engine.load_image_from_bytes(target_img_bytes)
        
        if target_cv_img is None: continue

        # 2. Шукаємо на OLX
        olx_results = await asyncio.to_thread(scraper.search_olx, target['title'])
        
        for item in olx_results:
            # Перевірка історії
            if item['url'] in history: continue

            # 3. Завантажуємо фото знахідки
            item_img_bytes = await asyncio.to_thread(scraper.download_image_bytes, item['image_url'])
            item_cv_img = cv_engine.load_image_from_bytes(item_img_bytes)

            # 4. Порівнюємо
            similarity = await asyncio.to_thread(cv_engine.compare, target_cv_img, item_cv_img)
            
            # Логіка повідомлення (Поріг > 20%)
            if similarity > 20.0:
                logger.info(f"MATCH FOUND: {similarity:.1f}% -> {item['title']}")
                
                status_icon = "⚠️ КОПІЯ/РЕПЛІКА" if item['is_replica'] else "✅ ЙМОВІРНО ОРИГІНАЛ"
                match_grade = "🔥 Висока" if similarity > 50 else "🟡 Середня"

                caption = (
                    f"🚨 **ЗНАЙДЕНО ВІДПОВІДНИК!**\n\n"
                    f"🔍 **Шукали:** {target['title']}\n"
                    f"📦 **Знайдено:** {item['title']}\n"
                    f"💵 **Ціна:** {item['price']}\n\n"
                    f"🛡 **Статус:** {status_icon}\n"
                    f"📊 **Схожість:** {match_grade} ({similarity:.1f}%)\n\n"
                    f"🔗 [Дивитись на OLX]({item['url']})"
                )

                try:
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=item['image_url'],
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    # Додаємо в історію
                    history.append(item['url'])
                    await JsonDatabase.save(HISTORY_FILE, history[-1000:]) # Тримаємо останні 1000
                except Exception as e:
                    logger.error(f"Send Telegram Error: {e}")

            # Пауза між запитами
            await asyncio.sleep(2)

# --- 🌍 WEB SERVER ДЛЯ RENDER/HEROKU ---
async def health_check(request):
    return web.Response(text="Bot is Alive & Running", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌍 Web Server listening on port {PORT}")

# --- 🚀 ЗАПУСК ---
async def on_startup(application: Application):
    """Виконується після ініціалізації бота"""
    await start_web_server()
    try:
        await application.bot.send_message(
            chat_id=CHANNEL_ID,
            text=f"🤖 **CollectorBot Pro v3.0** запущено!\nМоніторинг активовано."
        )
    except Exception as e:
        logger.warning(f"Welcome message failed: {e}")

def main():
    # ApplicationBuilder - найкраща практика для версій 20.x+
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(on_startup) # Хук запуску
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .build()
    )

    # Реєстрація хендлерів
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_upload))

    # Реєстрація фонових задач
    if application.job_queue:
        # Запуск кожні 5 хвилин (300 сек)
        application.job_queue.run_repeating(monitoring_loop, interval=300, first=20)

    print("🚀 Bot is starting polling...")
    
    # drop_pending_updates=True - критично важливо для уникнення Conflict Error
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
