
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

# Встановіть: pip install curl-cffi
from curl_cffi import requests as curl_requests
from curl_cffi.requests import BrowserType

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
    CallbackQueryHandler, MessageHandler, filters, ConversationHandler
)
from telegram.constants import ParseMode

# ============================================================================
# [1] КОНФІГУРАЦІЯ
# ============================================================================

# Стани для ConversationHandler
WAITING_FOR_PHOTO, WAITING_FOR_NAME = range(2)

class Config:
    def __init__(self):
        # Telegram
        self.TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
        self.CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
        self.PORT = int(os.getenv("PORT", 8080"))
        
        # ===== НАЛАШТУВАННЯ ПОШУКУ =====
        self.SIMILARITY_THRESHOLD = 0.80
        self.DEFAULT_SEARCH_QUERY = "годинник б у"
        
        # ===== АНТИ-БЛОК НАЛАШТУВАННЯ =====
        self.USE_PROXY = os.getenv("USE_PROXY", "false").lower() == "true"
        self.PROXY_URL = os.getenv("PROXY_URL", "")
        self.REQUEST_TIMEOUT = 30
        self.MIN_DELAY = 5
        self.MAX_DELAY = 15
        self.MAX_RETRIES = 3
        
        # Налаштування пошуку
        self.SCAN_INTERVAL = 600
        self.MAX_TARGETS_PER_SCAN = 3
        self.MAX_ADS_PER_TARGET = 20
        self.SEARCH_PAGES = 2
        
        # Шляхи
        self.BASE_DIR = Path(__file__).parent.resolve()
        self.DATA_DIR = self.BASE_DIR / "watch_data"
        self.CACHE_DIR = self.DATA_DIR / "cache"
        self.LOGS_DIR = self.DATA_DIR / "logs"
        self.TARGETS_DIR = self.DATA_DIR / "targets"
        
        for d in [self.DATA_DIR, self.CACHE_DIR, self.LOGS_DIR, self.TARGETS_DIR]:
            d.mkdir(parents=True, exist_ok=True)
        
        self.DB_PATH = self.DATA_DIR / "watch_finder.db"
        
        # YOLO
        self.YOLO_MODEL = "yolov8n.pt"
        self.YOLO_CONFIDENCE = 0.3
        
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
log = logging.getLogger("WatchFinder")

log_file = CONFIG.LOGS_DIR / "watch_finder.log"
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
                search_query TEXT NOT NULL,
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
                (id, name, path, search_query, created, priority)
                VALUES (?, ?, ?, ?, ?, ?)
            '''
            await self.execute(query, (
                target['id'],
                target['name'],
                target['path'],
                target.get('search_query', CONFIG.DEFAULT_SEARCH_QUERY),
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
            'SELECT * FROM matches WHERE sent_to_channel = 0 ORDER BY similarity DESC, timestamp DESC LIMIT 50'
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
    
    def detect_watch(self, image_path: str) -> bool:
        if self.model is None:
            return True
        
        try:
            results = self.model(image_path, conf=0.3, verbose=False)
            watch_classes = ['watch', 'clock']
            
            for r in results:
                if r.boxes is None:
                    continue
                for box in r.boxes:
                    cls = int(box.cls[0])
                    label = self.model.names.get(cls, "").lower()
                    if any(watch_class in label for watch_class in watch_classes):
                        del results
                        gc.collect()
                        return True
            
            del results
            gc.collect()
            return False
        except Exception as e:
            log.error(f"YOLO помилка: {e}")
            return True

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
            
            final = (phash_score * 0.35 + sift_score * 0.35 + ssim_score * 0.30)
            final = min(1.0, max(0.0, final))
            
            self.cache[cache_key] = final
            if len(self.cache) > 500:
                self.cache.clear()
            
            del img1, img2, gray1, gray2, kp1, kp2, des1, des2
            gc.collect()
            
            return final
        except Exception as e:
            log.debug(f"CV помилка: {e}")
            return 0.0
    
    def clear_cache(self):
        self.cache.clear()
        gc.collect()
        log.info("🧹 CV кеш очищено")

cv_engine = CVEngine()

# ============================================================================
# [6] OLX ПАРСЕР - ВИПРАВЛЕНИЙ
# ============================================================================

class OLXParser:
    def __init__(self):
        self.ua = UserAgent()
        self.last_request_time = 0
        self.request_count = 0
        
        # ПРАВИЛЬНИЙ список браузерів - використовуємо об'єкти BrowserType
        self.browsers = [
            BrowserType.chrome120,
            BrowserType.chrome119,
            BrowserType.firefox110,
            BrowserType.edge101,
            BrowserType.safari17_0
        ]
    
    async def _get_delay(self):
        now = time.time()
        if self.last_request_time > 0:
            elapsed = now - self.last_request_time
            if elapsed < CONFIG.MIN_DELAY:
                delay = random.uniform(CONFIG.MIN_DELAY, CONFIG.MAX_DELAY)
                log.info(f"⏳ Пауза {delay:.0f} секунд...")
                await asyncio.sleep(delay)
        
        self.last_request_time = time.time()
        self.request_count += 1
        
        if self.request_count % 10 == 0:
            log.info("🔄 Довга пауза після 10 запитів...")
            await asyncio.sleep(30)
    
    async def fetch_page(self, url: str, retry: int = 0) -> Optional[str]:
        await self._get_delay()
        
        try:
            # Вибираємо випадковий браузер
            browser = random.choice(self.browsers)
            
            headers = {
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'uk-UA,uk;q=0.9,en;q=0.8,ru;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
            
            proxy = CONFIG.PROXY_URL if CONFIG.USE_PROXY else None
            
            # Виконуємо запит
            response = curl_requests.get(
                url,
                headers=headers,
                impersonate=browser,
                proxy=proxy,
                timeout=CONFIG.REQUEST_TIMEOUT,
                allow_redirects=True
            )
            
            if response.status_code == 200:
                log.info(f"✅ Запит успішний")
                return response.text
            elif response.status_code == 403:
                log.warning(f"❌ Блок 403! Спроба {retry + 1}/{CONFIG.MAX_RETRIES}")
                if retry < CONFIG.MAX_RETRIES - 1:
                    wait_time = random.uniform(60, 120)
                    log.info(f"⏳ Очікування {wait_time:.0f} секунд...")
                    await asyncio.sleep(wait_time)
                    return await self.fetch_page(url, retry + 1)
                return None
            else:
                log.warning(f"⚠️ Статус: {response.status_code}")
                return None
                
        except Exception as e:
            log.error(f"❌ Помилка запиту: {e}")
            if retry < CONFIG.MAX_RETRIES - 1:
                wait_time = random.uniform(30, 60)
                log.info(f"⏳ Повторна спроба через {wait_time:.0f} секунд...")
                await asyncio.sleep(wait_time)
                return await self.fetch_page(url, retry + 1)
            return None
    
    async def search_watches(self, query: str, pages: int = 2) -> List[Dict]:
        """Пошук годинників за запитом"""
        all_ads = []
        seen_urls = set()
        
        query_encoded = query.replace(' ', '-')
        base_url = f"https://www.olx.ua/uk/list/q-{query_encoded}/"
        
        log.info(f"🔍 Пошук: {query}")
        
        for page in range(1, pages + 1):
            url = base_url
            if page > 1:
                url += f"?page={page}"
            
            html = await self.fetch_page(url)
            if not html:
                continue
            
            try:
                soup = BeautifulSoup(html, 'lxml')
                cards = soup.select('div[data-cy="l-card"]')
                
                if not cards:
                    cards = soup.select('div.css-1apmciz, div.css-1sw7q4x')
                
                if not cards:
                    log.debug(f"Немає карток на сторінці {page}")
                    continue
                
                log.info(f"📊 Сторінка {page}: {len(cards)} оголошень")
                
                for card in cards:
                    try:
                        if card.select_one('[data-testid="adCard-featured"]'):
                            continue
                        
                        title_elem = card.select_one('h6')
                        if not title_elem:
                            title_elem = card.select_one('a.css-1bbgabe')
                        
                        if not title_elem:
                            continue
                        
                        title = title_elem.get_text(strip=True)
                        
                        link_elem = card.select_one('a[href]')
                        if not link_elem:
                            continue
                        
                        href = link_elem.get('href', '')
                        ad_url = href if href.startswith('http') else f"https://www.olx.ua{href}"
                        
                        if ad_url in seen_urls:
                            continue
                        seen_urls.add(ad_url)
                        
                        price_elem = card.select_one('[data-testid="ad-price"]')
                        if not price_elem:
                            price_elem = card.select_one('.css-10b0b6q')
                        
                        price = price_elem.get_text(strip=True) if price_elem else "—"
                        
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
                
                if page < pages:
                    delay = random.uniform(10, 20)
                    log.info(f"⏳ Пауза {delay:.0f} секунд...")
                    await asyncio.sleep(delay)
                
            except Exception as e:
                log.error(f"Помилка парсингу: {e}")
                continue
        
        log.info(f"✅ Знайдено {len(all_ads)} оголошень для '{query}'")
        return all_ads[:CONFIG.MAX_ADS_PER_TARGET]

olx = OLXParser()

# ============================================================================
# [7] МОНІТОРИНГ
# ============================================================================

class WatchMonitor:
    def __init__(self):
        self.is_running = False
        self.task = None
        self.stats = {
            'processed': 0, 
            'matches': 0, 
            'errors': 0, 
            'ads_checked': 0,
            'above_80': 0
        }
    
    async def start(self, app):
        if self.is_running:
            return
        
        self.is_running = True
        self.task = asyncio.create_task(self._run(app))
        log.info("🚀 Моніторинг запущено")
        return self.task
    
    async def stop(self):
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        log.info("🛑 Моніторинг зупинено")
    
    async def _run(self, app):
        log.info("🔄 Головний цикл моніторингу")
        
        while self.is_running:
            try:
                targets = await DB.get_targets()
                
                if not targets:
                    await asyncio.sleep(60)
                    continue
                
                log.info(f"🎯 Сканування {len(targets)} цілей")
                
                for target in targets[:CONFIG.MAX_TARGETS_PER_SCAN]:
                    try:
                        await self._process_target(target, app)
                        self.stats['processed'] += 1
                        await asyncio.sleep(random.uniform(20, 30))
                        gc.collect()
                    except Exception as e:
                        self.stats['errors'] += 1
                        log.error(f"Помилка цілі {target['name']}: {e}")
                
                await self._send_pending_matches(app)
                
                log.info(f"⏳ Наступне сканування через {CONFIG.SCAN_INTERVAL} сек")
                await asyncio.sleep(CONFIG.SCAN_INTERVAL)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Помилка циклу: {e}")
                await asyncio.sleep(60)
    
    async def _process_target(self, target: Dict, app):
        if not os.path.exists(target['path']):
            log.warning(f"Фото не знайдено: {target['path']}")
            return
        
        search_query = target.get('search_query', CONFIG.DEFAULT_SEARCH_QUERY)
        log.info(f"🔍 Скануємо: {target['name']} (запит: {search_query})")
        
        ads = await olx.search_watches(search_query, pages=CONFIG.SEARCH_PAGES)
        
        if not ads:
            log.debug(f"Немає оголошень")
            await DB.update_target_stats(target['id'])
            return
        
        new_ads = []
        for ad in ads:
            if not await DB.is_ad_seen(ad['url']):
                new_ads.append(ad)
        
        log.info(f"📊 {len(ads)} всього, {len(new_ads)} нових")
        
        for ad in new_ads:
            if not ad.get('images'):
                continue
            
            best_score = 0.0
            best_image = None
            
            for img_url in ad['images'][:3]:
                score = await self._analyze_watch_image(target['path'], img_url)
                if score > best_score:
                    best_score = score
                    best_image = img_url
            
            if best_score >= CONFIG.SIMILARITY_THRESHOLD:
                await DB.add_match(target, ad, best_score, best_image)
                self.stats['matches'] += 1
                self.stats['above_80'] += 1
            
            self.stats['ads_checked'] += 1
            gc.collect()
        
        await DB.update_target_stats(target['id'])
    
    async def _analyze_watch_image(self, target_path: str, img_url: str) -> float:
        if not img_url:
            return 0.0
        
        temp_path = CONFIG.CACHE_DIR / f"watch_{secrets.token_hex(6)}.jpg"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(img_url, timeout=15) as resp:
                    if resp.status != 200:
                        return 0.0
                    content = await resp.read()
                    if len(content) < 1000:
                        return 0.0
                    async with aiofiles.open(temp_path, 'wb') as f:
                        await f.write(content)
            
            is_watch = yolo.detect_watch(str(temp_path))
            
            if not is_watch:
                temp_path.unlink(missing_ok=True)
                return 0.0
            
            score = cv_engine.compare(target_path, str(temp_path))
            
            temp_path.unlink(missing_ok=True)
            return score
                
        except Exception as e:
            log.debug(f"Помилка аналізу: {e}")
            temp_path.unlink(missing_ok=True)
            return 0.0
    
    async def _send_pending_matches(self, app):
        matches = await DB.get_unsent_matches()
        
        if not matches:
            return
        
        log.info(f"📨 Відправляємо {len(matches)} збігів в канал")
        
        for match in matches:
            try:
                caption = (
                    f"🔥 <b>ЗНАЙДЕНО ГОДИННИК!</b>\n\n"
                    f"🎯 <b>Ціль:</b> {match['target_name']}\n"
                    f"📦 <b>Опис:</b> {match['ad_title'][:150]}\n"
                    f"💰 <b>Ціна:</b> {match['ad_price']}\n"
                    f"📊 <b>Схожість:</b> {match['similarity']:.1%}\n"
                )
                
                if match['similarity'] >= 0.90:
                    caption += "🔥 ІДЕАЛЬНИЙ ЗБІГ!\n"
                
                caption += f"\n🔗 <a href='{match['ad_url']}'>🔍 ПЕРЕЙТИ ДО ОГОЛОШЕННЯ</a>"
                
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
                log.info(f"✅ Відправлено в канал")
                await asyncio.sleep(1)
                
            except Exception as e:
                log.error(f"Помилка відправки: {e}")

monitor = WatchMonitor()

# ============================================================================
# [8] TELEGRAM БОТ - ВИПРАВЛЕНИЙ
# ============================================================================

class WatchBot:
    def __init__(self):
        self.app = ApplicationBuilder() \
            .token(CONFIG.TOKEN) \
            .post_init(self.post_init) \
            .build()
        
        self._handlers()
    
    def _handlers(self):
        # ConversationHandler для додавання цілей
        conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.start_add_target, pattern="^add_target$")],
            states={
                WAITING_FOR_PHOTO: [MessageHandler(filters.PHOTO, self.handle_photo)],
                WAITING_FOR_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_name)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            per_message=False
        )
        
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("stats", self.cmd_stats))
        self.app.add_handler(CommandHandler("scan", self.cmd_scan_now))
        self.app.add_handler(conv_handler)
        self.app.add_handler(CallbackQueryHandler(self.callback_handler))
        self.app.add_error_handler(self.error_handler)
    
    async def post_init(self, app):
        await self._web_server()
        asyncio.create_task(self._start_monitor_delayed(app))
        log.info("✅ Бот ініціалізовано")
    
    async def _start_monitor_delayed(self, app):
        await asyncio.sleep(10)
        await monitor.start(app)
    
    async def _web_server(self):
        app = web.Application()
        app.router.add_get('/', self.web_index)
        app.router.add_get('/api/stats', self.web_stats)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', CONFIG.PORT)
        await site.start()
        log.info(f"🌐 Веб-дашборд: http://0.0.0.0:{CONFIG.PORT}")
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update or not update.effective_user:
            return
        
        if update.effective_user.id != CONFIG.ADMIN_ID:
            await update.message.reply_text("⛔ Доступ заборонено")
            return
        
        targets = await DB.get_targets()
        
        keyboard = [
            [InlineKeyboardButton("🎯 МОЇ ЦІЛІ", callback_data="targets_list")],
            [InlineKeyboardButton("➕ ДОДАТИ ГОДИННИК", callback_data="add_target")],
            [InlineKeyboardButton("▶️ СТАРТ МОНІТОРИНГУ", callback_data="monitor_start"),
             InlineKeyboardButton("⏹ СТОП", callback_data="monitor_stop")],
            [InlineKeyboardButton("📊 СТАТИСТИКА", callback_data="stats"),
             InlineKeyboardButton("🧹 ОЧИСТИТИ КЕШ", callback_data="clean_cache")],
            [InlineKeyboardButton("⚡ ШВИДКИЙ ПОШУК", callback_data="quick_search")],
        ]
        
        await update.message.reply_text(
            f"⌚ <b>Watch Finder Pro v5.0</b>\n"
            f"Візуальний пошук годинників на OLX\n\n"
            f"📊 <b>Статус:</b>\n"
            f"• Цілей в базі: {len(targets)}\n"
            f"• Моніторинг: {'🟢 АКТИВНИЙ' if monitor.is_running else '🔴 ЗУПИНЕНО'}\n"
            f"• Поріг збігу: 80%\n"
            f"• Інтервал: {CONFIG.SCAN_INTERVAL} сек",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def start_add_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Початок додавання цілі"""
        query = update.callback_query
        await query.answer()
        
        await query.edit_message_text(
            "📸 Надішліть фото годинника, який хочете знайти.\n\n"
            "Бот буде шукати схожі моделі на OLX за запитом 'годинник б у'"
        )
        
        return WAITING_FOR_PHOTO
    
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка фото"""
        try:
            photo = update.message.photo[-1]
            file = await photo.get_file()
            
            filename = f"watch_{secrets.token_hex(8)}.jpg"
            path = CONFIG.TARGETS_DIR / filename
            await file.download_to_drive(path)
            
            context.user_data['photo_path'] = str(path)
            
            keyboard = [
                [InlineKeyboardButton("✅ ГОДИННИК Б/У", callback_data="name_default")],
                [InlineKeyboardButton("✏️ ВВЕСТИ СВОЮ НАЗВУ", callback_data="name_custom")],
                [InlineKeyboardButton("❌ СКАСУВАТИ", callback_data="cancel_add")],
            ]
            
            await update.message.reply_text(
                "✅ Фото збережено!\n\n"
                "Оберіть пошуковий запит для OLX:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
            return WAITING_FOR_NAME
            
        except Exception as e:
            log.error(f"Помилка: {e}")
            await update.message.reply_text(f"❌ Помилка: {str(e)[:100]}")
            return ConversationHandler.END
    
    async def handle_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка введеної назви"""
        name = update.message.text.strip()
        photo_path = context.user_data.get('photo_path')
        
        if not photo_path or not os.path.exists(photo_path):
            await update.message.reply_text("❌ Фото не знайдено. Почніть заново.")
            return ConversationHandler.END
        
        target = {
            'id': f"WATCH_{secrets.token_hex(4)}",
            'name': f"Годинник {datetime.now().strftime('%d.%m %H:%M')}",
            'path': photo_path,
            'search_query': name,
            'created': int(time.time()),
            'priority': 1
        }
        
        if await DB.add_target(target):
            keyboard = [
                [InlineKeyboardButton("🎯 МОЇ ЦІЛІ", callback_data="targets_list")],
                [InlineKeyboardButton("➕ ДОДАТИ ЩЕ", callback_data="add_target")],
                [InlineKeyboardButton("🏠 ГОЛОВНЕ МЕНЮ", callback_data="main_menu")],
            ]
            
            await update.message.reply_text(
                f"✅ <b>ГОДИННИК ДОДАНО!</b>\n\n"
                f"🔍 Пошуковий запит: {name}\n\n"
                f"Бот почне пошук автоматично.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text("❌ Помилка додавання")
        
        context.user_data.clear()
        return ConversationHandler.END
    
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Скасування операції"""
        await update.message.reply_text("❌ Операцію скасовано")
        context.user_data.clear()
        return ConversationHandler.END
    
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != CONFIG.ADMIN_ID:
            return
        
        targets = await DB.get_targets()
        matches = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches')
        above_80 = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches WHERE similarity >= 0.8')
        
        await update.message.reply_text(
            f"📈 <b>Статистика</b>\n\n"
            f"<b>База даних:</b>\n"
            f"• Цілей: {len(targets)}\n"
            f"• Всього збігів: {matches[0]['cnt'] if matches else 0}\n"
            f"• Збігів 80%+: {above_80[0]['cnt'] if above_80 else 0}\n\n"
            f"<b>За поточну сесію:</b>\n"
            f"• Сканувань: {monitor.stats['processed']}\n"
            f"• Збігів: {monitor.stats['above_80']}\n"
            f"• Перевірено фото: {monitor.stats['ads_checked']}",
            parse_mode=ParseMode.HTML
        )
    
    async def cmd_scan_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != CONFIG.ADMIN_ID:
            return
        
        await update.message.reply_text("⏳ Запускаю швидкий пошук...")
        asyncio.create_task(self._quick_scan(update, context))
    
    async def _quick_scan(self, update: Update, context):
        try:
            targets = await DB.get_targets()
            if not targets:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text="❌ Немає цілей"
                )
                return
            
            total = 0
            found = 0
            
            for target in targets[:2]:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text=f"🔍 Шукаю {target['name']}..."
                )
                
                ads = await olx.search_watches(target['search_query'], pages=1)
                total += len(ads)
                
                for ad in ads[:5]:
                    if ad.get('images'):
                        score = await monitor._analyze_watch_image(target['path'], ad['images'][0])
                        if score >= 0.80:
                            found += 1
                
                await asyncio.sleep(20)
            
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=f"✅ Пошук завершено!\n"
                     f"Перевірено: {total} оголошень\n"
                     f"Збігів 80%+: {found}"
            )
        except Exception as e:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=f"❌ Помилка: {str(e)[:100]}"
            )
    
    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update or not update.callback_query:
            return
        
        query = update.callback_query
        data = query.data
        
        try:
            if data == "targets_list":
                await query.answer()
                await self._show_targets(query)
            
            elif data == "add_target":
                await query.answer()
                await self.start_add_target(update, context)
            
            elif data == "name_default":
                await query.answer()
                photo_path = context.user_data.get('photo_path')
                if photo_path:
                    target = {
                        'id': f"WATCH_{secrets.token_hex(4)}",
                        'name': f"Годинник {datetime.now().strftime('%d.%m %H:%M')}",
                        'path': photo_path,
                        'search_query': "годинник б у",
                        'created': int(time.time()),
                        'priority': 1
                    }
                    
                    if await DB.add_target(target):
                        keyboard = [
                            [InlineKeyboardButton("🎯 МОЇ ЦІЛІ", callback_data="targets_list")],
                            [InlineKeyboardButton("➕ ДОДАТИ ЩЕ", callback_data="add_target")],
                            [InlineKeyboardButton("🏠 ГОЛОВНЕ МЕНЮ", callback_data="main_menu")],
                        ]
                        
                        await query.edit_message_text(
                            f"✅ <b>ГОДИННИК ДОДАНО!</b>\n\n"
                            f"🔍 Пошуковий запит: годинник б/у\n\n"
                            f"Бот почне пошук автоматично.",
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode=ParseMode.HTML
                        )
                    context.user_data.clear()
            
            elif data == "name_custom":
                await query.answer()
                await query.edit_message_text(
                    "✏️ Напишіть назву для пошуку (наприклад: 'Casio', 'Omega', 'Seiko'):"
                )
            
            elif data == "cancel_add":
                await query.answer()
                context.user_data.clear()
                await query.edit_message_text("❌ Додавання скасовано")
            
            elif data == "monitor_start":
                await query.answer()
                await monitor.start(context.application)
                await query.edit_message_text("🚀 Моніторинг запущено")
            
            elif data == "monitor_stop":
                await query.answer()
                await monitor.stop()
                await query.edit_message_text("🛑 Моніторинг зупинено")
            
            elif data == "quick_search":
                await query.answer()
                await self._quick_search(query, context)
            
            elif data == "stats":
                await query.answer()
                await self.cmd_stats(update, context)
            
            elif data == "clean_cache":
                await query.answer()
                cv_engine.clear_cache()
                count = 0
                for f in CONFIG.CACHE_DIR.glob("*.jpg"):
                    f.unlink(missing_ok=True)
                    count += 1
                await query.edit_message_text(f"🧹 Кеш очищено! Видалено {count} файлів")
            
            elif data == "main_menu":
                await query.answer()
                await self.cmd_start(update, context)
            
            elif data.startswith("target_del_"):
                await query.answer()
                await self._target_delete_confirm(query)
            
            elif data.startswith("target_del_yes_"):
                await query.answer()
                await self._target_delete(query)
            
            elif data == "targets_clear_all":
                await query.answer()
                await self._targets_clear_confirm(query)
            
            elif data == "targets_clear_confirm":
                await query.answer()
                await self._targets_clear(query)
            
            elif data == "back":
                await query.answer()
                await self.cmd_start(update, context)
                
        except Exception as e:
            log.error(f"Callback помилка: {e}")
            try:
                await query.answer(f"❌ Помилка: {str(e)[:50]}")
            except:
                pass
    
    async def _quick_search(self, query, context):
        targets = await DB.get_targets()
        if not targets:
            await query.edit_message_text("❌ Немає цілей")
            return
        
        await query.edit_message_text("⏳ Виконую швидкий пошук...")
        
        found = 0
        for target in targets[:2]:
            ads = await olx.search_watches(target['search_query'], pages=1)
            for ad in ads[:3]:
                if ad.get('images'):
                    score = await monitor._analyze_watch_image(target['path'], ad['images'][0])
                    if score >= 0.80:
                        found += 1
            await asyncio.sleep(15)
        
        await query.edit_message_text(f"✅ Знайдено {found} збігів 80%+")
    
    async def _show_targets(self, query):
        targets = await DB.get_targets()
        
        if not targets:
            await query.edit_message_text("❌ Список порожній")
            return
        
        keyboard = []
        for t in targets[:10]:
            keyboard.append([
                InlineKeyboardButton(
                    f"🗑 {t['name'][:20]} ({t.get('match_count', 0)} збігів)",
                    callback_data=f"target_del_{t['id']}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("❌ ВИДАЛИТИ ВСЕ", callback_data="targets_clear_all")])
        keyboard.append([InlineKeyboardButton("◀️ НАЗАД", callback_data="main_menu")])
        
        await query.edit_message_text(
            f"🎯 <b>Мої цілі ({len(targets)}):</b>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _target_delete_confirm(self, query):
        tid = query.data.replace("target_del_", "")
        target = await DB.fetch_one('SELECT name FROM targets WHERE id = ?', (tid,))
        
        if not target:
            await query.edit_message_text("❌ Не знайдено")
            return
        
        keyboard = [
            [
                InlineKeyboardButton("✅ ТАК", callback_data=f"target_del_yes_{tid}"),
                InlineKeyboardButton("❌ НІ", callback_data="targets_list")
            ]
        ]
        
        await query.edit_message_text(
            f"⚠️ <b>Видалити?</b>\n\n{target['name']}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _target_delete(self, query):
        tid = query.data.replace("target_del_yes_", "")
        await DB.delete_target(tid)
        await query.edit_message_text("✅ Видалено")
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
            f"⚠️ <b>ВИДАЛИТИ ВСЕ?</b>\nВсього: {len(targets)} шт.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _targets_clear(self, query):
        await DB.delete_all_targets()
        await query.edit_message_text("✅ Все видалено")
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        log.error(f"Помилка: {context.error}")
        try:
            if update and update.effective_message:
                await update.effective_message.reply_text(
                    f"❌ Сталася помилка. Бот продовжує роботу."
                )
        except:
            pass
    
    async def web_index(self, request):
        targets = await DB.get_targets()
        matches = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches')
        above_80 = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches WHERE similarity >= 0.8')
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Watch Finder</title>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial; background: #0a0a0c; color: #e4e4e7; padding: 30px; }}
                h1 {{ color: #4caf50; }}
                .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }}
                .card {{ background: #16161a; padding: 20px; border-radius: 10px; }}
                .value {{ font-size: 2em; font-weight: bold; color: #4caf50; }}
            </style>
        </head>
        <body>
            <h1>⌚ Watch Finder Pro</h1>
            <div class="stats">
                <div class="card">
                    <div>Цілі</div>
                    <div class="value">{len(targets)}</div>
                </div>
                <div class="card">
                    <div>Збіги 80%+</div>
                    <div class="value">{above_80[0]['cnt'] if above_80 else 0}</div>
                </div>
                <div class="card">
                    <div>Всього збігів</div>
                    <div class="value">{matches[0]['cnt'] if matches else 0}</div>
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
            'total_matches': matches[0]['cnt'] if matches else 0,
            'processed': monitor.stats['processed'],
            'ads_checked': monitor.stats['ads_checked']
        })
    
    def run(self):
        print("""
        ╔════════════════════════════════════════════════════════════╗
        ║     Watch Finder Pro v5.0 - FULLY FIXED                  ║
        ║     Візуальний пошук годинників на OLX                   ║
        ║     Працюючі кнопки · Оптимізація пам'яті                ║
        ╚════════════════════════════════════════════════════════════╝
        """)
        
        log.info("🚀 Запуск...")
        self.app.run_polling(drop_pending_updates=True)

# ============================================================================
# [9] MAIN
# ============================================================================

def main():
    try:
        bot = WatchBot()
        bot.run()
    except KeyboardInterrupt:
        log.info("🛑 Зупинено")
    except Exception as e:
        print(f"💥 КРИТИЧНА ПОМИЛКА: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
