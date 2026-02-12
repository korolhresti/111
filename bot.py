

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
# [1] КОНФІГУРАЦІЯ
# ============================================================================

REPLICA_KEYWORDS = [
    "репліка", "копія", "copy", "aaa", "aa+", 
    "1:1", "replica", "clone", "реп", "дублікат",
    "підробка", "fake", "unoriginal"
]

class AutoConfig:
    def __init__(self):
        self.env = os.getenv("ENVIRONMENT", "production")
        self.start_time = time.time()
        self.performance_metrics = deque(maxlen=1000)
        
        self.TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
        self.CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
        self.PORT = int(os.getenv("PORT", 8080))
        
        self.CPU_COUNT = os.cpu_count() or 2
        self.RAM_GB = self._get_ram_gb()
        self.IS_RENDER = os.getenv("RENDER", "false").lower() == "true"
        
        self.SIMILARITY_THRESHOLD = 0.75
        self.BATCH_SIZE = 5
        self.SCRAPE_INTERVAL = 600
        self.MAX_WORKERS = 2
        
        self.USE_TORCH = False  # Відключаємо Torch на Render
        self.YOLO_MODEL = "yolov8n.pt"
        
        self._init_paths()
        self._validate()
    
    def _get_ram_gb(self):
        try:
            import psutil
            return psutil.virtual_memory().total / (1024**3)
        except:
            return 1.0
    
    def _init_paths(self):
        self.BASE_DIR = Path(__file__).parent.resolve()
        self.DATA_DIR = self.BASE_DIR / "industrial_data"
        self.MODELS_DIR = self.DATA_DIR / "models"
        self.CACHE_DIR = self.DATA_DIR / "cache"
        self.LOGS_DIR = self.DATA_DIR / "logs"
        self.TARGETS_DIR = self.DATA_DIR / "targets"
        
        for d in [self.DATA_DIR, self.MODELS_DIR, self.CACHE_DIR, 
                  self.LOGS_DIR, self.TARGETS_DIR]:
            d.mkdir(parents=True, exist_ok=True)
    
    def _validate(self):
        if not self.TOKEN or ":" not in self.TOKEN:
            raise RuntimeError("❌ Invalid TELEGRAM_BOT_TOKEN")
        if self.ADMIN_CHAT_ID == 0:
            raise RuntimeError("❌ ADMIN_CHAT_ID missing")

CONFIG = AutoConfig()

# ============================================================================
# [2] ЛОГУВАННЯ
# ============================================================================

class IndustrialLogger:
    def __init__(self):
        self.logger = logging.getLogger("IndustrialCollector")
        self.logger.setLevel(logging.INFO)
        
        formatter = logging.Formatter(
            '%(asctime)s.%(msecs)03d | %(levelname)8s | %(name)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        log_file = CONFIG.LOGS_DIR / "industrial.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10*1024*1024, backupCount=3
        )
        file_handler.setFormatter(formatter)
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        
        self.metrics = defaultdict(list)
    
    def info(self, msg, **kwargs):
        self.logger.info(msg)
    
    def error(self, msg, **kwargs):
        self.logger.error(msg)
    
    def warning(self, msg, **kwargs):
        self.logger.warning(msg)
    
    def debug(self, msg, **kwargs):
        self.logger.debug(msg)

log = IndustrialLogger()

# ============================================================================
# [3] БАЗА ДАНИХ
# ============================================================================

class IndustrialDatabase:
    def __init__(self):
        self.db_path = CONFIG.DATA_DIR / "collector.db"
        self.conn = None
        self._init_db()
    
    def _init_db(self):
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        
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
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id TEXT,
                ad_url TEXT UNIQUE,
                ad_title TEXT,
                ad_price REAL,
                similarity REAL,
                timestamp INTEGER,
                source TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS market_intel (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_name TEXT,
                price REAL,
                timestamp INTEGER,
                source TEXT
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
    
    async def add_target(self, target: Dict):
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
            target.get('price', 0),
            target.get('created', int(time.time())),
            target.get('priority', 1),
            target.get('tags', '[]'),
            target.get('metadata', '{}')
        ))
        return True
    
    async def get_targets(self):
        query = 'SELECT * FROM targets ORDER BY priority DESC, created DESC'
        return await self.fetch_all(query)
    
    async def delete_target(self, target_id: str):
        await self.execute('DELETE FROM targets WHERE id = ?', (target_id,))
        await self.execute('DELETE FROM search_history WHERE target_id = ?', (target_id,))
    
    async def delete_all_targets(self):
        targets = await self.get_targets()
        for t in targets:
            if os.path.exists(t['path']):
                try:
                    os.remove(t['path'])
                except:
                    pass
        await self.execute('DELETE FROM targets')
        await self.execute('DELETE FROM search_history')
    
    async def add_match(self, target_id: str, ad: Dict, similarity: float):
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
        
        await self.execute('''
            UPDATE targets 
            SET match_count = match_count + 1 
            WHERE id = ?
        ''', (target_id,))

