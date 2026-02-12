

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
# [1] КОНФІГУРАЦІЯ З АВТОВИЗНАЧЕННЯМ
# ============================================================================

class Config:
    def __init__(self):
        self.TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
        self.CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
        self.PORT = int(os.getenv("PORT", 8080))
        
        self.SIMILARITY_THRESHOLD = 0.75
        self.SCAN_INTERVAL = 900
        self.MAX_TARGETS_PER_SCAN = 5
        self.MAX_IMAGES_PER_AD = 3
        self.YOLO_MODEL = "yolov8n.pt"
        
        self.BASE_DIR = Path(__file__).parent.resolve()
        self.DATA_DIR = self.BASE_DIR / "industrial_data"
        self.CACHE_DIR = self.DATA_DIR / "cache"
        self.LOGS_DIR = self.DATA_DIR / "logs"
        self.TARGETS_DIR = self.DATA_DIR / "targets"
        
        for d in [self.DATA_DIR, self.CACHE_DIR, self.LOGS_DIR, self.TARGETS_DIR]:
            d.mkdir(parents=True, exist_ok=True)
        
        self.DB_PATH = self.DATA_DIR / "collector.db"
        
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
        self.logger = logging.getLogger("CollectorBot")
        self.logger.setLevel(logging.INFO)
        
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        log_file = CONFIG.LOGS_DIR / "bot.log"
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
# [3] БАЗА ДАНИХ SQLITE
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
                source TEXT,
                source_url TEXT,
                price REAL,
                created INTEGER,
                priority INTEGER DEFAULT 1,
                tags TEXT,
                metadata TEXT,
                search_count INTEGER DEFAULT 0,
                match_count INTEGER DEFAULT 0
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
                source TEXT,
                sent_to_channel INTEGER DEFAULT 0
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS empress_sync (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                added INTEGER
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
                (id, name, path, source, source_url, price, created, priority, tags, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            '''
            await self.execute(query, (
                target['id'],
                target['name'],
                target['path'],
                target.get('source', 'manual'),
                target.get('source_url', ''),
                target.get('price', 0),
                target.get('created', int(time.time())),
                target.get('priority', 1),
                json.dumps(target.get('tags', [])),
                json.dumps(target.get('metadata', {}))
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
    
    async def delete_all_targets(self):
        targets = await self.get_targets()
        for t in targets:
            if os.path.exists(t['path']):
                try:
                    os.remove(t['path'])
                except: pass
        await self.execute('DELETE FROM targets')
    
    async def add_match(self, target: Dict, ad: Dict, similarity: float, image_url: str = None):
        query = '''
            INSERT OR IGNORE INTO matches 
            (target_id, target_name, ad_title, ad_price, ad_url, similarity, image_url, timestamp, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        '''
        await self.execute(query, (
            target['id'],
            target['name'],
            ad['title'],
            ad['price'],
            ad['url'],
            similarity,
            image_url or (ad.get('images', [])[0] if ad.get('images') else None),
            int(time.time()),
            ad.get('source', 'olx')
        ))
        
        await self.execute('UPDATE targets SET match_count = match_count + 1 WHERE id = ?', (target['id'],))
    
    async def get_unsent_matches(self) -> List[Dict]:
        return await self.fetch_all(
            'SELECT * FROM matches WHERE sent_to_channel = 0 ORDER BY timestamp DESC LIMIT 20'
        )
    
    async def mark_match_sent(self, match_id: int):
        await self.execute('UPDATE matches SET sent_to_channel = 1 WHERE id = ?', (match_id,))
    
    async def is_url_synced(self, url: str) -> bool:
        result = await self.fetch_one('SELECT id FROM empress_sync WHERE url = ?', (url,))
        return result is not None
    
    async def mark_url_synced(self, url: str):
        await self.execute('INSERT OR IGNORE INTO empress_sync (url, added) VALUES (?, ?)', 
                          (url, int(time.time())))

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
            log.info("✅ YOLO model loaded")
            return True
        except Exception as e:
            log.error(f"❌ YOLO load failed: {e}")
            return False
    
    def detect(self, image_path: str) -> List[Dict]:
        if self.model is None:
            return []
        
        try:
            results = self.model(image_path, conf=0.4, verbose=False)
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
            log.error(f"YOLO detection error: {e}")
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
        ssim_score = ssim(gray1, gray2)
        
        # Фінальний скор
        final = (phash_score * 0.3 + sift_score * 0.4 + ssim_score * 0.3)
        final = min(1.0, max(0.0, final))
        
        self.cache[cache_key] = final
        if len(self.cache) > 500:
            self.cache.clear()
        
        return final
    
    def clear_cache(self):
        self.cache.clear()

cv_engine = CVEngine()

# ============================================================================
# [6] EMPRESS СКАНЕР
# ============================================================================

class EmpressScanner:
    def __init__(self):
        self.ua = UserAgent()
        self.session = None
    
    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self.session
    
    async def download_image(self, url: str) -> Optional[str]:
        if not url:
            return None
        
        try:
            session = await self.get_session()
            headers = {'User-Agent': self.ua.random}
            
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                
                img_data = await resp.read()
                filename = f"empress_{secrets.token_hex(8)}.jpg"
                filepath = CONFIG.TARGETS_DIR / filename
                
                async with aiofiles.open(filepath, 'wb') as f:
                    await f.write(img_data)
                
                return str(filepath)
        except Exception as e:
            log.error(f"Image download failed: {e}")
            return None
    
    async def scan_category(self, category_url: str, max_pages: int = 2) -> int:
        session = await self.get_session()
        added = 0
        
        for page in range(1, max_pages + 1):
            url = f"{category_url}?page={page}"
            
            try:
                headers = {'User-Agent': self.ua.random}
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        break
                    
                    html = await resp.text()
                    soup = BeautifulSoup(html, 'lxml')
                    
                    # Селектори для Empress
                    cards = soup.select('.product-card, .grid-view-item, .product-item, div[class*="product"]')
                    
                    if not cards:
                        break
                    
                    for card in cards:
                        try:
                            # Заголовок
                            title_elem = (card.select_one('.product-card__title') or 
                                        card.select_one('.h4') or 
                                        card.select_one('.product-item__title') or
                                        card.select_one('a[href*="/products/"]'))
                            
                            if not title_elem:
                                continue
                            
                            title = title_elem.get_text(strip=True)
                            if not title:
                                continue
                            
                            # Посилання
                            link_elem = card.select_one('a[href*="/products/"]')
                            if not link_elem:
                                continue
                            
                            href = link_elem.get('href', '')
                            if not href.startswith('http'):
                                product_url = 'https://empress.cc' + href
                            else:
                                product_url = href
                            
                            # Перевірка дублікатів
                            if await DB.is_url_synced(product_url):
                                continue
                            
                            # Зображення
                            img_elem = card.select_one('img')
                            img_url = None
                            if img_elem:
                                img_url = img_elem.get('data-src') or img_elem.get('src')
                                if img_url and img_url.startswith('//'):
                                    img_url = 'https:' + img_url
                                if '{width}' in img_url:
                                    img_url = img_url.replace('{width}', '800')
                            
                            # Завантаження зображення
                            local_path = None
                            if img_url:
                                local_path = await self.download_image(img_url)
                            
                            # Ціна
                            price_elem = (card.select_one('.price-item') or 
                                        card.select_one('.product-card__price') or
                                        card.select_one('.price'))
                            price = 0
                            if price_elem:
                                price_text = price_elem.get_text(strip=True)
                                price = int(''.join(filter(str.isdigit, price_text))) if price_text else 0
                            
                            # Додаємо в базу
                            target = {
                                'id': f"EMP_{secrets.token_hex(4)}",
                                'name': title[:100],
                                'path': local_path or product_url,
                                'source': 'empress',
                                'source_url': product_url,
                                'price': price,
                                'created': int(time.time()),
                                'priority': 2,
                                'tags': ['watch', 'vintage', 'empress'],
                                'metadata': {'category': category_url}
                            }
                            
                            if await DB.add_target(target):
                                await DB.mark_url_synced(product_url)
                                added += 1
                            
                        except Exception as e:
                            log.debug(f"Card parsing error: {e}")
                            continue
                    
                    await asyncio.sleep(0.5)  # Anti-ban
                    
            except Exception as e:
                log.error(f"Category scan error: {e}")
                break
        
        return added
    
    async def scan_all(self) -> int:
        categories = [
            "https://empress.cc/collections/gents-vintage-watches",
            "https://empress.cc/collections/ladies-vintage-watches",
            "https://empress.cc/collections/pocket-watches",
            "https://empress.cc/collections/omega-vintage-watches",
            "https://empress.cc/collections/all-vintage-watches",
            "https://empress.cc/collections/new-arrivals",
            "https://empress.cc/collections/vintage-chronographs",
            "https://empress.cc/collections/swiss-vintage-watches",
            "https://empress.cc/collections/american-vintage-watches",
            "https://empress.cc/collections/art-deco-watches",
            "https://empress.cc/collections/military-style-vintage-watches",
            "https://empress.cc/collections/high-end-vintage-watches",
            "https://empress.cc/collections/solid-gold-vintage-watches",
            "https://empress.cc/collections/stainless-steel-vintage-watches",
            "https://empress.cc/collections/gold-filled-vintage-watches",
            "https://empress.cc/collections/40s-vintage-watches",
            "https://empress.cc/collections/50s-vintage-watches",
            "https://empress.cc/collections/60s-vintage-watches",
            "https://empress.cc/collections/70s-vintage-watches",
            "https://empress.cc/collections/gruen-vintage-watches",
            "https://empress.cc/collections/bulova-vintage-watches",
            "https://empress.cc/collections/hamilton-usa-vintage-watches"
        ]
        
        log.info(f"🚀 Scanning {len(categories)} Empress categories...")
        total_added = 0
        
        for cat in categories:
            try:
                cat_name = cat.split('/')[-1].split('?')[0]
                log.info(f"📁 Category: {cat_name}")
                added = await self.scan_category(cat, max_pages=3)
                total_added += added
                log.info(f"  ➕ Added: {added} items")
                await asyncio.sleep(1)
            except Exception as e:
                log.error(f"Category {cat} error: {e}")
        
        log.info(f"✅ Empress sync complete! Total added: {total_added}")
        return total_added

empress = EmpressScanner()

# ============================================================================
# [7] OLX ПАРСЕР
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
    
    async def search(self, query: str, limit: int = 10) -> List[Dict]:
        session = await self.get_session()
        url = f"https://www.olx.ua/d/uk/list/q-{query.replace(' ', '-')}/"
        
        try:
            headers = {'User-Agent': self.ua.random}
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return []
                
                html = await resp.text()
            
            soup = BeautifulSoup(html, 'lxml')
            cards = soup.select('div[data-cy="l-card"]')
            
            results = []
            for card in cards[:limit]:
                if card.select_one('[data-testid="adCard-featured"]'):
                    continue
                
                title_elem = card.select_one('h6, h4, a[class*="title"]')
                if not title_elem:
                    continue
                
                title = title_elem.get_text(strip=True)
                
                price_elem = card.select_one('[data-testid="ad-price"], .price, [class*="price"]')
                price = price_elem.get_text(strip=True) if price_elem else "—"
                
                link_elem = card.select_one('a[href]')
                ad_url = None
                if link_elem and link_elem.get('href'):
                    href = link_elem['href']
                    ad_url = href if href.startswith('http') else f"https://www.olx.ua{href}"
                
                img_elem = card.select_one('img')
                images = []
                if img_elem:
                    img_url = img_elem.get('src') or img_elem.get('data-src')
                    if img_url:
                        if img_url.startswith('//'):
                            img_url = 'https:' + img_url
                        images.append(img_url)
                
                results.append({
                    'title': title,
                    'price': price,
                    'url': ad_url,
                    'images': images,
                    'source': 'olx'
                })
            
            return results
            
        except Exception as e:
            log.error(f"OLX search error: {e}")
            return []

olx = OLXParser()

# ============================================================================
# [8] МОНІТОРИНГ ТА МАТЧІНГ
# ============================================================================

class Monitor:
    def __init__(self):
        self.is_running = False
        self.task = None
        self.stats = {'processed': 0, 'matches': 0, 'errors': 0}
    
    async def start(self, context):
        if self.is_running:
            return
        self.is_running = True
        self.task = asyncio.create_task(self._run(context))
        log.info("🚀 Monitor started")
    
    async def stop(self):
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except:
                pass
        log.info("🛑 Monitor stopped")
    
    async def _run(self, context):
        while self.is_running:
            try:
                targets = await DB.get_targets()
                
                if not targets:
                    await asyncio.sleep(60)
                    continue
                
                # Беремо пріоритетні цілі
                targets = sorted(targets, key=lambda x: (
                    -x.get('priority', 1),
                    -x.get('match_count', 0)
                ))[:CONFIG.MAX_TARGETS_PER_SCAN]
                
                for target in targets:
                    try:
                        await self._process_target(target, context)
                        self.stats['processed'] += 1
                        await asyncio.sleep(random.uniform(5, 10))
                    except Exception as e:
                        self.stats['errors'] += 1
                        log.error(f"Target error: {e}")
                
                # Відправляємо нерозіслані матчі в канал
                await self._send_pending_matches(context)
                
                await asyncio.sleep(CONFIG.SCAN_INTERVAL)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Monitor error: {e}")
                await asyncio.sleep(60)
    
    async def _process_target(self, target: Dict, context):
        """Обробка однієї цілі"""
        if not os.path.exists(target['path']):
            log.warning(f"Target image not found: {target['path']}")
            return
        
        ads = await olx.search(target['name'])
        
        if not ads:
            return
        
        for ad in ads[:3]:  # Максимум 3 оголошення
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
                log.info(f"✅ Match found: {target['name']} - {best_score:.1%}")
    
    async def _analyze_image(self, target_path: str, img_url: str) -> float:
        """Аналіз зображення з YOLO детекцією"""
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
                    async with aiofiles.open(temp_path, 'wb') as f:
                        await f.write(content)
            
            # YOLO детекція
            detections = yolo.detect(str(temp_path))
            
            if detections:
                best_score = 0.0
                img = cv2.imread(str(temp_path))
                
                for det in detections[:2]:
                    bbox = det['bbox']
                    x1, y1, x2, y2 = map(int, bbox)
                    crop = img[y1:y2, x1:x2]
                    
                    if crop.size > 0:
                        crop_path = CONFIG.CACHE_DIR / f"crop_{secrets.token_hex(6)}.jpg"
                        cv2.imwrite(str(crop_path), crop)
                        
                        score = cv_engine.compare(target_path, str(crop_path))
                        best_score = max(best_score, score)
                        
                        crop_path.unlink(missing_ok=True)
                
                temp_path.unlink(missing_ok=True)
                return best_score
            else:
                score = cv_engine.compare(target_path, str(temp_path))
                temp_path.unlink(missing_ok=True)
                return score
                
        except Exception as e:
            log.debug(f"Image analysis error: {e}")
            temp_path.unlink(missing_ok=True)
            return 0.0
    
    async def _send_pending_matches(self, context):
        """Відправка знайдених матчів в канал"""
        matches = await DB.get_unsent_matches()
        
        for match in matches:
            try:
                # Перевіряємо репліки
                is_replica = any(k in match['ad_title'].lower() for k in 
                    ['репліка', 'копія', 'replica', 'clone', 'aaa', '1:1'])
                
                # Формуємо повідомлення
                caption = (
                    f"🔥 <b>ЗНАЙДЕНО ЗБІГ!</b>\n\n"
                    f"🎯 <b>Ціль:</b> {match['target_name']}\n"
                    f"📦 <b>Товар:</b> {match['ad_title'][:100]}\n"
                    f"💰 <b>Ціна:</b> {match['ad_price']}\n"
                    f"📊 <b>Схожість:</b> {match['similarity']:.1%}\n"
                    f"{'⚠️ <b>РЕПЛІКА/КОПІЯ</b>' if is_replica else '✅ Ймовірно оригінал'}\n\n"
                    f"🔗 <a href='{match['ad_url']}'>ПЕРЕЙТИ ДА ОГОЛОШЕННЯ</a>"
                )
                
                # Відправляємо в канал
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
                log.info(f"📨 Match sent to channel: {match['target_name']}")
                await asyncio.sleep(1)
                
            except Exception as e:
                log.error(f"Failed to send match to channel: {e}")

monitor = Monitor()

# ============================================================================
# [9] TELEGRAM БОТ
# ============================================================================

class CollectorBot:
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
        log.info("✅ Bot initialized")
    
    async def _web_server(self):
        app = web.Application()
        app.router.add_get('/', self.web_index)
        app.router.add_get('/api/stats', self.web_stats)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', CONFIG.PORT)
        await site.start()
        log.info(f"🌐 Dashboard: http://0.0.0.0:{CONFIG.PORT}")
    
    async def _auto_start(self):
        await asyncio.sleep(5)
        await monitor.start(ContextTypes.DEFAULT_TYPE(application=self.app))
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != CONFIG.ADMIN_ID:
            await update.message.reply_text("⛔ Access denied")
            return
        
        targets = await DB.get_targets()
        matches = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches')
        match_count = matches[0]['cnt'] if matches else 0
        
        keyboard = [
            [InlineKeyboardButton("🌐 SYNC EMPRESS", callback_data="sync_empress")],
            [InlineKeyboardButton("🎯 TARGETS", callback_data="targets_list"),
             InlineKeyboardButton("➕ ADD TARGET", callback_data="add_target")],
            [InlineKeyboardButton("▶️ START", callback_data="monitor_start"),
             InlineKeyboardButton("⏹ STOP", callback_data="monitor_stop")],
            [InlineKeyboardButton("📊 STATS", callback_data="stats"),
             InlineKeyboardButton("🧹 CLEAN", callback_data="clean_cache")],
        ]
        
        await update.message.reply_text(
            f"🏭 <b>CollectorBot Industrial v30.0</b>\n"
            f"Промисловий AI моніторинг\n\n"
            f"📊 <b>Статус:</b>\n"
            f"• Цілей в базі: {len(targets)}\n"
            f"• Знайдено матчів: {match_count}\n"
            f"• Поріг схожості: {int(CONFIG.SIMILARITY_THRESHOLD*100)}%\n"
            f"• Моніторинг: {'🟢 АКТИВНИЙ' if monitor.is_running else '🔴 ЗУПИНЕНО'}\n"
            f"• Оброблено: {monitor.stats['processed']}\n"
            f"• Збігів: {monitor.stats['matches']}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        targets = await DB.get_targets()
        matches = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches')
        unsent = await DB.fetch_all('SELECT COUNT(*) as cnt FROM matches WHERE sent_to_channel = 0')
        
        await update.message.reply_text(
            f"📈 <b>Детальна статистика</b>\n\n"
            f"<b>Система:</b>\n"
            f"• CPU: {os.cpu_count()} cores\n"
            f"• RAM: {CONFIG.DATA_DIR.stat().st_size if CONFIG.DATA_DIR.exists() else 0} bytes\n"
            f"• Uptime: {timedelta(seconds=int(time.time()-CONFIG.DATA_DIR.stat().st_ctime)) if CONFIG.DATA_DIR.exists() else 'N/A'}\n\n"
            f"<b>База даних:</b>\n"
            f"• Цілі: {len(targets)}\n"
            f"• Всього матчів: {matches[0]['cnt'] if matches else 0}\n"
            f"• Очікує відправки: {unsent[0]['cnt'] if unsent else 0}\n"
            f"• CV кеш: {len(cv_engine.cache)}\n\n"
            f"<b>Продуктивність:</b>\n"
            f"• Оброблено пошуків: {monitor.stats['processed']}\n"
            f"• Знайдено збігів: {monitor.stats['matches']}\n"
            f"• Помилок: {monitor.stats['errors']}\n"
            f"• YOLO: {'✅' if yolo.model else '❌'}",
            parse_mode=ParseMode.HTML
        )
    
    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        
        if data == "sync_empress":
            await query.edit_message_text("⏳ Сканування Empress.cc...")
            added = await empress.scan_all()
            await query.edit_message_text(f"✅ Синхронізація завершена!\nДодано нових цілей: {added}")
        
        elif data == "targets_list":
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
            await query.edit_message_text(f"🧹 Кеш очищено! Видалено файлів: {count}")
        
        elif data == "back":
            await self.cmd_start(update, context)
    
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
        if context.user_data.get("state") != "wait_img":
            return
        
        try:
            file = await update.message.photo[-1].get_file()
            filename = f"manual_{secrets.token_hex(8)}.jpg"
            path = CONFIG.TARGETS_DIR / filename
            await file.download_to_drive(path)
            
            context.user_data["tmp_p"] = str(path)
            context.user_data["state"] = "wait_name"
            
            await update.message.reply_text(
                "✅ Фото збережено!\n"
                "📝 Введіть назву товару:"
            )
            
        except Exception as e:
            log.error(f"Photo error: {e}")
            await update.message.reply_text("❌ Помилка збереження фото")
    
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.user_data.get("state") == "wait_name":
            name = update.message.text.strip()
            tmp_path = context.user_data.get("tmp_p")
            
            if not tmp_path or not os.path.exists(tmp_path):
                await update.message.reply_text("❌ Фото не знайдено")
                context.user_data.clear()
                return
            
            target = {
                'id': f"MAN_{secrets.token_hex(4)}",
                'name': name[:100],
                'path': tmp_path,
                'source': 'manual',
                'created': int(time.time()),
                'priority': 1,
                'tags': ['manual'],
                'metadata': {}
            }
            
            if await DB.add_target(target):
                await update.message.reply_text(f"✅ Ціль '{name}' додана!")
            else:
                await update.message.reply_text("❌ Помилка додавання цілі")
            
            context.user_data.clear()
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        error_msg = f"❌ Помилка: {str(context.error)[:200]}"
        log.error(f"Error: {context.error}")
        
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
            <title>CollectorBot Industrial</title>
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
                <h1>🏭 CollectorBot Industrial v30.0</h1>
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
                        <div class="label">Статус моніторингу</div>
                        <div class="value" style="color: {'#4caf50' if monitor.is_running else '#f44336'}">
                            {'АКТИВНИЙ' if monitor.is_running else 'ЗУПИНЕНО'}
                        </div>
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
            'found': monitor.stats['matches'],
            'uptime': time.time()
        })
    
    def run(self):
        print("""
        ╔════════════════════════════════════════════════════════════╗
        ║     CollectorBot Industrial v30.0                         ║
        ║     Промисловий AI моніторинг товарів                    ║
        ║     Empress Sync · OLX Search · YOLOv8 · Channel Post     ║
        ╚════════════════════════════════════════════════════════════╝
        """)
        
        log.info("🚀 Starting bot...")
        self.app.run_polling(drop_pending_updates=True)

# ============================================================================
# [10] MAIN
# ============================================================================

def main():
    try:
        bot = CollectorBot()
        bot.run()
    except KeyboardInterrupt:
        log.info("🛑 Bot stopped by user")
    except Exception as e:
        print(f"💥 CRITICAL ERROR: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
