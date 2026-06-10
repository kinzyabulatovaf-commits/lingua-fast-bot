# main.py — Ultra-safe version for Python 3.14 + Render
import os, sqlite3, time, logging, json, asyncio, httpx
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from aiohttp import web

from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, LinkPreviewOptions
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import feedparser
from deep_translator import GoogleTranslator
from dotenv import load_dotenv

# Загружаем .env
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Конфиг
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT = int(os.getenv("PORT", 8000))
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

BASE_DIR = Path(__file__).parent.resolve()
STATIC_DIR = BASE_DIR / "static"
DB_PATH = BASE_DIR / "news.db"

RSS_FEEDS = [
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "https://feeds.reuters.com/reuters/technologyNews"
]

class BotStates(StatesGroup):
    choosing_src_lang = State()

# База данных
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'en')")
    c.execute("CREATE TABLE IF NOT EXISTS news (id INTEGER PRIMARY KEY AUTOINCREMENT, link TEXT UNIQUE, source TEXT, pub_date TEXT, orig_title TEXT, orig_summary TEXT, translations TEXT)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_date ON news(pub_date DESC)")
    conn.commit()
    conn.close()
    logging.info("DB initialized")

def get_user_lang(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT lang FROM users WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "en"

def set_user_lang(chat_id, lang):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (chat_id, lang) VALUES (?, ?)", (chat_id, lang))
    conn.commit()
    conn.close()

# NewsAPI
def fetch_newsapi():
    if not NEWSAPI_KEY:
        return 0
    logging.info("Fetching NewsAPI...")
    url = "https://newsapi.org/v2/everything"
    count = 0
    today = datetime.now()
    for i in range(7):
        day = today - timedelta(days=i)
        date_str = day.strftime("%Y-%m-%d")
        params = {"q": "technology", "from": date_str, "to": date_str, "sortBy": "publishedAt", "pageSize": 50, "apiKey": NEWSAPI_KEY}
        try:
            res = httpx.get(url, params=params, timeout=10)
            data = res.json()
            if data.get("status") != "ok":
                continue
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            for art in data.get("articles", []):
                link = art.get("url", "")
                if not link or art.get("title") == "[Removed]":
                    continue
                c.execute("SELECT id FROM news WHERE link=?", (link,))
                if c.fetchone():
                    continue
                pub_dt = datetime.fromisoformat(art["publishedAt"].replace("Z", "+00:00"))
                pub_date = pub_dt.strftime("%Y-%m-%d")
                c.execute("INSERT OR IGNORE INTO news (link, source, pub_date, orig_title, orig_summary, translations) VALUES (?, ?, ?, ?, ?, '{}')",
                    (link, art["source"]["name"], pub_date, art.get("title",""), art.get("description","") or ""))
                count += 1
            conn.commit()
            conn.close()
            time.sleep(1.1)
        except Exception as e:
            logging.error("NewsAPI error: %s", e)
    logging.info("Added %d articles", count)
    return count

# Перевод
def translate_and_cache(lang, date):
    if lang == "en":
        return
    tr = GoogleTranslator(source='auto', target=lang)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, orig_title, orig_summary, translations FROM news WHERE pub_date=?", (date,))
    rows = c.fetchall()
    for r in rows:
        nid, orig_title, orig_summary, translations = r
        cache = json.loads(translations or "{}")
        if lang in cache:
            continue
        try:
            t_title = tr.translate(orig_title)
            t_sum = tr.translate(orig_summary) if orig_summary else t_title
            cache[lang] = t_title + "|||" + t_sum
            c.execute("UPDATE news SET translations=? WHERE id=?", (json.dumps(cache), nid))
            time.sleep(0.3)
        except Exception as e:
            logging.warning("Translate error: %s", e)
            cache[lang] = orig_title + "|||" + (orig_summary or "")
            c.execute("UPDATE news SET translations=? WHERE id=?", (json.dumps(cache), nid))
    conn.commit()
    conn.close()

# OpenRouter AI
async def ask_openrouter(prompt):
    if not OPENROUTER_API_KEY:
        return "AI not configured"
    headers = {
        "Authorization": "Bearer " + OPENROUTER_API_KEY,
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/kinzyabulatovaf-commits/lingua_fast_bot",
        "X-Title": "TG Bot",
    }
    payload = {
        "model": "nvidia/nemotron-3.5-content-safety:free",
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 500,
    }
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            async with client.stream("POST", "https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload) as response:
                if response.status_code != 200:
                    return "AI service error"
                full_response = ""
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if content:
                                full_response += content
                        except:
                            continue
                return full_response.strip() if full_response.strip() else "No response"
    except Exception as e:
        logging.error("OpenRouter error: %s", e)
        return "Error processing request"

# Lifespan
@asynccontextmanager
async def lifespan(app):
    init_db()
    if NEWSAPI_KEY:
        await asyncio.to_thread(fetch_newsapi)
    async def periodic():
        while True:
            await asyncio.sleep(1800)
            if NEWSAPI_KEY:
                await asyncio.to_thread(fetch_newsapi)
    task = asyncio.create_task(periodic())
    yield
    task.cancel()

# FastAPI app
app = FastAPI(lifespan=lifespan)

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/refresh")
def refresh():
    fetch_newsapi()
    return {"status": "updated"}

@app.get("/api/news")
def get_news(lang="ru", date=Query("latest"), offset=0, limit=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if date == "latest":
        c.execute("SELECT * FROM news WHERE lang=? ORDER BY pub_date DESC, id DESC LIMIT ? OFFSET ?", (lang, limit, offset))
    else:
        translate_and_cache(lang, date)
        c.execute("SELECT * FROM news WHERE pub_date=? AND lang=? ORDER BY id DESC LIMIT ? OFFSET ?", (date, lang, limit, offset))
    rows = c.fetchall()
    conn.close()
    result = []
    for r in rows:
        translations = json.loads(r[6] or "{}")
        if lang in translations:
            parts = translations[lang].split("|||", 1)
            t, s = parts[0], parts[1] if len(parts) > 1 else ""
        else:
            t, s = r[4], r[5]
        result.append({"title": t, "summary": s, "link": r[1], "source": r[2], "date": r[3]})
    return result

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Bot handlers
@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    try:
        lang = get_user_lang(msg.chat.id)
        kb = InlineKeyboardBuilder()
        for code, name in [("ru","🇷🇺 Русский"),("en","🇬🇧 English"),("es","🇪🇸 Español"),("de","🇩🇪 Deutsch"),("fr","🇫🇷 Français")]:
            kb.button(text=name, callback_data="lang:" + code)
        kb.adjust(2)
        await msg.answer("👋 Привет! Выбери язык:\nТекущий: " + lang.upper(), reply_markup=kb.as_markup())
        await state.set_state(BotStates.choosing_src_lang)
    except Exception as e:
        logging.error("Start error: %s", e)

@router.callback_query(BotStates.choosing_src_lang)
async def process_lang(cb: CallbackQuery, state: FSMContext):
    try:
        data = cb.data
        lang = data.split(":")[1] if ":" in data else data
        set_user_lang(cb.from_user.id, lang)
        await state.clear()
        if cb.message:
            await cb.message.edit_text("✅ Язык: " + lang.upper() + "\nОтправь /news")
        else:
            await cb.answer("✅ " + lang.upper())
    except Exception as e:
        logging.error("Lang error: %s", e)
        await cb.answer("Error", show_alert=True)
    finally:
        await cb.answer()

@router.message(Command("news"))
async def cmd_news(msg: Message):
    try:
        await bot.send_chat_action(msg.chat.id, "typing")
        lang = get_user_lang(msg.chat.id)
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM news WHERE pub_date=? AND lang=? ORDER BY id DESC LIMIT 10", (today, lang))
        rows = c.fetchall()
        conn.close()
        if not rows:
            await msg.answer("📭 Новостей нет. Попробуй /refresh")
            return
        text = "📰 Новости на " + today + " (" + lang.upper() + ")\n\n"
        for i, r in enumerate(rows, 1):
            translations = json.loads(r[6] or "{}")
            if lang in translations:
                parts = translations[lang].split("|||", 1)
                t, s = parts[0], parts[1] if len(parts) > 1 else ""
            else:
                t, s = r[4], r[5]
            text += str(i) + ". <b>" + t + "</b>\n" + s + "\n🔗 <a href='" + r[1] + "'>Источник</a>\n\n"
        await msg.answer(text, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    except Exception as e:
        logging.error("News error: %s", e)
        await msg.answer("⚠️ Error loading news")

@router.message()
async def handle_text(msg: Message):
    if not msg.text or msg.text.startswith("/"):
        return
    try:
        await bot.send_chat_action(msg.chat.id, "typing")
        prompt = "Ты — помощник в боте с новостями. Отвечай кратко. Вопрос: " + msg.text
        ai_response = await ask_openrouter(prompt)
        # Разбиваем на части по 4000 символов
        for i in range(0, len(ai_response), 4000):
            chunk = ai_response[i:i+4000]
            await msg.answer(chunk)
    except Exception as e:
        logging.error("AI error: %s", e)
        await msg.answer("⚠️ Error")

# Startup/shutdown
async def on_startup():
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL + "/webhook", allowed_updates=dp.resolve_used_update_types())
        logging.info("Webhook: %s/webhook", WEBHOOK_URL)
    else:
        await bot.delete_webhook()
        logging.info("Polling mode")

async def on_shutdown():
    if WEBHOOK_URL:
        await bot.delete_webhook()

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    if WEBHOOK_URL:
        app_obj = web.Application()
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app_obj, path="/webhook")
        setup_application(app_obj, dp, bot=bot)
        runner = web.AppRunner(app_obj)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logging.info("Server on port %d", PORT)
        await asyncio.Event().wait()
    else:
        await dp.start_polling(bot)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)