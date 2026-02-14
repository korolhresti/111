

import os
import json
import time
import asyncio
import random
import hashlib
import secrets
import logging
import logging.handlers
import re
import traceback
import gc
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple, Union, Set
from collections import deque, defaultdict
from pathlib import Path

import aiohttp
import aiofiles
from aiohttp import web
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

import cv2
import numpy as np
from PIL import Image

from skimage.metrics import structural_similarity as ssim

from ultralytics import YOLO

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters
)
from telegram.constants import ParseMode

# ============================================================================
# [1] КОНФІГУРАЦІЯ
# ============================================================================

class Config:
    def __init__(self):
        # Telegram
        self.TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
        self.CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
        self.PORT = int(os.getenv("PORT", 8080))
        
        # Налаштування пошуку - ПОСИЛЕНІ
        self.SIMILARITY_THRESHOLD = 0.60  # Знижуємо поріг для більшої кількості збігів
        self.SCAN_INTERVAL = 60  # Скануємо кожну ХВИЛИНУ
        self.MAX_TARGETS_PER_SCAN = 20  # Більше цілей за раз
        self.MAX_ADS_PER_TARGET = 50  # Більше оголошень на ціль
        self.SEARCH_PAGES = 3  # Кількість сторінок пошуку
        
        # Шляхи
        self.BASE_DIR = Path(__file__).parent.resolve()
        self.DATA_DIR = self.BASE_DIR / "olx_data"
        self.CACHE_DIR = self.DATA_DIR / "cache"
        self.LOGS_DIR = self.DATA_DIR / "logs"
        self.TARGETS_DIR = self.DATA_DIR / "targets"
        
        for d in [self.DATA_DIR, self.CACHE_DIR, self.LOGS_DIR, self.TARGETS_DIR]:
            d.mkdir(parents=True, exist_ok=True)
        
        self.DB_PATH = self.DATA_DIR / "olx_monitor.db"
        
        # YOLO
        self.YOLO_MODEL = "yolov8n.pt"
        self.YOLO_CONFIDENCE = 0.25  # Ще нижчий поріг
        
        self._validate()
    
    def _validate(self):
        if not self.TOKEN or ":" not in self.TOKEN:
            raise RuntimeError("❌ Invalid TELEGRAM_BOT_TOKEN")
        if self.ADMIN_ID == 0:
            raise RuntimeError("❌ ADMIN_CHAT_ID missing")
        if self.CHANNEL_ID == 0:
            self.CHANNEL_ID = self.ADMIN_ID

CONFIG = Config()

# ============================================================================
# [2] ЛОГУВАННЯ
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger("OLXMonitor")

# Додаємо файловий логер
log_file = CONFIG.LOGS_DIR / "olx_monitor.log"
file_handler = logging.handlers.RotatingFileHandler(
    log_file, maxBytes=10*1024*1024, backupCount=5
)
file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
log.addHandler(file_handler)

# ============================================================================
# [3] БАЗА ДАНИХ
# ============================================================================

