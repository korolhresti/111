import logging
import json
import os
import asyncio
import requests
import cv2
import numpy as np
import re
from io import BytesIO
from datetime import datetime
from PIL import Image
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --- КОНФІГУРАЦІЯ ---
TOKEN = "8509179556:AAFWu5bGnGDNShzmynZE2fHZKYo3BYmKhqE"
ADMIN_ID = 8184456641
CHANNEL_ID = -1003680291028

# Файли для "бази даних" (JSON)
SOURCES_FILE = "sources.json"  # Звідки беремо зразки (наприклад, Violity)
TARGETS_FILE = "targets.json"  # Збережені предмети для пошуку
HISTORY_FILE = "history.json"  # Історія, щоб не постити дублі

# Налаштування логування
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ЛОКАЛЬНЕ ЗБЕРІГАННЯ (JSON) ---

def load_json(filename, default_data):
    if not os.path.exists(filename):
        with open(filename, 'w') as f:
            json.dump(default_data, f)
        return default_data
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except:
        return default_data

def save_json(filename, data):
    with open(filename, 'w') as f:
        json.dump(data, f, indent=4)

# --- MACHINE LEARNING / COMPUTER VISION (ORB) ---

class VisualSearchEngine:
    """
    Використовує алгоритм ORB (Oriented FAST and Rotated BRIEF) для пошуку
    схожих ознак на зображеннях. Це працює швидко і локально без AI API.
    """
    def __init__(self):
        self.orb = cv2.ORB_create(nfeatures=1000)
        # BFMatcher object with Hamming distance
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    def download_image_cv(self, url):
        """Завантажує зображення з URL і конвертує в формат OpenCV"""
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                return None
            image_array = np.asarray(bytearray(resp.content), dtype=np.uint8)
            img = cv2.imdecode(image_array, cv2.IMREAD_GRAYSCALE)
            return img
        except Exception as e:
            logger.error(f"Error downloading image: {e}")
            return None

    def compare_images(self, img_ref, img_target):
        """
        Порівнює два зображення. Повертає True, якщо знайдено збіг.
        Здатний знайти об'єкт reference всередині target (групове фото).
        """
        if img_ref is None or img_target is None:
            return False, 0

        # Знаходимо ключові точки та дескриптори
        kp1, des1 = self.orb.detectAndCompute(img_ref, None)
        kp2, des2 = self.orb.detectAndCompute(img_target, None)

        if des1 is None or des2 is None:
            return False, 0

        # Співставлення дескрипторів
        matches = self.bf.match(des1, des2)
        
        # Сортуємо за відстанню (чим менше, тим краще)
        matches = sorted(matches, key=lambda x: x.distance)

        # Беремо топ найкращих збігів
        good_matches = [m for m in matches if m.distance < 60]
        
        score = len(good_matches)
        
        # Логіка рішення: якщо знайдено багато хороших збігів, це той самий об'єкт.
        # Для групових фото поріг може бути нижчим, для точних копій - вищим.
        is_match = score > 15  # Експериментальний поріг
        
        return is_match, score

vision = VisualSearchEngine()

# --- ВЕБ-СКРЕЙПІНГ ---

ua = UserAgent()

def get_headers():
    return {'User-Agent': ua.random}

async def scrape_violity_sample(url):
    """
    Приклад парсингу лота з Віоліті або подібного сайту для отримання зразка.
    Повертає словник {title, image_url, price}.
    """
    try:
        resp = requests.get(url, headers=get_headers(), timeout=10)
        soup = BeautifulSoup(resp.text, 'lxml')
        
        # Спрощена логіка для Violity (потрібно адаптувати під реальну верстку)
        # Це приклад універсального пошуку картинки і заголовка
        title = soup.find('h1').text.strip() if soup.find('h1') else "Unknown Item"
        
        # Шукаємо основне зображення
        img_tag = soup.find('a', class_='highslide') 
        img_url = img_tag['href'] if img_tag else None
        
        if not img_url:
            # Fallback
            imgs = soup.find_all('img')
            for i in imgs:
                if 'jpg' in i.get('src', '') and len(i.get('src', '')) > 50:
                    img_url = i['src']
                    break

        return {
            'title': title,
            'image_url': img_url,
            'source_url': url
        }
    except Exception as e:
        logger.error(f"Scrape error: {e}")
        return None

async def search_olx(query, min_price=0):
    """Шукає товари на OLX за запитом."""
    search_query = query.replace(" ", "-")
    url = f"https://www.olx.ua/uk/list/q-{search_query}/"
    
    results = []
    try:
        resp = requests.get(url, headers=get_headers(), timeout=10)
        soup = BeautifulSoup(resp.text, 'lxml')
        
        # Актуальний селектор для карток OLX (може змінюватися)
        items = soup.find_all('div', {'data-cy': 'l-card'})
        
        for item in items[:10]: # Беремо перші 10 результатів
            try:
                link_tag = item.find('a', href=True)
                url = link_tag['href']
                if not url.startswith('http'):
                    url = "https://www.olx.ua" + url
                
                title_tag = item.find('h6')
                title = title_tag.text.strip() if title_tag else "No Title"
                
                img_tag = item.find('img')
                img_url = img_tag['src'] if img_tag else None
                
                # Перевірка на копію (примітивна логіка по тексту)
                is_copy = "копия" in title.lower() or "реплика" in title.lower() or "copy" in title.lower()
                status = "⚠️ КОПІЯ" if is_copy else "✅ ОРИГІНАЛ (Ймовірно)"

                if img_url:
                    results.append({
                        'title': title,
                        'url': url,
                        'image_url': img_url,
                        'status': status
                    })
            except Exception as e:
                continue
                
    except Exception as e:
        logger.error(f"OLX Search error: {e}")
        
    return results

