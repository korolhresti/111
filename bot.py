import os, asyncio, asyncpg, aiohttp, hashlib, io, re
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from google import genai
from PIL import Image

BOT_TOKEN=os.getenv("BOT_TOKEN")
GEMINI_API_KEY=os.getenv("GEMINI_API_KEY")
DATABASE_URL=os.getenv("DATABASE_URL")
CHANNEL_ID=os.getenv("CHANNEL_ID")
USD=38.0

bot=Bot(BOT_TOKEN,parse_mode="HTML")
dp=Dispatcher()
gemini=genai.Client(api_key=GEMINI_API_KEY)

# ---------- AI ----------
def embed(img):
    r=gemini.models.embed_content(model="models/embedding-001",content=img)
    return r["embedding"]

def ai_watch(img):
    prompt="""Return JSON:
    brand, model, authenticity(true/false), liquidity(1-10), est_price_usd"""
    im=Image.open(io.BytesIO(img))
    r=gemini.models.generate_content(model="gemini-2.0-flash",contents=[prompt,im])
    return eval(r.text)

# ---------- Utils ----------
async def fetch(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url,timeout=30) as r:
            return await r.read()

async def fetch_text(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url,timeout=30) as r:
            return await r.text()

# ---------- DB ----------
async def init_db(db):
    async with db.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS empress(
            id SERIAL PRIMARY KEY,
            name TEXT,
            price_usd FLOAT,
            img TEXT,
            embedding vector(768)
        );
        CREATE TABLE IF NOT EXISTS olx(
            id SERIAL PRIMARY KEY,
            title TEXT,
            price_uah FLOAT,
            url TEXT,
            img TEXT,
            embedding vector(768)
        );
        CREATE TABLE IF NOT EXISTS deals(
            id SERIAL PRIMARY KEY,
            olx_id INT,
            empress_id INT,
            profit FLOAT,
            ts TIMESTAMP DEFAULT now()
        );
        """)

# ---------- Empress ----------
async def scrape_empress(db):
    html=await fetch_text("https://empress.cc/collections/swiss-vintage-watches")
    soup=BeautifulSoup(html,"html.parser")
    items=soup.select(".grid-product__content")
    async with db.acquire() as c:
        for it in items:
            name=it.select_one(".grid-product__title").text.strip()
            price=float(re.sub(r"[^\d.]","",it.select_one(".money").text))
            img=it.select_one("img")["src"]
            img="https:"+img
            img_bytes=await fetch(img)
            emb=embed(img_bytes)
            await c.execute("INSERT INTO empress(name,price_usd,img,embedding) VALUES($1,$2,$3,$4)",
                name,price,img,emb)

# ---------- OLX ----------
async def scrape_olx(db,brand):
    q=f"https://www.olx.ua/uk/list/q-{brand}/"
    html=await fetch_text(q)
    soup=BeautifulSoup(html,"html.parser")
    ads=soup.select("div[data-cy=l-card]")
    async with db.acquire() as c:
        for ad in ads:
            title=ad.select_one("h6").text
            price=float(re.sub(r"[^\d]","",ad.select_one("p").text))
            url="https://www.olx.ua"+ad.select_one("a")["href"]
            img=ad.select_one("img")["src"]
            img_bytes=await fetch(img)
            emb=embed(img_bytes)
            await c.execute("INSERT INTO olx(title,price_uah,url,img,embedding) VALUES($1,$2,$3,$4,$5)",
                title,price,url,img,emb)

# ---------- DEAL ENGINE ----------
async def find_deals(db):
    async with db.acquire() as c:
        rows=await c.fetch("""
        SELECT *
        FROM(
          SELECT o.id oid,o.title,o.price_uah,o.url,o.img,
                 e.id eid,e.price_usd,
                 1-(e.embedding<=>o.embedding) sim
          FROM olx o CROSS JOIN empress e
        )s
        ORDER BY sim DESC LIMIT 30;
        """)
        for r in rows:
            olx_usd=r["price_uah"]/USD
            disc=1-(olx_usd/r["price_usd"])
            if r["sim"]>0.85 and disc>0.5:
                await bot.send_message(CHANNEL_ID,
                    f"🔥 <b>SUPER DEAL</b>\n{r['title']}\nOLX ${olx_usd:.0f}\nMarket ${r['price_usd']}\nProfit {int(disc*100)}%\n{r['url']}")

# ---------- TELEGRAM ----------
@dp.message(F.photo)
async def photo(m:Message):
    p=m.photo[-1]
    f=await bot.download(p)
    img=f.read()
    ai=ai_watch(img)
    await m.answer(f"Brand: {ai['brand']}\nModel: {ai['model']}\nAuthentic: {ai['authenticity']}\nLiquidity: {ai['liquidity']}\nEst: ${ai['est_price_usd']}")

@dp.message(F.text=="/start")
async def start(m:Message):
    await m.answer("Watch-Expert AI Pro v7.2 running")

# ---------- MAIN ----------
async def main():
    db=await asyncpg.create_pool(DATABASE_URL)
    await init_db(db)
    await scrape_empress(db)
    await scrape_olx(db,"rolex")
    asyncio.create_task(dp.start_polling(bot))
    while True:
        await find_deals(db)
        await asyncio.sleep(300)

if __name__=="__main__":
    asyncio.run(main())