class Database:
    def __init__(self):
        self.conn = sqlite3.connect(CONFIG.DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self.lock = asyncio.Lock()
    
    def _init_db(self):
        cursor = self.conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS targets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                created INTEGER,
                priority INTEGER DEFAULT 1,
                search_count INTEGER DEFAULT 0,
                match_count INTEGER DEFAULT 0,
                last_search INTEGER DEFAULT 0
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id TEXT,
                target_name TEXT,
                ad_title TEXT,
                ad_price TEXT,
                ad_url TEXT UNIQUE,
                similarity REAL,
                image_url TEXT,
                timestamp INTEGER,
                sent_to_channel INTEGER DEFAULT 0
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS seen_ads (
                ad_url TEXT PRIMARY KEY,
                first_seen INTEGER,
                last_seen INTEGER,
                target_id TEXT
            )
        ''')
        
        self.conn.commit()
    
    async def execute(self, query: str, params: tuple = ()):
        async with self.lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._sync_execute, query, params
            )
    
    def _sync_execute(self, query, params):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        self.conn.commit()
        return cursor
    
    async def fetch_all(self, query: str, params: tuple = ()):
        async with self.lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._sync_fetch_all, query, params
            )
    
    def _sync_fetch_all(self, query, params):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]
    
    async def fetch_one(self, query: str, params: tuple = ()):
        rows = await self.fetch_all(query, params)
        return rows[0] if rows else None
    
    async def add_target(self, target: Dict) -> bool:
        try:
            query = '''
                INSERT OR REPLACE INTO targets 
                (id, name, path, created, priority)
                VALUES (?, ?, ?, ?, ?)
            '''
            await self.execute(query, (
                target['id'],
                target['name'],
                target['path'],
                target.get('created', int(time.time())),
                target.get('priority', 1)
            ))
            log.info(f"✅ Ціль додано: {target['name']}")
            return True
        except Exception as e:
            log.error(f"❌ Помилка додавання цілі: {e}")
            return False
    
    async def get_targets(self) -> List[Dict]:
        return await self.fetch_all('SELECT * FROM targets ORDER BY priority DESC, created DESC')
    
    async def delete_target(self, target_id: str):
        target = await self.fetch_one('SELECT path FROM targets WHERE id = ?', (target_id,))
        if target and os.path.exists(target['path']):
            try:
                os.remove(target['path'])
            except: pass
        await self.execute('DELETE FROM targets WHERE id = ?', (target_id,))
        await self.execute('DELETE FROM seen_ads WHERE target_id = ?', (target_id,))
        log.info(f"🗑 Ціль видалено: {target_id}")
    
    async def delete_all_targets(self):
        targets = await self.get_targets()
        for t in targets:
            if os.path.exists(t['path']):
                try:
                    os.remove(t['path'])
                except: pass
        await self.execute('DELETE FROM targets')
        await self.execute('DELETE FROM seen_ads')
        log.info("🗑 Всі цілі видалено")
    
    async def is_ad_seen(self, ad_url: str) -> bool:
        result = await self.fetch_one('SELECT ad_url FROM seen_ads WHERE ad_url = ?', (ad_url,))
        return result is not None
    
    async def mark_ad_seen(self, ad_url: str, target_id: str):
        now = int(time.time())
        await self.execute('''
            INSERT OR REPLACE INTO seen_ads (ad_url, first_seen, last_seen, target_id)
            VALUES (?, ?, ?, ?)
        ''', (ad_url, now, now, target_id))
    
    async def update_target_stats(self, target_id: str, found_match: bool = False):
        if found_match:
            await self.execute('UPDATE targets SET match_count = match_count + 1 WHERE id = ?', (target_id,))
        await self.execute('UPDATE targets SET search_count = search_count + 1, last_search = ? WHERE id = ?', 
                          (int(time.time()), target_id))
    
    async def add_match(self, target: Dict, ad: Dict, similarity: float, image_url: str = None):
        try:
            query = '''
                INSERT OR IGNORE INTO matches 
                (target_id, target_name, ad_title, ad_price, ad_url, similarity, image_url, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            '''
            await self.execute(query, (
                target['id'],
                target['name'],
                ad['title'][:200],
                ad['price'],
                ad['url'],
                similarity,
                image_url or (ad.get('images', [])[0] if ad.get('images') else None),
                int(time.time())
            ))
            
            await self.update_target_stats(target['id'], found_match=True)
            await self.mark_ad_seen(ad['url'], target['id'])
            log.info(f"🔥 ЗБІГ! {target['name']} - {similarity:.1%}")
            return True
        except Exception as e:
            log.error(f"❌ Помилка додавання збігу: {e}")
            return False
    
    async def get_unsent_matches(self) -> List[Dict]:
        return await self.fetch_all(
            'SELECT * FROM matches WHERE sent_to_channel = 0 ORDER BY timestamp DESC LIMIT 50'
        )
    
    async def mark_match_sent(self, match_id: int):
        await self.execute('UPDATE matches SET sent_to_channel = 1 WHERE id = ?', (match_id,))

DB = Database()

# ============================================================================
# [4] YOLO ДЕТЕКТОР
# ============================================================================

class YOLODetector:
    def __init__(self):
        self.model = None
        self.load_model()
    
    def load_model(self):
        try:
            self.model = YOLO(CONFIG.YOLO_MODEL)
            log.info("✅ YOLO модель завантажена")
            return True
        except Exception as e:
            log.error(f"❌ Помилка завантаження YOLO: {e}")
            return False
    
    def detect(self, image_path: str) -> List[Dict]:
        if self.model is None:
            return []
        
        try:
            results = self.model(image_path, conf=CONFIG.YOLO_CONFIDENCE, verbose=False)
            detections = []
            
            for r in results:
                if r.boxes is None:
                    continue
                for box in r.boxes:
                    cls = int(box.cls[0])
                    label = self.model.names.get(cls, "")
                    conf = float(box.conf[0])
                    
                    detections.append({
                        'label': label,
                        'confidence': conf,
                        'bbox': box.xyxy[0].tolist()
                    })
            return detections
        except Exception as e:
            log.error(f"YOLO помилка: {e}")
            return []

yolo = YOLODetector()

# ============================================================================
# [5] CV ENGINE
# ============================================================================

class CVEngine:
    def __init__(self):
        self.cache = {}
        self.sift = cv2.SIFT_create()
        self.bf = cv2.BFMatcher()
    
    def _phash(self, img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        resized = cv2.resize(gray, (32, 32))
        dct = cv2.dct(np.float32(resized))
        dct_low = dct[:8, :8]
        median = np.median(dct_low)
        return (dct_low > median).flatten()
    
    def compare(self, path1: str, path2: str) -> float:
        if not os.path.exists(path1) or not os.path.exists(path2):
            return 0.0
        
        cache_key = f"{path1}:{path2}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        
        img1 = cv2.imread(path1)
        img2 = cv2.imread(path2)
        
        if img1 is None or img2 is None:
            return 0.0
        
        try:
            img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
            
            # pHash
            h1 = self._phash(img1)
            h2 = self._phash(img2)
            phash_score = 1.0 - (np.count_nonzero(h1 != h2) / len(h1))
            
            # SIFT
            kp1, des1 = self.sift.detectAndCompute(img1, None)
            kp2, des2 = self.sift.detectAndCompute(img2, None)
            
            sift_score = 0.0
            if des1 is not None and des2 is not None and len(kp1) > 0 and len(kp2) > 0:
                matches = self.bf.knnMatch(des1, des2, k=2)
                good = []
                for m, n in matches:
                    if m.distance < 0.75 * n.distance:
                        good.append(m)
                sift_score = len(good) / max(len(kp1), len(kp2), 1)
            
            # SSIM
            gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
            gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
            ssim_score = ssim(gray1, gray2)
            
            # Фінальний скор
            final = (phash_score * 0.3 + sift_score * 0.4 + ssim_score * 0.3)
            final = min(1.0, max(0.0, final))
            
            self.cache[cache_key] = final
            if len(self.cache) > 1000:
                self.cache.clear()
            
            return final
        except Exception as e:
            log.debug(f"CV помилка: {e}")
            return 0.0
    
    def clear_cache(self):
        self.cache.clear()
        log.info("🧹 CV кеш очищено")

cv_engine = CVEngine()

# ============================================================================
# [6] OLX ПАРСЕР
# ============================================================================

class OLXParser:
    def __init__(self):
        self.ua = UserAgent()
        self.session = None
    
    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self.session
    
    async def search(self, query: str, pages: int = 3) -> List[Dict]:
        """Пошук по OLX з пагінацією"""
        session = await self.get_session()
        all_ads = []
        seen_urls = set()
        
        for page in range(1, pages + 1):
            # Формуємо URL
            url = f"https://www.olx.ua/d/uk/list/q-{query.replace(' ', '-')}/"
            if page > 1:
                url += f"?page={page}"
            
            try:
                headers = {
                    'User-Agent': self.ua.random,
                    'Accept-Language': 'uk-UA,uk;q=0.9,en;q=0.8',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Referer': 'https://www.olx.ua/'
                }
                
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        log.warning(f"OLX сторінка {page}: {resp.status}")
                        break
                    
                    html = await resp.text()
                
                soup = BeautifulSoup(html, 'lxml')
                
                # Шукаємо картки
                cards = soup.select('div[data-cy="l-card"]')
                
                if not cards:
                    cards = soup.select('div.css-1apmciz, div.css-1sw7q4x')
                
                if not cards:
                    log.debug(f"Немає карток на сторінці {page}")
                    break
                
                log.info(f"📄 Сторінка {page}: {len(cards)} оголошень")
                
                for card in cards:
                    try:
                        # Пропускаємо TOP
                        if card.select_one('[data-testid="adCard-featured"]'):
                            continue
                        
                        # Заголовок
                        title_elem = card.select_one('h6')
                        if not title_elem:
                            title_elem = card.select_one('a.css-1bbgabe')
                        
                        if not title_elem:
                            continue
                        
                        title = title_elem.get_text(strip=True)
                        if not title or len(title) < 3:
                            continue
                        
                        # Посилання
                        link_elem = card.select_one('a[href]')
                        if not link_elem:
                            continue
                        
                        href = link_elem.get('href', '')
                        ad_url = href if href.startswith('http') else f"https://www.olx.ua{href}"
                        
                        if ad_url in seen_urls:
                            continue
                        seen_urls.add(ad_url)
                        
                        # Ціна
                        price_elem = card.select_one('[data-testid="ad-price"]')
                        if not price_elem:
                            price_elem = card.select_one('.css-10b0b6q')
                        
                        price = price_elem.get_text(strip=True) if price_elem else "—"
                        
                        # Зображення
                        img_elem = card.select_one('img')
                        images = []
                        if img_elem:
                            img_url = img_elem.get('src') or img_elem.get('data-src')
                            if img_url and not img_url.endswith('.svg'):
                                if img_url.startswith('//'):
                                    img_url = 'https:' + img_url
                                images.append(img_url)
                        
                        all_ads.append({
                            'title': title,
                            'price': price,
                            'url': ad_url,
                            'images': images,
                            'page': page
                        })
                        
                    except Exception as e:
                        continue
                
                # Затримка між сторінками
                if page < pages:
                    await asyncio.sleep(random.uniform(1, 3))
                
            except Exception as e:
                log.error(f"Помилка завантаження сторінки {page}: {e}")
                break
        
        log.info(f"✅ Знайдено {len(all_ads)} оголошень для '{query}'")
        return all_ads[:CONFIG.MAX_ADS_PER_TARGET]

olx = OLXParser()

# ============================================================================
# [7] МОНІТОРИНГ - ФІКСОВАНИЙ
# ============================================================================

class Monitor:
    def __init__(self):
        self.is_running = False
        self.task = None
        self.stats = {
            'processed': 0, 
            'matches': 0, 
            'errors': 0, 
            'ads_checked': 0
        }
    
    async def start(self, app):
        """Запуск моніторингу"""
        if self.is_running:
            log.info("Моніторинг вже працює")
            return
        
        self.is_running = True
        # Створюємо задачу з правильним контекстом
        self.task = asyncio.create_task(self._run(app))
        log.info("🚀 Моніторинг запущено")
        return self.task
    
    async def stop(self):
        """Зупинка моніторингу"""
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error(f"Помилка при зупинці: {e}")
        log.info("🛑 Моніторинг зупинено")
    
    async def _run(self, app):
        """Головний цикл моніторингу"""
        log.info("🔄 Запуск головного циклу моніторингу")
        
        while self.is_running:
            try:
                # Отримуємо цілі
                targets = await DB.get_targets()
                
                if not targets:
                    log.debug("Немає цілей для моніторингу")
                    await asyncio.sleep(30)
                    continue
                
                log.info(f"🎯 Починаємо сканування {len(targets)} цілей")
                
                # Беремо пріоритетні цілі
                targets_to_scan = targets[:CONFIG.MAX_TARGETS_PER_SCAN]
                
                for target in targets_to_scan:
                    try:
                        await self._process_target(target, app)
                        self.stats['processed'] += 1
                        # Коротка пауза між цілями
                        await asyncio.sleep(random.uniform(2, 5))
                    except Exception as e:
                        self.stats['errors'] += 1
                        log.error(f"Помилка цілі {target['name']}: {e}")
                
                # Відправляємо знайдені матчі в канал
                await self._send_pending_matches(app)
                
                log.info(f"⏳ Наступне сканування через {CONFIG.SCAN_INTERVAL} сек")
                await asyncio.sleep(CONFIG.SCAN_INTERVAL)
                
            except asyncio.CancelledError:
                log.info("Моніторинг скасовано")
                break
            except Exception as e:
                log.error(f"Помилка в головному циклі: {e}")
                await asyncio.sleep(30)
    
    async def _process_target(self, target: Dict, app):
        """Обробка однієї цілі"""
        if not os.path.exists(target['path']):
            log.warning(f"Фото цілі не знайдено: {target['path']}")
            return
        
        log.info(f"🔍 Скануємо: {target['name']}")
        
        # Пошук по OLX
        ads = await olx.search(target['name'], pages=CONFIG.SEARCH_PAGES)
        
        if not ads:
            log.debug(f"Немає оголошень для {target['name']}")
            await DB.update_target_stats(target['id'])
            return
        
        # Фільтруємо нові оголошення
        new_ads = []
        for ad in ads:
            if not await DB.is_ad_seen(ad['url']):
                new_ads.append(ad)
        
        log.info(f"📊 {target['name']}: {len(ads)} всього, {len(new_ads)} нових")
        
        # Аналізуємо нові оголошення
        for ad in new_ads:
            if not ad.get('images'):
                continue
            
            best_score = 0.0
            best_image = None
            
            for img_url in ad['images'][:3]:  # Максимум 3 фото
                score = await self._analyze_image(target['path'], img_url)
                if score > best_score:
                    best_score = score
                    best_image = img_url
            
            if best_score >= CONFIG.SIMILARITY_THRESHOLD:
                await DB.add_match(target, ad, best_score, best_image)
                self.stats['matches'] += 1
            
            self.stats['ads_checked'] += 1
        
        await DB.update_target_stats(target['id'])
    
    async def _analyze_image(self, target_path: str, img_url: str) -> float:
        """Аналіз зображення"""
        if not img_url:
            return 0.0
        
        temp_path = CONFIG.CACHE_DIR / f"img_{secrets.token_hex(6)}.jpg"
        
        try:
            # Завантажуємо зображення
            async with aiohttp.ClientSession() as session:
                async with session.get(img_url, timeout=15) as resp:
                    if resp.status != 200:
                        return 0.0
                    content = await resp.read()
                    if len(content) < 1000:
                        return 0.0
                    async with aiofiles.open(temp_path, 'wb') as f:
                        await f.write(content)
            
            # YOLO детекція
            score = cv_engine.compare(target_path, str(temp_path))
            
            # Видаляємо тимчасовий файл
            temp_path.unlink(missing_ok=True)
            return score
                
        except Exception as e:
            log.debug(f"Помилка аналізу зображення: {e}")
            temp_path.unlink(missing_ok=True)
            return 0.0
    
    async def _send_pending_matches(self, app):
        """Відправка знайдених збігів в канал"""
        matches = await DB.get_unsent_matches()
        
        if not matches:
            return
        
        log.info(f"📨 Відправляємо {len(matches)} збігів в канал")
        
        for match in matches:
            try:
                # Перевіряємо на репліки
                title_lower = match['ad_title'].lower()
                is_replica = any(k in title_lower for k in 
                    ['реплік', 'копі', 'replica', 'clone', 'aaa', '1:1', 'підроб'])
                
                # Формуємо повідомлення
                caption = (
                    f"🔥 <b>ЗНАЙДЕНО ТОВАР!</b>\n\n"
                    f"🎯 <b>Ціль:</b> {match['target_name']}\n"
                    f"📦 <b>Назва:</b> {match['ad_title'][:150]}\n"
                    f"💰 <b>Ціна:</b> {match['ad_price']}\n"
                    f"📊 <b>Схожість:</b> {match['similarity']:.1%}\n"
                )
                
                if is_replica:
                    caption += f"⚠️ <b>Можлива репліка!</b>\n"
                
                caption += f"\n🔗 <a href='{match['ad_url']}'>🔍 ПЕРЕЙТИ ДО ОГОЛОШЕННЯ</a>"
                
                # Відправляємо
                if match['image_url']:
                    await app.bot.send_photo(
                        chat_id=CONFIG.CHANNEL_ID,
                        photo=match['image_url'],
                        caption=caption,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await app.bot.send_message(
                        chat_id=CONFIG.CHANNEL_ID,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=False
                    )
                
                await DB.mark_match_sent(match['id'])
                log.info(f"✅ Відправлено в канал: {match['target_name']}")
                await asyncio.sleep(1)
                
            except Exception as e:
                log.error(f"Помилка відправки в канал: {e}")

# Глобальний екземпляр монітора
monitor = Monitor()

# ============================================================================
# [8] TELEGRAM БОТ
# ============================================================================

class OLXBot:
    def __init__(self):
        self.app = ApplicationBuilder() \
            .token(CONFIG.TOKEN) \
            .post_init(self.post_init) \
            .build()
        
        self._handlers()
    
    def _handlers(self):
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("stats", self.cmd_stats))
        self.app.add_handler(CommandHandler("scan", self.cmd_scan_now))  # Команда для примусового сканування
        self.app.add_handler(CallbackQueryHandler(self.callback_handler))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        self.app.add_error_handler(self.error_handler)
    
    async def post_init(self, app):
        """Ініціалізація після запуску"""
        # Запускаємо веб-сервер
        await self._web_server()
        
        # Запускаємо моніторинг
        asyncio.create_task(self._start_monitor_delayed(app))
        
        log.info("✅ Бот ініціалізовано")
    
    async def _start_monitor_delayed(self, app):
        """Запуск моніторингу з затримкою"""
        await asyncio.sleep(5)
        await monitor.start(app)
    
    async def _web_server(self):
        """Запуск веб-сервера для статусу"""
        app = web.Application()
        app.router.add_get('/', self.web_index)
        app.router.add_get('/api/stats', self.web_stats)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', CONFIG.PORT)
        await site.start()
        log.info(f"🌐 Веб-дашборд: http://0.0.0.0:{CONFIG.PORT}")
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        if not update or not update.effective_user:
            return
        
        if update.effective_user.id != CONFIG.ADMIN_ID:
            await update.message.reply_text("⛔ Доступ заборонено")
            return
        
        targets = await DB.get_targets()
        
        # Формуємо клавіатуру
        keyboard = [
            [InlineKeyboardButton("🎯 ЦІЛІ", callback_data="targets_list"),
             InlineKeyboardButton("➕ ДОДАТИ", callback_data="add_target")],
            [InlineKeyboardButton("▶️ СТАРТ", callback_data="monitor_start"),
             InlineKeyboardButton("⏹ СТОП", callback_data="monitor_stop")],
            [InlineKeyboardButton("📊 СТАТИСТИКА", callback_data="stats"),
             InlineKeyboardButton("🧹 ОЧИСТИТИ", callback_data="clean_cache")],
            [InlineKeyboardButton("⚡ СКАНУВАТИ ЗАРАЗ", callback_data="scan_now")],
        ]
        
        await update.message.reply_text(
            f"🏭 <b>OLX Monitor Pro v2.0</b>\n"
            f"AI моніторинг OLX.ua\n\n"
            f"📊 <b>Статус:</b>\n"
            f"• Цілей в базі: {len(targets)}\n"
            f"• Моніторинг: {'🟢 АКТИВНИЙ' if monitor.is_running else '🔴 ЗУПИНЕНО'}\n"
            f"• Перевірено оголошень: {monitor.stats['ads_checked']}\n"
            f"• Знайдено збігів: {monitor.stats['matches']}\n"
            f"• Інтервал: {CONFIG.SCAN_INTERVAL} сек\n"
            f"• Поріг схожості: {int(CONFIG.SIMILARITY_THRESHOLD*100)}%",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /stats"""
        if update.effective_user.id != CONFIG.ADMIN_ID:
            await update.message.reply_text("⛔ Доступ заборонено")
            return
        
        targets = await DB.get_targets()
        matches = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches')
        seen = await DB.fetch_all('SELECT COUNT(*) as cnt FROM seen_ads')
        
        await update.message.reply_text(
            f"📈 <b>Детальна статистика</b>\n\n"
            f"<b>Система:</b>\n"
            f"• Моніторинг: {'АКТИВНИЙ' if monitor.is_running else 'ЗУПИНЕНО'}\n"
            f"• YOLO: {'✅' if yolo.model else '❌'}\n"
            f"• CV кеш: {len(cv_engine.cache)}\n\n"
            f"<b>База даних:</b>\n"
            f"• Активних цілей: {len(targets)}\n"
            f"• Всього матчів: {matches[0]['cnt'] if matches else 0}\n"
            f"• Переглянуто оголошень: {seen[0]['cnt'] if seen else 0}\n\n"
            f"<b>За поточну сесію:</b>\n"
            f"• Сканувань: {monitor.stats['processed']}\n"
            f"• Збігів: {monitor.stats['matches']}\n"
            f"• Перевірено фото: {monitor.stats['ads_checked']}\n"
            f"• Помилок: {monitor.stats['errors']}",
            parse_mode=ParseMode.HTML
        )
    
    async def cmd_scan_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Примусове сканування зараз"""
        if update.effective_user.id != CONFIG.ADMIN_ID:
            return
        
        await update.message.reply_text("⏳ Запускаю примусове сканування...")
        
        # Запускаємо сканування в окремому завданні
        asyncio.create_task(self._force_scan(update, context))
    
    async def _force_scan(self, update: Update, context):
        """Примусове сканування"""
        try:
            targets = await DB.get_targets()
            if not targets:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text="❌ Немає цілей для сканування"
                )
                return
            
            count = 0
            for target in targets[:5]:  # Максимум 5 за раз
                ads = await olx.search(target['name'], pages=2)
                if ads:
                    count += len(ads)
                await asyncio.sleep(2)
            
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=f"✅ Примусове сканування завершено!\nПеревірено {count} оголошень"
            )
        except Exception as e:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=f"❌ Помилка: {str(e)[:100]}"
            )
    
    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка натискань кнопок"""
        if not update or not update.callback_query:
            return
        
        query = update.callback_query
        await query.answer()
        data = query.data
        
        try:
            if data == "targets_list":
                await self._show_targets(query)
            
            elif data.startswith("target_del_"):
                await self._target_delete_confirm(query)
            
            elif data.startswith("target_del_yes_"):
                await self._target_delete(query)
            
            elif data == "targets_clear_all":
                await self._targets_clear_confirm(query)
            
            elif data == "targets_clear_confirm":
                await self._targets_clear(query)
            
            elif data == "add_target":
                context.user_data["state"] = "wait_img"
                await query.edit_message_text(
                    "📸 Надішліть фото товару.\n"
                    "Назва буде взята з файлу або створена автоматично."
                )
            
            elif data == "monitor_start":
                await monitor.start(context.application)
                await query.edit_message_text("🚀 Моніторинг запущено")
            
            elif data == "monitor_stop":
                await monitor.stop()
                await query.edit_message_text("🛑 Моніторинг зупинено")
            
            elif data == "scan_now":
                await query.edit_message_text("⏳ Запускаю сканування...")
                # Запускаємо сканування
                targets = await DB.get_targets()
                count = 0
                for target in targets[:5]:
                    ads = await olx.search(target['name'], pages=2)
                    count += len(ads)
                    await asyncio.sleep(2)
                await query.edit_message_text(f"✅ Сканування завершено!\nПеревірено {count} оголошень")
            
            elif data == "stats":
                await self.cmd_stats(update, context)
            
            elif data == "clean_cache":
                cv_engine.clear_cache()
                count = 0
                for f in CONFIG.CACHE_DIR.glob("*.jpg"):
                    f.unlink(missing_ok=True)
                    count += 1
                await query.edit_message_text(f"🧹 Кеш очищено! Видалено {count} файлів")
            
            elif data == "back":
                await self.cmd_start(update, context)
                
        except Exception as e:
            log.error(f"Callback помилка: {e}")
            try:
                await query.edit_message_text(f"❌ Помилка: {str(e)[:100]}")
            except:
                pass
    
    async def _show_targets(self, query):
        """Показати список цілей"""
        targets = await DB.get_targets()
        
        if not targets:
            await query.edit_message_text("❌ Список цілей порожній")
            return
        
        keyboard = []
        for t in targets[:10]:
            keyboard.append([
                InlineKeyboardButton(
                    f"🗑 {t['name'][:30]} ({t.get('match_count', 0)} збігів)",
                    callback_data=f"target_del_{t['id']}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("❌ ВИДАЛИТИ ВСЕ", callback_data="targets_clear_all")])
        keyboard.append([InlineKeyboardButton("◀️ НАЗАД", callback_data="back")])
        
        await query.edit_message_text(
            f"🎯 <b>Цілі ({len(targets)}):</b>\n"
            f"Натисніть для видалення:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _target_delete_confirm(self, query):
        """Підтвердження видалення"""
        tid = query.data.replace("target_del_", "")
        target = await DB.fetch_one('SELECT * FROM targets WHERE id = ?', (tid,))
        
        if not target:
            await query.edit_message_text("❌ Ціль не знайдено")
            return
        
        keyboard = [
            [
                InlineKeyboardButton("✅ ТАК", callback_data=f"target_del_yes_{tid}"),
                InlineKeyboardButton("❌ НІ", callback_data="targets_list")
            ]
        ]
        
        await query.edit_message_text(
            f"⚠️ <b>Видалити ціль?</b>\n\n"
            f"📦 {target['name']}\n"
            f"🎯 Збігів: {target.get('match_count', 0)}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _target_delete(self, query):
        """Видалення цілі"""
        tid = query.data.replace("target_del_yes_", "")
        await DB.delete_target(tid)
        await query.edit_message_text("✅ Ціль видалено")
        await asyncio.sleep(1)
        await self._show_targets(query)
    
    async def _targets_clear_confirm(self, query):
        """Підтвердження видалення всіх"""
        targets = await DB.get_targets()
        
        keyboard = [
            [
                InlineKeyboardButton("🔥 ПІДТВЕРДИТИ", callback_data="targets_clear_confirm"),
                InlineKeyboardButton("❌ СКАСУВАТИ", callback_data="targets_list")
            ]
        ]
        
        await query.edit_message_text(
            f"⚠️ <b>ВИДАЛИТИ ВСІ ЦІЛІ?</b>\n"
            f"Всього: {len(targets)} шт.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _targets_clear(self, query):
        """Видалення всіх цілей"""
        await DB.delete_all_targets()
        await query.edit_message_text("✅ Всі цілі видалено")
    
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка фото - додавання цілі"""
        if context.user_data.get("state") != "wait_img":
            return
        
        try:
            # Отримуємо фото
            photo = update.message.photo[-1]
            file = await photo.get_file()
            
            # Генеруємо назву з дати та часу
            now = datetime.now()
            default_name = f"Товар {now.strftime('%d.%m %H:%M')}"
            
            # Зберігаємо фото
            filename = f"target_{secrets.token_hex(8)}.jpg"
            path = CONFIG.TARGETS_DIR / filename
            await file.download_to_drive(path)
            
            # Додаємо ціль з автоматичною назвою
            target = {
                'id': f"TGT_{secrets.token_hex(4)}",
                'name': default_name,
                'path': str(path),
                'created': int(time.time()),
                'priority': 1
            }
            
            if await DB.add_target(target):
                await update.message.reply_text(
                    f"✅ Ціль додана!\n"
                    f"📝 Назва: {default_name}\n"
                    f"🆔 ID: {target['id']}\n\n"
                    f"Можете змінити назву командою /rename {target['id']} нова_назва"
                )
            else:
                await update.message.reply_text("❌ Помилка додавання цілі")
            
            context.user_data.clear()
            
        except Exception as e:
            log.error(f"Помилка обробки фото: {e}")
            await update.message.reply_text(f"❌ Помилка: {str(e)[:100]}")
            context.user_data.clear()
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробник помилок"""
        error_msg = f"❌ Помилка: {str(context.error)[:150]}"
        log.error(f"Помилка: {context.error}")
        
        try:
            if update and update.callback_query:
                await update.callback_query.edit_message_text(error_msg)
            elif update and update.message:
                await update.message.reply_text(error_msg)
        except:
            pass
    
    async def web_index(self, request):
        """Головна сторінка веб-дашборду"""
        targets = await DB.get_targets()
        matches = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches')
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>OLX Monitor Pro</title>
            <meta charset="UTF-8">
            <meta http-equiv="refresh" content="30">
            <style>
                body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #0a0a0c; color: #e4e4e7; margin: 0; padding: 30px; }}
                .container {{ max-width: 1200px; margin: 0 auto; }}
                h1 {{ color: #4caf50; font-size: 2.5em; margin-bottom: 30px; }}
                .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
                .card {{ background: #16161a; border-radius: 12px; padding: 25px; border: 1px solid #2a2a2e; }}
                .value {{ font-size: 2.5em; font-weight: bold; color: #4caf50; margin: 10px 0; }}
                .label {{ color: #888; text-transform: uppercase; font-size: 0.9em; }}
                .status {{ padding: 5px 10px; border-radius: 5px; display: inline-block; }}
                .active {{ background: #1e3a2e; color: #4caf50; }}
                .inactive {{ background: #3a1e1e; color: #f44336; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🏭 OLX Monitor Pro v2.0</h1>
                <div class="stats">
                    <div class="card">
                        <div class="label">Статус моніторингу</div>
                        <div class="value">
                            <span class="status {'active' if monitor.is_running else 'inactive'}">
                                {'АКТИВНИЙ' if monitor.is_running else 'ЗУПИНЕНО'}
                            </span>
                        </div>
                    </div>
                    <div class="card">
                        <div class="label">Активні цілі</div>
                        <div class="value">{len(targets)}</div>
                    </div>
                    <div class="card">
                        <div class="label">Знайдено матчів</div>
                        <div class="value">{matches[0]['cnt'] if matches else 0}</div>
                    </div>
                    <div class="card">
                        <div class="label">Перевірено оголошень</div>
                        <div class="value">{monitor.stats['ads_checked']}</div>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')
    
    async def web_stats(self, request):
        """API для статистики"""
        targets = await DB.get_targets()
        matches = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches')
        
        return web.json_response({
            'status': 'active' if monitor.is_running else 'stopped',
            'targets': len(targets),
            'matches': matches[0]['cnt'] if matches else 0,
            'processed': monitor.stats['processed'],
            'matches_found': monitor.stats['matches'],
            'ads_checked': monitor.stats['ads_checked'],
            'errors': monitor.stats['errors'],
            'cache_size': len(cv_engine.cache),
            'interval': CONFIG.SCAN_INTERVAL,
            'threshold': CONFIG.SIMILARITY_THRESHOLD
        })
    
    def run(self):
        """Запуск бота"""
        print("""
        ╔════════════════════════════════════════════════════════════╗
        ║     OLX Monitor Pro v2.0                                  ║
        ║     AI моніторинг OLX.ua                                  ║
        ║     Автоматичне сканування · Пости в канал                ║
        ╚════════════════════════════════════════════════════════════╝
        """)
        
        log.info("🚀 Запуск OLX Monitor...")
        self.app.run_polling(drop_pending_updates=True)

# ============================================================================
# [9] MAIN
# ============================================================================

def main():
    try:
        bot = OLXBot()
        bot.run()
    except KeyboardInterrupt:
        log.info("🛑 Бот зупинено користувачем")
    except Exception as e:
        print(f"💥 КРИТИЧНА ПОМИЛКА: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
