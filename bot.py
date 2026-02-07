import logging
import json
import os
import asyncio
import requests
import cv2
import numpy as np
import re
import html
import traceback
from io import BytesIO
from fake_useragent import UserAgent
from bs4 import BeautifulSoup
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ApplicationBuilder
)

# --- ⚙️ КОНФІГУРАЦІЯ ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8509179556:AAFWu5bGnGDNShzmynZE2fHZKYo3BYmKhqE")
try:
    ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID", "8184456641"))
    CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003680291028"))
    PORT = int(os.getenv("PORT", 8080))
except ValueError:
    print("❌ Помилка: Перевірте ID чатів та порту в змінних середовища.")
    exit(1)

SOURCES_FILE = "sources.json"
HISTORY_FILE = "history.json"
DB_IMAGES_DIR = "db_images"

# Налаштування логування
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
# Прибираємо шум від бібліотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("ExpertBot")

# --- 💾 РОБОТА З ДАНИМИ ---
class JsonDB:
    """Клас для безпечної роботи з JSON (потокобезпечний)"""
    _lock = asyncio.Lock()

    @staticmethod
    async def load(filename, default=None):
        if default is None: default = []
        async with JsonDB._lock:
            if not os.path.exists(filename):
                return default
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"DB Load Error {filename}: {e}")
                return default

    @staticmethod
    async def save(filename, data):
        async with JsonDB._lock:
            try:
                # Запис у тимчасовий файл для запобігання пошкодженню
                temp_file = filename + ".tmp"
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                os.replace(temp_file, filename)
            except Exception as e:
                logger.error(f"DB Save Error {filename}: {e}")

# --- 👁 КОМП'ЮТЕРНИЙ ЗІР (OpenCV) ---
class VisionEngine:
    def __init__(self):
        # ORB - оптимальний баланс швидкості та точності
        self.orb = cv2.ORB_create(nfeatures=1500)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    def get_image_from_source(self, source):
        """Завантажує фото з URL або локального файлу"""
        try:
            content = None
            if source.startswith(('http://', 'https://')):
                headers = {'User-Agent': UserAgent().random}
                resp = requests.get(source, timeout=10, headers=headers)
                if resp.status_code == 200:
                    content = resp.content
            elif os.path.exists(source):
                with open(source, 'rb') as f:
                    content = f.read()
            
            if content:
                arr = np.asarray(bytearray(content), dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                return img
        except Exception as e:
            logger.error(f"Image load error ({source}): {e}")
        return None

    def compare_images(self, img1, img2):
        """Повертає відсоток схожості (0-100)"""
        if img1 is None or img2 is None: return 0
        
        try:
            # 1. ORB (Геометрія)
            gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR_GRAY)
            gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR_GRAY)
            
            kp1, des1 = self.orb.detectAndCompute(gray1, None)
            kp2, des2 = self.orb.detectAndCompute(gray2, None)

            if des1 is None or des2 is None or len(des1) < 5 or len(des2) < 5:
                return 0

            matches = self.bf.match(des1, des2)
            # Фільтруємо найкращі збіги (дистанція < 50)
            good_matches = [m for m in matches if m.distance < 50]
            
            # Нормалізація результату
            match_score = len(good_matches)
            # Евристика: 20 гарних точок - це вже дуже схоже
            score_geo = min(100, (match_score / 20) * 100)

            return score_geo
        except Exception as e:
            logger.error(f"CV Error: {e}")
            return 0

vision = VisionEngine()

