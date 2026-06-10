# main.py — News Aggregator + Telegram Bot (FastAPI + aiogram 3.x)
import os, sqlite3, time, logging, json, asyncio, httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import asynccontextmanager

# FastAPI
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# aiogram 3.x
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, LinkPreviewOptions
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.exceptions import TelegramNotFoundError

# RSS + Translation + External APIs
import feedparser
from deep_translator import GoogleTranslator
from dateutil import parser as date_parser
from dotenv import load_dotenv

# 🔧 Конфигурация
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT = int(os.getenv("PORT", 8000))
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env или настройках Render!")

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Пути
BASE_DIR = Path(__file__).parent.resolve()
STATIC_DIR = BASE_DIR / "static"
DB_PATH = BASE_DIR / "news.db"

# RSS-источники
RSS_FEEDS = [
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "https://feeds.reuters.com/reuters/technologyNews",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml"
]

# ==================== FSM States ====================
class BotStates(StatesGroup):
    choosing_src_lang = State()
    waiting_for_question = State()

# ==================== База данных ====================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'en')")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT UNIQUE, source TEXT, pub_date TEXT,
                orig_title TEXT, orig_summary TEXT,
                translations TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON news(pub_date DESC)")
        conn.commit()
    logging.info("✅ База данных готова")