DB = IndustrialDatabase()

# ============================================================================
# [4] YOLO ДЕТЕКЦІЯ (Спрощена)
# ============================================================================

class YOLODetector:
    def __init__(self):
        self.model = None
        self._load_model()
    
    def _load_model(self):
        try:
            self.model = YOLO(CONFIG.YOLO_MODEL)
            log.info("✅ YOLO model loaded")
        except Exception as e:
            log.error(f"Failed to load YOLO: {e}")
            self.model = None
    
    def detect_objects(self, image_path: str) -> List[Dict]:
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

yolo_detector = YOLODetector()

# ============================================================================
# [5] CV-ENGINE (Спрощений)
# ============================================================================

class CVEngine:
    def __init__(self):
        self.cache = {}
    
    def _phash(self, img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        resized = cv2.resize(gray, (32, 32))
        dct = cv2.dct(np.float32(resized))
        dct_low = dct[:8, :8]
        median = np.median(dct_low)
        return (dct_low > median).flatten()
    
    def analyze(self, target_path: str, candidate_path: str) -> Dict:
        cache_key = f"{target_path}:{candidate_path}"
        
        if cache_key in self.cache:
            return self.cache[cache_key]
        
        img1 = cv2.imread(target_path)
        img2 = cv2.imread(candidate_path)
        
        if img1 is None or img2 is None:
            return {'score': 0.0}
        
        img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
        
        # pHash
        h1 = self._phash(img1)
        h2 = self._phash(img2)
        phash_score = 1.0 - (np.count_nonzero(h1 != h2) / len(h1))
        
        # ORB
        orb = cv2.ORB_create(500)
        kp1, des1 = orb.detectAndCompute(img1, None)
        kp2, des2 = orb.detectAndCompute(img2, None)
        
        orb_score = 0.0
        if des1 is not None and des2 is not None:
            bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
            matches = bf.match(des1, des2)
            orb_score = len(matches) / max(len(kp1), len(kp2), 1)
        
        # SSIM
        gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
        gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
        ssim_score = ssim(gray1, gray2)
        
        # Фінальний скор
        final_score = (phash_score * 0.4 + orb_score * 0.3 + ssim_score * 0.3)
        
        result = {'score': float(final_score)}
        self.cache[cache_key] = result
        
        if len(self.cache) > 500:
            self.cache.clear()
        
        return result
    
    def clear_cache(self):
        self.cache.clear()

cv_engine = CVEngine()

# ============================================================================
# [6] ПАРСЕР OLX
# ============================================================================

class OLXParser:
    def __init__(self):
        self.session = None
        self.ua = UserAgent()
    
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
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    return []
                
                html = await response.text()
            
            soup = BeautifulSoup(html, 'lxml')
            cards = soup.select('div[data-cy="l-card"]')
            
            results = []
            for card in cards[:limit]:
                if card.select_one('[data-testid="adCard-featured"]'):
                    continue
                
                title_elem = card.select_one('h6')
                if not title_elem:
                    continue
                
                title = title_elem.text.strip()
                
                price_elem = card.select_one('[data-testid="ad-price"]')
                price = price_elem.text.strip() if price_elem else "—"
                
                link_elem = card.select_one('a[href]')
                url = None
                if link_elem and link_elem.get('href'):
                    href = link_elem['href']
                    url = href if href.startswith('http') else f"https://www.olx.ua{href}"
                
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
                    'url': url,
                    'images': images,
                    'source': 'olx',
                    'timestamp': time.time()
                })
            
            return results
            
        except Exception as e:
            log.error(f"OLX search error: {e}")
            return []

olx_parser = OLXParser()

# ============================================================================
# [7] МОНІТОРИНГ
# ============================================================================

