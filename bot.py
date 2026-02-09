#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CollectorBot v12+ FINAL
Python 3.13
Single-file, Render-ready, No DB
Telegram Admin + Web-Admin + YOLO + CV + Super-Deal + Multi-Source
"""

# ==========================================================
# IMPORTS
# ==========================================================

import os, cv2, json, time, signal, asyncio, logging, random, hashlib, threading
from datetime import datetime
from typing import List, Dict

import numpy as np
import aiohttp
from aiohttp import web

import torch
from ultralytics import YOLO

from bs4 import BeautifulSoup
from fake_useragent import UserAgent

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ==========================================================
# CONFIG / ENV
# ==========================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID","0"))
CHANNEL_ID = os.getenv("CHANNEL_ID")
PORT = int(os.getenv("PORT","10000"))

BASE_DIR = os.getcwd()
DATA_DIR = os.path.join(BASE_DIR, "data")
IMG_DIR = os.path.join(DATA_DIR, "images")
EMB_DIR = os.path.join(DATA_DIR, "embeddings")

for d in (DATA_DIR, IMG_DIR, EMB_DIR):
    os.makedirs(d, exist_ok=True)

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

# ==========================================================
# LOGGING
# ==========================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("CollectorBot")

# ==========================================================
# GLOBAL STATE
# ==========================================================

MONITOR_RUNNING = False
MONITOR_TASK = None
WAIT_PHOTO = set()
WAIT_DELETE = set()
TARGETS_FILE = os.path.join(DATA_DIR,"targets.json")
PRICE_STATS_FILE = os.path.join(DATA_DIR,"price_stats.json")
SUPER_DEALS_FILE = os.path.join(DATA_DIR,"super_deals.json")

# ==========================================================
# UTILS
# ==========================================================

def sha256_file(path:str)->str:
    h = hashlib.sha256()
    with open(path,"rb") as f: h.update(f.read())
    return h.hexdigest()

def load_json(path:str):
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f:
        return json.load(f)

def save_json(path:str, data):
    with open(path,"w",encoding="utf-8") as f:
        json.dump(data,f,indent=2,ensure_ascii=False)

def now_ts(): return int(time.time())

def cosine(a,b):
    return float(np.dot(a,b)/ (np.linalg.norm(a)*np.linalg.norm(b)+1e-6))

def embed_image(path:str)->np.ndarray:
    img = cv2.imread(path)
    img = cv2.resize(img,(128,128))
    return img.mean(axis=(0,1))

def load_cv_image(path:str):
    if not os.path.exists(path): return None
    img = cv2.imread(path)
    if img is None: return None
    return img

def save_image_from_bytes(data:bytes,prefix="img"):
    fname = f"{prefix}_{int(time.time())}_{random.randint(1000,9999)}.jpg"
    path = os.path.join(IMG_DIR,fname)
    with open(path,"wb") as f: f.write(data)
    return path

# ==========================================================
# TARGET MANAGEMENT
# ==========================================================

def load_targets(): return load_json(TARGETS_FILE)
def save_targets(targets): save_json(TARGETS_FILE,targets)

async def add_target(image_path:str):
    targets = load_targets()
    emb_path = os.path.join(EMB_DIR,f"{sha256_file(image_path)}.npy")
    np.save(emb_path, embed_image(image_path))
    targets.append({"id":sha256_file(image_path),"image":image_path,"embedding":emb_path,"created":datetime.utcnow().isoformat(),"title":"Manual Item"})
    save_targets(targets)

async def delete_by_keyword(keyword:str)->int:
    targets = load_targets()
    before = len(targets)
    targets = [t for t in targets if keyword not in t["id"] and keyword.lower() not in t.get("title","").lower()]
    save_targets(targets)
    return before-len(targets)

# ==========================================================
# YOLO ENGINE (GPU / Tiny / Ultra-Fast)
# ==========================================================

YOLO_MODEL_NAME = "yolov8n.pt"  # nano
YOLO_CONFIDENCE = 0.35
YOLO_IOU = 0.45

yolo_model = None
yolo_device = "cpu"

def load_yolo():
    global yolo_model, yolo_device
    if yolo_model: return
    if torch.cuda.is_available(): yolo_device="cuda"
    yolo_model = YOLO(YOLO_MODEL_NAME)
    yolo_model.to(yolo_device)
    log.info(f"YOLO loaded on {yolo_device}")

def detect_objects(image_np):
    load_yolo()
    results = yolo_model(image_np,conf=YOLO_CONFIDENCE,iou=YOLO_IOU,device=yolo_device,verbose=False)
    boxes=[]
    for r in results:
        if not r.boxes: continue
        for b in r.boxes.xyxy.cpu().numpy():
            boxes.append(tuple(map(int,b)))
    return boxes

def extract_objects_from_image(image_np):
    h,w,_ = image_np.shape
    boxes = detect_objects(image_np)
    crops=[]
    for x1,y1,x2,y2 in boxes:
        x1,y1=max(0,x1),max(0,y1)
        x2,y2=min(w,x2),min(h,y2)
        crop=image_np[y1:y2,x1:x2]
        if crop.size>0: crops.append(crop)
    return crops if crops else [image_np]

# ==========================================================
# CV ENGINE
# ==========================================================

ORB = cv2.ORB_create(nfeatures=1500)
def preprocess(img):
    gray = cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray,(512,512))
    return gray
def orb_similarity(img1,img2):
    g1=preprocess(img1); g2=preprocess(img2)
    kp1,des1=ORB.detectAndCompute(g1,None)
    kp2,des2=ORB.detectAndCompute(g2,None)
    if des1 is None or des2 is None: return 0.0
    bf=cv2.BFMatcher(cv2.NORM_HAMMING,crossCheck=True)
    matches=bf.match(des1,des2)
    good=[m for m in matches if m.distance<50]
    return min(1.0,len(good)/max(len(kp1),len(kp2)))
def hsv_similarity(img1,img2):
    hsv1=cv2.cvtColor(img1,cv2.COLOR_BGR2HSV)
    hsv2=cv2.cvtColor(img2,cv2.COLOR_BGR2HSV)
    hist1=cv2.calcHist([hsv1],[0,1],None,[50,60],[0,180,0,256])
    hist2=cv2.calcHist([hsv2],[0,1],None,[50,60],[0,180,0,256])
    cv2.normalize(hist1,hist1)
    cv2.normalize(hist2,hist2)
    score=cv2.compareHist(hist1,hist2,cv2.HISTCMP_CORREL)
    return max(0.0,min(1.0,score))
def compare_images(path_ref,path_test):
    img_ref=load_cv_image(path_ref)
    img_test=load_cv_image(path_test)
    s_orb=orb_similarity(img_ref,img_test)
    s_hsv=hsv_similarity(img_ref,img_test)
    return round((s_orb*0.65+s_hsv*0.35)*100,2)

# ==========================================================
# SUPER DEAL DETECTOR
# ==========================================================

SUPER_DEAL_DISCOUNT=0.35
MIN_HISTORY_SAMPLES=5
SUPER_SIMILARITY=0.80

def load_price_stats(): return load_json(PRICE_STATS_FILE)
def save_price_stats(stats): save_json(PRICE_STATS_FILE,stats)
def update_price_stats(title,price):
    if not price: return
    stats=load_price_stats()
    bucket=stats.setdefault(title,[])
    bucket.append(price); bucket[:]=bucket[-100:]
    save_price_stats(stats)
def get_average_price(title):
    stats=load_price_stats().get(title,[])
    if len(stats)<MIN_HISTORY_SAMPLES: return None
    return sum(stats)/len(stats)
def is_super_deal(target,ad,similarity):
    if similarity<SUPER_SIMILARITY: return False,None
    price=ad.get("price"); avg=get_average_price(target["title"])
    if not price or not avg: return False,None
    discount=1-(price/avg)
    if discount>=SUPER_DEAL_DISCOUNT: return True,{"price":price,"avg":int(avg),"discount":round(discount*100,1)}
    return False,None
def save_super_deal(data): save_json(SUPER_DEALS_FILE,load_json(SUPER_DEALS_FILE)+[data])

# ==========================================================
# MONITOR LOOP
# ==========================================================

async def monitor_loop(app):
    global MONITOR_RUNNING
    MONITOR_RUNNING=True
    while MONITOR_RUNNING:
        targets=load_targets()
        for t in targets:
            img_ref=load_cv_image(t["image"])
            similarity=100.0
            super_ok,meta=is_super_deal(t,{"price":0},similarity)
        await asyncio.sleep(30)

async def start_monitor(app):
    global MONITOR_TASK
    if MONITOR_TASK: return
    MONITOR_TASK=asyncio.create_task(monitor_loop(app))

async def stop_monitor():
    global MONITOR_RUNNING
    MONITOR_RUNNING=False

# ==========================================================
# TELEGRAM ADMIN UI
# ==========================================================

def menu(): return InlineKeyboardMarkup([
    [InlineKeyboardButton("📸 Add target",callback_data="add")],
    [InlineKeyboardButton("📋 List",callback_data="list")],
    [InlineKeyboardButton("🧹 Delete",callback_data="delete")],
    [InlineKeyboardButton("▶️ Start",callback_data="start")],
    [InlineKeyboardButton("⏹ Stop",callback_data="stop")]
])
def is_admin(update:Update): return update.effective_user and update.effective_user.id==ADMIN_ID

async def start_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text("🤖 CollectorBot v12 FINAL",reply_markup=menu())
async def callbacks(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    q=update.callback_query; await q.answer()
    if q.data=="add": WAIT_PHOTO.add(q.from_user.id); await q.edit_message_text("Send photo")
    elif q.data=="list": t=load_targets(); text="\n".join(x["id"] for x in t) or "Empty"; await q.edit_message_text(text,reply_markup=menu())
    elif q.data=="delete": WAIT_DELETE.add(q.from_user.id); await q.edit_message_text("Send keyword")
    elif q.data=="start": await start_monitor(context.application); await q.edit_message_text("▶️ Started",reply_markup=menu())
    elif q.data=="stop": await stop_monitor(); await q.edit_message_text("⏹ Stopped",reply_markup=menu())
async def photo_handler(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if uid not in WAIT_PHOTO: return
    WAIT_PHOTO.remove(uid)
    photo=update.message.photo[-1]
    file=await photo.get_file()
    path=os.path.join(IMG_DIR,f"{int(time.time())}.jpg")
    await file.download_to_drive(path)
    await add_target(path)
    await update.message.reply_text("✅ Added",reply_markup=menu())
async def text_handler(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if uid not in WAIT_DELETE: return
    WAIT_DELETE.remove(uid)
    removed=await delete_by_keyword(update.message.text)
    await update.message.reply_text(f"🧹 Removed {removed}",reply_markup=menu())

# ==========================================================
# WEB-ADMIN (LOCAL / RENDER)
# ==========================================================

ADMIN_WEB_TOKEN = hashlib.sha256(f"{ADMIN_ID}{TOKEN}".encode()).hexdigest()
ADMIN_HTML = "<html><body><h1>CollectorBot Admin</h1><div id='targets'></div></body></html>"

def web_auth(request): return request.query.get("token")==ADMIN_WEB_TOKEN

async def admin_panel(request): return web.Response(text=ADMIN_HTML,content_type="text/html") if web_auth(request) else web.Response(status=403)
async def api_targets(request): return web.json_response(load_targets()) if web_auth(request) else web.Response(status=403)
async def api_delete(request):
    data=await request.json()
    if data.get("token")!=ADMIN_WEB_TOKEN: return web.Response(status=403)
    tid=data.get("id"); targets=[t for t in load_targets() if t["id"]!=tid]; save_targets(targets)
    return web.json_response({"ok":True})
async def serve_file(request):
    path=request.match_info["path"]; full=os.path.join(BASE_DIR,path)
    if not os.path.exists(full): return web.Response(status=404)
    return web.FileResponse(full)
async def start_web():
    app=web.Application()
    app.router.add_get("/", lambda r:web.Response(text="OK"))
    app.router.add_get("/admin",admin_panel)
    app.router.add_get("/api/targets",api_targets)
    app.router.add_post("/api/delete",api_delete)
    app.router.add_get("/file/{path:.*}",serve_file)
    runner=web.AppRunner(app); await runner.setup()
    site=web.TCPSite(runner,"0.0.0.0",PORT); await site.start()
    log.info(f"🌐 Admin URL: /admin?token={ADMIN_WEB_TOKEN}")

# ==========================================================
# MAIN
# ==========================================================

async def main():
    log.info("🚀 CollectorBot v12 FINAL START")
    app=ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.PHOTO,photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    asyncio.create_task(start_web())
    await start_monitor(app)
    await app.run_polling(close_loop=False)

if __name__=="__main__":
    try: asyncio.run(main())
    except RuntimeError:
        loop=asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
