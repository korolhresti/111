# ===========================
# CollectorBot Pro v8.0
# ===========================

import os
import json
import cv2
import aiohttp
import asyncio
import random
import logging
import numpy as np
from datetime import datetime
from fake_useragent import UserAgent
from bs4 import BeautifulSoup

from telegram import Update, InputFile, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================= CONFIG =================

VERSION = "8.0"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

BASE = os.path.dirname(__file__)
DATA = f"{BASE}/data"
IMAGES = f"{BASE}/images"
TARGET_IMG = f"{IMAGES}/targets"
TEMP_IMG = f"{IMAGES}/temp"

for d in [DATA, IMAGES, TARGET_IMG, TEMP_IMG]:
    os.makedirs(d, exist_ok=True)

FILES = {
    "targets": f"{DATA}/targets.json",
    "history": f"{DATA}/history.json",
    "state": f"{DATA}/state.json",
}

SIM_THRESHOLD = 80

ua = UserAgent()

# ================= LOG =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("v8")

# ================= STORAGE =================

def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

TARGETS = load(FILES["targets"], {})
HISTORY = load(FILES["history"], [])
STATE = load(FILES["state"], {})

# ================= SECURITY =================

def is_admin(uid):
    return uid == ADMIN_ID

# ================= CV ENGINE =================

def compare_images(img1_path, img2_path):
    img1 = cv2.imread(img1_path)
    img2 = cv2.imread(img2_path)

    if img1 is None or img2 is None:
        return 0

    orb = cv2.ORB_create(1000)
    kp1, des1 = orb.detectAndCompute(img1, None)
    kp2, des2 = orb.detectAndCompute(img2, None)

    if des1 is None or des2 is None:
        return 0

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(des1, des2)

    orb_score = len(matches) / max(len(kp1), 1) * 100

    hsv1 = cv2.cvtColor(img1, cv2.COLOR_BGR2HSV)
    hsv2 = cv2.cvtColor(img2, cv2.COLOR_BGR2HSV)

    hist1 = cv2.calcHist([hsv1], [0,1], None, [50,60], [0,180,0,256])
    hist2 = cv2.calcHist([hsv2], [0,1], None, [50,60], [0,180,0,256])

    cv2.normalize(hist1, hist1)
    cv2.normalize(hist2, hist2)

    color_score = cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL) * 100

    return (orb_score * 0.6 + color_score * 0.4)

# ================= SCRAPER =================

async def fetch(session, url):
    async with session.get(url, headers={"User-Agent": ua.random}) as r:
        return await r.text()

# ================= MONITOR LOOP =================

async def monitor_loop(app):
    while True:
        try:
            if not TARGETS:
                await asyncio.sleep(30)
                continue

            sample = random.sample(list(TARGETS.values()), min(3, len(TARGETS)))

            async with aiohttp.ClientSession() as session:
                for t in sample:
                    query = t["name"] + " б.у"
                    url = f"https://www.olx.ua/d/uk/list/q-{query.replace(' ', '-')}/"

                    html = await fetch(session, url)
                    soup = BeautifulSoup(html, "lxml")

                    for ad in soup.select("div[data-cy='l-card']"):
                        link = ad.find("a")
                        if not link:
                            continue

                        href = "https://www.olx.ua" + link["href"]
                        if href in HISTORY:
                            continue

                        HISTORY.append(href)
                        save(FILES["history"], HISTORY)

                        img = ad.find("img")
                        if not img or not img.get("src"):
                            continue

                        img_url = img["src"]
                        img_path = f"{TEMP_IMG}/{random.randint(1,999999)}.jpg"

                        async with session.get(img_url) as r:
                            with open(img_path, "wb") as f:
                                f.write(await r.read())

                        score = compare_images(t["image"], img_path)

                        if score >= SIM_THRESHOLD:
                            await app.bot.send_photo(
                                chat_id=CHANNEL_ID,
                                photo=InputFile(img_path),
                                caption=(
                                    f"🚨 MATCH FOUND\n\n"
                                    f"🎯 {t['name']}\n"
                                    f"📊 {score:.2f}%\n"
                                    f"🔗 {href}"
                                )
                            )

                        await asyncio.sleep(random.uniform(5, 9))

            save(FILES["history"], HISTORY)

        except Exception as e:
            log.error(e)

        await asyncio.sleep(300)

# ================= BOT =================

async def start(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        f"🤖 CollectorBot v{VERSION}\n\n"
        "Команди:\n"
        "/add_target\n"
        "/list_targets\n"
        "/clear_targets"
    )

async def add_target(update: Update, ctx):
    STATE[str(update.effective_user.id)] = "await_photo"
    save(FILES["state"], STATE)
    await update.message.reply_text("📸 Надішли фото еталона")

async def photo_handler(update: Update, ctx):
    uid = str(update.effective_user.id)
    if STATE.get(uid) != "await_photo":
        return

    photo = update.message.photo[-1]
    path = f"{TARGET_IMG}/{photo.file_id}.jpg"
    await photo.get_file().download_to_drive(path)

    TARGETS[photo.file_id] = {
        "name": "Manual Target",
        "image": path,
        "created": datetime.utcnow().isoformat(),
    }
    save(FILES["targets"], TARGETS)

    STATE.pop(uid)
    save(FILES["state"], STATE)

    await update.message.reply_text("✅ Ціль додана")

async def list_targets(update: Update, ctx):
    if not TARGETS:
        return await update.message.reply_text("❌ Немає цілей")

    msg = "\n".join([f"- {t['name']}" for t in TARGETS.values()])
    await update.message.reply_text(msg)

async def clear_targets(update: Update, ctx):
    TARGETS.clear()
    save(FILES["targets"], TARGETS)
    await update.message.reply_text("🧹 Очищено")

# ================= MAIN =================

async def post_init(app):
    app.create_task(monitor_loop(app))

def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add_target", add_target))
    app.add_handler(CommandHandler("list_targets", list_targets))
    app.add_handler(CommandHandler("clear_targets", clear_targets))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    log.info("🚀 BOT v8 STARTED")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
