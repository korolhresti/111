
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
import pickle
import socket
import struct
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple, Union, Set, Callable
from collections import deque, defaultdict
from pathlib import Path
from functools import wraps, lru_cache
from contextlib import asynccontextmanager

# ============================================================================
# [1] БЕЗПЕЧНІ ІМПОРТИ З ПЕРЕВІРКОЮ
# ============================================================================

# Спроба імпорту з обробкою помилок
try:
    import aiohttp
    from aiohttp import web
    from aiohttp_socks import ProxyConnector, ProxyType
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None
    web = None
    ProxyConnector = None
    ProxyType = None
    AIOHTTP_AVAILABLE = False
    print("⚠️ aiohttp не встановлено. Встановіть: pip install aiohttp aiohttp-socks")

try:
    import aiofiles
    AIOFILES_AVAILABLE = True
except ImportError:
    aiofiles = None
    AIOFILES_AVAILABLE = False
    print("⚠️ aiofiles не встановлено. Встановіть: pip install aiofiles")

try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests import BrowserType
    CURL_AVAILABLE = True
except ImportError:
    curl_requests = None
    BrowserType = None
    CURL_AVAILABLE = False

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    redis = None
    REDIS_AVAILABLE = False

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BeautifulSoup = None
    BS4_AVAILABLE = False

try:
    from fake_useragent import UserAgent
    FAKE_UA_AVAILABLE = True
except ImportError:
    UserAgent = None
    FAKE_UA_AVAILABLE = False

try:
    import cv2
    import numpy as np
    from PIL import Image
    from skimage.metrics import structural_similarity as ssim
    CV_AVAILABLE = True
except ImportError:
    cv2 = None
    np = None
    Image = None
    ssim = None
    CV_AVAILABLE = False
    print("⚠️ OpenCV не встановлено. Встановіть: pip install opencv-python-headless numpy scikit-image Pillow")

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO = None
    YOLO_AVAILABLE = False

try:
    import easyocr
    EASYOCR_AVAILABLE = True
except ImportError:
    EASYOCR_AVAILABLE = False

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters, ConversationHandler
)
from telegram.constants import ParseMode

# ============================================================================
# [2] КОНФІГУРАЦІЯ
# ============================================================================

# Стани для ConversationHandler
WAITING_FOR_PHOTOS, WAITING_FOR_NAME = range(2)

class Config:
    """Конфігурація з валідацією"""
    
    def __init__(self):
        # Telegram
        self.TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
        self.CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
        self.PORT = int(os.getenv("PORT", "8080"))
        
        # Пошук
        self.SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.80"))
        self.DEFAULT_SEARCH_QUERY = os.getenv("DEFAULT_SEARCH_QUERY", "годинник б у")
        
        # Проксі
        self.USE_PROXY = os.getenv("USE_PROXY", "false").lower() == "true"
        self.PROXY_LIST = self._load_proxy_list()
        self.PROXY_ROTATION_INTERVAL = int(os.getenv("PROXY_ROTATION_INTERVAL", "300"))
        self.PROXY_CHECK_TIMEOUT = int(os.getenv("PROXY_CHECK_TIMEOUT", "10"))
        self.PROXY_MAX_FAILURES = int(os.getenv("PROXY_MAX_FAILURES", "3"))
        
        # Розподілене сканування
        self.DISTRIBUTED_WORKERS = int(os.getenv("DISTRIBUTED_WORKERS", "3"))
        self.WORKER_QUEUE_SIZE = int(os.getenv("WORKER_QUEUE_SIZE", "100"))
        self.TASK_TIMEOUT = int(os.getenv("TASK_TIMEOUT", "60"))
        
        # Redis
        self.REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
        self.REDIS_TTL = int(os.getenv("REDIS_TTL", "3600"))
        self.REDIS_ENABLED = bool(self.REDIS_URL) and REDIS_AVAILABLE
        
        # Анти-блок
        self.REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
        self.MIN_DELAY = float(os.getenv("MIN_DELAY", "2.0"))
        self.MAX_DELAY = float(os.getenv("MAX_DELAY", "5.0"))
        self.MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
        self.RETRY_DELAY = int(os.getenv("RETRY_DELAY", "60"))
        
        # Сканування
        self.SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "600"))
        self.MAX_TARGETS_PER_SCAN = int(os.getenv("MAX_TARGETS_PER_SCAN", "3"))
        self.MAX_ADS_PER_TARGET = int(os.getenv("MAX_ADS_PER_TARGET", "100"))
        self.SEARCH_PAGES = int(os.getenv("SEARCH_PAGES", "2"))
        
        # Розпізнавання
        self.BRAND_RECOGNITION_ENABLED = os.getenv("BRAND_RECOGNITION_ENABLED", "true").lower() == "true"
        self.BRAND_RECOGNITION_CONFIDENCE = float(os.getenv("BRAND_RECOGNITION_CONFIDENCE", "0.5"))
        self.CONDITION_ASSESSMENT_ENABLED = os.getenv("CONDITION_ASSESSMENT_ENABLED", "true").lower() == "true"
        
        # Кеш
        self.CACHE_MAX_SIZE = int(os.getenv("CACHE_MAX_SIZE", "500"))
        self.CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))
        
        # Шляхи
        self._init_paths()
        self._validate()
    
    def _load_proxy_list(self) -> List[str]:
        """Завантаження списку проксі"""
        proxies = []
        
        # З файлу
        proxy_file = Path(__file__).parent / "proxies.txt"
        if proxy_file.exists():
            try:
                with open(proxy_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        proxy = line.strip()
                        if proxy and not proxy.startswith('#'):
                            proxies.append(proxy)
            except Exception as e:
                print(f"⚠️ Помилка читання proxies.txt: {e}")
        
        # З змінної оточення
        env_proxies = os.getenv("PROXY_LIST", "")
        if env_proxies:
            for proxy in env_proxies.split(','):
                proxy = proxy.strip()
                if proxy:
                    proxies.append(proxy)
        
        return proxies
    
    def _init_paths(self):
        """Ініціалізація шляхів"""
        self.BASE_DIR = Path(__file__).parent.resolve()
        self.DATA_DIR = self.BASE_DIR / "data"
        self.CACHE_DIR = self.DATA_DIR / "cache"
        self.LOGS_DIR = self.DATA_DIR / "logs"
        self.TARGETS_DIR = self.DATA_DIR / "targets"
        self.TEMP_DIR = self.DATA_DIR / "temp"
        
        for d in [self.DATA_DIR, self.CACHE_DIR, self.LOGS_DIR, 
                  self.TARGETS_DIR, self.TEMP_DIR]:
            d.mkdir(parents=True, exist_ok=True)
        
        self.DB_PATH = self.DATA_DIR / "watch_finder.db"
        self.PROXY_FILE = self.DATA_DIR / "proxies.txt"
        
        # YOLO модель
        self.YOLO_MODEL = os.getenv("YOLO_MODEL", "yolov8n.pt")
        self.YOLO_CONFIDENCE = float(os.getenv("YOLO_CONFIDENCE", "0.3"))
    
    def _validate(self):
        """Валідація конфігурації"""
        if not self.TOKEN or ":" not in self.TOKEN:
            print("❌ TELEGRAM_BOT_TOKEN не валідний")
            self.TOKEN = None
        
        if self.ADMIN_ID == 0:
            print("❌ ADMIN_CHAT_ID не вказано")

CONFIG = Config()

# ============================================================================
# [3] ЛОГУВАННЯ
# ============================================================================

class Logger:
    """Логер з кольоровим виводом"""
    
    COLORS = {
        'INFO': '\033[92m',
        'WARNING': '\033[93m',
        'ERROR': '\033[91m',
        'DEBUG': '\033[94m',
        'RESET': '\033[0m'
    }
    
    def __init__(self, name):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        
        # Форматування
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)8s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Кольоровий консольний handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(self._ColoredFormatter())
        self.logger.addHandler(console_handler)
        
        # Файловий handler
        try:
            log_file = CONFIG.LOGS_DIR / "watch_finder.log"
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10*1024*1024, backupCount=5
            )
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
        except Exception as e:
            self.logger.warning(f"⚠️ Не вдалося створити файловий логер: {e}")
    
    class _ColoredFormatter(logging.Formatter):
        def format(self, record):
            levelname = record.levelname
            if levelname in Logger.COLORS:
                record.levelname = f"{Logger.COLORS[levelname]}{levelname}{Logger.COLORS['RESET']}"
            return super().format(record)
    
    def info(self, msg): self.logger.info(msg)
    def warning(self, msg): self.logger.warning(msg)
    def error(self, msg): self.logger.error(msg)
    def debug(self, msg): self.logger.debug(msg)

