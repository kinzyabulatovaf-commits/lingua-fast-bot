import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramNotFoundError
from openai import AsyncOpenAI
from aiohttp import web

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "")
WEBHOOK_PATH = "/webhook/"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

openrouter_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1"
) if OPENROUTER_API_KEY else None

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

# --- ДАННЫЕ ---
LANGS = {"ru": "🇷🇺 Русский", "en": "🇬🇧 English", "es": "🇪🇸 Español", "de": "🇩🇪 Deutsch"}
LANG_NAMES = {"ru": "русский", "en": "английский", "es": "испанский", "de": "немецкий"}
GOALS = {"study": "учебы", "work": "работы", "travel": "путешествий", "social": "социальных сетей", "daily": "повседневного общения"}
STYLES = {"formal": "формальный", "informal": "разговорный", "neutral": "нейтральный"}

user_data = {}

# --- МАШИНА СОСТОЯНИЙ ---
class BotStates(StatesGroup):
    choosing_src_lang = State()
    choosing_tgt_lang = State()
    choosing_goal = State()
    choosing_style = State()
    waiting_for_text = State()

# --- КЛАВИАТУРЫ ---
def get_lang_kb(exclude=None):
    buttons = [[InlineKeyboardButton(text=v, callback_data=f"lang_{k}")] for k, v in LANGS.items() if k != exclude]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_goal_kb():
    buttons = [[InlineKeyboardButton(text=v, callback_data=f"goal_{k}")] for k, v in GOALS.items()]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_style_kb():
    # ✅ Кнопки стиля с явным префиксом
    buttons = [[InlineKeyboardButton(text=v, callback_data=f"style_{k}")] for k, v in STYLES.items()]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_action_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Копировать", callback_data="copy")],
        [InlineKeyboardButton(text="🌐 Изменить язык", callback_data="change_lang"),
         InlineKeyboardButton(text="🎨 Изменить стиль", callback_data="change_style")]
    ])

# --- ЛОГИКА ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    user_data[message.from_user.id] = {}
    await message.answer("👋 Привет! Выбери язык ОРИГИНАЛА:", reply_markup=get_lang_kb())
    await state.set_state(BotStates.choosing_src_lang)

from aiogram.exceptions import TelegramNotFoundError
from aiogram.fsm.context import FSMContext
# ... остальные импорты ...

@dp.callback_query(BotStates.choosing_src_lang)
async def process_lang_selection(cb: CallbackQuery, state: FSMContext):
    try:
        # 🔹 Твоя логика выбора языка (подставь свои переменные/методы)
        chosen_lang = cb.data  # или как ты достаёшь язык
        set_user_lang(cb.from_user.id, chosen_lang)
        await state.update_data(lang=chosen_lang)
        # await state.set_state(СледующийСтейт)  # если нужен переход

        # 🔹 Безопасная отправка
        if cb.message:
            await cb.message.edit_text(f"✅ Язык установлен: {chosen_lang.upper()}\nОтправь /news")
        else:
            await cb.answer("✅ Язык сохранён. Отправьте /news")
            
    except TelegramNotFoundError:
        # Сообщение удалено/изменено или сессия протухла после рестарта
        await cb.answer("⚠️ Сессия обновлена. Напишите /start заново.")
    except Exception as e:
        logging.error(f"FSM lang error: {e}")
        await cb.answer("⚠️ Ошибка выбора языка", show_alert=True)
    finally:
        await cb.answer()  # 🔥 Обязательно! Снимает "часики" загрузки в Telegram

@dp.callback_query(BotStates.choosing_tgt_lang)
async def set_tgt_lang(cb: types.CallbackQuery, state: FSMContext):
    try:
        code = cb.data.split("_")[1]
        user_data[cb.from_user.id]["tgt"] = code
        await cb.message.edit_text(f"✅ Цель: {LANGS[code]}. Для какой сферы нужен перевод?", reply_markup=get_goal_kb())
        await state.set_state(BotStates.choosing_goal)
    except (IndexError, KeyError) as e:
        logging.error(f"Error in set_tgt_lang: {e} | data: {cb.data}")
        await cb.answer("⚠️ Ошибка. Начни с /start", show_alert=True)

@dp.callback_query(BotStates.choosing_goal)
async def set_goal(cb: types.CallbackQuery, state: FSMContext):
    try:
        goal = cb.data.split("_")[1]
        user_data[cb.from_user.id]["goal"] = goal
        await cb.message.edit_text(f"🎯 Сфера: {GOALS[goal]}. Выбери стиль общения:", reply_markup=get_style_kb())
        await state.set_state(BotStates.choosing_style)
    except (IndexError, KeyError) as e:
        logging.error(f"Error in set_goal: {e} | data: {cb.data}")
        await cb.answer("⚠️ Ошибка. Начни с /start", show_alert=True)

