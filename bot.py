# ===========================
# CollectorBot Pro v10.0
# ===========================

import os, json, cv2, aiohttp, asyncio, random, logging, numpy as np
from datetime import datetime
from fake_useragent import UserAgent
from bs4 import BeautifulSoup
from aiohttp import web

from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ================= CONFIG =================

VERSION = "10.0"

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
    "sources": f"{DATA}/sources.json",
}

SIM_THRESHOLD = 80
BATCH = 5
DEDUPE_HOURS = 24

ua = UserAgent()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("v10")

# ================= STORAGE =================

def load(p, d): return d if not os.path.exists(p) else json.load(open(p,"r",encoding="utf-8"))
def save(p, d): json.dump(d, open(p,"w",encoding="utf-8"), ensure_ascii=False, indent=2)

TARGETS = load(FILES["targets"], {})
HISTORY = load(FILES["history"], {})
STATE = load(FILES["state"], {})
SOURCES = load(FILES["sources"], {"olx": True})

# ================= UTIL =================

def is_admin(uid): return uid == ADMIN_ID
def now(): return datetime.utcnow().isoformat()
def seen_recent(url):
    ts = HISTORY.get(url)
    if not ts: return False
    return (datetime.utcnow() - datetime.fromisoformat(ts)).total_seconds() < DEDUPE_HOURS*3600

REPLICA_KEYWORDS = ["репліка","копія","copy","aaa","aa+","1:1","replica"]

def detect_replica(text): return any(k in text.lower() for k in REPLICA_KEYWORDS)
def is_super_deal(found_price, ref_price):
    try:
        f = float(found_price.replace(" ","").replace("грн",""))
        r = float(ref_price.replace(" ","").replace("грн",""))
        return f < r*0.6
    except: return False

# ================= CV =================

def compare_images(p1,p2):
    i1,i2=cv2.imread(p1),cv2.imread(p2)
    if i1 is None or i2 is None: return 0
    orb=cv2.ORB_create(1500)
    k1,d1=orb.detectAndCompute(i1,None)
    k2,d2=orb.detectAndCompute(i2,None)
    if d1 is None or d2 is None: return 0
    bf=cv2.BFMatcher(cv2.NORM_HAMMING,crossCheck=True)
    m=bf.match(d1,d2)
    orb_score=len(m)/max(len(k1),1)*100
    h1=cv2.calcHist([cv2.cvtColor(i1,cv2.COLOR_BGR2HSV)],[0,1],None,[50,60],[0,180,0,256])
    h2=cv2.calcHist([cv2.cvtColor(i2,cv2.COLOR_BGR2HSV)],[0,1],None,[50,60],[0,180,0,256])
    cv2.normalize(h1,h1); cv2.normalize(h2,h2)
    color=cv2.compareHist(h1,h2,cv2.HISTCMP_CORREL)*100
    return orb_score*0.65+color*0.35

# ================= SCRAPERS =================

async def fetch(session,url):
    async with session.get(url,headers={"User-Agent":ua.random},timeout=20) as r:
        return await r.text()

async def scrape_olx(session,query):
    url=f"https://www.olx.ua/d/uk/list/q-{query.replace(' ','-')}/"
    html=await fetch(session,url)
    soup=BeautifulSoup(html,"lxml")
    ads=[]
    for a in soup.select("div[data-cy='l-card']"):
        if a.select_one("[data-testid='adCard-featured']"): continue
        link=a.find("a"); img=a.find("img"); price=a.select_one("p[data-testid='ad-price']")
        if not link or not img: continue
        ads.append({
            "url":"https://www.olx.ua"+link["href"],
            "img":img.get("src"),
            "price":price.text if price else ""
        })
    return ads

async def extract_gallery_images(session,ad_url):
    html=await fetch(session,ad_url)
    soup=BeautifulSoup(html,"lxml")
    imgs=[]
    for img in soup.select("img"):
        src=img.get("src","")
        if "olx" in src and src.endswith(".jpg"): imgs.append(src)
    return list(set(imgs))[:8]

async def sync_empress():
    async with aiohttp.ClientSession() as s:
        page=1; added=0
        while True:
            html=await fetch(s,f"https://empress.cc/page/{page}")
            soup=BeautifulSoup(html,"lxml")
            items=soup.select(".product")
            if not items: break
            for it in items:
                title=it.select_one(".woocommerce-loop-product__title")
                img=it.find("img")
                price=it.select_one(".price")
                if not title or not img: continue
                fid=img["src"].split("/")[-1]
                if fid in TARGETS: continue
                img_path=f"{TARGET_IMG}/{fid}"
                async with s.get(img["src"]) as r:
                    with open(img_path,"wb") as f: f.write(await r.read())
                TARGETS[fid]={"name":title.text.strip(),"image":img_path,"price":price.text if price else "","created":now()}
                added+=1
            page+=1
        save(FILES["targets"],TARGETS)
        return added

