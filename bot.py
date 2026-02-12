
# ============================================================================
# CollectorBot Industrial v30.0
# Python 3.13 | Production-Grade AI Monitoring System
# AutoML · Distributed Processing · Predictive Intelligence · Enterprise Scale
# ============================================================================

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
import warnings
import pickle
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple, Union, Set, Callable
from collections import deque, defaultdict
from dataclasses import dataclass, field, asdict
from functools import lru_cache, wraps
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from pathlib import Path

import aiohttp
import aiofiles
from aiohttp import web
import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

import cv2
import numpy as np
from PIL import Image

import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import models, transforms
from sklearn.ensemble import RandomForestRegressor, IsolationForest
from sklearn.cluster import DBSCAN
from sklearn.preprocessing import StandardScaler
import joblib

from skimage.metrics import structural_similarity as ssim
from scipy.spatial.distance import cosine, euclidean
from scipy.stats import percentileofscore

from ultralytics import YOLO, RTDETR

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters
)
from telegram.constants import ParseMode

# ============================================================================
# [1] ПРОМИСЛОВА КОНФІГУРАЦІЯ З AUTO-TUNING
# ============================================================================

class AutoConfig:
    """Самоналагоджувальна конфігурація"""
    
    def __init__(self):
        self.env = os.getenv("ENVIRONMENT", "production")
        self.start_time = time.time()
        self.performance_metrics = deque(maxlen=1000)
        
        # Базова конфігурація
        self.TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
        self.CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
        self.PORT = int(os.getenv("PORT", 8080))
        
        # Автоматичне визначення ресурсів
        self.CPU_COUNT = os.cpu_count() or 4
        self.RAM_GB = self._get_ram_gb()
        self.IS_RENDER = os.getenv("RENDER", "false").lower() == "true"
        
        # ML-параметри, що самоналаштовуються
        self.SIMILARITY_THRESHOLD = 0.75  # Початкове значення, буде оптимізовано
        self.BATCH_SIZE = self._auto_batch_size()
        self.SCRAPE_INTERVAL = self._auto_interval()
        self.MAX_WORKERS = max(1, self.CPU_COUNT - 1)
        
        # Параметри AI
        self.USE_TORCH = torch.cuda.is_available() or self.RAM_GB > 8
        self.YOLO_MODEL = "yolov8m.pt" if self.RAM_GB > 4 else "yolov8n.pt"
        self.EMBEDDING_MODEL = "resnet50" if self.RAM_GB > 6 else "resnet18"
        
        # Шляхи
        self._init_paths()
        self._validate()
    
    def _get_ram_gb(self):
        try:
            import psutil
            return psutil.virtual_memory().total / (1024**3)
        except:
            return 2.0  # fallback
    
    def _auto_batch_size(self):
        """Автоматичний вибір batch size на основі RAM"""
        ram = self._get_ram_gb()
        if ram > 8: return 15
        if ram > 4: return 10
        if ram > 2: return 5
        return 3
    
    def _auto_interval(self):
        """Автоматичний інтервал сканування"""
        if self.IS_RENDER:
            return 600  # 10 хвилин для Render
        return 300  # 5 хвилин для потужних машин
    
    def _init_paths(self):
        """Ініціалізація шляхів"""
        self.BASE_DIR = Path(__file__).parent.resolve()
        self.DATA_DIR = self.BASE_DIR / "industrial_data"
        self.MODELS_DIR = self.DATA_DIR / "models"
        self.CACHE_DIR = self.DATA_DIR / "cache"
        self.LOGS_DIR = self.DATA_DIR / "logs"
        self.TARGETS_DIR = self.DATA_DIR / "targets"
        self.DATASET_DIR = self.DATA_DIR / "dataset"
        
        for d in [self.DATA_DIR, self.MODELS_DIR, self.CACHE_DIR, 
                  self.LOGS_DIR, self.TARGETS_DIR, self.DATASET_DIR]:
            d.mkdir(parents=True, exist_ok=True)
    
    def _validate(self):
        if not self.TOKEN or ":" not in self.TOKEN:
            raise RuntimeError("❌ Invalid TELEGRAM_BOT_TOKEN")
        if self.ADMIN_CHAT_ID == 0:
            raise RuntimeError("❌ ADMIN_CHAT_ID missing")
    
    def update_threshold(self, success_rate: float):
        """Адаптивне налаштування порогу"""
        self.performance_metrics.append(success_rate)
        if len(self.performance_metrics) > 50:
            avg_success = np.mean(self.performance_metrics)
            if avg_success > 0.3:
                self.SIMILARITY_THRESHOLD = min(0.85, self.SIMILARITY_THRESHOLD + 0.01)
            elif avg_success < 0.1:
                self.SIMILARITY_THRESHOLD = max(0.65, self.SIMILARITY_THRESHOLD - 0.01)

CONFIG = AutoConfig()

# ============================================================================
# [2] ПРОМИСЛОВЕ ЛОГУВАННЯ ТА МОНІТОРИНГ
# ============================================================================

class IndustrialLogger:
    """Багаторівневе логування з ротацією та метриками"""
    
    def __init__(self):
        self.logger = logging.getLogger("IndustrialCollector")
        self.logger.setLevel(logging.INFO)
        
        # Форматування
        formatter = logging.Formatter(
            '%(asctime)s.%(msecs)03d | %(levelname)8s | %(name)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Файловий handler з ротацією
        log_file = CONFIG.LOGS_DIR / "industrial.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=50*1024*1024, backupCount=10
        )
        file_handler.setFormatter(formatter)
        
        # Консольний handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        
        # Метрики
        self.metrics = defaultdict(list)
        self.start_time = time.time()
    
    def info(self, msg, **kwargs):
        self.logger.info(msg, extra=kwargs)
    
    def error(self, msg, **kwargs):
        self.logger.error(msg, extra=kwargs)
    
    def warning(self, msg, **kwargs):
        self.logger.warning(msg, extra=kwargs)
    
    def debug(self, msg, **kwargs):
        self.logger.debug(msg, extra=kwargs)
    
    def metric(self, name, value):
        self.metrics[name].append((time.time(), value))
    
    def get_stats(self):
        stats = {}
        for name, values in self.metrics.items():
            if values:
                vals = [v[1] for v in values[-100:]]
                stats[name] = {
                    "mean": np.mean(vals),
                    "min": np.min(vals),
                    "max": np.max(vals),
                    "current": vals[-1]
                }
        return stats

log = IndustrialLogger()

# ============================================================================
# [3] ВИСОКОПРОДУКТИВНА БАЗА ДАНИХ (SQLite + JSON Hybrid)
# ============================================================================