@dp.callback_query(BotStates.choosing_style)
async def set_style(cb: types.CallbackQuery, state: FSMContext):
    try:
        style = cb.data.split("_")[1]
        user_data[cb.from_user.id]["style"] = style
        cfg = user_data[cb.from_user.id]
        await cb.message.edit_text(
            f"✅ Настройки сохранены!\n"
            f"📝 С {LANGS[cfg['src']]} на {LANGS[cfg['tgt']]}\n"
            f"🎯 Для: {GOALS[cfg['goal']]}, Стиль: {STYLES[cfg['style']]}\n\n"
            f"📩 Отправляй текст для перевода!"
        )
        await state.set_state(BotStates.waiting_for_text)
    except (IndexError, KeyError) as e:
        logging.error(f"Error in set_style: {e} | data: {cb.data}")
        await cb.answer("⚠️ Ошибка. Начни с /start", show_alert=True)

@dp.message(BotStates.waiting_for_text)
async def translate_text(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_data:
        await message.answer("⚠️ Пожалуйста, начни с /start")
        return
    cfg = user_data[user_id]
    if not all(k in cfg for k in ("src", "tgt", "goal", "style")):
        await message.answer("⚠️ Настройки неполные. Напиши /start")
        return
    text = message.text.strip()
    if not text:
        return
    if not openrouter_client:
        await message.answer("🔑 Ошибка: API-ключ не настроен.")
        return
    try:
        prompt = (
            f"Переведи текст с {LANG_NAMES[cfg['src']]} на {LANG_NAMES[cfg['tgt']]}. "
            f"Контекст: {GOALS[cfg['goal']]}. Стиль: {STYLES[cfg['style']]}. "
            f"Верни ТОЛЬКО перевод, без кавычек и пояснений.\n\nТекст: {text}"
        )
        MODEL_NAME = "baidu/cobuddy:free"
        response = await openrouter_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=512,
            extra_headers={"HTTP-Referer": "https://github.com", "X-Title": "LinguaFast Bot"}
        )
        result = response.choices[0].message.content.strip().strip('"').strip("'")
        cfg['last_text'] = text
        cfg['last_result'] = result
        await message.answer(f"📥 Оригинал: {text}\n\n📤 Перевод: {result}", reply_markup=get_action_kb())
    except Exception as e:
        error_msg = str(e)
        logging.error(f"❌ OpenRouter Error: {type(e).__name__} | {error_msg[:200]}")
        if "unauthorized" in error_msg.lower() or "api_key" in error_msg.lower():
            await message.answer("🔑 Ошибка ключа. Проверь OPENROUTER_API_KEY.")
        elif "notfound" in error_msg.lower() or "model" in error_msg.lower():
            await message.answer("❌ Модель недоступна. Попробуй через минуту.")
        elif "429" in error_msg or "rate limit" in error_msg.lower():
            await message.answer("⏳ Лимит запросов. Подожди 30 секунд.")
        else:
            await message.answer(f"⚠️ Ошибка: {type(e).__name__}. Напиши /start")

# ✅ УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК КНОПОК ДЕЙСТВИЙ
@dp.callback_query(F.data.in_(["change_lang", "change_style", "copy"]))
async def handle_actions(cb: types.CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    if user_id not in user_data:
        await cb.answer("⚠️ Начни с /start", show_alert=True)
        return
        
    cfg = user_data[user_id]
    
    if cb.data == "change_lang":
        # ✅ Сохраняем текущие настройки и переходим к выбору языка
        await cb.message.edit_text("Выбери новый язык оригинала:", reply_markup=get_lang_kb())
        await state.set_state(BotStates.choosing_src_lang)
        await cb.answer()
        
    elif cb.data == "change_style":
        # ✅ Переходим к выбору стиля
        await cb.message.edit_text("Выбери новый стиль:", reply_markup=get_style_kb())
        await state.set_state(BotStates.choosing_style)
        await cb.answer()
        
    elif cb.data == "copy":
        await cb.answer("📋 Скопировано! (Нажми и удерживай сообщение)")

# ✅ FALLBACK: ловим все необработанные callback, чтобы бот не падал
@dp.callback_query()
async def unhandled_callback(cb: types.CallbackQuery):
    logging.warning(f"⚠️ Unhandled callback: {cb.data} from user {cb.from_user.id}")
    # Не возвращаем ошибку, просто игнорируем неизвестные кнопки
    await cb.answer()

# --- WEBHOOK ---
async def handle_webhook(request: web.Request):
    try:
        update = types.Update(**await request.json())
        await dp.feed_update(bot, update)
        return web.Response()
    except Exception as e:
        logging.error(f"Webhook error: {type(e).__name__} | {str(e)[:200]}")
        return web.Response(status=500)

async def on_startup():
    if WEBHOOK_HOST:
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        logging.info(f"🔗 Webhook: {WEBHOOK_URL}")

async def main():
    await on_startup()
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()
    logging.info("🚀 Bot running on Render...")
    try:
        while True: await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await runner.cleanup()

if __name__ == "__main__":
    if not WEBHOOK_HOST: 
        asyncio.run(dp.start_polling(bot))
    else: 
        asyncio.run(main())