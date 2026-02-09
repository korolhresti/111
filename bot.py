# ============================================================
# CollectorBot PRO v12 FINAL — PART 1 / 3
# Python 3.13 — Render Ready — Single-file (split for deploy)
# ============================================================

import os
import cv2
import json
import time
import asyncio
import signal
import random
import hashlib
import logging
from datetime import datetime
from typing import List, Dict, Any

import numpy as np
import aiohttp
from aiohttp import web

from bs4 import BeautifulSoup
from fake_useragent import UserAgent

import torch
from ultralytics import YOLO

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV / CONFIG
# =========================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID")
PORT = int(os.getenv("PORT", "10000"))

if not TOKEN or not ADMIN_ID:
    raise RuntimeError("ENV NOT SET")

BASE_DIR = os.getcwd()
DATA_DIR = os.path.join(BASE_DIR, "data")
IMG_DIR = os.path.join(DATA_DIR, "images")
EMB_DIR = os.path.join(DATA_DIR, "embeddings")

for d in (DATA_DIR, IMG_DIR, EMB_DIR):
    os.makedirs(d, exist_ok=True)

TARGETS_FILE = os.path.join(DATA_DIR, "targets.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
PRICE_STATS_FILE = os.path.join(DATA_DIR, "price_stats.json")
SUPER_DEALS_FILE = os.path.join(DATA_DIR, "super_deals.json")

SIMILARITY_THRESHOLD = 0.80
SUPER_DEAL_DISCOUNT = 0.35
MIN_HISTORY_SAMPLES = 5

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("CollectorBot")

# =========================
# GLOBAL STATE
# =========================

MONITOR_RUNNING = False
MONITOR_TASK = None
WAIT_PHOTO = set()
WAIT_DELETE = set()

# =========================
# UTILS
# =========================

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()

def now():
    return int(time.time())

def cosine(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-6))

def save_image_bytes(data: bytes):
    name = f"{now()}_{random.randint(1000,9999)}.jpg"
    path = os.path.join(IMG_DIR, name)
    with open(path, "wb") as f:
        f.write(data)
    return path

def embed_image(path):
    img = cv2.imread(path)
    img = cv2.resize(img, (128, 128))
    return img.mean(axis=(0, 1))

# =========================
# TARGET MANAGEMENT
# =========================

def load_targets():
    return load_json(TARGETS_FILE, [])

def save_targets(t):
    save_json(TARGETS_FILE, t)

def add_target(image_path, title="Manual Item"):
    targets = load_targets()
    tid = sha256_file(image_path)
    emb = embed_image(image_path)
    emb_path = os.path.join(EMB_DIR, f"{tid}.npy")
    np.save(emb_path, emb)

    targets.append({
        "id": tid,
        "title": title,
        "image": image_path,
        "embedding": emb_path,
        "created": datetime.utcnow().isoformat()
    })
    save_targets(targets)

def delete_target_by_keyword(keyword):
    targets = load_targets()
    before = len(targets)
    targets = [
        t for t in targets
        if keyword.lower() not in t["id"].lower()
        and keyword.lower() not in t["title"].lower()
    ]
    save_targets(targets)
    return before - len(targets)

# =========================
# HISTORY / PRICE STATS
# =========================

def load_history():
    return load_json(HISTORY_FILE, [])

def save_history(h):
    save_json(HISTORY_FILE, h)

def load_price_stats():
    return load_json(PRICE_STATS_FILE, {})

def save_price_stats(p):
    save_json(PRICE_STATS_FILE, p)

def update_price_stats(title, price):
    if not price:
        return
    stats = load_price_stats()
    bucket = stats.setdefault(title, [])
    bucket.append(price)
    bucket[:] = bucket[-100:]
    save_price_stats(stats)

def avg_price(title):
    stats = load_price_stats().get(title, [])
    if len(stats) < MIN_HISTORY_SAMPLES:
        return None
    return sum(stats) / len(stats)

# =========================
# YOLO ENGINE
# =========================

YOLO_MODEL_NAME = "yolov8n.pt"
YOLO_CONF = 0.35
YOLO_IOU = 0.45

yolo_model = None
yolo_device = "cpu"

def load_yolo():
    global yolo_model, yolo_device
    if yolo_model:
        return
    yolo_device = "cuda" if torch.cuda.is_available() else "cpu"
    yolo_model = YOLO(YOLO_MODEL_NAME)
    yolo_model.to(yolo_device)
    log.info(f"YOLO loaded on {yolo_device}")

def detect_objects(img_np):
    load_yolo()
    res = yolo_model(
        img_np,
        conf=YOLO_CONF,
        iou=YOLO_IOU,
        device=yolo_device,
        verbose=False
    )
    boxes = []
    for r in res:
        if r.boxes:
            for b in r.boxes.xyxy.cpu().numpy():
                boxes.append(tuple(map(int, b)))
    return boxes