log = Logger("WatchFinder")

# ============================================================================
# [4] БАЗА ДАНИХ
# ============================================================================

class Database:
    """База даних SQLite"""
    
    def __init__(self):
        self.db_path = CONFIG.DB_PATH
        self.conn = None
        self.lock = asyncio.Lock()
        self._init_db()
    
    def _init_db(self):
        """Ініціалізація БД"""
        try:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            
            cursor = self.conn.cursor()
            
            # Таблиця цілей
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
                    last_search INTEGER DEFAULT 0,
                    batch_id TEXT,
                    brand TEXT,
                    condition TEXT,
                    defects TEXT,
                    metadata TEXT
                )
            ''')
            
            # Таблиця збігів
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id TEXT,
                    target_name TEXT,
                    target_brand TEXT,
                    target_condition TEXT,
                    ad_title TEXT,
                    ad_price REAL,
                    ad_url TEXT UNIQUE,
                    similarity REAL,
                    image_url TEXT,
                    timestamp INTEGER,
                    sent_to_channel INTEGER DEFAULT 0,
                    ad_brand TEXT,
                    ad_condition TEXT,
                    metadata TEXT
                )
            ''')
            
            # Таблиця переглянутих оголошень
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS seen_ads (
                    ad_url TEXT PRIMARY KEY,
                    first_seen INTEGER,
                    last_seen INTEGER,
                    target_id TEXT
                )
            ''')
            
            # Таблиця статистики брендів
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS brand_stats (
                    brand TEXT PRIMARY KEY,
                    count INTEGER DEFAULT 0,
                    avg_price REAL,
                    last_seen INTEGER
                )
            ''')
            
            self.conn.commit()
            log.info("✅ База даних ініціалізована")
            
        except Exception as e:
            log.error(f"❌ Помилка ініціалізації БД: {e}")
            self.conn = None
    
    async def execute(self, query: str, params: tuple = ()) -> Optional[sqlite3.Cursor]:
        """Виконання SQL запиту"""
        if not self.conn:
            return None
        
        async with self.lock:
            try:
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._sync_execute, query, params
                )
            except Exception as e:
                log.error(f"Помилка виконання запиту: {e}")
                return None
    
    def _sync_execute(self, query, params):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        self.conn.commit()
        return cursor
    
    async def fetch_all(self, query: str, params: tuple = ()) -> List[Dict]:
        """Отримання всіх записів"""
        if not self.conn:
            return []
        
        async with self.lock:
            try:
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._sync_fetch_all, query, params
                )
            except Exception as e:
                log.error(f"Помилка отримання даних: {e}")
                return []
    
    def _sync_fetch_all(self, query, params):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]
    
    async def fetch_one(self, query: str, params: tuple = ()) -> Optional[Dict]:
        """Отримання одного запису"""
        rows = await self.fetch_all(query, params)
        return rows[0] if rows else None
    
    async def add_target(self, target: Dict) -> bool:
        """Додавання цілі"""
        try:
            query = '''
                INSERT OR REPLACE INTO targets 
                (id, name, path, search_query, created, priority, batch_id, 
                 brand, condition, defects, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            '''
            await self.execute(query, (
                target['id'],
                target['name'],
                target['path'],
                target.get('search_query', CONFIG.DEFAULT_SEARCH_QUERY),
                target.get('created', int(time.time())),
                target.get('priority', 1),
                target.get('batch_id', ''),
                target.get('brand', ''),
                target.get('condition', ''),
                json.dumps(target.get('defects', [])),
                json.dumps(target.get('metadata', {}))
            ))
            log.info(f"✅ Ціль додано: {target['name']}")
            return True
        except Exception as e:
            log.error(f"❌ Помилка додавання цілі: {e}")
            return False
    
    async def get_targets(self) -> List[Dict]:
        """Отримання всіх цілей"""
        return await self.fetch_all('SELECT * FROM targets ORDER BY created DESC')
    
    async def get_targets_count(self) -> int:
        """Кількість цілей"""
        result = await self.fetch_one('SELECT COUNT(*) as cnt FROM targets')
        return result['cnt'] if result else 0
    
    async def delete_target(self, target_id: str):
        """Видалення цілі"""
        target = await self.fetch_one('SELECT path FROM targets WHERE id = ?', (target_id,))
        if target and os.path.exists(target['path']):
            try:
                os.remove(target['path'])
            except:
                pass
        await self.execute('DELETE FROM targets WHERE id = ?', (target_id,))
        log.info(f"🗑 Ціль видалено: {target_id}")
    
    async def delete_all_targets(self):
        """Видалення всіх цілей"""
        targets = await self.get_targets()
        for t in targets:
            if os.path.exists(t['path']):
                try:
                    os.remove(t['path'])
                except:
                    pass
        await self.execute('DELETE FROM targets')
        log.info("🗑 Всі цілі видалено")
    
    async def is_ad_seen(self, ad_url: str) -> bool:
        """Перевірка чи бачили оголошення"""
        result = await self.fetch_one('SELECT ad_url FROM seen_ads WHERE ad_url = ?', (ad_url,))
        return result is not None
    
    async def mark_ad_seen(self, ad_url: str, target_id: str):
        """Позначити оголошення як переглянуте"""
        now = int(time.time())
        await self.execute('''
            INSERT OR REPLACE INTO seen_ads (ad_url, first_seen, last_seen, target_id)
            VALUES (?, ?, ?, ?)
        ''', (ad_url, now, now, target_id))
    
    async def add_match(self, target: Dict, ad: Dict, similarity: float, image_url: str = None) -> bool:
        """Додавання збігу"""
        try:
            # Парсинг ціни
            price_text = ad['price'].replace(' ', '').replace('грн', '')
            price = float(re.sub(r'[^\d.]', '', price_text)) if re.search(r'\d', price_text) else 0.0
            
            query = '''
                INSERT OR IGNORE INTO matches 
                (target_id, target_name, target_brand, target_condition, 
                 ad_title, ad_price, ad_url, similarity, image_url, timestamp,
                 ad_brand, ad_condition, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            '''
            await self.execute(query, (
                target['id'],
                target['name'],
                target.get('brand', ''),
                target.get('condition', ''),
                ad['title'][:200],
                price,
                ad['url'],
                similarity,
                image_url or (ad.get('images', [])[0] if ad.get('images') else None),
                int(time.time()),
                ad.get('brand', ''),
                ad.get('condition', ''),
                json.dumps(ad.get('metadata', {}))
            ))
            
            await self.mark_ad_seen(ad['url'], target['id'])
            log.info(f"🔥 Збіг! {target['name']} - {similarity:.1%}")
            return True
        except Exception as e:
            log.error(f"❌ Помилка додавання збігу: {e}")
            return False
    
    async def get_unsent_matches(self) -> List[Dict]:
        """Отримання невідправлених збігів"""
        return await self.fetch_all(
            'SELECT * FROM matches WHERE sent_to_channel = 0 ORDER BY similarity DESC, timestamp DESC LIMIT 50'
        )
    
    async def mark_match_sent(self, match_id: int):
        """Позначити збіг як відправлений"""
        await self.execute('UPDATE matches SET sent_to_channel = 1 WHERE id = ?', (match_id,))
    
    async def get_stats(self) -> Dict:
        """Отримання статистики"""
        targets = await self.get_targets_count()
        matches = await self.fetch_one('SELECT COUNT(*) as cnt FROM matches')
        brands = await self.fetch_all('''
            SELECT brand, COUNT(*) as count, AVG(ad_price) as avg_price 
            FROM matches WHERE brand != '' GROUP BY brand ORDER BY count DESC LIMIT 10
        ''')
        
        return {
            'targets': targets,
            'matches': matches['cnt'] if matches else 0,
            'top_brands': brands
        }

