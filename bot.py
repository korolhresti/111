import logging
import json
import os
import asyncio
import requests
import cv2
import numpy as np
import re
from io import BytesIO
from fake_useragent import UserAgent
from bs4 import BeautifulSoup

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --- ⚙️ КОНФІГУРАЦІЯ ---
TOKEN = "8509179556:AAFWu5bGnGDNShzmynZE2fHZKYo3BYmKhqE"
ADMIN_ID = 8184456641
CHANNEL_ID = -1003680291028

# Файли сховища (замість Бази Даних)
SOURCES_FILE = "sources.json"   # Тут зберігаємо еталони (Empress, Violity)
HISTORY_FILE = "history.json"   # Тут історія знайденого (щоб не було дублів)

# Налаштування логування
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("CollectorBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- 💾 СИСТЕМА ЗБЕРІГАННЯ (JSON) ---

class JsonDB:
    @staticmethod
    def load(filename, default=[]):
        if not os.path.exists(filename):
            JsonDB.save(filename, default)
            return default
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return default

    @staticmethod
    def save(filename, data):
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

# --- 👁 КОМП'ЮТЕРНИЙ ЗІР (OpenCV) ---

class VisualEye:
    def __init__(self):
        # Використовуємо ORB (швидкий та ефективний для пошуку співпадінь)
        self.orb = cv2.ORB_create(nfeatures=1500)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    def download_image(self, url):
        """Завантажує зображення в пам'ять для OpenCV"""
        try:
            headers = {'User-Agent': UserAgent().random}
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200: return None
            
            image_array = np.asarray(bytearray(resp.content), dtype=np.uint8)
            img = cv2.imdecode(image_array, cv2.IMREAD_GRAYSCALE) # Ч/Б для аналізу
            return img
        except Exception as e:
            logger.error(f"Img Download Error: {e}")
            return None

    def find_object_in_scene(self, reference_img, scene_img):
        """
        Шукає reference_img (еталон) всередині scene_img (фото з ОЛХ).
        Повертає % схожості.
        """
        if reference_img is None or scene_img is None:
            return 0

        # Знаходимо ключові точки
        kp1, des1 = self.orb.detectAndCompute(reference_img, None)
        kp2, des2 = self.orb.detectAndCompute(scene_img, None)

        if des1 is None or des2 is None:
            return 0

        # Співставлення
        matches = self.bf.match(des1, des2)
        # Сортуємо: найкращі матчі перші
        matches = sorted(matches, key=lambda x: x.distance)

        # Беремо топ-20 точок
        good_matches = [m for m in matches if m.distance < 50]
        
        # Евристика: якщо знайдено багато співпадаючих точок, об'єкт присутній
        score = len(good_matches)
        
        # Нормалізація (0-100%) - дуже приблизно
        max_possible = len(kp1) if len(kp1) > 0 else 1
        confidence = min(100, (score / 15) * 100) # 15 точок вважаємо гарним збігом
        
        return confidence

vision = VisualEye()

# --- 🕸 ПАРСЕРИ САЙТІВ ---

class Scraper:
    def __init__(self):
        self.ua = UserAgent()

    def get_headers(self):
        return {'User-Agent': self.ua.random}

    def detect_replica(self, text):
        """Перевіряє текст на ознаки підробки"""
        keywords = ["копия", "копія", "реплика", "репліка", "copy", "replica", "homage", "хомаж"]
        text_lower = text.lower()
        if any(word in text_lower for word in keywords):
            return "⚠️ КОПІЯ / РЕПЛІКА"
        return "✅ ОРИГІНАЛ (Ймовірно)"

    def parse_empress_cc(self, url, soup):
        """Парсер для Empress.cc (з ТЗ)"""
        title = soup.find('h1', class_='product-single__title')
        title = title.text.strip() if title else "Empress Watch"
        
        # Ціна
        price_tag = soup.find('span', class_='product__price')
        price = price_tag.text.strip() if price_tag else "N/A"
        
        # Фото
        img = soup.find('div', class_='product__main-photos')
        img_url = None
        if img:
            img_tag = img.find('img')
            if img_tag:
                img_url = "https:" + img_tag['src'] if img_tag['src'].startswith('//') else img_tag['src']
        
        return {"title": title, "price": price, "image_url": img_url, "url": url, "source": "Empress.cc"}

    def parse_generic(self, url):
        """Універсальний парсер (Violity та інші)"""
        try:
            resp = requests.get(url, headers=self.get_headers(), timeout=10)
            soup = BeautifulSoup(resp.text, 'lxml')
            
            if "empress.cc" in url:
                return self.parse_empress_cc(url, soup)

            # Спроба витягнути OpenGraph теги (працює для Violity, OLX, eBay)
            title = soup.find("meta", property="og:title")
            title = title["content"] if title else soup.title.string
            
            image = soup.find("meta", property="og:image")
            image_url = image["content"] if image else None
            
            return {
                "title": title, 
                "price": "Аукціон/Невідомо", 
                "image_url": image_url, 
                "url": url,
                "source": "Web"
            }
        except Exception as e:
            logger.error(f"Parse Error: {e}")
            return None

    def search_olx(self, query):
        """Пошук на OLX"""
        clean_query = re.sub(r'[^\w\s]', '', query) # Видаляємо спецсимволи
        search_url = f"https://www.olx.ua/uk/list/q-{clean_query.replace(' ', '-')}/"
        
        results = []
        try:
            resp = requests.get(search_url, headers=self.get_headers(), timeout=10)
            soup = BeautifulSoup(resp.text, 'lxml')
            
            # Селектор OLX (може змінюватись, data-cy надійний)
            cards = soup.find_all('div', {'data-cy': 'l-card'})
            
            for card in cards[:8]: # Перші 8 результатів
                try:
                    link = card.find('a', href=True)
                    url = link['href']
                    if not url.startswith('http'): url = "https://www.olx.ua" + url
                    
                    title_tag = card.find('h6')
                    title = title_tag.text.strip() if title_tag else "No Title"
                    
                    price_tag = card.find('p', {'data-testid': 'ad-price'})
                    price = price_tag.text.strip() if price_tag else "?"
                    
                    img_tag = card.find('img')
                    img_url = img_tag['src'] if img_tag else None

                    status = self.detect_replica(title)

                    if img_url:
                        results.append({
                            "title": title,
                            "price": price,
                            "url": url,
                            "image_url": img_url,
                            "status": status
                        })
                except: continue
        except Exception as e:
            logger.error(f"OLX Search Failed: {e}")
            
        return results

scraper = Scraper()

# --- 🤖 ЛОГІКА БОТА ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(
        "🖥 **Панель керування колекціонера**\n\n"
        "Цей бот автоматично моніторить OLX на основі ваших зразків.\n"
        "Використовується комп'ютерний зір для пошуку на фото.\n\n"
        "**Команди:**\n"
        "➕ `/add <посилання>` — Додати лот (Empress, Violity) в базу еталонів.\n"
        "📋 `/list` — Список того, що шукаємо.\n"
        "🗑 `/clear` — Очистити базу пошуку."
    )