def extract_objects(img_np):
    h, w, _ = img_np.shape
    boxes = detect_objects(img_np)
    crops = []
    for x1, y1, x2, y2 in boxes:
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        crop = img_np[y1:y2, x1:x2]
        if crop.size > 0:
            crops.append(crop)
    return crops if crops else [img_np]

# =========================
# CV ENGINE
# =========================

ORB = cv2.ORB_create(1500)

def orb_score(a, b):
    a = cv2.cvtColor(cv2.resize(a, (512, 512)), cv2.COLOR_BGR2GRAY)
    b = cv2.cvtColor(cv2.resize(b, (512, 512)), cv2.COLOR_BGR2GRAY)
    k1, d1 = ORB.detectAndCompute(a, None)
    k2, d2 = ORB.detectAndCompute(b, None)
    if d1 is None or d2 is None:
        return 0.0
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    m = bf.match(d1, d2)
    good = [x for x in m if x.distance < 50]
    return min(1.0, len(good) / max(len(k1), len(k2)))

def hsv_score(a, b):
    h1 = cv2.cvtColor(a, cv2.COLOR_BGR2HSV)
    h2 = cv2.cvtColor(b, cv2.COLOR_BGR2HSV)
    c1 = cv2.calcHist([h1], [0, 1], None, [50, 60], [0, 180, 0, 256])
    c2 = cv2.calcHist([h2], [0, 1], None, [50, 60], [0, 180, 0, 256])
    cv2.normalize(c1, c1)
    cv2.normalize(c2, c2)
    return max(0.0, min(1.0, cv2.compareHist(c1, c2, cv2.HISTCMP_CORREL)))

def compare_images(path_a, path_b):
    a = cv2.imread(path_a)
    b = cv2.imread(path_b)
    o = orb_score(a, b)
    h = hsv_score(a, b)
    return o * 0.65 + h * 0.35
# ============================================================
# CollectorBot PRO v12 FINAL — PART 2 / 3
# ============================================================

# =========================
# NETWORK / SCRAPING
# =========================

UA = UserAgent()

async def fetch(session, url):
    await asyncio.sleep(random.uniform(1.2, 3.0))
    headers = {"User-Agent": UA.random}
    async with session.get(url, headers=headers, timeout=30) as r:
        return await r.text()

async def fetch_bytes(session, url):
    await asyncio.sleep(random.uniform(1.2, 2.5))
    headers = {"User-Agent": UA.random}
    async with session.get(url, headers=headers, timeout=30) as r:
        return await r.read()

def parse_price(text):
    try:
        return int("".join([c for c in text if c.isdigit()]))
    except:
        return None

# =========================
# EMPRESS SYNC
# =========================

async def sync_empress():
    url = "https://empress.cc/collections/all"
    async with aiohttp.ClientSession() as session:
        html = await fetch(session, url)
        soup = BeautifulSoup(html, "lxml")

        items = soup.select("div.product-item")
        added = 0

        for it in items:
            title = it.select_one(".product-title")
            price = it.select_one(".price")
            img = it.select_one("img")

            if not title or not img:
                continue

            img_url = img.get("src")
            if not img_url.startswith("http"):
                img_url = "https:" + img_url

            data = await fetch_bytes(session, img_url)
            img_path = save_image_bytes(data)
            add_target(img_path, title.text.strip())
            added += 1

        return added

# =========================
# OLX SEARCH
# =========================

async def search_olx(query):
    url = f"https://www.olx.ua/d/uk/list/q-{query.replace(' ', '-')}/"
    async with aiohttp.ClientSession() as session:
        html = await fetch(session, url)
        soup = BeautifulSoup(html, "lxml")

        ads = []
        for card in soup.select("div[data-cy='l-card']"):
            if card.select_one("[data-testid='adCard-featured']"):
                continue

            link = card.select_one("a")
            title = card.select_one("h6")
            price = card.select_one("p[data-testid='ad-price']")
            img = card.select_one("img")

            if not link or not img:
                continue

            ads.append({
                "title": title.text.strip() if title else "",
                "price": parse_price(price.text) if price else None,
                "url": "https://www.olx.ua" + link["href"],
                "image": img.get("src")
            })

        return ads

# =========================
# MATCH ENGINE
# =========================

async def process_target(target):
    history = load_history()
    seen = {h["url"] for h in history}

    ads = await search_olx(target["title"] + " б.у")

    async with aiohttp.ClientSession() as session:
        for ad in ads:
            if ad["url"] in seen:
                continue

            img_data = await fetch_bytes(session, ad["image"])
            img_path = save_image_bytes(img_data)

            score = compare_images(target["image"], img_path)
            update_price_stats(target["title"], ad["price"])

            history.append({
                "url": ad["url"],
                "score": score,
                "time": now()
            })
            save_history(history)

            if score >= SIMILARITY_THRESHOLD:
                await send_match(target, ad, score)