# ============================================================================
# [5] КЕШ
# ============================================================================

class TTLCache:
    """Кеш з автоматичним видаленням"""
    
    def __init__(self, max_size=500, ttl=3600):
        self.cache = {}
        self.max_size = max_size
        self.ttl = ttl
        self.lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0
    
    async def get(self, key: str) -> Optional[Any]:
        """Отримання з кешу"""
        async with self.lock:
            if key in self.cache:
                value, timestamp = self.cache[key]
                if time.time() - timestamp < self.ttl:
                    self.hits += 1
                    return value
                else:
                    del self.cache[key]
            self.misses += 1
            return None
    
    async def set(self, key: str, value: Any):
        """Збереження в кеш"""
        async with self.lock:
            # Видаляємо застарілі
            current_time = time.time()
            expired = [k for k, (_, ts) in self.cache.items() 
                      if current_time - ts >= self.ttl]
            for k in expired:
                del self.cache[k]
            
            # Видаляємо найстаріші якщо переповнено
            if len(self.cache) >= self.max_size:
                oldest = min(self.cache.items(), key=lambda x: x[1][1])
                del self.cache[oldest[0]]
            
            self.cache[key] = (value, current_time)
    
    async def clear(self):
        """Очищення кешу"""
        async with self.lock:
            self.cache.clear()
            self.hits = 0
            self.misses = 0
    
    def get_stats(self) -> Dict:
        """Статистика кешу"""
        total = self.hits + self.misses
        return {
            'size': len(self.cache),
            'max_size': self.max_size,
            'hits': self.hits,
            'misses': self.misses,
            'hit_rate': self.hits / total if total > 0 else 0
        }

# ============================================================================
# [6] YOLO ДЕТЕКТОР
# ============================================================================

class YOLODetector:
    """YOLO детектор"""
    
    def __init__(self):
        self.model = None
        self.watch_classes = ['watch', 'clock']
        if YOLO_AVAILABLE:
            self.load_model()
    
    def load_model(self):
        """Завантаження моделі"""
        try:
            self.model = YOLO(CONFIG.YOLO_MODEL)
            log.info("✅ YOLO модель завантажена")
            return True
        except Exception as e:
            log.error(f"❌ Помилка завантаження YOLO: {e}")
            return False
    
    def detect_watch(self, image_path: str) -> Tuple[bool, List[Dict]]:
        """Виявлення годинника"""
        if self.model is None:
            return True, []
        
        try:
            results = self.model(image_path, conf=CONFIG.YOLO_CONFIDENCE, verbose=False)
            detections = []
            
            for r in results:
                if r.boxes is None:
                    continue
                for box in r.boxes:
                    cls = int(box.cls[0])
                    label = self.model.names.get(cls, "").lower()
                    conf = float(box.conf[0])
                    
                    if any(watch_class in label for watch_class in self.watch_classes):
                        detections.append({
                            'label': label,
                            'confidence': conf,
                            'bbox': box.xyxy[0].tolist()
                        })
            
            del results
            gc.collect()
            
            return len(detections) > 0, detections
            
        except Exception as e:
            log.error(f"YOLO помилка: {e}")
            return True, []

# ============================================================================
# [7] CV ENGINE
# ============================================================================

class CVEngine:
    """CV двигун"""
    
    def __init__(self):
        self.cache = TTLCache(max_size=CONFIG.CACHE_MAX_SIZE, ttl=CONFIG.CACHE_TTL)
        self.sift = cv2.SIFT_create() if cv2 else None
        self.bf = cv2.BFMatcher() if cv2 else None
    
    def _phash(self, img):
        """Perceptual hash"""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        resized = cv2.resize(gray, (32, 32))
        dct = cv2.dct(np.float32(resized))
        dct_low = dct[:8, :8]
        median = np.median(dct_low)
        return (dct_low > median).flatten()
    
    async def compare(self, path1: str, path2: str) -> float:
        """Порівняння зображень"""
        if not cv2:
            return 0.5
        
        if not os.path.exists(path1) or not os.path.exists(path2):
            return 0.0
        
        cache_key = f"{path1}:{path2}"
        
        # Кеш
        cached = await self.cache.get(cache_key)
        if cached is not None:
            return cached
        
        # Виконуємо в thread pool
        result = await asyncio.get_event_loop().run_in_executor(
            None, self._sync_compare, path1, path2
        )
        
        await self.cache.set(cache_key, result)
        return result
    
    def _sync_compare(self, path1: str, path2: str) -> float:
        """Синхронне порівняння"""
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
            sift_score = 0.0
            if self.sift and self.bf:
                kp1, des1 = self.sift.detectAndCompute(img1, None)
                kp2, des2 = self.sift.detectAndCompute(img2, None)
                
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
            
            return final
            
        except Exception as e:
            log.debug(f"CV помилка: {e}")
            return 0.0
    
    async def clear_cache(self):
        """Очищення кешу"""
        await self.cache.clear()
        log.info("🧹 CV кеш очищено")

# ============================================================================
# [8] БРЕНДИ
# ============================================================================