def get_user_lang(chat_id: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT lang FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else "en"

def set_user_lang(chat_id: int, lang: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO users (chat_id, lang) VALUES (?, ?)", (chat_id, lang))
        conn.commit()

# ==================== NewsAPI + RSS ====================
def fetch_newsapi():
    if not NEWSAPI_KEY:
        logging.warning("⚠️ NEWSAPI_KEY не задан. Загрузка пропущена.")
        return 0
    logging.info("📰 Загрузка из NewsAPI (последние 7 дней)...")
    url = "https://newsapi.org/v2/everything"
    count = 0
    today = datetime.now()  # Локальное время

    for i in range(7):
        day = today - timedelta(days=i)
        date_str = day.strftime("%Y-%m-%d")
        params = {
            "q": "technology", "from": date_str, "to": date_str,
            "sortBy": "publishedAt", "pageSize": 50, "apiKey": NEWSAPI_KEY
        }
        try:
            res = httpx.get(url, params=params, timeout=10)
            data = res.json()
            if data.get("status") != "ok": continue
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                for art in data.get("articles", []):
                    link = art.get("url", "")
                    if not link or art.get("title") == "[Removed]": continue
                    cursor.execute("SELECT id FROM news WHERE link=?", (link,))
                    if cursor.fetchone(): continue
                    pub_dt = datetime.fromisoformat(art["publishedAt"].replace("Z", "+00:00"))
                    pub_date = pub_dt.strftime("%Y-%m-%d")
                    cursor.execute("""
                        INSERT OR IGNORE INTO news (link, source, pub_date, orig_title, orig_summary, translations)
                        VALUES (?, ?, ?, ?, ?, '{}')
                    """, (link, art["source"]["name"], pub_date, art.get("title",""), art.get("description","") or ""))
                    count += 1
                conn.commit()
            time.sleep(1.1)
        except Exception as e:
            logging.error(f"❌ Ошибка NewsAPI ({date_str}): {e}")
    logging.info(f"✅ NewsAPI: добавлено {count} статей.")
    return count

def translate_and_cache(lang: str, date: str):
    if lang == "en": return
    logging.info(f"🌐 Перевод на {lang} за {date}...")
    tr = GoogleTranslator(source='auto', target=lang)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, orig_title, orig_summary, translations FROM news WHERE pub_date=?", (date,)).fetchall()
        for r in rows:
            cache = json.loads(r["translations"] or "{}")
            if lang in cache: continue
            try:
                t_title = tr.translate(r["orig_title"])
                t_sum = tr.translate(r["orig_summary"]) if r["orig_summary"] else t_title
                cache[lang] = f"{t_title}|||{t_sum}"
                conn.execute("UPDATE news SET translations=? WHERE id=?", (json.dumps(cache), r["id"]))
                time.sleep(0.3)
            except Exception as e:
                logging.warning(f"⚠️ Пропуск перевода: {e}")
                cache[lang] = f"{r['orig_title']}|||{r['orig_summary']}"
                conn.execute("UPDATE news SET translations=? WHERE id=?", (json.dumps(cache), r["id"]))
        conn.commit()

# ==================== OpenRouter AI ====================
async def ask_openrouter(prompt: str, user_id: int = None) -> str:
    if not OPENROUTER_API_KEY:
        return "⚠️ AI-сервис не настроен."
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/kinzyabulatovaf-commits/tg-news-bot",
        "X-Title": "TG News Bot",
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
                if response.status_code == 404:
                    return "⚠️ AI-модель временно недоступна."
                elif response.status_code == 401:
                    return "⚠️ Ошибка авторизации в нейросети."
                elif response.status_code != 200:
                    return "⚠️ Ошибка подключения к нейросети."
                full_response = ""
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if data == "[DONE]": break
                        try:
                            chunk = json.loads(data)
                            content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if content: full_response += content
                        except: continue
                return full_response.strip() if full_response.strip() else "🤔 Нейросеть не сгенерировала ответ."
    except Exception as e:
        logging.error(f"❌ OpenRouter error: {e}")
        return "⚠️ Произошла ошибка при обработке запроса."

# ==================== FastAPI Endpoints ====================
@app.get("/")
def index(): return FileResponse(STATIC_DIR / "index.html")

@app.get("/refresh")
def refresh():
    fetch_newsapi()
    return {"status": "updated"}

@app.get("/api/news")
def get_news(lang: str = "ru", date: str = Query("latest"), offset: int = 0, limit: int = 10):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if date == "latest":
            rows = conn.execute("SELECT * FROM news WHERE lang=? ORDER BY pub_date DESC, id DESC LIMIT ? OFFSET ?", (lang, limit, offset)).fetchall()
        else:
            translate_and_cache(lang, date)
            rows = conn.execute("SELECT * FROM news WHERE pub_date=? AND lang=? ORDER BY id DESC LIMIT ? OFFSET ?", (date, lang, limit, offset)).fetchall()
        result = []
        for r in rows:
            cache = json.loads(r["translations"] or "{}")
            if lang in cache: t, s = cache[lang].split("|||", 1)
            else: t, s = (r["orig_title"], r["orig_summary"]) if lang == "en" else (r["orig_title"], r["orig_summary"])
            result.append({"title": t, "summary": s, "link": r["link"], "source": r["source"], "date": r["pub_date"]})
        return result

# ==================== Telegram Bot Handlers ====================
@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    try:
        lang = get_user_lang(msg.chat.id)
        kb = InlineKeyboardBuilder()
        for code, name in [("ru","🇷🇺 Русский"),("en","🇬🇧 English"),("es","🇪🇸 Español"),("de","🇩🇪 Deutsch"),("fr","🇫🇷 Français")]:
            kb.button(text=name, callback_data=f"lang:{code}")
        kb.adjust(2)
        await msg.answer(f"👋 Привет! Выбери язык новостей:\nТекущий: {lang.upper()}", reply_markup=kb.as_markup())
        await state.set_state(BotStates.choosing_src_lang)
    except TelegramNotFoundError:
        logging.warning("⚠️ Сообщение не найдено при /start")
    except Exception as e:
        logging.error(f"❌ Ошибка /start: {e}")

@router.callback_query(BotStates.choosing_src_lang)
async def process_lang_selection(cb: CallbackQuery, state: FSMContext):
    try:
        lang = cb.data.split(":")[1] if ":" in cb.data else cb.data
        set_user_lang(cb.from_user.id, lang)
        await state.clear()
        if cb.message:
            await cb.message.edit_text(f"✅ Язык установлен: {lang.upper()}\nОтправь /news для загрузки.")
        else:
            await cb.answer(f"✅ Язык: {lang.upper()}")
    except TelegramNotFoundError:
        await cb.answer("⚠️ Сессия обновлена. Напишите /start заново.")
    except Exception as e:
        logging.error(f"❌ Ошибка выбора языка: {e}")
        await cb.answer("⚠️ Ошибка", show_alert=True)
    finally:
        await cb.answer()

@router.message(Command("news"))
async def cmd_news(msg: Message):
    try:
        await bot.send_chat_action(msg.chat.id, "typing")
        lang = get_user_lang(msg.chat.id)
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM news WHERE pub_date=? AND lang=? ORDER BY id DESC LIMIT 10", (today, lang)).fetchall()
        if not rows:
            await msg.answer("📭 Новостей на сегодня пока нет. Попробуй другую дату или нажми /refresh.")
            return
        text = f"📰 Новости на {today} ({lang.upper()})\n\n"
        for i, r in enumerate(rows, 1):
            cache = json.loads(r["translations"] or "{}")
            t, s = (cache[lang].split("|||",1) if lang in cache else (r["orig_title"], r["orig_summary"]))
            text += f"{i}. <b>{t}</b>\n{s}\n🔗 <a href='{r['link']}'>Источник</a>\n\n"
        await msg.answer(text, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    except TelegramNotFoundError:
        logging.warning("⚠️ Сообщение не найдено при /news")
    except Exception as e:
        logging.error(f"❌ Ошибка /news: {e}")
        await msg.answer("⚠️ Ошибка загрузки новостей.")

@router.message()
async def handle_user_message(msg: Message, state: FSMContext):
    if not msg.text or msg.text.startswith("/"): return
    try:
        await bot.send_chat_action(msg.chat.id, "typing")
        prompt = f"Ты — помощник в телеграм-боте с новостями. Отвечай кратко, на языке пользователя. Вопрос: {msg.text}"
        ai_response = await ask_openrouter(prompt, msg.from_user.id)
        for chunk in [ai_response[i:i+4000] for i in range(0, len(ai_response), 4000)]:
            await msg.answer(chunk)
    except TelegramNotFoundError:
        logging.warning("⚠️ Сообщение не найдено при обработке текста")
    except Exception as e:
        logging.error(f"❌ Ошибка AI-обработки: {e}")
        await msg.answer("⚠️ Ошибка при обработке запроса.")

# ==================== Lifespan + App Setup ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if NEWSAPI_KEY:
        await asyncio.to_thread(fetch_newsapi)
    async def periodic():
        while True:
            await asyncio.sleep(1800)
            if NEWSAPI_KEY: await asyncio.to_thread(fetch_newsapi)
    task = asyncio.create_task(periodic())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ==================== Webhook / Polling Setup ====================
async def on_startup():
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook", allowed_updates=dp.resolve_used_update_types())
        logging.info(f"🌐 Webhook: {WEBHOOK_URL}/webhook")
    else:
        await bot.delete_webhook()
        logging.info("📡 Polling mode")

async def on_shutdown():
    if WEBHOOK_URL: await bot.delete_webhook()

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
        logging.info(f"✅ Server on port {PORT}")
        await asyncio.Event().wait()
    else:
        await dp.start_polling(bot)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)