class IndustrialMonitor:
    def __init__(self):
        self.is_running = False
        self.task = None
        self.stats = {'processed': 0, 'matches': 0, 'errors': 0}
    
    async def start(self, context):
        if self.is_running:
            return
        self.is_running = True
        self.task = asyncio.create_task(self._monitor_loop(context))
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
    
    async def _monitor_loop(self, context):
        while self.is_running:
            try:
                targets = await DB.get_targets()
                
                if not targets:
                    await asyncio.sleep(60)
                    continue
                
                for target in targets[:CONFIG.BATCH_SIZE]:
                    try:
                        await self._process_target(target, context)
                        await asyncio.sleep(random.uniform(5, 10))
                    except Exception as e:
                        log.error(f"Target error: {e}")
                        self.stats['errors'] += 1
                
                await asyncio.sleep(CONFIG.SCRAPE_INTERVAL)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Monitor error: {e}")
                await asyncio.sleep(60)
    
    async def _process_target(self, target: Dict, context):
        ads = await olx_parser.search(target['name'])
        
        if not ads:
            return
        
        history = await DB.fetch_all(
            'SELECT ad_url FROM search_history WHERE target_id = ?',
            (target['id'],)
        )
        seen_urls = {h['ad_url'] for h in history}
        
        for ad in ads[:3]:
            if ad.get('url') in seen_urls:
                continue
            
            best_score = 0.0
            
            for img_url in ad.get('images', [])[:1]:
                score = await self._analyze_image(target['path'], img_url)
                best_score = max(best_score, score)
            
            if best_score >= CONFIG.SIMILARITY_THRESHOLD:
                await self._send_match(target, ad, best_score, context)
                self.stats['matches'] += 1
            
            self.stats['processed'] += 1
    
    async def _analyze_image(self, target_path: str, img_url: str) -> float:
        temp_path = CONFIG.CACHE_DIR / f"img_{secrets.token_hex(4)}.jpg"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(img_url, timeout=15) as response:
                    if response.status != 200:
                        return 0.0
                    content = await response.read()
                    
                    async with aiofiles.open(temp_path, 'wb') as f:
                        await f.write(content)
            
            # YOLO детекція
            detections = yolo_detector.detect_objects(str(temp_path))
            
            if detections:
                best_score = 0.0
                img = cv2.imread(str(temp_path))
                
                for det in detections[:2]:
                    bbox = det['bbox']
                    x1, y1, x2, y2 = map(int, bbox)
                    crop = img[y1:y2, x1:x2]
                    
                    if crop.size > 0:
                        crop_path = CONFIG.CACHE_DIR / f"crop_{secrets.token_hex(4)}.jpg"
                        cv2.imwrite(str(crop_path), crop)
                        
                        result = cv_engine.analyze(target_path, str(crop_path))
                        best_score = max(best_score, result['score'])
                        
                        crop_path.unlink(missing_ok=True)
                
                temp_path.unlink(missing_ok=True)
                return best_score
            else:
                result = cv_engine.analyze(target_path, str(temp_path))
                temp_path.unlink(missing_ok=True)
                return result['score']
                
        except Exception as e:
            log.debug(f"Image analysis error: {e}")
            temp_path.unlink(missing_ok=True)
            return 0.0
    
    async def _send_match(self, target: Dict, ad: Dict, similarity: float, context):
        await DB.add_match(target['id'], ad, similarity)
        
        is_replica = any(k in ad['title'].lower() for k in REPLICA_KEYWORDS)
        
        caption = (
            f"🔥 <b>MATCH FOUND!</b>\n\n"
            f"🎯 <b>Target:</b> {target['name'][:50]}\n"
            f"📦 <b>Found:</b> {ad['title'][:100]}\n"
            f"💰 <b>Price:</b> {ad['price']}\n"
            f"📊 <b>Similarity:</b> {similarity:.1%}\n"
            f"{'⚠️ REPLICA DETECTED' if is_replica else '✅ Original'}\n\n"
            f"🔗 <a href='{ad['url']}'>View Listing</a>"
        )
        
        try:
            if ad.get('images'):
                await context.bot.send_photo(
                    chat_id=CONFIG.ADMIN_CHAT_ID,
                    photo=ad['images'][0],
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            else:
                await context.bot.send_message(
                    chat_id=CONFIG.ADMIN_CHAT_ID,
                    text=caption,
                    parse_mode=ParseMode.HTML
                )
            
            log.info(f"✅ Match sent: {target['name']} @ {ad['price']}")
            
        except Exception as e:
            log.error(f"Failed to send match: {e}")

monitor = IndustrialMonitor()

# ============================================================================
# [8] TELEGRAM БОТ
# ============================================================================

class IndustrialBot:
    def __init__(self):
        self.app = ApplicationBuilder() \
            .token(CONFIG.TOKEN) \
            .post_init(self.post_init) \
            .build()
        
        self._setup_handlers()
    
    def _setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("stats", self.cmd_stats))
        self.app.add_handler(CommandHandler("clean", self.cmd_clean))
        self.app.add_handler(CallbackQueryHandler(self.callback_handler))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        self.app.add_error_handler(self.error_handler)
    
    async def post_init(self, app):
        await self._start_web_server()
        asyncio.create_task(self._auto_start_monitor())
        log.info("✅ Bot initialized")
    
    async def _start_web_server(self):
        app = web.Application()
        app.router.add_get('/', self.web_index)
        app.router.add_get('/api/stats', self.web_stats)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', CONFIG.PORT)
        await site.start()
        
        log.info(f"🌐 Dashboard: http://0.0.0.0:{CONFIG.PORT}")
    
    async def _auto_start_monitor(self):
        await asyncio.sleep(5)
        await monitor.start(ContextTypes.DEFAULT_TYPE(application=self.app))
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != CONFIG.ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Access denied")
            return
        
        targets = await DB.get_targets()
        
        keyboard = [
            [InlineKeyboardButton("🌐 SYNC EMPRESS", callback_data="sync_empress")],
            [InlineKeyboardButton("🎯 TARGETS", callback_data="targets_list"),
             InlineKeyboardButton("➕ ADD TARGET", callback_data="add_target")],
            [InlineKeyboardButton("▶️ START", callback_data="monitor_start"),
             InlineKeyboardButton("⏹ STOP", callback_data="monitor_stop")],
            [InlineKeyboardButton("📊 STATS", callback_data="stats"),
             InlineKeyboardButton("🧹 CLEAN", callback_data="clean")],
        ]
        
        await update.message.reply_text(
            f"🏭 <b>CollectorBot Industrial v30.0</b>\n\n"
            f"📊 <b>Status:</b>\n"
            f"• Targets: {len(targets)}\n"
            f"• Threshold: {int(CONFIG.SIMILARITY_THRESHOLD*100)}%\n"
            f"• Monitor: {'🟢' if monitor.is_running else '🔴'}\n"
            f"• Processed: {monitor.stats['processed']}\n"
            f"• Matches: {monitor.stats['matches']}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        targets = await DB.get_targets()
        history = await DB.fetch_all('SELECT COUNT(*) as cnt FROM search_history')
        history_count = history[0]['cnt'] if history else 0
        
        await update.message.reply_text(
            f"📈 <b>Statistics</b>\n\n"
            f"<b>System:</b>\n"
            f"• CPU: {CONFIG.CPU_COUNT} cores\n"
            f"• RAM: {CONFIG.RAM_GB:.1f} GB\n"
            f"• Uptime: {timedelta(seconds=int(time.time()-CONFIG.start_time))}\n\n"
            f"<b>Database:</b>\n"
            f"• Targets: {len(targets)}\n"
            f"• History: {history_count}\n"
            f"• Cache: {len(cv_engine.cache)}\n\n"
            f"<b>Performance:</b>\n"
            f"• Processed: {monitor.stats['processed']}\n"
            f"• Matches: {monitor.stats['matches']}\n"
            f"• Errors: {monitor.stats['errors']}",
            parse_mode=ParseMode.HTML
        )
    
    async def cmd_clean(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cv_engine.clear_cache()
        
        count = 0
        for f in CONFIG.CACHE_DIR.glob("*.jpg"):
            f.unlink(missing_ok=True)
            count += 1
        
        await update.message.reply_text(f"🧹 Cache cleaned: {count} files removed")
    
    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        
        if data == "sync_empress":
            await query.edit_message_text("⏳ Scanning empress.cc...")
            added = await self.sync_empress()
            await query.edit_message_text(f"✅ Sync complete! Added: {added} targets")
        
        elif data == "targets_list":
            await self._show_targets(query)
        
        elif data.startswith("target_del_"):
            await self._target_delete_confirm(query)
        
        elif data.startswith("target_del_yes_"):
            await self._target_delete_yes(query)
        
        elif data == "targets_clear_all":
            await self._targets_clear_all(query)
        
        elif data == "targets_clear_confirm":
            await self._targets_clear_confirm(query)
        
        elif data == "add_target":
            context.user_data["state"] = "wait_img"
            await query.edit_message_text("📸 Send photo of the target item")
        
        elif data == "monitor_start":
            await monitor.start(context)
            await query.edit_message_text("🚀 Monitor started")
        
        elif data == "monitor_stop":
            await monitor.stop()
            await query.edit_message_text("🛑 Monitor stopped")
        
        elif data == "stats":
            await self.cmd_stats(update, context)
        
        elif data == "clean":
            cv_engine.clear_cache()
            await query.edit_message_text("✅ Cache cleaned")
        
        elif data == "back":
            await self.cmd_start(update, context)
    
    async def _show_targets(self, query):
        targets = await DB.get_targets()
        
        if not targets:
            await query.edit_message_text("❌ No targets found")
            return
        
        keyboard = []
        for t in targets[:10]:
            keyboard.append([
                InlineKeyboardButton(
                    f"🗑 {t['name'][:30]} ({t.get('match_count', 0)} matches)",
                    callback_data=f"target_del_{t['id']}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("❌ CLEAR ALL", callback_data="targets_clear_all")])
        keyboard.append([InlineKeyboardButton("◀️ BACK", callback_data="back")])
        
        await query.edit_message_text(
            f"🎯 <b>Targets ({len(targets)}):</b>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _target_delete_confirm(self, query):
        tid = query.data.replace("target_del_", "")
        target = await DB.fetch_one('SELECT * FROM targets WHERE id = ?', (tid,))
        
        if not target:
            await query.edit_message_text("❌ Target not found")
            return
        
        keyboard = [
            [
                InlineKeyboardButton("✅ YES", callback_data=f"target_del_yes_{tid}"),
                InlineKeyboardButton("❌ NO", callback_data="targets_list")
            ]
        ]
        
        await query.edit_message_text(
            f"⚠️ <b>Delete target?</b>\n\n"
            f"📦 {target['name']}\n"
            f"🎯 Matches: {target.get('match_count', 0)}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _target_delete_yes(self, query):
        tid = query.data.replace("target_del_yes_", "")
        await DB.delete_target(tid)
        
        await query.edit_message_text("✅ Target deleted")
        await asyncio.sleep(1)
        await self._show_targets(query)
    
    async def _targets_clear_all(self, query):
        targets = await DB.get_targets()
        
        keyboard = [
            [
                InlineKeyboardButton("🔥 CONFIRM", callback_data="targets_clear_confirm"),
                InlineKeyboardButton("❌ CANCEL", callback_data="targets_list")
            ]
        ]
        
        await query.edit_message_text(
            f"⚠️ <b>DELETE ALL TARGETS?</b>\n"
            f"Total: {len(targets)} items",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _targets_clear_confirm(self, query):
        await DB.delete_all_targets()
        await query.edit_message_text("✅ All targets deleted")
    
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            log.error(f"Photo error: {e}")
            await update.message.reply_text("❌ Failed to save photo")
    
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.user_data.get("state") == "wait_name":
            name = update.message.text.strip()
            tmp_path = context.user_data.get("tmp_p")
            
            if not tmp_path or not os.path.exists(tmp_path):
                await update.message.reply_text("❌ Photo not found")
                context.user_data.clear()
                return
            
            target = {
                'id': f"MAN_{secrets.token_hex(4)}",
                'name': name,
                'path': tmp_path,
                'source': 'manual',
                'created': int(time.time()),
                'priority': 1,
                'tags': '["manual"]',
                'metadata': '{}'
            }
            
            await DB.add_target(target)
            context.user_data.clear()
            
            await update.message.reply_text(f"✅ Target '{name}' added!")
    
    async def sync_empress(self) -> int:
        added = 0
        
        urls = [
            "https://empress.cc/collections/gents-vintage-watches",
            "https://empress.cc/collections/pocket-watches",
            "https://empress.cc/collections/ladies-vintage-watches",
            "https://empress.cc/collections/omega-vintage-watches",
            "https://empress.cc/collections/all-vintage-watches"
        ]

        log.info(f"🚀 Scanning {len(urls)} categories...")
        
        current_targets = await DB.get_targets()
        existing_urls = {t.get('source_url') for t in current_targets if t.get('source_url')}
        
        ua = UserAgent()
        
        async with aiohttp.ClientSession() as session:
            for base_url in urls:
                page = 1
                
                while page <= 2:
                    url = f"{base_url}?page={page}"
                    
                    try:
                        headers = {'User-Agent': ua.random}
                        async with session.get(url, headers=headers, timeout=20) as resp:
                            if resp.status != 200:
                                break
                            
                            html = await resp.text()
                            soup = BeautifulSoup(html, 'lxml')
                            
                            cards = soup.select('.product-card, .grid-view-item, .product-item')
                            
                            for card in cards:
                                try:
                                    title_elem = card.select_one('.product-card__title, .h4, .product-item__title')
                                    link_elem = card.select_one('a[href*="/products/"]')
                                    
                                    if not title_elem or not link_elem:
                                        continue
                                    
                                    title = title_elem.get_text(strip=True)
                                    prod_url = "https://empress.cc" + link_elem['href']
                                    
                                    if prod_url in existing_urls:
                                        continue
                                    
                                    img_elem = card.select_one('img')
                                    img_url = ""
                                    if img_elem:
                                        img_url = img_elem.get('data-src') or img_elem.get('src') or ""
                                        if img_url.startswith('//'):
                                            img_url = 'https:' + img_url
                                    
                                    local_path = ""
                                    if img_url:
                                        try:
                                            async with session.get(img_url, headers=headers) as img_resp:
                                                if img_resp.status == 200:
                                                    img_bytes = await img_resp.read()
                                                    filename = f"emp_{secrets.token_hex(6)}.jpg"
                                                    save_path = CONFIG.TARGETS_DIR / filename
                                                    async with aiofiles.open(save_path, 'wb') as f:
                                                        await f.write(img_bytes)
                                                    local_path = str(save_path)
                                        except:
                                            pass
                                    
                                    target = {
                                        'id': f"EMP_{secrets.token_hex(4)}",
                                        'name': title,
                                        'path': local_path or prod_url,
                                        'source': 'empress',
                                        'source_url': prod_url,
                                        'price': 0,
                                        'created': int(time.time()),
                                        'priority': 1,
                                        'tags': '["watch","empress"]',
                                        'metadata': '{}'
                                    }
                                    
                                    if await DB.add_target(target):
                                        added += 1
                                        existing_urls.add(prod_url)
                                    
                                except Exception as e:
                                    continue
                            
                            page += 1
                            await asyncio.sleep(0.5)
                            
                    except Exception as e:
                        log.error(f"Error: {e}")
                        break
        
        log.info(f"✅ Sync complete! Added {added} targets")
        return added
    
    async def web_index(self, request):
        targets = await DB.get_targets()
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Industrial Collector</title>
            <style>
                body {{ font-family: Arial; background: #0a0a0c; color: #e4e4e7; padding: 30px; }}
                h1 {{ color: #4caf50; }}
                .card {{ background: #16161a; padding: 20px; border-radius: 10px; margin: 10px 0; }}
                .stat {{ font-size: 2em; color: #4caf50; }}
            </style>
        </head>
        <body>
            <h1>🏭 Industrial Collector v30.0</h1>
            <div class="card">
                <div>Active Targets</div>
                <div class="stat">{len(targets)}</div>
            </div>
            <div class="card">
                <div>Processed</div>
                <div class="stat">{monitor.stats['processed']}</div>
            </div>
            <div class="card">
                <div>Matches</div>
                <div class="stat">{monitor.stats['matches']}</div>
            </div>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')
    
    async def web_stats(self, request):
        targets = await DB.get_targets()
        return web.json_response({
            'targets': len(targets),
            'processed': monitor.stats['processed'],
            'matches': monitor.stats['matches'],
            'monitor': monitor.is_running
        })
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        log.error(f"Update {update.update_id} caused error {context.error}")
        
        try:
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    f"❌ Error: {str(context.error)[:100]}"
                )
            elif update.message:
                await update.message.reply_text(
                    f"❌ Error: {str(context.error)[:100]}"
                )
        except:
            pass
    
    def run(self):
        print("""
        ╔══════════════════════════════════════════════════════════╗
        ║     CollectorBot Industrial v30.0                       ║
        ║     Ready for Render | Python 3.13 | YOLOv8 | SQLite    ║
        ╚══════════════════════════════════════════════════════════╝
        """)
        
        log.info("Starting bot...")
        self.app.run_polling(drop_pending_updates=True)

# ============================================================================
# [9] MAIN
# ============================================================================

def main():
    try:
        bot = IndustrialBot()
        bot.run()
    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