# =========================
# SUPER DEAL DETECTOR
# =========================

async def send_match(target, ad, score):
    avg = avg_price(target["title"])
    deal = False

    if avg and ad["price"]:
        if ad["price"] < avg * (1 - SUPER_DEAL_DISCOUNT):
            deal = True
            deals = load_json(SUPER_DEALS_FILE, [])
            deals.append({
                "title": target["title"],
                "price": ad["price"],
                "avg": avg,
                "url": ad["url"],
                "time": now()
            })
            save_json(SUPER_DEALS_FILE, deals)

    text = (
        f"🚨 MATCH FOUND\n"
        f"🎯 {target['title']}\n"
        f"📦 {ad['title']}\n"
        f"💵 {ad['price']} грн\n"
        f"📊 Similarity: {score*100:.1f}%\n"
        f"{'🔥 SUPER DEAL' if deal else ''}\n"
        f"{ad['url']}"
    )

    await app.bot.send_message(chat_id=CHANNEL_ID or ADMIN_ID, text=text)

# =========================
# MONITOR LOOP
# =========================

async def monitor_loop():
    global MONITOR_RUNNING
    MONITOR_RUNNING = True
    while MONITOR_RUNNING:
        targets = load_targets()
        random.shuffle(targets)
        for t in targets[:5]:
            await process_target(t)
        await asyncio.sleep(300)
# ============================================================
# CollectorBot PRO v12 FINAL — PART 3 / 3
# ============================================================

# =========================
# TELEGRAM COMMANDS
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ Додати еталон", callback_data="add")],
        [InlineKeyboardButton("📦 Список еталонів", callback_data="list")],
        [InlineKeyboardButton("▶️ Старт моніторингу", callback_data="run")],
        [InlineKeyboardButton("⏹ Стоп моніторингу", callback_data="stop")],
        [InlineKeyboardButton("🔥 Супер-угоди", callback_data="deals")],
        [InlineKeyboardButton("🧠 YOLO детекція", callback_data="yolo")],
        [InlineKeyboardButton("🌐 Web-Admin", callback_data="web")]
    ]
    await update.message.reply_text(
        "CollectorBot PRO v12\nОберіть дію:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "run":
        asyncio.create_task(monitor_loop())
        await q.edit_message_text("▶️ Моніторинг запущено")

    elif q.data == "stop":
        global MONITOR_RUNNING
        MONITOR_RUNNING = False
        await q.edit_message_text("⏹ Моніторинг зупинено")

    elif q.data == "list":
        targets = load_targets()
        text = "\n".join([f"{i+1}. {t['title']}" for i, t in enumerate(targets)]) or "Порожньо"
        await q.edit_message_text(text)

    elif q.data == "deals":
        deals = load_json(SUPER_DEALS_FILE, [])
        if not deals:
            await q.edit_message_text("Немає супер-угод")
            return
        txt = "\n\n".join([
            f"{d['title']}\n💵 {d['price']} (avg {d['avg']})\n{d['url']}"
            for d in deals[-5:]
        ])
        await q.edit_message_text(txt[:4000])

    elif q.data == "web":
        await q.edit_message_text(f"Web Admin: http://0.0.0.0:{WEB_PORT}")

    elif q.data == "yolo":
        await q.edit_message_text("YOLO активний (детекція при додаванні фото)")

    elif q.data == "add":
        context.user_data["wait_photo"] = True
        await q.edit_message_text("Надішліть фото еталону")

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("wait_photo"):
        return

    file = await update.message.photo[-1].get_file()
    path = os.path.join(IMG_DIR, f"user_{update.effective_user.id}_{now()}.jpg")
    await file.download_to_drive(path)

    context.user_data["wait_photo"] = False
    context.user_data["photo_path"] = path
    context.user_data["wait_title"] = True

    await update.message.reply_text("Введіть назву еталону")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("wait_title"):
        title = update.message.text
        path = context.user_data.get("photo_path")
        add_target(path, title)
        context.user_data.clear()
        await update.message.reply_text("✅ Еталон додано")

# =========================
# WEB ADMIN (LOCAL)
# =========================

async def web_index(request):
    targets = load_targets()
    html = "<h1>CollectorBot Admin</h1><ul>"
    for t in targets:
        html += f"<li>{t['title']}</li>"
    html += "</ul>"
    return web.Response(text=html, content_type="text/html")

async def start_web():
    appw = web.Application()
    appw.router.add_get("/", web_index)
    runner = web.AppRunner(appw)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()

# =========================
# MAIN
# =========================

def main():
    global app
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    asyncio.get_event_loop().create_task(start_web())
    app.run_polling()

if __name__ == "__main__":
    main()
    