class BrandRecognizer:
    """Розпізнавання брендів"""
    
    def __init__(self):
        self.reader = None
        self.brands = [
            'Rolex', 'Omega', 'Casio', 'Seiko', 'Tissot', 'Longines',
            'Breitling', 'Tag Heuer', 'Patek Philippe', 'Audemars Piguet',
            'Vacheron Constantin', 'IWC', 'Jaeger-LeCoultre', 'Cartier',
            'Panerai', 'Hublot', 'Richard Mille', 'Zenith', 'Girard-Perregaux',
            'Ulysse Nardin', 'Blancpain', 'Breguet', 'Glashütte Original',
            'A. Lange & Söhne', 'Franck Muller', 'Bell & Ross', 'Corum',
            'Raymond Weil', 'Oris', 'Hamilton', 'Mido', 'Certina',
            'Tudor', 'Grand Seiko', 'Citizen', 'Bulova', 'Fossil'
        ]
        self.cache = TTLCache(max_size=200, ttl=86400)
        
        if CONFIG.BRAND_RECOGNITION_ENABLED and EASYOCR_AVAILABLE:
            self._init_ocr()
    
    def _init_ocr(self):
        """Ініціалізація OCR"""
        try:
            self.reader = easyocr.Reader(['en'], gpu=False)
            log.info("✅ OCR ініціалізовано")
        except Exception as e:
            log.error(f"❌ Помилка ініціалізації OCR: {e}")
    
    async def recognize_from_image(self, image_path: str) -> Tuple[Optional[str], float]:
        """Розпізнавання з зображення"""
        if not self.reader or not os.path.exists(image_path):
            return None, 0.0
        
        cache_key = f"brand:{image_path}"
        
        cached = await self.cache.get(cache_key)
        if cached:
            return cached
        
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, self._sync_recognize, image_path
            )
            
            await self.cache.set(cache_key, result)
            return result
            
        except Exception as e:
            log.error(f"Помилка розпізнавання: {e}")
            return None, 0.0
    
    def _sync_recognize(self, image_path: str):
        """Синхронне розпізнавання"""
        try:
            result = self.reader.readtext(
                image_path,
                paragraph=False,
                width_ths=0.7,
                height_ths=0.7
            )
            
            best_match = None
            best_confidence = 0.0
            
            for detection in result:
                text = detection[1]
                confidence = detection[2]
                
                if confidence < CONFIG.BRAND_RECOGNITION_CONFIDENCE:
                    continue
                
                text_lower = text.lower()
                for brand in self.brands:
                    brand_lower = brand.lower()
                    if brand_lower in text_lower:
                        if confidence > best_confidence:
                            best_match = brand
                            best_confidence = confidence
            
            return best_match, best_confidence
            
        except Exception as e:
            log.error(f"Помилка синхронного розпізнавання: {e}")
            return None, 0.0

# ============================================================================
# [9] СТАН
# ============================================================================

class ConditionAssessor:
    """Оцінка стану"""
    
    def __init__(self):
        self.levels = ['Відмінний', 'Добрий', 'Задовільний', 'Поганий']
        self.cache = TTLCache(max_size=200, ttl=86400)
    
    async def assess(self, image_path: str) -> Dict:
        """Оцінка стану"""
        if not CONFIG.CONDITION_ASSESSMENT_ENABLED or not os.path.exists(image_path):
            return {'condition': 'Невідомо', 'defects': [], 'score': 0.0}
        
        cache_key = f"condition:{image_path}"
        
        cached = await self.cache.get(cache_key)
        if cached:
            return cached
        
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, self._sync_assess, image_path
            )
            
            await self.cache.set(cache_key, result)
            return result
            
        except Exception as e:
            log.error(f"Помилка оцінки стану: {e}")
            return {'condition': 'Невідомо', 'defects': [], 'score': 0.0}
    
    def _sync_assess(self, image_path: str):
        """Синхронна оцінка"""
        try:
            img = cv2.imread(image_path)
            if img is None:
                return {'condition': 'Невідомо', 'defects': [], 'score': 0.0}
            
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # Різкість
            blur = cv2.Laplacian(gray, cv2.CV_64F).var()
            sharpness_score = min(blur / 500, 1.0)
            
            # Освітленість
            brightness = np.mean(gray)
            brightness_score = min(brightness / 200, 1.0)
            
            # Контраст
            contrast = np.std(gray)
            contrast_score = min(contrast / 50, 1.0)
            
            # Дефекти
            defects = []
            defect_score = 0.0
            
            edges = cv2.Canny(gray, 50, 150)
            scratch_density = np.sum(edges > 0) / edges.size
            if scratch_density > 0.1:
                defects.append('scratch')
                defect_score += 0.3
            
            blurred = cv2.GaussianBlur(gray, (5,5), 0)
            texture_diff = np.mean(np.abs(gray.astype(float) - blurred.astype(float)))
            if texture_diff < 5:
                defects.append('wear')
                defect_score += 0.2
            
            quality_score = (
                sharpness_score * 0.3 +
                brightness_score * 0.2 +
                contrast_score * 0.2 +
                (1.0 - min(defect_score, 1.0)) * 0.3
            )
            
            if quality_score >= 0.8:
                condition = self.levels[0]
            elif quality_score >= 0.6:
                condition = self.levels[1]
            elif quality_score >= 0.4:
                condition = self.levels[2]
            else:
                condition = self.levels[3]
            
            return {
                'condition': condition,
                'defects': defects,
                'score': round(quality_score, 2)
            }
            
        except Exception as e:
            log.error(f"Помилка синхронної оцінки: {e}")
            return {'condition': 'Невідомо', 'defects': [], 'score': 0.0}

# ============================================================================
# [10] МЕНЕДЖЕР ПРОКСІ
# ============================================================================

