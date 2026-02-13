

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
        
        # Налаштування пошуку
        self.SIMILARITY_THRESHOLD = 0.70  # Знижуємо поріг для більшої кількості збігів
        self.SCAN_INTERVAL = 180  # Скануємо кожні 3 хвилини
        self.MAX_TARGETS_PER_SCAN = 10  # Більше цілей за раз
        self.MAX_ADS_PER_TARGET = 30  # Більше оголошень на ціль
        self.MAX_IMAGES_PER_AD = 5  # Більше фото на оголошення
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
        self.YOLO_CONFIDENCE = 0.3  # Знижуємо поріг впевненості
        
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

class Logger:
    def __init__(self):
        self.logger = logging.getLogger("OLXMonitor")
        self.logger.setLevel(logging.INFO)
        
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        log_file = CONFIG.LOGS_DIR / "olx_monitor.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10*1024*1024, backupCount=5
        )
        file_handler.setFormatter(formatter)
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
    
    def info(self, msg): self.logger.info(msg)
    def error(self, msg): self.logger.error(msg)
    def warning(self, msg): self.logger.warning(msg)
    def debug(self, msg): self.logger.debug(msg)

log = Logger()

# ============================================================================
# [3] БАЗА ДАНИХ
# ============================================================================

class Database:
    def __init__(self):
        self.conn = sqlite3.connect(CONFIG.DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
    
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
        return await asyncio.get_event_loop().run_in_executor(
            None, self._sync_execute, query, params
        )
    
    def _sync_execute(self, query, params):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        self.conn.commit()
        return cursor
    
    async def fetch_all(self, query: str, params: tuple = ()):
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
            return True
        except Exception as e:
            log.error(f"DB add_target error: {e}")
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
    
    async def delete_all_targets(self):
        targets = await self.get_targets()
        for t in targets:
            if os.path.exists(t['path']):
                try:
                    os.remove(t['path'])
                except: pass
        await self.execute('DELETE FROM targets')
        await self.execute('DELETE FROM seen_ads')
    
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
        cache_key = f"{path1}:{path2}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        
        img1 = cv2.imread(path1)
        img2 = cv2.imread(path2)
        
        if img1 is None or img2 is None:
            return 0.0
        
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
        try:
            ssim_score = ssim(gray1, gray2)
        except:
            ssim_score = 0.5
        
        # Фінальний скор
        final = (phash_score * 0.3 + sift_score * 0.4 + ssim_score * 0.3)
        final = min(1.0, max(0.0, final))
        
        self.cache[cache_key] = final
        if len(self.cache) > 1000:
            self.cache.clear()
        
        return final
    
    def clear_cache(self):
        self.cache.clear()
        log.info("🧹 CV кеш очищено")

cv_engine = CVEngine()

# ============================================================================
# [6] OLX ПАРСЕР - ПОСИЛЕНИЙ
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
        """Посилений пошук по OLX з пагінацією"""
        session = await self.get_session()
        all_ads = []
        seen_urls = set()
        
        for page in range(1, pages + 1):
            # Формуємо URL з пагінацією
            if page == 1:
                url = f"https://www.olx.ua/d/uk/list/q-{query.replace(' ', '-')}/"
            else:
                url = f"https://www.olx.ua/d/uk/list/q-{query.replace(' ', '-')}/?page={page}"
            
            try:
                headers = {
                    'User-Agent': self.ua.random,
                    'Accept-Language': 'uk-UA,uk;q=0.9,en;q=0.8,ru;q=0.7',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Referer': 'https://www.olx.ua/'
                }
                
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        log.warning(f"OLX сторінка {page}: {resp.status}")
                        break
                    
                    html = await resp.text()
                
                soup = BeautifulSoup(html, 'lxml')
                
                # Основні селектори для OLX
                cards = soup.select('div[data-cy="l-card"]')
                
                if not cards:
                    # Альтернативні селектори
                    cards = soup.select('div.css-1apmciz, div.css-1sw7q4x, div.offer-wrapper')
                
                if not cards:
                    log.debug(f"Немає карток на сторінці {page}")
                    break
                
                log.info(f"📄 Сторінка {page}: знайдено {len(cards)} оголошень")
                
                for card in cards:
                    try:
                        # Пропускаємо TOP/VIP
                        if card.select_one('[data-testid="adCard-featured"], .css-14nq1co, .featured'):
                            continue
                        
                        # Заголовок
                        title_elem = (
                            card.select_one('h6') or 
                            card.select_one('a.css-1bbgabe') or 
                            card.select_one('a[href*="/d/uk/"]') or
                            card.select_one('a[class*="title"]')
                        )
                        
                        if not title_elem:
                            continue
                        
                        title = title_elem.get_text(strip=True)
                        if not title or len(title) < 3:
                            continue
                        
                        # Посилання
                        link_elem = title_elem if title_elem.name == 'a' else card.select_one('a[href]')
                        if not link_elem:
                            continue
                        
                        href = link_elem.get('href', '')
                        if not href:
                            continue
                        
                        ad_url = href if href.startswith('http') else f"https://www.olx.ua{href}"
                        
                        # Уникаємо дублікатів на сторінці
                        if ad_url in seen_urls:
                            continue
                        seen_urls.add(ad_url)
                        
                        # Ціна
                        price_elem = (
                            card.select_one('[data-testid="ad-price"]') or
                            card.select_one('.css-10b0b6q') or
                            card.select_one('.price') or
                            card.select_one('[class*="price"]')
                        )
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
                                
                                # Шукаємо додаткові зображення в галереї
                                gallery_imgs = card.select('img[src*="images.olx.ua"]')
                                for g_img in gallery_imgs[:4]:
                                    g_url = g_img.get('src') or g_img.get('data-src')
                                    if g_url and g_url not in images and not g_url.endswith('.svg'):
                                        if g_url.startswith('//'):
                                            g_url = 'https:' + g_url
                                        images.append(g_url)
                        
                        # Перевіряємо чи це не сміття
                        if len(title) < 5 or "ремонт" in title.lower() or "послуг" in title.lower():
                            continue
                        
                        all_ads.append({
                            'title': title,
                            'price': price,
                            'url': ad_url,
                            'images': images[:CONFIG.MAX_IMAGES_PER_AD],
                            'page': page
                        })
                        
                    except Exception as e:
                        log.debug(f"Помилка парсингу картки: {e}")
                        continue
                
                # Затримка між сторінками
                if page < pages:
                    await asyncio.sleep(random.uniform(2, 4))
                
            except Exception as e:
                log.error(f"Помилка завантаження сторінки {page}: {e}")
                break
        
        log.info(f"✅ Знайдено всього {len(all_ads)} оголошень для '{query}'")
        return all_ads[:CONFIG.MAX_ADS_PER_TARGET]

olx = OLXParser()

# ============================================================================
# [7] МОНІТОРИНГ - ПОСИЛЕНИЙ
# ============================================================================

class Monitor:
    def __init__(self):
        self.is_running = False
        self.task = None
        self.stats = {'processed': 0, 'matches': 0, 'errors': 0, 'ads_checked': 0}
    
    async def start(self, context):
        if self.is_running:
            return
        self.is_running = True
        self.task = asyncio.create_task(self._run(context))
        log.info("🚀 Моніторинг запущено")
    
    async def stop(self):
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except:
                pass
        log.info("🛑 Моніторинг зупинено")
    
    async def _run(self, context):
        while self.is_running:
            try:
                targets = await DB.get_targets()
                
                if not targets:
                    await asyncio.sleep(30)
                    continue
                
                log.info(f"🎯 Починаємо сканування {len(targets)} цілей")
                
                for target in targets[:CONFIG.MAX_TARGETS_PER_SCAN]:
                    try:
                        await self._process_target(target, context)
                        self.stats['processed'] += 1
                        await asyncio.sleep(random.uniform(3, 6))
                    except Exception as e:
                        self.stats['errors'] += 1
                        log.error(f"Помилка цілі {target['name']}: {e}")
                
                # Відправляємо знайдені матчі в канал
                await self._send_pending_matches(context)
                
                log.info(f"⏳ Наступне сканування через {CONFIG.SCAN_INTERVAL} сек")
                await asyncio.sleep(CONFIG.SCAN_INTERVAL)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Помилка моніторингу: {e}")
                await asyncio.sleep(60)
    
    async def _process_target(self, target: Dict, context):
        """Посилена обробка цілі"""
        if not os.path.exists(target['path']):
            log.warning(f"Фото цілі не знайдено: {target['path']}")
            return
        
        log.info(f"🔍 Скануємо: {target['name']}")
        
        # Пошук по OLX з пагінацією
        ads = await olx.search(target['name'], pages=CONFIG.SEARCH_PAGES)
        
        if not ads:
            log.debug(f"Немає оголошень для {target['name']}")
            await DB.update_target_stats(target['id'])
            return
        
        # Фільтруємо вже переглянуті
        new_ads = []
        for ad in ads:
            if not await DB.is_ad_seen(ad['url']):
                new_ads.append(ad)
        
        log.info(f"📊 {target['name']}: {len(ads)} всього, {len(new_ads)} нових")
        
        if not new_ads:
            await DB.update_target_stats(target['id'])
            return
        
        # Аналізуємо нові оголошення
        for ad in new_ads[:15]:  # Обмежуємо для швидкості
            best_score = 0.0
            best_image = None
            
            for img_url in ad.get('images', [])[:CONFIG.MAX_IMAGES_PER_AD]:
                score = await self._analyze_image(target['path'], img_url)
                if score > best_score:
                    best_score = score
                    best_image = img_url
            
            if best_score >= CONFIG.SIMILARITY_THRESHOLD:
                await DB.add_match(target, ad, best_score, best_image)
                self.stats['matches'] += 1
                log.info(f"✅ ЗБІГ! {target['name']} - {best_score:.1%}")
            
            self.stats['ads_checked'] += 1
        
        await DB.update_target_stats(target['id'])
    
    async def _analyze_image(self, target_path: str, img_url: str) -> float:
        """Аналіз зображення з YOLO"""
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
                    if len(content) < 1000:  # Занадто мале зображення
                        return 0.0
                    async with aiofiles.open(temp_path, 'wb') as f:
                        await f.write(content)
            
            # YOLO детекція
            detections = yolo.detect(str(temp_path))
            
            if detections:
                best_score = 0.0
                img = cv2.imread(str(temp_path))
                
                if img is not None:
                    for det in detections[:3]:
                        bbox = det['bbox']
                        x1, y1, x2, y2 = map(int, bbox)
                        x1, y1 = max(0, x1), max(0, y1)
                        x2, y2 = min(img.shape[1], x2), min(img.shape[0], y2)
                        
                        if x2 > x1 and y2 > y1:
                            crop = img[y1:y2, x1:x2]
                            
                            if crop.size > 0:
                                crop_path = CONFIG.CACHE_DIR / f"crop_{secrets.token_hex(6)}.jpg"
                                cv2.imwrite(str(crop_path), crop)
                                
                                score = cv_engine.compare(target_path, str(crop_path))
                                best_score = max(best_score, score)
                                
                                crop_path.unlink(missing_ok=True)
                
                temp_path.unlink(missing_ok=True)
                return best_score if best_score > 0 else cv_engine.compare(target_path, str(temp_path))
            else:
                score = cv_engine.compare(target_path, str(temp_path))
                temp_path.unlink(missing_ok=True)
                return score
                
        except Exception as e:
            log.debug(f"Помилка аналізу зображення: {e}")
            temp_path.unlink(missing_ok=True)
            return 0.0
    
    async def _send_pending_matches(self, context):
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
                    ['реплік', 'копі', 'replica', 'clone', 'aaa', '1:1', 'підроб', 'fake'])
                
                # Форматуємо ціну
                price = match['ad_price']
                if len(price) > 20:
                    price = price[:20] + "..."
                
                # Формуємо повідомлення
                caption = (
                    f"🔥 <b>ЗНАЙДЕНО ТОВАР!</b>\n\n"
                    f"🎯 <b>Ціль:</b> {match['target_name']}\n"
                    f"📦 <b>Назва:</b> {match['ad_title'][:150]}\n"
                    f"💰 <b>Ціна:</b> {price}\n"
                    f"📊 <b>Схожість:</b> {match['similarity']:.1%}\n"
                )
                
                if is_replica:
                    caption += f"⚠️ <b>УВАГА! Можлива репліка/копія</b>\n"
                
                caption += f"\n🔗 <a href='{match['ad_url']}'>🔍 ПЕРЕЙТИ ДО ОГОЛОШЕННЯ</a>"
                
                # Відправляємо
                if match['image_url']:
                    await context.bot.send_photo(
                        chat_id=CONFIG.CHANNEL_ID,
                        photo=match['image_url'],
                        caption=caption,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await context.bot.send_message(
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
        self.app.add_handler(CallbackQueryHandler(self.callback_handler))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        self.app.add_error_handler(self.error_handler)
    
    async def post_init(self, app):
        await self._web_server()
        asyncio.create_task(self._auto_start())
        log.info("✅ Бот ініціалізовано")
    
    async def _web_server(self):
        app = web.Application()
        app.router.add_get('/', self.web_index)
        app.router.add_get('/api/stats', self.web_stats)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', CONFIG.PORT)
        await site.start()
        log.info(f"🌐 Веб-дашборд: http://0.0.0.0:{CONFIG.PORT}")
    
    async def _auto_start(self):
        await asyncio.sleep(5)
        await monitor.start(ContextTypes.DEFAULT_TYPE(application=self.app))
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update or not update.effective_user:
            return
        
        if update.effective_user.id != CONFIG.ADMIN_ID:
            await update.message.reply_text("⛔ Доступ заборонено")
            return
        
        targets = await DB.get_targets()
        matches = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches')
        match_count = matches[0]['cnt'] if matches else 0
        unsent = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches WHERE sent_to_channel = 0')
        unsent_count = unsent[0]['cnt'] if unsent else 0
        
        keyboard = [
            [InlineKeyboardButton("🎯 ЦІЛІ", callback_data="targets_list"),
             InlineKeyboardButton("➕ ДОДАТИ", callback_data="add_target")],
            [InlineKeyboardButton("▶️ СТАРТ", callback_data="monitor_start"),
             InlineKeyboardButton("⏹ СТОП", callback_data="monitor_stop")],
            [InlineKeyboardButton("📊 СТАТИСТИКА", callback_data="stats"),
             InlineKeyboardButton("🧹 ОЧИСТИТИ", callback_data="clean_cache")],
        ]
        
        await update.message.reply_text(
            f"🏭 <b>OLX Monitor Pro v1.0</b>\n"
            f"AI моніторинг OLX.ua\n\n"
            f"📊 <b>Статус:</b>\n"
            f"• Цілей в базі: {len(targets)}\n"
            f"• Знайдено матчів: {match_count}\n"
            f"• Очікує відправки: {unsent_count}\n"
            f"• Поріг схожості: {int(CONFIG.SIMILARITY_THRESHOLD*100)}%\n"
            f"• Моніторинг: {'🟢 АКТИВНИЙ' if monitor.is_running else '🔴 ЗУПИНЕНО'}\n"
            f"• Перевірено оголошень: {monitor.stats['ads_checked']}\n"
            f"• Знайдено збігів: {monitor.stats['matches']}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update or not update.effective_user:
            return
        
        if update.effective_user.id != CONFIG.ADMIN_ID:
            await update.message.reply_text("⛔ Доступ заборонено")
            return
        
        targets = await DB.get_targets()
        matches = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches')
        seen = await DB.fetch_all('SELECT COUNT(*) as cnt FROM seen_ads')
        
        await update.message.reply_text(
            f"📈 <b>Детальна статистика</b>\n\n"
            f"<b>Система:</b>\n"
            f"• CPU: {os.cpu_count()} cores\n"
            f"• YOLO: {'✅' if yolo.model else '❌'}\n"
            f"• Кеш CV: {len(cv_engine.cache)}\n\n"
            f"<b>База даних:</b>\n"
            f"• Активних цілей: {len(targets)}\n"
            f"• Всього матчів: {matches[0]['cnt'] if matches else 0}\n"
            f"• Переглянуто оголошень: {seen[0]['cnt'] if seen else 0}\n\n"
            f"<b>Моніторинг:</b>\n"
            f"• Всього сканувань: {monitor.stats['processed']}\n"
            f"• Знайдено збігів: {monitor.stats['matches']}\n"
            f"• Перевірено фото: {monitor.stats['ads_checked']}\n"
            f"• Помилок: {monitor.stats['errors']}",
            parse_mode=ParseMode.HTML
        )
    
    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                await query.edit_message_text("📸 Надішліть фото товару для додавання в базу")
            
            elif data == "monitor_start":
                await monitor.start(context)
                await query.edit_message_text("🚀 Моніторинг запущено")
            
            elif data == "monitor_stop":
                await monitor.stop()
                await query.edit_message_text("🛑 Моніторинг зупинено")
            
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
        tid = query.data.replace("target_del_yes_", "")
        await DB.delete_target(tid)
        await query.edit_message_text("✅ Ціль видалено")
        await asyncio.sleep(1)
        await self._show_targets(query)
    
    async def _targets_clear_confirm(self, query):
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
        await DB.delete_all_targets()
        await query.edit_message_text("✅ Всі цілі видалено")
    
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update or not update.message:
            return
        
        if context.user_data.get("state") != "wait_img":
            return
        
        try:
            file = await update.message.photo[-1].get_file()
            filename = f"target_{secrets.token_hex(8)}.jpg"
            path = CONFIG.TARGETS_DIR / filename
            await file.download_to_drive(path)
            
            context.user_data["tmp_p"] = str(path)
            context.user_data["state"] = "wait_name"
            
            await update.message.reply_text(
                "✅ Фото збережено!\n"
                "📝 Введіть назву товару:"
            )
            
        except Exception as e:
            log.error(f"Помилка збереження фото: {e}")
            await update.message.reply_text("❌ Помилка збереження фото")
    
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update or not update.message:
            return
        
        if context.user_data.get("state") == "wait_name":
            name = update.message.text.strip()
            tmp_path = context.user_data.get("tmp_p")
            
            if not tmp_path or not os.path.exists(tmp_path):
                await update.message.reply_text("❌ Фото не знайдено")
                context.user_data.clear()
                return
            
            target = {
                'id': f"TGT_{secrets.token_hex(4)}",
                'name': name[:100],
                'path': tmp_path,
                'created': int(time.time()),
                'priority': 1
            }
            
            if await DB.add_target(target):
                await update.message.reply_text(f"✅ Ціль '{name}' додана!")
            else:
                await update.message.reply_text("❌ Помилка додавання цілі")
            
            context.user_data.clear()
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Безпечний обробник помилок"""
        error_msg = f"❌ Помилка: {str(context.error)[:150]}"
        log.error(f"Update {update.update_id if update else 'N/A'} caused error: {context.error}")
        
        try:
            if update and update.callback_query:
                await update.callback_query.edit_message_text(error_msg)
            elif update and update.message:
                await update.message.reply_text(error_msg)
        except:
            pass
    
    async def web_index(self, request):
        targets = await DB.get_targets()
        matches = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches')
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>OLX Monitor Pro</title>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #0a0a0c; color: #e4e4e7; margin: 0; padding: 30px; }}
                .container {{ max-width: 1200px; margin: 0 auto; }}
                h1 {{ color: #4caf50; font-size: 2.5em; margin-bottom: 30px; }}
                .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
                .card {{ background: #16161a; border-radius: 12px; padding: 25px; border: 1px solid #2a2a2e; }}
                .value {{ font-size: 2.5em; font-weight: bold; color: #4caf50; margin: 10px 0; }}
                .label {{ color: #888; text-transform: uppercase; font-size: 0.9em; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🏭 OLX Monitor Pro v1.0</h1>
                <div class="stats">
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
        targets = await DB.get_targets()
        matches = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches')
        
        return web.json_response({
            'targets': len(targets),
            'matches': matches[0]['cnt'] if matches else 0,
            'monitor': monitor.is_running,
            'processed': monitor.stats['processed'],
            'matches_found': monitor.stats['matches'],
            'ads_checked': monitor.stats['ads_checked'],
            'cache_size': len(cv_engine.cache)
        })
    
    def run(self):
        print("""
        ╔════════════════════════════════════════════════════════════╗
        ║     OLX Monitor Pro v1.0                                  ║
        ║     AI моніторинг OLX.ua                                  ║
        ║     High-Speed · YOLOv8 · CV Engine · Channel Posts       ║
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