# --- 🕸 ПАРСЕР ---
class Scraper:
    def __init__(self):
        self.ua = UserAgent()

    def _headers(self):
        return {
            'User-Agent': self.ua.random,
            'Accept-Language': 'uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
        }

    def sync_empress(self):
        """Парсинг Empress.cc"""
        url = "https://empress.cc/collections/all"
        products = []
        try:
            logger.info("Starting Empress sync...")
            res = requests.get(url, headers=self._headers(), timeout=20)
            soup = BeautifulSoup(res.text, 'lxml')
            
            # Пошук товарів на сторінці колекції
            items = soup.select('.grid-product__content, .product-card')
            
            for item in items[:10]: # Обмежимо 10 останніми для швидкості демо
                try:
                    title_tag = item.select_one('.grid-product__title, .product-card__title')
                    price_tag = item.select_one('.grid-product__price, .price-item--regular')
                    img_tag = item.select_one('img')
                    link_tag = item.select_one('a')

                    if not title_tag or not img_tag: continue

                    title = title_tag.text.strip()
                    price = price_tag.text.strip() if price_tag else "N/A"
                    
                    # Обробка URL картинки
                    img_url = img_tag.get('src') or img_tag.get('data-src')
                    if img_url:
                        img_url = "https:" + img_url if img_url.startswith('//') else img_url
                        # Видаляємо параметри розміру (наприклад _300x300)
                        img_url = re.sub(r'_\d+x\d+', '', img_url)

                    prod_url = "https://empress.cc" + link_tag['href']

                    products.append({
                        "title": title,
                        "price": price,
                        "url": prod_url,
                        "image_url": img_url,
                        "source": "Empress.cc"
                    })
                except Exception as e:
                    continue
            return products
        except Exception as e:
            logger.error(f"Empress Sync Error: {e}")
            return []

    def search_olx(self, query):
        """Розширений пошук OLX"""
        clean_q = re.sub(r'[^\w\s]', '', query).strip().replace(' ', '-')
        # Фільтр: шукаємо з фото, ціна від 100 грн
        url = f"https://www.olx.ua/uk/list/q-{clean_q}/?search%5Bphotos%5D=1"
        
        results = []
        try:
            res = requests.get(url, headers=self._headers(), timeout=15)
            soup = BeautifulSoup(res.text, 'lxml')
            
            cards = soup.find_all('div', {'data-cy': 'l-card'})
            
            for card in cards[:8]:
                try:
                    link = card.find('a', href=True)
                    if not link: continue
                    
                    href = link['href']
                    full_url = href if href.startswith('http') else f"https://www.olx.ua{href}"
                    
                    # Ігноруємо просунуті (рекламні) оголошення, бо вони часто нерелевантні
                    if 'promoted' in str(card).lower():
                        continue

                    title = card.find('h6').text.strip()
                    price_div = card.find('p', {'data-testid': 'ad-price'})
                    price = price_div.text.strip() if price_div else "?"
                    
                    img = card.find('img')
                    img_src = img.get('src') or img.get('data-src')
                    
                    if not img_src: continue

                    is_copy = any(w in title.lower() for w in ["копия", "реплика", "copy", "replica", "aa", "aaa"])
                    
                    results.append({
                        "title": title,
                        "url": full_url,
                        "price": price,
                        "image_url": img_src,
                        "is_copy": is_copy
                    })
                except: continue
        except Exception as e:
            logger.error(f"OLX Error: {e}")
            
        return results

scraper = Scraper()