async def add_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    if not context.args:
        await update.message.reply_text("❌ Вкажіть посилання. Приклад:\n`/add https://empress.cc/...`", parse_mode=ParseMode.MARKDOWN)
        return

    url = context.args[0]
    msg = await update.message.reply_text("⏳ Аналізую сайт та завантажую еталон...")

    # 1. Парсинг
    data = await asyncio.to_thread(scraper.parse_generic, url)
    
    if not data or not data['image_url']:
        await msg.edit_text("❌ Не вдалося отримати фото або дані. Спробуйте інше посилання.")
        return

    # 2. Збереження
    sources = JsonDB.load(SOURCES_FILE)
    # Перевірка на дублікат
    if any(s['url'] == url for s in sources):
        await msg.edit_text("⚠️ Це посилання вже є в базі.")
        return

    sources.append(data)
    JsonDB.save(SOURCES_FILE, sources)

    await msg.edit_text(f"✅ **Успішно додано!**\n\n🎯 Ціль: {data['title']}\n💰 Орієнтир: {data['price']}")

    # 3. Пост в канал про нову ціль
    caption = (
        f"🆕 **НОВА ЦІЛЬ ДЛЯ ПОШУКУ**\n"
        f"➖➖➖➖➖➖➖➖\n"
        f"🕰 **Назва:** {data['title']}\n"
        f"💵 **Еталонна ціна:** {data['price']}\n"
        f"🌐 **Джерело:** {data['source']}\n\n"
        f"🤖 *Система почала сканування OLX...*"
    )
    try:
        await context.bot.send_photo(chat_id=CHANNEL_ID, photo=data['image_url'], caption=caption, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Channel post error: {e}")

async def list_sources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    sources = JsonDB.load(SOURCES_FILE)
    if not sources:
        await update.message.reply_text("📭 Список пустий.")
        return
    
    text = "📋 **Активні цілі пошуку:**\n"
    for i, s in enumerate(sources, 1):
        text += f"{i}. [{s['title']}]({s['url']})\n"
    
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def clear_sources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    JsonDB.save(SOURCES_FILE, [])
    await update.message.reply_text("🗑 Список очищено. Моніторинг зупинено.")

# --- 🔄 ФОНОВИЙ ПРОЦЕС ПОШУКУ ---

async def background_search(context: ContextTypes.DEFAULT_TYPE):
    """
    Основний цикл:
    1. Бере еталони з sources.json.
    2. Шукає по назві в OLX.
    3. Порівнює фото еталону і фото з OLX (CV).
    4. Якщо знайдено і це не дубль -> Постить в канал.
    """
    sources = JsonDB.load(SOURCES_FILE)
    history = JsonDB.load(HISTORY_FILE)
    
    if not sources: return

    logger.info(f"🔄 Запуск циклу сканування для {len(sources)} цілей...")

    for target in sources:
        # Завантажуємо еталонне фото
        ref_img = await asyncio.to_thread(vision.download_image, target['image_url'])
        if ref_img is None: continue

        # Шукаємо на OLX
        olx_items = await asyncio.to_thread(scraper.search_olx, target['title'])

        for item in olx_items:
            # Перевірка історії (щоб не постити те саме)
            if item['url'] in history: continue

            # Завантажуємо фото з OLX
            scene_img = await asyncio.to_thread(vision.download_image, item['image_url'])
            
            # Порівнюємо (Computer Vision)
            confidence = await asyncio.to_thread(vision.find_object_in_scene, ref_img, scene_img)

            # Логіка рішення:
            # Якщо confidence > 20% (орієнтовно), або якщо назва дуже схожа
            is_visual_match = confidence > 15 
            
            if is_visual_match:
                logger.info(f"MATCH FOUND! {item['title']} (Conf: {confidence:.2f}%)")
                
                # Пост в канал
                match_level = "🟢 Висока" if confidence > 40 else "🟡 Середня"
                
                text = (
                    f"🚨 **ЗНАЙДЕНО СПІВПАДІННЯ!**\n"
                    f"➖➖➖➖➖➖➖➖\n"
                    f"🎯 **Шукали:** {target['title']}\n"
                    f"📦 **Лот OLX:** {item['title']}\n"
                    f"💰 **Ціна:** {item['price']}\n\n"
                    f"🛡 **Статус:** {item['status']}\n"
                    f"👁 **Візуальна схожість:** {match_level} ({confidence:.1f}%)\n\n"
                    f"👉 [Перейти до оголошення]({item['url']})"
                )

                try:
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=item['image_url'],
                        caption=text,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    # Додаємо в історію
                    history.append(item['url'])
                    JsonDB.save(HISTORY_FILE, history)
                except Exception as e:
                    logger.error(f"Post error: {e}")
            
            # Пауза між запитами, щоб не блокували
            await asyncio.sleep(2)

    # Чистка історії (тримати останні 500)
    if len(history) > 500:
        JsonDB.save(HISTORY_FILE, history[-500:])

# --- 🚀 ЗАПУСК І ВІТАННЯ ---

async def post_init(application: Application):
    """Виконується 1 раз при старті бота"""
    logger.info("Bot started. Sending welcome message...")
    try:
        welcome_text = (
            "🚀 **СИСТЕМА КОЛЕКЦІЙНОГО ПОШУКУ АКТИВОВАНА**\n\n"
            "🤖 Бот запущено в режимі 24/7.\n"
            "📂 База даних: Локальна (JSON)\n"
            "🧠 ШІ: Computer Vision (ORB/SIFT)\n"
            "🌍 Джерела: Empress, Violity, OLX\n\n"
            "✅ *Очікую нові завдання від адміністратора.*"
        )
        await application.bot.send_message(chat_id=CHANNEL_ID, text=welcome_text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to send welcome: {e}")

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # Хендлери команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_source))
    app.add_handler(CommandHandler("list", list_sources))
    app.add_handler(CommandHandler("clear", clear_sources))

    # Фонові задачі (кожні 5 хвилин = 300 сек)
    if app.job_queue:
        app.job_queue.run_repeating(background_search, interval=300, first=10)

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