class ProxyManager:
    """Менеджер проксі"""
    
    def __init__(self):
        self.proxies = CONFIG.PROXY_LIST.copy()
        self.working_proxies = []
        self.failed_proxies = defaultdict(int)
        self.current_proxy = None
        self.last_rotation = 0
        self.lock = asyncio.Lock()
        self.test_in_progress = False
        self.stats = {
            'total': len(self.proxies),
            'working': 0,
            'failed': 0,
            'rotations': 0
        }
        
        if self.proxies:
            log.info(f"📡 Завантажено {len(self.proxies)} проксі")
    
    async def test_proxy(self, proxy: str) -> Tuple[bool, float]:
        """Тестування проксі"""
        if not AIOHTTP_AVAILABLE or not ProxyConnector:
            return False, 0
        
        try:
            connector = ProxyConnector.from_url(proxy)
            start = time.time()
            
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    'http://httpbin.org/ip',
                    timeout=CONFIG.PROXY_CHECK_TIMEOUT
                ) as resp:
                    if resp.status == 200:
                        latency = time.time() - start
                        return True, latency
        except Exception as e:
            log.debug(f"Проксі {proxy} не працює: {e}")
        
        return False, 0
    
    async def test_all_proxies(self):
        """Тестування всіх проксі"""
        if self.test_in_progress or not self.proxies:
            return
        
        self.test_in_progress = True
        log.info("🔍 Тестування проксі...")
        
        working = []
        for proxy in self.proxies:
            is_working, _ = await self.test_proxy(proxy)
            if is_working:
                working.append(proxy)
            await asyncio.sleep(0.5)
        
        self.working_proxies = working
        self.stats['working'] = len(working)
        self.stats['failed'] = len(self.proxies) - len(working)
        
        log.info(f"✅ Знайдено {len(working)} робочих проксі")
        self.test_in_progress = False
    
    async def get_proxy(self) -> Optional[str]:
        """Отримання проксі"""
        if not CONFIG.USE_PROXY or not self.proxies:
            return None
        
        async with self.lock:
            # Перевіряємо поточний
            if (self.current_proxy and 
                self.current_proxy in self.working_proxies and
                time.time() - self.last_rotation < CONFIG.PROXY_ROTATION_INTERVAL):
                return self.current_proxy
            
            # Оновлюємо список робочих
            if not self.working_proxies:
                await self.test_all_proxies()
            
            if not self.working_proxies:
                return None
            
            # Вибираємо новий
            self.current_proxy = random.choice(self.working_proxies)
            self.last_rotation = time.time()
            self.stats['rotations'] += 1
            
            log.info(f"🔄 Змінено проксі на {self.current_proxy}")
            return self.current_proxy
    
    async def mark_failed(self, proxy: str):
        """Позначити як несправний"""
        async with self.lock:
            self.failed_proxies[proxy] += 1
            if self.failed_proxies[proxy] >= CONFIG.PROXY_MAX_FAILURES:
                if proxy in self.working_proxies:
                    self.working_proxies.remove(proxy)
                self.stats['working'] = len(self.working_proxies)
    
    def get_stats(self) -> Dict:
        """Статистика"""
        return self.stats

# ============================================================================
# [11] ІМІТАЦІЯ ЛЮДИНИ
# ============================================================================

class HumanBehaviorSimulator:
    """Імітація людини"""
    
    def __init__(self):
        self.last_action_time = time.time()
        self.action_count = 0
        self.user_agent = UserAgent().random if FAKE_UA_AVAILABLE else 'Mozilla/5.0'
    
    async def random_delay(self):
        """Випадкова затримка"""
        now = time.time()
        time_since_last = now - self.last_action_time
        self.action_count += 1
        
        if time_since_last < 0.5:
            delay = random.uniform(CONFIG.MIN_DELAY * 2, CONFIG.MAX_DELAY * 2)
        else:
            delay = random.uniform(CONFIG.MIN_DELAY, CONFIG.MAX_DELAY)
            
            if self.action_count % 10 == 0:
                delay += random.uniform(5, 10)
        
        await asyncio.sleep(delay)
        self.last_action_time = time.time()
    
    def get_headers(self) -> Dict:
        """Заголовки"""
        return {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': random.choice([
                'uk-UA,uk;q=0.9,en;q=0.8,ru;q=0.7',
                'en-US,en;q=0.9',
                'ru-RU,ru;q=0.9,en;q=0.8',
            ]),
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': random.choice(['1', '0']),
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'User-Agent': self.user_agent
        }

# ============================================================================
# [12] OLX ПАРСЕР
# ============================================================================

