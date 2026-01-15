import asyncio, os, json, logging, aiohttp, asyncpg, requests
from datetime import datetime, time
import pytz
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai

# ================= CONFIG =================
load_dotenv()
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID=int(os.getenv("CHANNEL_ID","0"))
ADMIN_ID=int(os.getenv("ADMIN_CHAT_ID","0"))
DB=os.getenv("DATABASE_URL")
GEMINI=os.getenv("GEMINI_API_KEY")
SERP=os.getenv("SERPAPI_KEY")

USD=40
TZ=pytz.timezone("Europe/Kyiv")
QUIET_FROM=time(23,0)
QUIET_TO=time(8,0)

EM="https://empress.cc"
COLS=["swiss-vintage-watches","dive-watches","military-watches"]
OLX="https://www.olx.ua/d/uk/list/q-годинник/"

gen=genai.Client(api_key=GEMINI)
bot=Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp=Dispatcher()
logging.basicConfig(level=logging.INFO)

# ================= DB =================
CREATE="""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS empress_watches(
 id SERIAL PRIMARY KEY,
 title TEXT,
 collection TEXT,
 price_usd REAL,
 image_url TEXT,
 embedding VECTOR(768)
);
CREATE TABLE IF NOT EXISTS olx_ads(
 id SERIAL PRIMARY KEY,
 title TEXT,
 price_uah REAL,
 url TEXT,
 image_url TEXT,
 embedding VECTOR(768)
);
CREATE TABLE IF NOT EXISTS portfolio(
 id SERIAL,
 olx_id INT,
 empress_id INT,
 buy_price REAL,
 market_price REAL,
 profit REAL,
 created_at TIMESTAMPTZ DEFAULT now()
);
"""

async def pool():
    p=await asyncpg.create_pool(DB)
    async with p.acquire() as c:
        await c.execute(CREATE)
    return p

# ================= UTILS =================
def quiet():
    t=datetime.now(TZ).time()
    return t>=QUIET_FROM or t<=QUIET_TO

async def fetch(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.text()

async def fetchb(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.read()

def embed(b):
    r=gen.embeddings.create(model="models/embedding-001",content=b)
    return r["embedding"]

def lens(img):
    if not SERP: return []
    r=requests.get("https://serpapi.com/search",params={
        "engine":"google_lens","url":img,"api_key":SERP
    }).json()
    return [v["link"] for v in r.get("visual_matches",[]) if "watch" in v.get("link","")]

def ai_risk(b):
    p="""Return JSON {"authentic":true,"liquidity":0-10}"""
    r=gen.models.generate_content("gemini-1.5-flash",
        [{"mime_type":"image/jpeg","data":b},p])
    return json.loads(r.text)

# ================= EMPRESS =================
async def load_empress(db):
    for col in COLS:
        html=await fetch(f"{EM}/collections/{col}")
        soup=BeautifulSoup(html,"html.parser")
        for a in soup.select("a.grid-product__link"):
            img=a.select_one("img")
            price=a.select_one(".grid-product__price")
            if not img or not price: continue
            img_url=img["src"]
            b=await fetchb(img_url)
            emb=embed(b)
            p=float(price.text.replace("$","").replace(",",""))
            async with db.acquire() as c:
                await c.execute("""
                INSERT INTO empress_watches(title,collection,price_usd,image_url,embedding)
                VALUES($1,$2,$3,$4,$5)
                """,a.text,col,p,img_url,emb)

# ================= OLX =================
async def scan_olx(db):
    html=await fetch(OLX)
    soup=BeautifulSoup(html,"html.parser")
    for a in soup.select("a.css-rc5s2u")[:30]:
        img=a.select_one("img")
        price=a.select_one(".css-8kqr5l")
        if not img or not price: continue
        url=a["href"]
        b=await fetchb(img["src"])
        emb=embed(b)
        p=float(price.text.replace("грн","").replace(" ",""))
        async with db.acquire() as c:
            await c.execute("""
            INSERT INTO olx_ads(title,price_uah,url,image_url,embedding)
            VALUES($1,$2,$3,$4,$5)
            """,a.text,p,url,img["src"],emb)

# ================= DEAL ENGINE =================
async def deals(db):
    async with db.acquire() as c:
        rows=await c.fetch("""
        SELECT o.id oid,o.title,o.price_uah,o.url,o.image_url,
               e.id eid,e.price_usd,
               1-(e.embedding <=> o.embedding) AS sim
        FROM olx_ads o JOIN empress_watches e
        ORDER BY e.embedding <=> o.embedding
        LIMIT 50
        """)
        for r in rows:
            olx_usd=r["price_uah"]/USD
            disc=1-(olx_usd/r["price_usd"])
            if r["sim"]<0.85 or disc<0.5: continue
            if not lens(r["image_url"]): continue
            img=await fetchb(r["image_url"])
            ai=ai_risk(img)
            if not ai["authentic"] or ai["liquidity"]<6: continue

            await c.execute("""
            INSERT INTO portfolio(olx_id,empress_id,buy_price,market_price,profit)
            VALUES($1,$2,$3,$4,$5)
            """,r["oid"],r["eid"],olx_usd,r["price_usd"],r["price_usd"]-olx_usd)

            if not quiet():
                await bot.send_message(CHANNEL_ID,
                    f"🔥 <b>SUPER DEAL</b>\n{r['title']}\nOLX ${olx_usd:.0f}\nMarket ${r['price_usd']}\nProfit {int(disc*100)}%\n{r['url']}")

# ================= TELEGRAM =================
@dp.message(CommandStart())
async def start(m:types.Message):
    await m.answer("Надішли фото годинника.")

# ================= MAIN =================
async def main():
    db=await pool()
    if ADMIN_ID: await bot.send_message(ADMIN_ID,"Watch-Expert v7.4 started")
    await load_empress(db)
    while True:
        await scan_olx(db)
        await deals(db)
        await asyncio.sleep(600)

asyncio.run(main())