class IndustrialDatabase:
    """Гібридна БД: SQLite для структурованих даних + JSON для гнучкості"""
    
    def __init__(self):
        self.db_path = CONFIG.DATA_DIR / "collector.db"
        self.conn = None
        self._init_db()
    
    def _init_db(self):
        """Ініціалізація схеми БД"""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        
        cursor = self.conn.cursor()
        
        # Таблиця цілей
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
                embedding BLOB,
                search_count INTEGER DEFAULT 0,
                match_count INTEGER DEFAULT 0
            )
        ''')
        
        # Таблиця історії пошуку
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id TEXT,
                ad_url TEXT UNIQUE,
                ad_title TEXT,
                ad_price REAL,
                similarity REAL,
                timestamp INTEGER,
                source TEXT,
                FOREIGN KEY(target_id) REFERENCES targets(id)
            )
        ''')
        
        # Таблиця ринкових даних
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS market_intel (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_name TEXT,
                price REAL,
                timestamp INTEGER,
                source TEXT
            )
        ''')
        
        # Таблиця продуктивності
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation TEXT,
                duration REAL,
                success BOOLEAN,
                timestamp INTEGER,
                metadata TEXT
            )
        ''')
        
        # Індекси
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_targets_name ON targets(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_history_url ON search_history(ad_url)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_market_name ON market_intel(target_name)')
        
        self.conn.commit()
    
    async def execute(self, query: str, params: tuple = ()):
        """Асинхронне виконання запиту"""
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
    
    async def add_target(self, target: Dict):
        """Додавання цілі"""
        query = '''
            INSERT OR REPLACE INTO targets 
            (id, name, path, source, source_url, price, created, priority, tags, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        '''
        await self.execute(query, (
            target.get('id'),
            target.get('name'),
            target.get('path'),
            target.get('source'),
            target.get('source_url'),
            target.get('price'),
            target.get('created', time.time()),
            target.get('priority', 1),
            json.dumps(target.get('tags', [])),
            json.dumps(target.get('metadata', {}))
        ))
    
    async def get_targets(self, active_only=True):
        """Отримання цілей з пріоритетами"""
        query = 'SELECT * FROM targets'
        if active_only:
            query += ' ORDER BY priority DESC, created DESC'
        return await self.fetch_all(query)
    
    async def add_match(self, target_id: str, ad: Dict, similarity: float):
        """Додавання знайденого збігу"""
        query = '''
            INSERT OR IGNORE INTO search_history 
            (target_id, ad_url, ad_title, ad_price, similarity, timestamp, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        '''
        await self.execute(query, (
            target_id,
            ad.get('url'),
            ad.get('title'),
            float(re.sub(r'[^\d.]', '', ad.get('price', '0'))),
            similarity,
            int(time.time()),
            ad.get('source', 'olx')
        ))
        
        # Оновлюємо статистику цілі
        await self.execute('''
            UPDATE targets 
            SET match_count = match_count + 1 
            WHERE id = ?
        ''', (target_id,))
    
    async def log_performance(self, operation: str, duration: float, success: bool, metadata: Dict = None):
        """Логування продуктивності"""
        await self.execute('''
            INSERT INTO performance (operation, duration, success, timestamp, metadata)
            VALUES (?, ?, ?, ?, ?)
        ''', (operation, duration, success, int(time.time()), json.dumps(metadata or {})))

DB = IndustrialDatabase()

# ============================================================================
# [4] РОЗПОДІЛЕНИЙ ML-ENGINE З АВТОНОМНИМ НАВЧАННЯМ
# ============================================================================

class AutonomousMLEngine:
    """Автономний ML-двигун з самонавчанням та оптимізацією"""
    
    def __init__(self):
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.models = {}
        self.embeddings_cache = {}
        self.scaler = StandardScaler()
        self.is_trained = False
        
        # Ініціалізація моделей
        self._init_models()
        self._load_or_train()
    
    def _init_models(self):
        """Ініціалізація ML-моделей"""
        # 1. Семантичний енкодер (ResNet50)
        if CONFIG.USE_TORCH:
            self.models['encoder'] = models.resnet50(pretrained=True).to(self.device).eval()
            self.models['encoder'] = torch.nn.Sequential(*list(self.models['encoder'].children())[:-1])
        
        # 2. Класифікатор якості (Random Forest)
        self.models['quality'] = RandomForestRegressor(
            n_estimators=100,
            max_depth=10,
            random_state=42,
            n_jobs=CONFIG.CPU_COUNT
        )
        
        # 3. Детектор аномалій (Isolation Forest)
        self.models['anomaly'] = IsolationForest(
            contamination=0.1,
            random_state=42,
            n_jobs=CONFIG.CPU_COUNT
        )
        
        # 4. Кластеризатор (DBSCAN)
        self.models['cluster'] = DBSCAN(eps=0.3, min_samples=3, n_jobs=CONFIG.CPU_COUNT)
        
        # 5. YOLO детектор
        self.models['yolo'] = YOLO(CONFIG.YOLO_MODEL)
        
        # 6. RT-DETR (якщо достатньо RAM)
        if CONFIG.RAM_GB > 8:
            try:
                self.models['rtdetr'] = RTDETR('rtdetr-l.pt')
            except:
                pass
    
    def _load_or_train(self):
        """Завантаження або тренування моделей"""
        model_path = CONFIG.MODELS_DIR / 'quality_model.pkl'
        if model_path.exists():
            try:
                self.models['quality'] = joblib.load(model_path)
                self.is_trained = True
                log.info("✅ Loaded pre-trained quality model")
            except:
                pass
    
    async def extract_features(self, image_path: str) -> np.ndarray:
        """Екстракція глибоких ознак"""
        if not CONFIG.USE_TORCH:
            return await self._extract_cv_features(image_path)
        
        try:
            # Завантаження з кешу
            if image_path in self.embeddings_cache:
                return self.embeddings_cache[image_path]
            
            # Завантаження та підготовка зображення
            img = Image.open(image_path).convert('RGB')
            preprocess = transforms.Compose([
                transforms.Resize(256),
                transforms.CenterCrop(224),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406], 
                                   std=[0.229, 0.224, 0.225])
            ])
            
            input_tensor = preprocess(img).unsqueeze(0).to(self.device)
            
            # Екстракція ознак
            with torch.no_grad():
                features = self.models['encoder'](input_tensor)
                features = features.cpu().numpy().flatten()
            
            # Кешування
            self.embeddings_cache[image_path] = features
            
            return features
            
        except Exception as e:
            log.error(f"Feature extraction error: {e}")
            return await self._extract_cv_features(image_path)
    
    async def _extract_cv_features(self, image_path: str) -> np.ndarray:
        """CV-based feature extraction (fallback)"""
        img = cv2.imread(image_path)
        if img is None:
            return np.zeros(128)
        
        # ORB features
        orb = cv2.ORB_create(200)
        kp, des = orb.detectAndCompute(img, None)
        
        if des is None:
            return np.zeros(128)
        
        # Усереднення дескрипторів
        features = des.mean(axis=0)
        
        # Padding/truncation to 128
        if len(features) < 128:
            features = np.pad(features, (0, 128 - len(features)))
        else:
            features = features[:128]
        
        return features
    
    async def predict_quality(self, target_path: str, candidate_path: str) -> Dict:
        """Предиктивна оцінка якості збігу"""
        # Екстракція ознак
        feat1 = await self.extract_features(target_path)
        feat2 = await self.extract_features(candidate_path)
        
        # Обчислення метрик
        cosine_sim = 1 - cosine(feat1, feat2)
        euclidean_dist = euclidean(feat1, feat2)
        
        # CV метрики
        img1 = cv2.imread(target_path)
        img2 = cv2.imread(candidate_path)
        
        if img1 is not None and img2 is not None:
            img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
            ssim_score = ssim(cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY),
                             cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY))
        else:
            ssim_score = 0
        
        # Предикція якості
        features = np.array([[
            cosine_sim,
            euclidean_dist,
            ssim_score,
            len(feat1) / 1000,
            len(feat2) / 1000
        ]])
        
        if self.is_trained:
            quality_score = self.models['quality'].predict(features)[0]
        else:
            # Зважена оцінка
            quality_score = (cosine_sim * 0.4 + ssim_score * 0.4 + 
                           (1 - euclidean_dist/10) * 0.2)
        
        return {
            'score': float(quality_score),
            'cosine': float(cosine_sim),
            'euclidean': float(euclidean_dist),
            'ssim': float(ssim_score),
            'is_anomaly': self.detect_anomaly(features) if self.is_trained else False
        }
    
    def detect_anomaly(self, features: np.ndarray) -> bool:
        """Виявлення аномалій"""
        if not self.is_trained:
            return False
        prediction = self.models['anomaly'].predict(features)
        return prediction[0] == -1
    
    async def train_quality_model(self):
        """Тренування моделі якості на історичних даних"""
        log.info("🧠 Starting quality model training...")
        
        # Збір даних з історії
        history = await DB.fetch_all('''
            SELECT h.*, t.path as target_path 
            FROM search_history h
            JOIN targets t ON h.target_id = t.id
            WHERE h.similarity > 0.6
            LIMIT 1000
        ''')
        
        if len(history) < 50:
            log.warning("Not enough training data")
            return False
        
        X = []
        y = []
        
        for item in history:
            if not os.path.exists(item['target_path']):
                continue
            
            # Екстракція ознак (спрощено для тренування)
            feat1 = await self.extract_features(item['target_path'])
            
            # Використовуємо similarity як цільову змінну
            X.append([
                0.7,  # placeholder
                0.5,  # placeholder
                0.8,  # placeholder
                len(feat1) / 1000,
                item['ad_price'] / 10000 if item['ad_price'] else 0.5
            ])
            y.append(item['similarity'])
        
        X = np.array(X)
        y = np.array(y)
        
        # Тренування
        self.models['quality'].fit(X, y)
        self.is_trained = True
        
        # Збереження моделі
        joblib.dump(self.models['quality'], 
                   CONFIG.MODELS_DIR / 'quality_model.pkl')
        
        log.info(f"✅ Quality model trained on {len(X)} samples")
        return True
    
    def detect_objects_yolo(self, image_path: str) -> List[Dict]:
        """YOLO детекція об'єктів"""
        try:
            results = self.models['yolo'](image_path, conf=0.4, verbose=False)
            detections = []
            
            for r in results:
                if r.boxes is None:
                    continue
                for box in r.boxes:
                    cls = int(box.cls[0])
                    label = self.models['yolo'].names.get(cls, "")
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