# ================= MONITOR =================

async def monitor(app):
    while True:
        try:
            if not TARGETS: await asyncio.sleep(60); continue
            sample=random.sample(list(TARGETS.values()),min(BATCH,len(TARGETS)))
            async with aiohttp.ClientSession() as session:
                for t in sample:
                    queries=[t["name"],t["name"]+" б.у",t["name"]+" used"]
                    for q in queries:
                        ads=await scrape_olx(session,q)
                        for ad in ads:
                            if seen_recent(ad["url"]): continue
                            HISTORY[ad["url"]]=now()
                            save(FILES["history"],HISTORY)
                            gallery=await extract_gallery_images(session,ad["url"])
                            best_score=0
                            for g in gallery:
                                img_path=f"{TEMP_IMG}/{random.randint(1,999999)}.jpg"
                                async with session.get(g) as r:
                                    with open(img_path,"wb") as f: f.write(await r.read())
                                s=compare_images(t["image"],img_path)
                                best_score=max(best_score,s)
                            if best_score>=SIM_THRESHOLD:
                                replica=detect_replica(ad["url"])
                                flags=["⚠️ Репліка" if replica else "✅ Оригінал"]
                                if "price" in t and is_super_deal(ad["price"],t["price"]):
                                    flags.append("🔥 SUPER DEAL")
                                await app.bot.send_photo(CHANNEL_ID,InputFile(img_path),
                                    caption=f"🚨 MATCH FOUND\n🎯 {t['name']}\n💵 {ad['price']}\n📊 {best_score:.2f}%\n{' | '.join(flags)}\n🔗 {ad['url']}")
                            await asyncio.sleep(random.uniform(4,8))
        except Exception as e: log.error(e)
        await asyncio.sleep(300)

# ================= HEALTH =================

async def health(_): return web.Response(text="OK")
async def start_health():
    app=web.Application()
    app.router.add_get("/",health)
    runner=web.AppRunner(app)
    await runner.setup()
    site=web.TCPSite(runner,"0.0.0.0",8080)
    await site.start()

# ================= BOT =================

async def start(update:Update,ctx):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text(f"🤖 CollectorBot v{VERSION}\n/sync_empress\n/add_target\n/list_targets\n/delete_target <id>\n/clear_targets")

async def sync_cmd(update:Update,ctx):
    if not is_admin(update.effective_user.id): return
    msg=await update.message.reply_text("⏳ Sync empress.cc …")
    added=await sync_empress()
    await msg.edit_text(f"✅ Added {added} items")

async def add_target(update:Update,ctx):
    STATE[str(update.effective_user.id)]="photo"
    save(FILES["state"],STATE)
    await update.message.reply_text("📸 Send photo")

async def photo_handler(update:Update,ctx):
    uid=str(update.effective_user.id)
    if STATE.get(uid)!="photo": return
    p=update.message.photo[-1]
    path=f"{TARGET_IMG}/{p.file_id}.jpg"
    await p.get_file().download_to_drive(path)
    TARGETS[p.file_id]={"name":"Manual Target","image":path,"created":now()}
    save(FILES["targets"],TARGETS)
    STATE.pop(uid); save(FILES["state"],STATE)
    await update.message.reply_text("✅ Added")

async def list_targets(update:Update,ctx):
    if not TARGETS: await update.message.reply_text("❌ Empty"); return
    await update.message.reply_text("\n".join([f"{k} | {v['name']}" for k,v in TARGETS.items()]))

async def delete_target(update:Update,ctx):
    if not ctx.args: return
    tid=ctx.args[0]
    if tid in TARGETS:
        os.remove(TARGETS[tid]["image"])
        TARGETS.pop(tid)
        save(FILES["targets"],TARGETS)
        await update.message.reply_text("🗑 Deleted")

async def clear_targets(update:Update,ctx):
    TARGETS.clear(); save(FILES["targets"],TARGETS)
    await update.message.reply_text("🧹 Cleared")

# ================= MAIN =================

async def post_init(app):
    asyncio.create_task(start_health())
    asyncio.create_task(monitor(app))

def main():
    app=ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("sync_empress",sync_cmd))
    app.add_handler(CommandHandler("add_target",add_target))
    app.add_handler(CommandHandler("list_targets",list_targets))
    app.add_handler(CommandHandler("delete_target",delete_target))
    app.add_handler(CommandHandler("clear_targets",clear_targets))
    app.add_handler(MessageHandler(filters.PHOTO,photo_handler))
    log.info("🚀 v10 started")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
    