# --- БОТ ЛОГІКА ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "👋 Бот запущено!\n\n"
        "Команди:\n"
        "/add_source <url> - Додати посилання на джерело (лот) для відстеження.\n"
        "/list_targets - Показати, що ми шукаємо.\n"
        "/clear_targets - Очистити список пошуку.\n\n"
        "Бот автоматично:\n"
        "1. Сканує джерела.\n"
        "2. Постить зразок в канал.\n"
        "3. Шукає схожі на OLX (в т.ч. на групових фото).\n"
        "4. Постить результати."
    )

async def add_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("Вкажіть посилання! Приклад: /add_source https://violity.com/...")
        return

    url = context.args[0]
    await update.message.reply_text("⏳ Сканую джерело...")
    
    # 1. Отримуємо дані про предмет
    item_data = await scrape_violity_sample(url)
    
    if not item_data or not item_data['image_url']:
        await update.message.reply_text("❌ Не вдалося отримати фото або назву з цього посилання.")
        return

    # 2. Зберігаємо в targets.json
    targets = load_json(TARGETS_FILE, [])
    # Перевірка на дублі
    if any(t['source_url'] == url for t in targets):
        await update.message.reply_text("⚠️ Цей предмет вже є в списку відстеження.")
        return

    targets.append(item_data)
    save_json(TARGETS_FILE, targets)
    
    await update.message.reply_text(f"✅ Додано: {item_data['title']}.\nПочинаю пошук...")
    
    # Одразу постимо в канал, що ми шукаємо цей предмет
    try:
        caption = (
            f"🔭 **НОВА ЦІЛЬ ПОШУКУ**\n\n"
            f"🏷 **Назва:** {item_data['title']}\n"
            f"🔗 [Джерело зразка]({item_data['source_url']})\n\n"
            f"🤖 Починаю моніторинг OLX..."
        )
        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=item_data['image_url'],
            caption=caption,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to post to channel: {e}")

async def list_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    targets = load_json(TARGETS_FILE, [])
    if not targets:
        await update.message.reply_text("Список пустий.")
        return
    text = "\n".join([f"- {t['title']}" for t in targets])
    await update.message.reply_text(f"Ми шукаємо:\n{text}")

async def clear_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    save_json(TARGETS_FILE, [])
    await update.message.reply_text("Список цілей очищено.")

# --- ФОНОВА ЗАДАЧА (LOOP) ---

async def monitoring_task(context: ContextTypes.DEFAULT_TYPE):
    """Ця функція працює постійно у фоні"""
    targets = load_json(TARGETS_FILE, [])
    history = load_json(HISTORY_FILE, [])
    
    if not targets:
        return

    logger.info(f"Starting scan cycle for {len(targets)} targets...")

    for target in targets:
        # 1. Завантажуємо референсне зображення (cv2)
        ref_img = vision.download_image_cv(target['image_url'])
        if ref_img is None:
            continue

        # 2. Шукаємо на OLX за назвою
        olx_results = await search_olx(target['title'])
        
        for res in olx_results:
            # Перевірка чи ми вже бачили це оголошення
            if res['url'] in history:
                continue
            
            # 3. Аналіз зображення (ML/CV)
            target_img = vision.download_image_cv(res['image_url'])
            is_match, score = vision.compare_images(ref_img, target_img)
            
            if is_match:
                # Знайдено збіг!
                logger.info(f"Match found! Score: {score} for {res['title']}")
                
                caption = (
                    f"🚨 **ЗНАЙДЕНО НА OLX!**\n\n"
                    f"🔍 **Шукали:** {target['title']}\n"
                    f"📦 **Знахідка:** {res['title']}\n"
                    f"🛡 **Статус:** {res['status']}\n"
                    f"📊 **Схожість (ORB Score):** {score}\n\n"
                    f"👉 [Перейти до оголошення]({res['url']})"
                )
                
                try:
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=res['image_url'],
                        caption=caption,
                        parse_mode="Markdown"
                    )
                    # Додаємо в історію, щоб не спамити
                    history.append(res['url'])
                    save_json(HISTORY_FILE, history)
                except Exception as e:
                    logger.error(f"Error sending photo: {e}")
            
            # Невелика затримка, щоб не блокувати цикл
            await asyncio.sleep(1)

    # Очистка історії, якщо вона дуже велика (>1000)
    if len(history) > 1000:
        history = history[-500:]
        save_json(HISTORY_FILE, history)

# --- ЗАПУСК ---

def main():
    # Перевіряємо наявність файлів
    load_json(SOURCES_FILE, [])
    load_json(TARGETS_FILE, [])
    load_json(HISTORY_FILE, [])

    application = Application.builder().token(TOKEN).build()

    # Додаємо команди
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add_source", add_source))
    application.add_handler(CommandHandler("list_targets", list_targets))
    application.add_handler(CommandHandler("clear_targets", clear_targets))

    # Додаємо фонову задачу (JobQueue)
    # Запускати кожні 5 хвилин (300 секунд)
    if application.job_queue:
        application.job_queue.run_repeating(monitoring_task, interval=300, first=10)

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