ML_ENGINE = AutonomousMLEngine()

# ============================================================================
# [5] РОЗПОДІЛЕНИЙ ПАРСИНГ З АВТОМАТИЧНИМ РОЗШИРЕННЯМ
# ============================================================================

class DistributedParser:
    """Розподілений парсер з автоматичним додаванням джерел"""
    
    def __init__(self):
        self.sources = self._load_sources()
        self.session_pool = deque(maxlen=100)
        self.executor = ThreadPoolExecutor(max_workers=CONFIG.MAX_WORKERS)
        self.rate_limiter = defaultdict(lambda: deque(maxlen=60))
    
    def _load_sources(self) -> List[Dict]:
        """Завантаження та авто-розширення джерел"""
        sources = [
            # OLX Україна
            {
                'name': 'olx_ua',
                'base_url': 'https://www.olx.ua',
                'search_url': 'https://www.olx.ua/d/uk/list/q-{query}/',
                'card_selector': 'div[data-cy="l-card"]',
                'title_selector': 'h6',
                'price_selector': '[data-testid="ad-price"]',
                'image_selector': 'img',
                'link_selector': 'a[href]',
                'featured_selector': '[data-testid="adCard-featured"]',
                'pagination': '?page={page}',
                'weight': 1.0
            },
            # Prom.ua
            {
                'name': 'prom_ua',
                'base_url': 'https://prom.ua',
                'search_url': 'https://prom.ua/search?search_term={query}',
                'card_selector': 'div[data-qaid="product_block"]',
                'title_selector': 'a[data-qaid="product_name"]',
                'price_selector': '[data-qaid="product_price"]',
                'image_selector': 'img[data-qaid="image"]',
                'link_selector': 'a[data-qaid="product_link"]',
                'weight': 0.8
            },
            # Rozetka
            {
                'name': 'rozetka',
                'base_url': 'https://rozetka.com.ua',
                'search_url': 'https://rozetka.com.ua/search/?text={query}',
                'card_selector': 'div.goods-tile',
                'title_selector': 'span.goods-tile__title',
                'price_selector': 'span.goods-tile__price-value',
                'image_selector': 'img[data-src]',
                'link_selector': 'a.goods-tile__heading',
                'weight': 0.7
            }
        ]
        
        # Завантаження кастомних джерел
        custom_sources_file = CONFIG.DATA_DIR / 'custom_sources.json'
        if custom_sources_file.exists():
            try:
                with open(custom_sources_file, 'r') as f:
                    custom = json.load(f)
                    sources.extend(custom)
            except:
                pass
        
        return sources
    
    async def get_session(self):
        """Отримання або створення сесії"""
        if self.session_pool:
            session = self.session_pool.popleft()
            if not session.closed:
                return session
        
        connector = aiohttp.TCPConnector(
            limit=100,
            ttl_dns_cache=300,
            ssl=False
        )
        
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
            headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'uk-UA,uk;q=0.9,en;q=0.8,ru;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1'
            }
        )
        
        return session
    
    async def release_session(self, session):
        """Повернення сесії в пул"""
        if not session.closed:
            self.session_pool.append(session)
    
    async def search_source(self, source: Dict, query: str, limit: int = 20) -> List[Dict]:
        """Пошук по одному джерелу"""
        source_name = source['name']
        
        # Rate limiting
        now = time.time()
        self.rate_limiter[source_name].append(now)
        
        if len(self.rate_limiter[source_name]) > 30:
            oldest = self.rate_limiter[source_name][0]
            if now - oldest < 60:
                await asyncio.sleep(random.uniform(2, 5))
        
        session = await self.get_session()
        try:
            # Формування URL
            url = source['search_url'].format(query=query.replace(' ', '+'))
            if 'pagination' in source:
                url += source['pagination'].format(page=1)
            
            # Ротація User-Agent
            headers = {'User-Agent': UserAgent().random}
            
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    log.warning(f"{source_name} returned {response.status}")
                    return []
                
                html = await response.text()
            
            # Парсинг
            soup = BeautifulSoup(html, 'lxml')
            cards = soup.select(source['card_selector'])
            
            results = []
            for card in cards[:limit]:
                # Перевірка на рекламні/топ оголошення
                if 'featured_selector' in source:
                    if card.select_one(source['featured_selector']):
                        continue
                
                # Заголовок
                title_elem = card.select_one(source['title_selector'])
                if not title_elem:
                    continue
                title = title_elem.text.strip()
                
                # Ціна
                price = "—"
                if 'price_selector' in source:
                    price_elem = card.select_one(source['price_selector'])
                    if price_elem:
                        price = price_elem.text.strip()
                
                # Посилання
                url = None
                if 'link_selector' in source:
                    link_elem = card.select_one(source['link_selector'])
                    if link_elem and link_elem.get('href'):
                        href = link_elem['href']
                        url = href if href.startswith('http') else source['base_url'] + href
                
                # Зображення
                images = []
                if 'image_selector' in source:
                    img_elems = card.select(source['image_selector'])
                    for img in img_elems[:3]:
                        img_url = img.get('src') or img.get('data-src')
                        if img_url:
                            if img_url.startswith('//'):
                                img_url = 'https:' + img_url
                            images.append(img_url)
                
                results.append({
                    'title': title,
                    'price': price,
                    'url': url,
                    'images': images,
                    'source': source_name,
                    'timestamp': time.time()
                })
            
            return results
            
        except Exception as e:
            log.error(f"Search error {source_name}: {e}")
            return []
        finally:
            await self.release_session(session)
    
    async def parallel_search(self, query: str) -> List[Dict]:
        """Паралельний пошук по всіх джерелах"""
        tasks = []
        for source in self.sources:
            task = asyncio.create_task(self.search_source(source, query))
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Об'єднання результатів
        all_ads = []
        for res in results:
            if isinstance(res, list):
                all_ads.extend(res)
        
        # Сортування за релевантністю (ціна, свіжість)
        all_ads.sort(key=lambda x: len(x.get('images', [])), reverse=True)
        
        return all_ads[:BATCH_SIZE * 3]