# --- 🤖 BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    keyboard = [
        [InlineKeyboardButton("➕ Додати фото", callback_data="add_photo")],
        [InlineKeyboardButton("🔄 Синхронізація Empress", callback_data="sync_empress")],
        [InlineKeyboardButton("📋 Список цілей", callback_data="list_targets")],
        [InlineKeyboardButton("🗑 Очистити все", callback_data="clear_all")]
    ]
    
    await update.message.reply_text(
        "👋 **Вітаю в ExpertCollector Bot!**\n\n"
        "Система працює 24/7. Я використовую комп'ютерний зір для пошуку колекційних предметів.\n\n"
        "👇 Оберіть дію:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "add_photo":
        await query.message.reply_text("📸 Надішліть мені фото предмета, який треба шукати.")
    
    elif query.data == "sync_empress":
        await query.message.reply_text("⏳ Починаю синхронізацію з Empress...")
        # Виконуємо в окремому потоці, щоб не блокувати бота
        items = await asyncio.to_thread(scraper.sync_empress)
        
        current = await JsonDB.load(SOURCES_FILE)
        count = 0
        for item in items:
            if not any(c['url'] == item['url'] for c in current):
                current.append(item)
                count += 1
        
        await JsonDB.save(SOURCES_FILE, current)
        await query.message.reply_text(f"✅ Додано {count} нових лотів з Empress.")
        
    elif query.data == "list_targets":
        sources = await JsonDB.load(SOURCES_FILE)
        if not sources:
            await query.message.reply_text("Список пустий.")
        else:
            msg = f"📋 **В базі {len(sources)} цілей:**\n"
            for s in sources[:10]: # Показуємо лише 10 перших
                msg += f"- [{s['title']}]({s.get('url', '#')})\n"
            if len(sources) > 10: msg += f"...і ще {len(sources)-10}"
            await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

    elif query.data == "clear_all":
        await JsonDB.save(SOURCES_FILE, [])
        await JsonDB.save(HISTORY_FILE, [])
        await query.message.reply_text("🗑 База очищена.")

async def handle_photo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    
    os.makedirs(DB_IMAGES_DIR, exist_ok=True)
    file_path = os.path.join(DB_IMAGES_DIR, f"{photo.file_id}.jpg")
    
    await file.download_to_drive(file_path)
    
    source_data = {
        "title": f"Manual Item {photo.file_id[:5]}",
        "image_url": file_path, # Локальний шлях
        "url": "local",
        "price": "N/A",
        "source": "User Upload"
    }
    
    current = await JsonDB.load(SOURCES_FILE)
    current.append(source_data)
    await JsonDB.save(SOURCES_FILE, current)
    
    await update.message.reply_text("✅ Фото додано до бази пошуку!")

# --- 🔄 ФОНОВА ЗАДАЧА ---
async def monitor_task(context: ContextTypes.DEFAULT_TYPE):
    """Головний цикл пошуку"""
    sources = await JsonDB.load(SOURCES_FILE)
    history = await JsonDB.load(HISTORY_FILE)
    
    if not sources: return
    
    logger.info(f"🔍 Scanning for {len(sources)} targets...")
    
    for target in sources:
        # 1. Отримуємо еталонне зображення
        # Використовуємо to_thread, бо OpenCV/Requests блокують
        ref_img = await asyncio.to_thread(vision.get_image_from_source, target['image_url'])
        if ref_img is None: continue

        # 2. Шукаємо на OLX
        olx_items = await asyncio.to_thread(scraper.search_olx, target['title'])
        
        for item in olx_items:
            if item['url'] in history: continue
            
            # 3. Порівнюємо зображення
            scene_img = await asyncio.to_thread(vision.get_image_from_source, item['image_url'])
            if scene_img is None: continue
            
            similarity = await asyncio.to_thread(vision.compare_images, ref_img, scene_img)
            
            # Поріг спрацювання (більше 20%)
            if similarity > 20:
                logger.info(f"MATCH: {similarity}% - {item['title']}")
                
                status = "⚠️ КОПІЯ" if item['is_copy'] else "✅ ЙМОВІРНО ОРИГІНАЛ"
                
                caption = (
                    f"🚨 **ЗНАЙДЕНО!**\n\n"
                    f"🎯 **Ціль:** {target['title']}\n"
                    f"📦 **Лот:** {item['title']}\n"
                    f"💰 **Ціна:** {item['price']}\n"
                    f"🛡 **Статус:** {status}\n"
                    f"📊 **Схожість:** {similarity:.1f}%\n\n"
                    f"🔗 [Оголошення на OLX]({item['url']})"
                )
                
                try:
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=item['image_url'],
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    history.append(item['url'])
                    await JsonDB.save(HISTORY_FILE, history[-1000:]) # Зберігаємо останні 1000
                except Exception as e:
                    logger.error(f"Send Error: {e}")
            
            # Невелика пауза, щоб не перевантажити сервер
            await asyncio.sleep(1)

# --- 🌐 WEB SERVER (HEALTH CHECK) ---
async def health_check(request):
    return web.Response(text="Bot is running OK", status=200)

async def start_web_server():
    """Запускає веб-сервер для Render/Heroku"""
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌍 Web server started on port {PORT}")

# --- 🚀 INIT ---
async def post_init(application: Application):
    """Ініціалізація після запуску"""
    await start_web_server() # Запускаємо сервер
    
    try:
        await application.bot.send_message(
            chat_id=CHANNEL_ID,
            text=f"🤖 **ExpertCollector v2.0 Запущено!**\nМоніторинг активовано."
        )
    except Exception as e:
        logger.warning(f"Could not send welcome message: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

def main():
    # Створення папок
    os.makedirs(DB_IMAGES_DIR, exist_ok=True)
    
    # Ініціалізація Application
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    # Хендлери
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_upload))
    
    # Обробка помилок
    application.add_error_handler(error_handler)
    
    # Фонові задачі (JobQueue)
    if application.job_queue:
        # Запускаємо пошук кожні 5 хвилин (300 сек)
        application.job_queue.run_repeating(monitor_task, interval=300, first=10)
    
    print("Bot is starting...")
    # drop_pending_updates=True допомагає уникнути конфліктів при перезапуску
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