class OLXParser:
    """Парсер OLX"""
    
    def __init__(self, proxy_manager, human_simulator):
        self.proxy_manager = proxy_manager
        self.human = human_simulator
        self.session = None
        self.stats = {
            'requests': 0,
            'success': 0,
            'failures': 0,
            'blocks': 0
        }
    
    async def get_session(self):
        """Отримання сесії"""
        if self.session and not self.session.closed:
            return self.session
        
        connector = None
        if CONFIG.USE_PROXY and AIOHTTP_AVAILABLE:
            proxy = await self.proxy_manager.get_proxy()
            if proxy and ProxyConnector:
                try:
                    connector = ProxyConnector.from_url(proxy)
                except Exception as e:
                    log.error(f"Помилка конектора: {e}")
        
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=CONFIG.REQUEST_TIMEOUT)
        ) if AIOHTTP_AVAILABLE else None
        
        return self.session
    
    async def close_session(self):
        """Закриття сесії"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
    
    async def fetch_page(self, url: str, retry: int = 0) -> Optional[str]:
        """Завантаження сторінки"""
        if not AIOHTTP_AVAILABLE or not BS4_AVAILABLE:
            log.error("❌ Потрібні бібліотеки: aiohttp та beautifulsoup4")
            return None
        
        await self.human.random_delay()
        self.stats['requests'] += 1
        
        try:
            session = await self.get_session()
            if not session:
                return None
            
            headers = self.human.get_headers()
            
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    self.stats['success'] += 1
                    return await response.text()
                elif response.status == 403:
                    self.stats['blocks'] += 1
                    log.warning(f"❌ Блок 403! Спроба {retry + 1}")
                    
                    if retry < CONFIG.MAX_RETRIES - 1:
                        wait_time = random.uniform(60, 120)
                        await asyncio.sleep(wait_time)
                        
                        if CONFIG.USE_PROXY:
                            await self.proxy_manager.mark_failed(
                                self.proxy_manager.current_proxy
                            )
                            await self.close_session()
                        
                        return await self.fetch_page(url, retry + 1)
                else:
                    log.warning(f"⚠️ Статус: {response.status}")
            
            self.stats['failures'] += 1
            return None
                
        except Exception as e:
            log.error(f"❌ Помилка запиту: {e}")
            self.stats['failures'] += 1
            
            if retry < CONFIG.MAX_RETRIES - 1:
                wait_time = random.uniform(CONFIG.RETRY_DELAY, CONFIG.RETRY_DELAY * 2)
                await asyncio.sleep(wait_time)
                
                if CONFIG.USE_PROXY:
                    await self.proxy_manager.mark_failed(
                        self.proxy_manager.current_proxy
                    )
                    await self.close_session()
                
                return await self.fetch_page(url, retry + 1)
            
            return None
    
    async def search_watches(self, query: str, pages: int = 2) -> List[Dict]:
        """Пошук годинників"""
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
                
                # Селектори
                cards = soup.select('div[data-cy="l-card"]')
                if not cards:
                    cards = soup.select('div.css-1apmciz')
                if not cards:
                    cards = soup.select('div.css-1sw7q4x')
                
                if not cards:
                    log.debug(f"Немає карток на сторінці {page}")
                    continue
                
                log.info(f"📊 Сторінка {page}: {len(cards)} оголошень")
                
                for card in cards:
                    try:
                        # Пропускаємо TOP
                        if card.select_one('[data-testid="adCard-featured"]'):
                            continue
                        
                        # Заголовок
                        title_elem = (
                            card.select_one('h6') or
                            card.select_one('a.css-1bbgabe')
                        )
                        
                        if not title_elem:
                            continue
                        
                        title = title_elem.get_text(strip=True)
                        
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
                        price_elem = (
                            card.select_one('[data-testid="ad-price"]') or
                            card.select_one('.css-10b0b6q')
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
                        
                        all_ads.append({
                            'title': title,
                            'price': price,
                            'url': ad_url,
                            'images': images,
                            'page': page,
                            'timestamp': time.time()
                        })
                        
                    except Exception as e:
                        log.debug(f"Помилка парсингу картки: {e}")
                        continue
                
                if page < pages:
                    await asyncio.sleep(random.uniform(10, 20))
                
            except Exception as e:
                log.error(f"Помилка парсингу сторінки {page}: {e}")
                continue
        
        log.info(f"✅ Знайдено {len(all_ads)} оголошень")
        return all_ads[:CONFIG.MAX_ADS_PER_TARGET]
    
    def get_stats(self) -> Dict:
        """Статистика"""
        return self.stats

# ============================================================================
# [13] МОНІТОРИНГ
# ============================================================================

class WatchMonitor:
    """Моніторинг"""
    
    def __init__(self, olx_parser, brand_recognizer, condition_assessor, cv_engine, yolo_detector):
        self.olx = olx_parser
        self.brand_recognizer = brand_recognizer
        self.condition_assessor = condition_assessor
        self.cv_engine = cv_engine
        self.yolo = yolo_detector
        self.is_running = False
        self.task = None
        self.stats = {
            'processed': 0,
            'matches': 0,
            'errors': 0,
            'ads_checked': 0,
            'above_80': 0,
            'brands_found': defaultdict(int),
            'avg_similarity': 0.0,
            'start_time': time.time()
        }
    
    async def start(self, app):
        """Запуск"""
        if self.is_running:
            return
        
        self.is_running = True
        self.task = asyncio.create_task(self._run(app))
        log.info("🚀 Моніторинг запущено")
    
    async def stop(self):
        """Зупинка"""
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except:
                pass
        log.info("🛑 Моніторинг зупинено")
    
    async def _run(self, app):
        """Головний цикл"""
        log.info("🔄 Головний цикл моніторингу")
        
        while self.is_running:
            try:
                targets = await DB.get_targets()
                
                if not targets:
                    await asyncio.sleep(60)
                    continue
                
                log.info(f"🎯 Сканування {len(targets)} цілей")
                
                # Вибираємо цілі
                scan_targets = targets[:CONFIG.MAX_TARGETS_PER_SCAN]
                
                for target in scan_targets:
                    try:
                        await self._process_target(target, app)
                        self.stats['processed'] += 1
                        await asyncio.sleep(random.uniform(20, 30))
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
        """Обробка цілі"""
        if not os.path.exists(target['path']):
            log.warning(f"Фото не знайдено: {target['path']}")
            return
        
        search_query = target.get('search_query', CONFIG.DEFAULT_SEARCH_QUERY)
        log.info(f"🔍 Скануємо: {target['name']}")
        
        ads = await self.olx.search_watches(search_query, pages=CONFIG.SEARCH_PAGES)
        
        if not ads:
            return
        
        matches_found = 0
        total_similarity = 0
        
        for ad in ads:
            if not ad.get('images'):
                continue
            
            best_score = 0.0
            best_image = None
            
            for img_url in ad['images'][:3]:
                score = await self._analyze_image(target['path'], img_url)
                if score > best_score:
                    best_score = score
                    best_image = img_url
            
            if best_score >= CONFIG.SIMILARITY_THRESHOLD:
                # Аналіз
                ad_metadata = await self._analyze_ad_image(best_image)
                if ad_metadata:
                    ad.update(ad_metadata)
                
                await DB.add_match(target, ad, best_score, best_image)
                matches_found += 1
                self.stats['matches'] += 1
                self.stats['above_80'] += 1
                total_similarity += best_score
            
            self.stats['ads_checked'] += 1
        
        if matches_found > 0:
            self.stats['avg_similarity'] = (
                self.stats['avg_similarity'] * 0.9 + 
                (total_similarity / matches_found) * 0.1
            )
    
    async def _analyze_image(self, target_path: str, img_url: str) -> float:
        """Аналіз зображення"""
        if not img_url or not aiofiles:
            return 0.0
        
        temp_path = CONFIG.TEMP_DIR / f"watch_{secrets.token_hex(6)}.jpg"
        
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
            
            score = await self.cv_engine.compare(target_path, str(temp_path))
            
            temp_path.unlink(missing_ok=True)
            return score
                
        except Exception as e:
            log.debug(f"Помилка аналізу: {e}")
            temp_path.unlink(missing_ok=True)
            return 0.0
    
    async def _analyze_ad_image(self, image_url: str) -> Dict:
        """Повний аналіз"""
        result = {}
        
        if not aiofiles:
            return result
        
        temp_path = CONFIG.TEMP_DIR / f"analyze_{secrets.token_hex(6)}.jpg"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url, timeout=15) as resp:
                    if resp.status != 200:
                        return result
                    content = await resp.read()
                    async with aiofiles.open(temp_path, 'wb') as f:
                        await f.write(content)
            
            # YOLO
            is_watch, detections = self.yolo.detect_watch(str(temp_path))
            if detections:
                result['detections'] = detections
            
            # Бренд
            brand, brand_conf = await self.brand_recognizer.recognize_from_image(str(temp_path))
            if brand:
                result['brand'] = brand
                self.stats['brands_found'][brand] += 1
            
            # Стан
            condition = await self.condition_assessor.assess(str(temp_path))
            if condition['condition'] != 'Невідомо':
                result['condition'] = condition['condition']
                result['condition_score'] = condition['score']
            
            temp_path.unlink(missing_ok=True)
            
        except Exception as e:
            log.error(f"Помилка аналізу: {e}")
            temp_path.unlink(missing_ok=True)
        
        return result
    
    async def _send_pending_matches(self, app):
        """Відправка збігів"""
        matches = await DB.get_unsent_matches()
        
        if not matches:
            return
        
        log.info(f"📨 Відправляємо {len(matches)} збігів")
        
        for match in matches:
            try:
                brand_info = f"🏷 Бренд: {match['target_brand']}\n" if match['target_brand'] else ""
                
                caption = (
                    f"🔥 <b>ЗНАЙДЕНО ГОДИННИК!</b>\n\n"
                    f"🎯 <b>Ціль:</b> {match['target_name']}\n"
                    f"{brand_info}"
                    f"📦 <b>Опис:</b> {match['ad_title'][:150]}\n"
                    f"💰 <b>Ціна:</b> {match['ad_price']} грн\n"
                    f"📊 <b>Схожість:</b> {match['similarity']:.1%}\n"
                    f"\n🔗 <a href='{match['ad_url']}'>🔍 ПЕРЕЙТИ ДО ОГОЛОШЕННЯ</a>"
                )
                
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
                        parse_mode=ParseMode.HTML
                    )
                
                await DB.mark_match_sent(match['id'])
                await asyncio.sleep(1)
                
            except Exception as e:
                log.error(f"Помилка відправки: {e}")
    
    def get_stats(self) -> Dict:
        """Статистика"""
        return dict(self.stats)

# ============================================================================
# [14] TELEGRAM БОТ
# ============================================================================

class WatchBot:
    """Telegram бот"""
    
    def __init__(self):
        if not CONFIG.TOKEN:
            raise ValueError("❌ TELEGRAM_BOT_TOKEN не вказано")
        
        self.app = ApplicationBuilder() \
            .token(CONFIG.TOKEN) \
            .post_init(self.post_init) \
            .build()
        
        # Компоненти
        self.proxy_manager = ProxyManager() if CONFIG.USE_PROXY else None
        self.human_simulator = HumanBehaviorSimulator()
        self.olx_parser = OLXParser(self.proxy_manager, self.human_simulator)
        self.brand_recognizer = BrandRecognizer()
        self.condition_assessor = ConditionAssessor()
        self.cv_engine = CVEngine()
        self.yolo_detector = YOLODetector()
        self.monitor = WatchMonitor(
            self.olx_parser,
            self.brand_recognizer,
            self.condition_assessor,
            self.cv_engine,
            self.yolo_detector
        )
        
        self._handlers()
    
    def _handlers(self):
        """Реєстрація обробників"""
        
        # ConversationHandler
        conv_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(self.start_add_target, pattern="^add_target$"),
                CommandHandler("add", self.start_add_target_command)
            ],
            states={
                WAITING_FOR_PHOTOS: [
                    MessageHandler(filters.PHOTO, self.handle_photos),
                    CommandHandler("done", self.finish_adding)
                ],
                WAITING_FOR_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_name),
                    CommandHandler("skip", self.skip_name)
                ],
            },
            fallbacks=[
                CommandHandler("cancel", self.cancel),
                CallbackQueryHandler(self.cancel_callback, pattern="^cancel$")
            ],
            allow_reentry=True
        )
        
        # Команди
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("stats", self.cmd_stats))
        self.app.add_handler(CommandHandler("count", self.cmd_count))
        self.app.add_handler(CommandHandler("add", self.start_add_target_command))
        self.app.add_handler(CommandHandler("clear", self.cmd_clear_cache))
        self.app.add_handler(conv_handler)
        
        # Callbacks
        self.app.add_handler(CallbackQueryHandler(self.callback_handler))
        
        # Помилки
        self.app.add_error_handler(self.error_handler)
    
    async def post_init(self, app):
        """Ініціалізація після запуску"""
        # Веб-сервер
        await self._web_server()
        
        # Тестування проксі
        if self.proxy_manager:
            asyncio.create_task(self.proxy_manager.test_all_proxies())
        
        # Запуск моніторингу
        asyncio.create_task(self._start_monitor_delayed(app))
        
        # Повідомлення
        await self._send_startup_message(app)
        
        log.info("✅ Бот ініціалізовано")
    
    async def _web_server(self):
        """Веб-сервер"""
        if not web:
            return
        
        app = web.Application()
        app.router.add_get('/', self.web_index)
        app.router.add_get('/api/stats', self.web_stats)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', CONFIG.PORT)
        await site.start()
        
        log.info(f"🌐 Веб-дашборд: http://0.0.0.0:{CONFIG.PORT}")
    
    async def _start_monitor_delayed(self, app):
        """Відкладений запуск"""
        await asyncio.sleep(10)
        await self.monitor.start(app)
    
    async def _send_startup_message(self, app):
        """Стартове повідомлення"""
        try:
            targets = await DB.get_targets_count()
            proxy_stats = self.proxy_manager.get_stats() if self.proxy_manager else {'working': 0, 'total': 0}
            
            message = (
                f"✅ <b>Watch Finder Pro v9.0 запущено!</b>\n\n"
                f"📊 <b>Статус:</b>\n"
                f"• Фото в базі: {targets}\n"
                f"• Проксі: {proxy_stats['working']}/{proxy_stats['total']}\n"
                f"• Поріг: {int(CONFIG.SIMILARITY_THRESHOLD * 100)}%\n\n"
                f"📸 Використовуйте /start для меню"
            )
            
            await app.bot.send_message(
                chat_id=CONFIG.ADMIN_ID,
                text=message,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            log.error(f"Помилка відправки: {e}")
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        if update.effective_user.id != CONFIG.ADMIN_ID:
            await update.message.reply_text("⛔ Доступ заборонено")
            return
        
        targets = await DB.get_targets_count()
        proxy_stats = self.proxy_manager.get_stats() if self.proxy_manager else {'working': 0, 'total': 0}
        monitor_stats = self.monitor.get_stats()
        
        keyboard = [
            [InlineKeyboardButton("📸 ДОДАТИ ФОТО", callback_data="add_target")],
            [InlineKeyboardButton("🎯 МОЇ ЦІЛІ", callback_data="targets_list")],
            [InlineKeyboardButton("📊 СТАТИСТИКА", callback_data="stats")],
            [InlineKeyboardButton("▶️ СТАРТ", callback_data="monitor_start"),
             InlineKeyboardButton("⏹ СТОП", callback_data="monitor_stop")],
        ]
        
        await update.message.reply_text(
            f"⌚ <b>Watch Finder Pro v9.0</b>\n\n"
            f"📊 <b>Статус:</b>\n"
            f"• Фото: {targets}\n"
            f"• Моніторинг: {'🟢' if self.monitor.is_running else '🔴'}\n"
            f"• Проксі: {proxy_stats['working']}/{proxy_stats['total']}\n"
            f"• Знайдено: {monitor_stats.get('matches', 0)}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def start_add_target_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Початок додавання через команду"""
        if update.effective_user.id != CONFIG.ADMIN_ID:
            await update.message.reply_text("⛔ Доступ заборонено")
            return ConversationHandler.END
        
        context.user_data['photos'] = []
        
        await update.message.reply_text(
            "📸 Надсилайте фото годинників.\n"
            "Коли закінчите, натисніть /done"
        )
        
        return WAITING_FOR_PHOTOS
    
    async def start_add_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Початок додавання через кнопку"""
        query = update.callback_query
        await query.answer()
        
        context.user_data['photos'] = []
        
        await query.edit_message_text(
            "📸 Надсилайте фото годинників.\n"
            "Коли закінчите, натисніть /done"
        )
        
        return WAITING_FOR_PHOTOS
    
    async def handle_photos(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка фото"""
        if not aiofiles:
            await update.message.reply_text("❌ Потрібна бібліотека aiofiles")
            return WAITING_FOR_PHOTOS
        
        try:
            photos = update.message.photo
            if not photos:
                return WAITING_FOR_PHOTOS
            
            photo = photos[-1]
            file = await photo.get_file()
            
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"watch_{timestamp}_{secrets.token_hex(4)}.jpg"
            path = CONFIG.TARGETS_DIR / filename
            await file.download_to_drive(path)
            
            if 'photos' not in context.user_data:
                context.user_data['photos'] = []
            
            context.user_data['photos'].append(str(path))
            
            count = len(context.user_data['photos'])
            await update.message.reply_text(f"✅ Фото #{count} збережено!")
            
            return WAITING_FOR_PHOTOS
            
        except Exception as e:
            log.error(f"Помилка: {e}")
            await update.message.reply_text(f"❌ Помилка: {str(e)[:100]}")
            return WAITING_FOR_PHOTOS
    
    async def finish_adding(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Завершення додавання"""
        photos = context.user_data.get('photos', [])
        
        if not photos:
            await update.message.reply_text("❌ Немає фото")
            return ConversationHandler.END
        
        keyboard = [
            [InlineKeyboardButton("✅ ПРОПУСТИТИ", callback_data="skip_name")],
            [InlineKeyboardButton("✏️ ВВЕСТИ НАЗВУ", callback_data="enter_name")],
        ]
        
        await update.message.reply_text(
            f"📸 Додано фото: {len(photos)}\n\n"
            f"Оберіть дію:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        return WAITING_FOR_NAME
    
    async def skip_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Пропустити назву"""
        photos = context.user_data.get('photos', [])
        added = 0
        
        for path in photos:
            target = {
                'id': f"WATCH_{secrets.token_hex(4)}",
                'name': f"Годинник {datetime.now().strftime('%d.%m %H:%M')}",
                'path': path,
                'search_query': CONFIG.DEFAULT_SEARCH_QUERY,
                'created': int(time.time()),
                'priority': 1
            }
            
            if await DB.add_target(target):
                added += 1
        
        context.user_data.clear()
        
        await update.message.reply_text(f"✅ Додано {added} фото!")
        return ConversationHandler.END
    
    async def handle_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка назви"""
        name = update.message.text.strip()
        photos = context.user_data.get('photos', [])
        added = 0
        
        for path in photos:
            target = {
                'id': f"WATCH_{secrets.token_hex(4)}",
                'name': f"Годинник {datetime.now().strftime('%d.%m %H:%M')}",
                'path': path,
                'search_query': name,
                'created': int(time.time()),
                'priority': 1
            }
            
            if await DB.add_target(target):
                added += 1
        
        context.user_data.clear()
        
        await update.message.reply_text(f"✅ Додано {added} фото з запитом '{name}'!")
        return ConversationHandler.END
    
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Скасування"""
        context.user_data.clear()
        await update.message.reply_text("❌ Скасовано")
        return ConversationHandler.END
    
    async def cancel_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Скасування через кнопку"""
        query = update.callback_query
        await query.answer()
        context.user_data.clear()
        await query.edit_message_text("❌ Скасовано")
        return ConversationHandler.END
    
    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка callback"""
        query = update.callback_query
        await query.answer()
        data = query.data
        
        try:
            if data == "targets_list":
                await self._show_targets(query)
            
            elif data == "stats":
                await self._show_stats(query)
            
            elif data == "monitor_start":
                await self.monitor.start(context.application)
                await query.edit_message_text("🚀 Моніторинг запущено")
            
            elif data == "monitor_stop":
                await self.monitor.stop()
                await query.edit_message_text("🛑 Моніторинг зупинено")
            
            elif data == "back":
                await self.cmd_start(update, context)
            
        except Exception as e:
            log.error(f"Помилка callback: {e}")
    
    async def _show_targets(self, query):
        """Показати цілі"""
        targets = await DB.get_targets()
        
        if not targets:
            await query.edit_message_text("❌ Список порожній")
            return
        
        text = f"🎯 <b>Всього фото: {len(targets)}</b>\n\n"
        for i, t in enumerate(targets[:10], 1):
            text += f"{i}. {t['name'][:20]} - {t.get('match_count', 0)} збігів\n"
        
        keyboard = [[InlineKeyboardButton("◀️ НАЗАД", callback_data="back")]]
        
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _show_stats(self, query):
        """Показати статистику"""
        db_stats = await DB.get_stats()
        monitor_stats = self.monitor.get_stats()
        cv_stats = self.cv_engine.cache.get_stats()
        
        text = (
            f"📊 <b>Статистика</b>\n\n"
            f"<b>База даних:</b>\n"
            f"• Фото: {db_stats['targets']}\n"
            f"• Збігів: {db_stats['matches']}\n\n"
            f"<b>Моніторинг:</b>\n"
            f"• Знайдено: {monitor_stats.get('matches', 0)}\n"
            f"• Перевірено: {monitor_stats.get('ads_checked', 0)}\n\n"
            f"<b>Кеш:</b>\n"
            f"• Розмір: {cv_stats['size']}/{CONFIG.CACHE_MAX_SIZE}\n"
            f"• Hit rate: {cv_stats['hit_rate']:.1%}"
        )
        
        keyboard = [[InlineKeyboardButton("◀️ НАЗАД", callback_data="back")]]
        
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /stats"""
        if update.effective_user.id != CONFIG.ADMIN_ID:
            return
        
        db_stats = await DB.get_stats()
        monitor_stats = self.monitor.get_stats()
        
        await update.message.reply_text(
            f"📊 <b>Статистика</b>\n\n"
            f"Фото: {db_stats['targets']}\n"
            f"Збігів: {db_stats['matches']}\n"
            f"Знайдено: {monitor_stats.get('matches', 0)}",
            parse_mode=ParseMode.HTML
        )
    
    async def cmd_count(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /count"""
        if update.effective_user.id != CONFIG.ADMIN_ID:
            return
        
        count = await DB.get_targets_count()
        await update.message.reply_text(f"📸 Всього фото: {count}")
    
    async def cmd_clear_cache(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /clear"""
        if update.effective_user.id != CONFIG.ADMIN_ID:
            return
        
        await self.cv_engine.clear_cache()
        
        count = 0
        for f in CONFIG.TEMP_DIR.glob("*.jpg"):
            f.unlink(missing_ok=True)
            count += 1
        
        await update.message.reply_text(f"🧹 Кеш очищено! Видалено {count} файлів")
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробник помилок"""
        log.error(f"Помилка: {context.error}")
    
    async def web_index(self, request):
        """Головна сторінка"""
        targets = await DB.get_targets()
        db_stats = await DB.get_stats()
        monitor_stats = self.monitor.get_stats()
        cv_stats = self.cv_engine.cache.get_stats()
        
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
                .value {{ font-size: 2em; color: #4caf50; }}
            </style>
        </head>
        <body>
            <h1>⌚ Watch Finder Pro v9.0</h1>
            <div class="stats">
                <div class="card">
                    <div>Фото в базі</div>
                    <div class="value">{len(targets)}</div>
                </div>
                <div class="card">
                    <div>Збігів</div>
                    <div class="value">{db_stats['matches']}</div>
                </div>
                <div class="card">
                    <div>Кеш</div>
                    <div class="value">{cv_stats['size']}/{CONFIG.CACHE_MAX_SIZE}</div>
                </div>
            </div>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')
    
    async def web_stats(self, request):
        """API статистики"""
        db_stats = await DB.get_stats()
        monitor_stats = self.monitor.get_stats()
        
        return web.json_response({
            'database': db_stats,
            'monitor': {
                'matches': monitor_stats.get('matches', 0),
                'ads_checked': monitor_stats.get('ads_checked', 0)
            }
        })
    
    def run(self):
        """Запуск"""
        print("""
        ╔════════════════════════════════════════════════════════════╗
        ║     Watch Finder Pro v9.0 - ULTIMATE EDITION             ║
        ║     ✓ AI розпізнавання брендів                           ║
        ║     ✓ Оцінка стану                                       ║
        ║     ✓ Ротація проксі                                     ║
        ║     ✓ Імітація людини                                    ║
        ║     ✓ Кешування                                          ║
        ╚════════════════════════════════════════════════════════════╝
        """)
        
        log.info("🚀 Запуск...")
        self.app.run_polling(drop_pending_updates=True)