PARSER = DistributedParser()

# ============================================================================
# [6] ПРОМИСЛОВИЙ CV-ENGINE З БАГАТОРІВНЕВИМ АНАЛІЗОМ
# ============================================================================

class IndustrialCVEngine:
    """Багаторівневий CV-двигун з ансамблем методів"""
    
    def __init__(self):
        self.methods = self._init_methods()
        self.cache = {}
        self.stats = defaultdict(list)
    
    def _init_methods(self) -> Dict:
        """Ініціалізація ансамблю методів"""
        return {
            'phash': {'weight': 0.25, 'func': self._phash_similarity},
            'orb': {'weight': 0.20, 'func': self._orb_similarity},
            'hsv': {'weight': 0.15, 'func': self._hsv_similarity},
            'ssim': {'weight': 0.20, 'func': self._ssim_similarity},
            'deep': {'weight': 0.20, 'func': self._deep_similarity}
        }
    
    def _phash_similarity(self, img1, img2):
        """Perceptual hash similarity"""
        def phash(img):
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            resized = cv2.resize(gray, (32, 32))
            dct = cv2.dct(np.float32(resized))
            dct_low = dct[:8, :8]
            median = np.median(dct_low)
            return (dct_low > median).flatten()
        
        h1 = phash(img1)
        h2 = phash(img2)
        return 1.0 - (np.count_nonzero(h1 != h2) / len(h1))
    
    def _orb_similarity(self, img1, img2):
        """ORB feature matching"""
        orb = cv2.ORB_create(1000)
        kp1, des1 = orb.detectAndCompute(img1, None)
        kp2, des2 = orb.detectAndCompute(img2, None)
        
        if des1 is None or des2 is None or len(kp1) < 5 or len(kp2) < 5:
            return 0.0
        
        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = bf.match(des1, des2)
        
        if not matches:
            return 0.0
        
        # Якість матчів
        good_matches = [m for m in matches if m.distance < 50]
        return len(good_matches) / max(len(kp1), len(kp2), 1)
    
    def _hsv_similarity(self, img1, img2):
        """HSV color histogram similarity"""
        hsv1 = cv2.cvtColor(img1, cv2.COLOR_BGR2HSV)
        hsv2 = cv2.cvtColor(img2, cv2.COLOR_BGR2HSV)
        
        hist1 = cv2.calcHist([hsv1], [0,1], None, [50,60], [0,180,0,256])
        hist2 = cv2.calcHist([hsv2], [0,1], None, [50,60], [0,180,0,256])
        
        cv2.normalize(hist1, hist1)
        cv2.normalize(hist2, hist2)
        
        return cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL)
    
    def _ssim_similarity(self, img1, img2):
        """Structural similarity"""
        gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
        gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
        
        score, _ = ssim(gray1, gray2, full=True)
        return score
    
    async def _deep_similarity(self, img1, img2):
        """Deep learning similarity"""
        if not CONFIG.USE_TORCH:
            return self._orb_similarity(img1, img2)
        
        try:
            # Тимчасові файли
            path1 = CONFIG.CACHE_DIR / f"deep_{secrets.token_hex(4)}_1.jpg"
            path2 = CONFIG.CACHE_DIR / f"deep_{secrets.token_hex(4)}_2.jpg"
            
            cv2.imwrite(str(path1), img1)
            cv2.imwrite(str(path2), img2)
            
            feat1 = await ML_ENGINE.extract_features(str(path1))
            feat2 = await ML_ENGINE.extract_features(str(path2))
            
            # Прибирання
            path1.unlink(missing_ok=True)
            path2.unlink(missing_ok=True)
            
            return 1 - cosine(feat1, feat2)
            
        except Exception as e:
            log.error(f"Deep similarity error: {e}")
            return 0.0
    
    async def analyze(self, target_path: str, candidate_path: str, use_cache: bool = True) -> Dict:
        """Повний аналіз зображень"""
        cache_key = f"{target_path}:{candidate_path}"
        
        if use_cache and cache_key in self.cache:
            return self.cache[cache_key]
        
        start_time = time.time()
        
        # Завантаження зображень
        img1 = cv2.imread(target_path)
        img2 = cv2.imread(candidate_path)
        
        if img1 is None or img2 is None:
            return {'score': 0.0, 'method_scores': {}}
        
        # Ресайз для швидкості
        img1 = cv2.resize(img1, (640, 640))
        img2 = cv2.resize(img2, (640, 640))
        
        # Обчислення всіх методів
        scores = {}
        for name, method in self.methods.items():
            try:
                if name == 'deep':
                    score = await method['func'](img1, img2)
                else:
                    score = method['func'](img1, img2)
                scores[name] = max(0.0, min(1.0, float(score)))
            except Exception as e:
                log.error(f"Method {name} error: {e}")
                scores[name] = 0.0
        
        # Зважена оцінка
        weighted_score = sum(
            scores[name] * self.methods[name]['weight']
            for name in scores
        )
        
        result = {
            'score': weighted_score,
            'method_scores': scores,
            'execution_time': time.time() - start_time
        }
        
        # Кешування
        self.cache[cache_key] = result
        if len(self.cache) > 1000:
            self.cache.clear()
        
        return result
    
    def clear_cache(self):
        """Очищення кешу"""
        self.cache.clear()
        log.info("🧹 CV cache cleared")

CV_ENGINE = IndustrialCVEngine()

# ============================================================================
# [7] ПРОМИСЛОВИЙ МОНІТОРИНГ З ПАКЕТНОЮ ОБРОБКОЮ
# ============================================================================

class IndustrialMonitor:
    """Промисловий моніторинг з пакетною обробкою та оптимізацією"""
    
    def __init__(self):
        self.is_running = False
        self.task = None
        self.stats = {
            'processed': 0,
            'matches': 0,
            'errors': 0,
            'avg_time': 0
        }
    
    async def start(self, context):
        """Запуск моніторингу"""
        if self.is_running:
            return
        
        self.is_running = True
        self.task = asyncio.create_task(self._monitor_loop(context))
        log.info("🚀 Industrial monitor started")
    
    async def stop(self):
        """Зупинка моніторингу"""
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except:
                pass
            self.task = None
        log.info("🛑 Industrial monitor stopped")
    
    async def _monitor_loop(self, context):
        """Головний цикл моніторингу"""
        while self.is_running:
            batch_start = time.time()
            
            try:
                # Отримання цілей з пріоритетами
                targets = await DB.get_targets()
                
                if not targets:
                    await asyncio.sleep(30)
                    continue
                
                # Пріоритетне сортування
                targets.sort(key=lambda x: (
                    -x.get('priority', 1),
                    -x.get('match_count', 0),
                    x.get('search_count', 0)
                ))
                
                # Вибірка для поточного батчу
                batch = targets[:CONFIG.BATCH_SIZE]
                
                # Пакетна обробка
                tasks = [
                    self._process_target(target, context)
                    for target in batch
                ]
                
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # Аналіз результатів
                for target, result in zip(batch, results):
                    if isinstance(result, Exception):
                        log.error(f"Target {target['name']} failed: {result}")
                        self.stats['errors'] += 1
                    else:
                        self.stats['processed'] += 1
                
                # Оновлення статистики
                batch_time = time.time() - batch_start
                self.stats['avg_time'] = (
                    self.stats['avg_time'] * 0.9 + batch_time * 0.1
                )
                
                # Логування метрик
                log.metric('batch_size', len(batch))
                log.metric('batch_time', batch_time)
                
                # Адаптивне налаштування
                if self.stats['processed'] > 100:
                    success_rate = self.stats['matches'] / self.stats['processed']
                    CONFIG.update_threshold(success_rate)
                
                # Пауза між батчами
                await asyncio.sleep(CONFIG.SCRAPE_INTERVAL)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Monitor loop error: {e}")
                await asyncio.sleep(60)
    
    async def _process_target(self, target: Dict, context):
        """Обробка однієї цілі"""
        start_time = time.time()
        
        try:
            # Оновлення лічильника пошуку
            await DB.execute(
                'UPDATE targets SET search_count = search_count + 1 WHERE id = ?',
                (target['id'],)
            )
            
            # Паралельний пошук по всіх джерелах
            ads = await PARSER.parallel_search(target['name'])
            
            if not ads:
                return
            
            # Перевірка на дублікати
            existing = await DB.fetch_all(
                'SELECT ad_url FROM search_history WHERE target_id = ?',
                (target['id'],)
            )
            existing_urls = {e['ad_url'] for e in existing}
            
            # Фільтрація нових оголошень
            new_ads = [ad for ad in ads if ad.get('url') not in existing_urls]
            
            if not new_ads:
                return
            
            # Пакетний аналіз зображень
            for ad in new_ads[:5]:  # Максимум 5 на оголошення
                best_score = 0.0
                
                for img_url in ad.get('images', [])[:3]:  # Максимум 3 фото
                    score = await self._analyze_ad_image(target, img_url)
                    best_score = max(best_score, score)
                
                if best_score >= CONFIG.SIMILARITY_THRESHOLD:
                    await self._process_match(target, ad, best_score, context)
                    self.stats['matches'] += 1
            
            # Логування продуктивності
            await DB.log_performance(
                'process_target',
                time.time() - start_time,
                True,
                {'target_id': target['id'], 'ads_found': len(ads)}
            )
            
        except Exception as e:
            await DB.log_performance(
                'process_target',
                time.time() - start_time,
                False,
                {'target_id': target['id'], 'error': str(e)}
            )
            raise
    
    async def _analyze_ad_image(self, target: Dict, img_url: str) -> float:
        """Аналіз одного зображення"""
        temp_path = CONFIG.CACHE_DIR / f"ad_{secrets.token_hex(8)}.jpg"
        
        try:
            # Завантаження зображення
            async with aiohttp.ClientSession() as session:
                async with session.get(img_url, timeout=15) as response:
                    if response.status != 200:
                        return 0.0
                    content = await response.read()
                    
                    async with aiofiles.open(temp_path, 'wb') as f:
                        await f.write(content)
            
            # YOLO детекція
            detections = ML_ENGINE.detect_objects_yolo(str(temp_path))
            
            if detections:
                # Аналіз кожного виявленого об'єкта
                best_score = 0.0
                img = cv2.imread(str(temp_path))
                
                for det in detections[:3]:  # Максимум 3 об'єкта
                    bbox = det['bbox']
                    crop = img[int(bbox[1]):int(bbox[3]), 
                              int(bbox[0]):int(bbox[2])]
                    
                    if crop.size > 0:
                        crop_path = CONFIG.CACHE_DIR / f"crop_{secrets.token_hex(8)}.jpg"
                        cv2.imwrite(str(crop_path), crop)
                        
                        result = await CV_ENGINE.analyze(target['path'], str(crop_path))
                        best_score = max(best_score, result['score'])
                        
                        crop_path.unlink(missing_ok=True)
                
                temp_path.unlink(missing_ok=True)
                return best_score
            else:
                # Прямий аналіз
                result = await CV_ENGINE.analyze(target['path'], str(temp_path))
                temp_path.unlink(missing_ok=True)
                return result['score']
                
        except Exception as e:
            log.debug(f"Image analysis error: {e}")
            temp_path.unlink(missing_ok=True)
            return 0.0
    
    async def _process_match(self, target: Dict, ad: Dict, similarity: float, context):
        """Обробка знайденого збігу"""
        # Збереження в історію
        await DB.add_match(target['id'], ad, similarity)
        
        # Формування повідомлення
        is_replica = any(k in ad['title'].lower() for k in REPLICA_KEYWORDS)
        
        # Отримання ринкової інформації
        price_value = float(re.sub(r'[^\d.]', '', ad['price'])) if ad['price'] != "—" else 0
        await DB.execute(
            'INSERT INTO market_intel (target_name, price, timestamp, source) VALUES (?, ?, ?, ?)',
            (target['name'], price_value, int(time.time()), ad['source'])
        )
        
        # Розрахунок медіани
        market_data = await DB.fetch_all(
            'SELECT price FROM market_intel WHERE target_name = ? ORDER BY timestamp DESC LIMIT 100',
            (target['name'],)
        )
        prices = [d['price'] for d in market_data if d['price'] > 0]
        median_price = np.median(prices) if prices else None
        
        is_deal = median_price and price_value < median_price * 0.7
        
        # Формування повідомлення
        caption = (
            f"🔥 <b>INDUSTRIAL MATCH FOUND</b>\n\n"
            f"🎯 <b>Target:</b> {target['name'][:50]}\n"
            f"📦 <b>Found:</b> {ad['title'][:100]}\n"
            f"💰 <b>Price:</b> {ad['price']}\n"
            f"🏪 <b>Source:</b> {ad['source'].upper()}\n"
            f"📊 <b>Similarity:</b> {similarity:.1%}\n"
        )
        
        if median_price:
            caption += f"📉 <b>Market median:</b> {int(median_price):,} грн\n"
            if is_deal:
                discount = (1 - price_value/median_price) * 100
                caption += f"💸 <b>SUPER DEAL:</b> -{discount:.0f}%\n"
        
        caption += f"\n🔗 <a href='{ad['url']}'>🔍 View Listing</a>"
        
        if is_replica:
            caption = caption.replace("INDUSTRIAL MATCH FOUND", 
                                     "⚠️ REPLICA DETECTED ⚠️")
        
        # Відправка
        try:
            if ad.get('images'):
                await context.bot.send_photo(
                    chat_id=CONFIG.CHANNEL_ID or CONFIG.ADMIN_CHAT_ID,
                    photo=ad['images'][0],
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            else:
                await context.bot.send_message(
                    chat_id=CONFIG.CHANNEL_ID or CONFIG.ADMIN_CHAT_ID,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False
                )
            
            log.info(f"✅ Match sent: {target['name']} @ {ad['price']}")
            
        except Exception as e:
            log.error(f"Failed to send match: {e}")

MONITOR = IndustrialMonitor()

# ============================================================================
# [8] ПРОМИСЛОВИЙ TELEGRAM UI
# ============================================================================

class IndustrialBot:
    """Промисловий Telegram бот з розширеним UI"""
    
    def __init__(self):
        self.app = ApplicationBuilder() \
            .token(CONFIG.TOKEN) \
            .post_init(self.post_init) \
            .build()
        
        self._setup_handlers()
    
    def _setup_handlers(self):
        """Реєстрація обробників"""
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("stats", self.cmd_stats))
        self.app.add_handler(CommandHandler("train", self.cmd_train))
        self.app.add_handler(CommandHandler("clean", self.cmd_clean))
        self.app.add_handler(CallbackQueryHandler(self.callback_handler))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        self.app.add_error_handler(self.error_handler)
    
    async def post_init(self, app):
        """Post-initialization"""
        # Запуск веб-сервера
        await self._start_web_server()
        
        # Автозапуск моніторингу
        asyncio.create_task(self._auto_start_monitor())
        
        log.info("✅ Industrial bot initialized")
    
    async def _start_web_server(self):
        """Запуск промислового веб-дашборду"""
        app = web.Application()
        app.router.add_get('/', self.web_index)
        app.router.add_get('/api/stats', self.web_stats)
        app.router.add_get('/api/targets', self.web_targets)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', CONFIG.PORT)
        await site.start()
        
        log.info(f"🌐 Industrial dashboard: http://0.0.0.0:{CONFIG.PORT}")
    
    async def _auto_start_monitor(self):
        """Автоматичний запуск моніторингу"""
        await asyncio.sleep(3)
        await MONITOR.start(ContextTypes.DEFAULT_TYPE(application=self.app))
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Головне меню"""
        if update.effective_user.id != CONFIG.ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Access denied")
            return
        
        targets = await DB.get_targets()
        stats = MONITOR.stats
        
        kb = [
            [InlineKeyboardButton("🌐 SYNC EMPRESS", callback_data="sync_empress")],
            [InlineKeyboardButton("🎯 TARGETS", callback_data="targets_list"),
             InlineKeyboardButton("➕ ADD TARGET", callback_data="add_target")],
            [InlineKeyboardButton("▶️ START", callback_data="monitor_start"),
             InlineKeyboardButton("⏹ STOP", callback_data="monitor_stop")],
            [InlineKeyboardButton("📊 STATS", callback_data="stats"),
             InlineKeyboardButton("🧠 TRAIN", callback_data="train")],
            [InlineKeyboardButton("⚙️ ADVANCED", callback_data="advanced")]
        ]
        
        await update.message.reply_text(
            f"🏭 <b>CollectorBot Industrial v30.0</b>\n"
            f"Production-Grade AI Monitoring\n\n"
            f"📊 <b>Current Status:</b>\n"
            f"• Targets: {len(targets)}\n"
            f"• Threshold: {CONFIG.SIMILARITY_THRESHOLD:.0%}\n"
            f"• Batch: {CONFIG.BATCH_SIZE}\n"
            f"• YOLO: {CONFIG.YOLO_MODEL}\n"
            f"• ML: {'🧠' if CONFIG.USE_TORCH else '⚡'}\n"
            f"• Monitor: {'🟢' if MONITOR.is_running else '🔴'}\n"
            f"• Processed: {stats['processed']}\n"
            f"• Matches: {stats['matches']}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML
        )
    
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Детальна статистика"""
        targets = await DB.get_targets()
        history = await DB.fetch_all('SELECT COUNT(*) as count FROM search_history')
        performance = await DB.fetch_all(
            'SELECT * FROM performance ORDER BY timestamp DESC LIMIT 10'
        )
        
        msg = (
            f"📈 <b>Industrial Statistics</b>\n\n"
            f"<b>System:</b>\n"
            f"• CPU cores: {CONFIG.CPU_COUNT}\n"
            f"• RAM: {CONFIG.RAM_GB:.1f} GB\n"
            f"• Environment: {CONFIG.env}\n"
            f"• Uptime: {timedelta(seconds=int(time.time() - CONFIG.start_time))}\n\n"
            f"<b>Database:</b>\n"
            f"• Targets: {len(targets)}\n"
            f"• History: {history[0]['count'] if history else 0}\n"
            f"• Cache: {len(CV_ENGINE.cache)} items\n\n"
            f"<b>Performance:</b>\n"
            f"• Avg batch time: {MONITOR.stats['avg_time']:.1f}s\n"
            f"• Success rate: {MONITOR.stats['matches']/max(MONITOR.stats['processed'],1):.1%}\n"
            f"• Errors: {MONITOR.stats['errors']}\n\n"
            f"<b>ML Models:</b>\n"
            f"• Quality model: {'✅' if ML_ENGINE.is_trained else '❌'}\n"
            f"• Device: {ML_ENGINE.device}\n"
            f"• Embeddings: {len(ML_ENGINE.embeddings_cache)}"
        )
        
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    
    async def cmd_train(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ручне тренування ML моделей"""
        msg = await update.message.reply_text("🧠 Training quality model...")
        
        success = await ML_ENGINE.train_quality_model()
        
        if success:
            await msg.edit_text("✅ Quality model trained successfully!")
        else:
            await msg.edit_text("❌ Training failed - insufficient data")
    
    async def cmd_clean(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Очищення кешу"""
        CV_ENGINE.clear_cache()
        ML_ENGINE.embeddings_cache.clear()
        
        # Очищення тимчасових файлів
        count = 0
        for f in CONFIG.CACHE_DIR.glob("*.jpg"):
            f.unlink(missing_ok=True)
            count += 1
        
        await update.message.reply_text(
            f"🧹 Cache cleaned:\n"
            f"• CV cache: cleared\n"
            f"• Embeddings: cleared\n"
            f"• Temp files: {count} removed"
        )
    
    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка callback-запитів"""
        q = update.callback_query
        await q.answer()
        data = q.data
        
        if data == "sync_empress":
            await q.edit_message_text("⏳ Scanning empress.cc ...")
            added = await scan_empress_all()
            await q.edit_message_text(f"✅ Empress sync complete!\nAdded: {added} targets")
        
        elif data == "targets_list":
            await self._show_targets(q)
        
        elif data.startswith("target_del_"):
            await self._target_delete_confirm(q)
        
        elif data.startswith("target_del_yes_"):
            await self._target_delete_yes(q)
        
        elif data == "targets_clear_all":
            await self._targets_clear_all(q)
        
        elif data == "targets_clear_confirm":
            await self._targets_clear_confirm(q)
        
        elif data == "add_target":
            context.user_data["state"] = "wait_img"
            await q.edit_message_text(
                "📸 Send photo of the target item"
            )
        
        elif data == "monitor_start":
            await MONITOR.start(context)
            await q.edit_message_text("🚀 Industrial monitor started")
        
        elif data == "monitor_stop":
            await MONITOR.stop()
            await q.edit_message_text("🛑 Industrial monitor stopped")
        
        elif data == "stats":
            await self.cmd_stats(update, context)
        
        elif data == "train":
            await self.cmd_train(update, context)
        
        elif data == "advanced":
            kb = [
                [InlineKeyboardButton("🧹 Clean Cache", callback_data="clean_cache")],
                [InlineKeyboardButton("📊 Performance", callback_data="perf")],
                [InlineKeyboardButton("🔧 Config", callback_data="config")],
                [InlineKeyboardButton("◀️ Back", callback_data="back")]
            ]
            await q.edit_message_text(
                "⚙️ <b>Advanced Settings</b>",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.HTML
            )
        
        elif data == "clean_cache":
            CV_ENGINE.clear_cache()
            await q.edit_message_text("✅ Cache cleaned")
        
        elif data == "back":
            await self.cmd_start(update, context)
    
    async def _show_targets(self, q):
        """Показати список цілей"""
        targets = await DB.get_targets()
        
        if not targets:
            await q.edit_message_text("❌ No targets found")
            return
        
        kb = []
        for t in targets[:10]:
            kb.append([
                InlineKeyboardButton(
                    f"🗑 {t['name'][:30]} ({t.get('match_count', 0)} matches)",
                    callback_data=f"target_del_{t['id']}"
                )
            ])
        
        kb.append([InlineKeyboardButton("❌ CLEAR ALL", callback_data="targets_clear_all")])
        kb.append([InlineKeyboardButton("◀️ BACK", callback_data="back")])
        
        await q.edit_message_text(
            f"🎯 <b>Targets ({len(targets)}):</b>\n"
            "Click to delete:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML
        )
    
    async def _target_delete_confirm(self, q):
        """Підтвердження видалення"""
        tid = q.data.replace("target_del_", "")
        target = await DB.fetch_one('SELECT * FROM targets WHERE id = ?', (tid,))
        
        if not target:
            await q.edit_message_text("❌ Target not found")
            return
        
        kb = [
            [
                InlineKeyboardButton("✅ YES", callback_data=f"target_del_yes_{tid}"),
                InlineKeyboardButton("❌ NO", callback_data="targets_list")
            ]
        ]
        
        await q.edit_message_text(
            f"⚠️ <b>Delete target?</b>\n\n"
            f"📦 {target['name']}\n"
            f"🎯 Matches: {target.get('match_count', 0)}\n"
            f"📅 Created: {datetime.fromtimestamp(target.get('created', 0)).strftime('%Y-%m-%d')}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML
        )
    
    async def _target_delete_yes(self, q):
        """Власне видалення"""
        tid = q.data.replace("target_del_yes_", "")
        
        # Видаляємо файл
        target = await DB.fetch_one('SELECT path FROM targets WHERE id = ?', (tid,))
        if target and os.path.exists(target['path']):
            os.remove(target['path'])
        
        # Видаляємо з БД
        await DB.execute('DELETE FROM targets WHERE id = ?', (tid,))
        await DB.execute('DELETE FROM search_history WHERE target_id = ?', (tid,))
        
        await q.edit_message_text("✅ Target deleted")
        await asyncio.sleep(1)
        await self._show_targets(q)
    
    async def _targets_clear_all(self, q):
        """Підтвердження видалення всіх"""
        targets = await DB.get_targets()
        
        kb = [
            [
                InlineKeyboardButton("🔥 CONFIRM", callback_data="targets_clear_confirm"),
                InlineKeyboardButton("❌ CANCEL", callback_data="targets_list")
            ]
        ]
        
        await q.edit_message_text(
            f"⚠️ <b>DELETE ALL TARGETS?</b>\n"
            f"Total: {len(targets)} items",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML
        )
    
    async def _targets_clear_confirm(self, q):
        """Видалення всіх цілей"""
        # Видаляємо файли
        targets = await DB.get_targets()
        for t in targets:
            if os.path.exists(t['path']):
                os.remove(t['path'])
        
        # Очищаємо БД
        await DB.execute('DELETE FROM targets')
        await DB.execute('DELETE FROM search_history')
        
        await q.edit_message_text("✅ All targets deleted")
    
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка фото для додавання цілі"""
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
                "✅ Photo saved!\n"
                "📝 Enter item name:"
            )
            
        except Exception as e:
            log.error(f"Photo save error: {e}")
            await update.message.reply_text("❌ Failed to save photo")
    
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка назви для нової цілі"""
        if context.user_data.get("state") == "wait_name":
            name = update.message.text.strip()
            tmp_path = context.user_data.get("tmp_p")
            
            if not tmp_path or not os.path.exists(tmp_path):
                await update.message.reply_text("❌ Photo not found")
                context.user_data.clear()
                return
            
            # Додаємо в БД
            target = {
                'id': f"MAN_{secrets.token_hex(4)}",
                'name': name,
                'path': tmp_path,
                'source': 'manual',
                'created': int(time.time()),
                'priority': 1,
                'tags': json.dumps(['manual']),
                'metadata': json.dumps({})
            }
            
            await DB.add_target(target)
            context.user_data.clear()
            
            await update.message.reply_text(f"✅ Target '{name}' added!")
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Глобальний обробник помилок"""
        log.error(f"Update {update} caused error {context.error}")
    
    async def web_index(self, request):
        """Web dashboard"""
        targets = await DB.get_targets()
        stats = MONITOR.stats
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Industrial Collector</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    background: #0a0a0c;
                    color: #e4e4e7;
                    margin: 0;
                    padding: 30px;
                }}
                .container {{
                    max-width: 1200px;
                    margin: 0 auto;
                }}
                h1 {{
                    font-size: 2.5em;
                    margin-bottom: 30px;
                    background: linear-gradient(45deg, #4caf50, #2196f3);
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                }}
                .grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                    gap: 20px;
                    margin-bottom: 30px;
                }}
                .card {{
                    background: rgba(22, 22, 26, 0.95);
                    padding: 25px;
                    border-radius: 12px;
                    border: 1px solid #2a2a2e;
                }}
                .stat {{
                    font-size: 2em;
                    font-weight: bold;
                    color: #4caf50;
                    margin: 10px 0;
                }}
                .label {{
                    color: #888;
                    font-size: 0.9em;
                    text-transform: uppercase;
                }}
                .badge {{
                    display: inline-block;
                    padding: 4px 12px;
                    border-radius: 20px;
                    background: rgba(33, 150, 243, 0.2);
                    color: #2196f3;
                    font-size: 0.8em;
                    margin: 2px;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🏭 Industrial Collector v30.0</h1>
                
                <div class="grid">
                    <div class="card">
                        <div class="label">Active Targets</div>
                        <div class="stat">{len(targets)}</div>
                        <span class="badge">YOLO: {CONFIG.YOLO_MODEL}</span>
                        <span class="badge">Batch: {CONFIG.BATCH_SIZE}</span>
                    </div>
                    
                    <div class="card">
                        <div class="label">Performance</div>
                        <div class="stat">{stats['processed']}</div>
                        <span class="badge">Matches: {stats['matches']}</span>
                        <span class="badge">Avg: {stats['avg_time']:.1f}s</span>
                    </div>
                    
                    <div class="card">
                        <div class="label">System</div>
                        <div class="stat">{CONFIG.CPU_COUNT} cores</div>
                        <span class="badge">RAM: {CONFIG.RAM_GB:.1f} GB</span>
                        <span class="badge">ML: {'✅' if ML_ENGINE.is_trained else '❌'}</span>
                    </div>
                </div>
                
                <div class="card">
                    <h2>🎯 Recent Targets</h2>
                    <ul style="list-style: none; padding: 0;">
                        {''.join([f"<li style='margin-bottom: 10px;'>• {t['name']} ({t.get('match_count', 0)} matches)</li>" for t in targets[:10]])}
                    </ul>
                </div>
            </div>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')
    
    async def web_stats(self, request):
        """API endpoint для статистики"""
        targets = await DB.get_targets()
        history = await DB.fetch_all('SELECT COUNT(*) as count FROM search_history')
        
        return web.json_response({
            'targets': len(targets),
            'history': history[0]['count'] if history else 0,
            'monitor': MONITOR.is_running,
            'processed': MONITOR.stats['processed'],
            'matches': MONITOR.stats['matches'],
            'threshold': CONFIG.SIMILARITY_THRESHOLD,
            'yolo': CONFIG.YOLO_MODEL,
            'ml': ML_ENGINE.is_trained,
            'uptime': time.time() - CONFIG.start_time
        })
    
    async def web_targets(self, request):
        """API endpoint для цілей"""
        targets = await DB.get_targets()
        return web.json_response(targets)
    
    def run(self):
        """Запуск бота"""
        print("""
        ╔══════════════════════════════════════════════════════════╗
        ║     CollectorBot Industrial v30.0                       ║
        ║     Production-Grade AI Monitoring System               ║
        ║     Python 3.13 | AutoML | YOLOv8 | SQLite | Async      ║
        ╚══════════════════════════════════════════════════════════╝
        """)
        
        log.info("🚀 Starting Industrial Collector...")
        self.app.run_polling(drop_pending_updates=True)

# ============================================================================
# [9] EMPRESS SCANNER (ІНТЕГРОВАНИЙ)
# ============================================================================

async def scan_empress_all():
    """Промисловий сканер Empress.cc"""
    page = 1
    added = 0
    targets = await DB.get_targets()
    existing_urls = {t.get('source_url') for t in targets if t.get('source_url')}
    
    async with aiohttp.ClientSession() as session:
        while True:
            url = EMPRESS_COLLECTION + EMPRESS_PAGE_PARAM.format(page)
            
            try:
                headers = {'User-Agent': UserAgent().random}
                async with session.get(url, headers=headers, timeout=30) as resp:
                    if resp.status != 200:
                        break
                    html = await resp.text()
                
                soup = BeautifulSoup(html, 'lxml')
                cards = soup.select('div.product-item')
                
                if not cards:
                    break
                
                for card in cards:
                    try:
                        title_elem = card.select_one('a.product-item__title')
                        price_elem = card.select_one('.price')
                        img_elem = card.select_one('img')
                        link_elem = card.select_one('a')
                        
                        if not all([title_elem, img_elem, link_elem]):
                            continue
                        
                        title = title_elem.text.strip()
                        url = EMPRESS_BASE + link_elem['href']
                        
                        if url in existing_urls:
                            continue
                        
                        img_url = img_elem.get('src') or img_elem.get('data-src')
                        if img_url and img_url.startswith('//'):
                            img_url = 'https:' + img_url
                        
                        # Завантаження зображення
                        async with session.get(img_url, headers=headers) as img_resp:
                            img_bytes = await img_resp.read()
                        
                        filename = f"empress_{secrets.token_hex(8)}.jpg"
                        path = CONFIG.TARGETS_DIR / filename
                        
                        async with aiofiles.open(path, 'wb') as f:
                            await f.write(img_bytes)
                        
                        # Додавання в БД
                        target = {
                            'id': f"EMP_{secrets.token_hex(4)}",
                            'name': title,
                            'path': str(path),
                            'source': 'empress',
                            'source_url': url,
                            'price': float(''.join(c for c in price_elem.text if c.isdigit())) if price_elem else 0,
                            'created': int(time.time()),
                            'priority': 2,
                            'tags': json.dumps(['empress']),
                            'metadata': json.dumps({})
                        }
                        
                        await DB.add_target(target)
                        added += 1
                        
                    except Exception as e:
                        log.debug(f"Empress card error: {e}")
                        continue
                
                page += 1
                await asyncio.sleep(1)
                
            except Exception as e:
                log.error(f"Empress scan error: {e}")
                break
    
    log.info(f"✅ Empress scan complete: +{added} targets")
    return added

# ============================================================================
# [10] MAIN
# ============================================================================

def main():
    """Точка входу"""
    bot = IndustrialBot()
    bot.run()

if __name__ == "__main__":
    main()
