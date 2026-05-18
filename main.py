import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from deep_translator import GoogleTranslator
from aiohttp import web

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "")
WEBHOOK_PATH = "/webhook/"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# --- ДАННЫЕ ---
LANGS = {"ru": "🇷🇺 Русский", "en": "🇬🇧 English", "es": "🇪🇸 Español", "de": "🇩🇪 Deutsch"}
GOALS = {"study": "📚 Учеба", "work": "💼 Работа", "travel": "✈️ Путешествие", "social": "💬 Соцсети", "daily": " Повседневное"}
STYLES = {"formal": "👔 Формальный", "informal": "😎 Разговорный", "neutral": "⚖️ Нейтральный"}

user_data = {}

# --- МАШИНА СОСТОЯНИЙ ---
class BotStates(StatesGroup):
    choosing_lang = State()
    choosing_goal = State()
    choosing_style = State()
    waiting_for_text = State()

# --- КЛАВИАТУРЫ ---
def get_lang_kb():
    buttons = [[InlineKeyboardButton(text=v, callback_data=f"lang_{k}")] for k, v in LANGS.items()]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_goal_kb():
    buttons = [[InlineKeyboardButton(text=v, callback_data=f"goal_{k}")] for k, v in GOALS.items()]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_style_kb():
    buttons = [[InlineKeyboardButton(text=v, callback_data=f"style_{k}")] for k, v in STYLES.items()]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_action_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Перевести обратно", callback_data="swap"),
         InlineKeyboardButton(text=" Копировать", callback_data="copy")],
        [InlineKeyboardButton(text="🌐 Изменить язык", callback_data="change_lang"),
         InlineKeyboardButton(text="🎨 Изменить стиль", callback_data="change_style")]
    ])

# --- ЛОГИКА ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    user_data[message.from_user.id] = {"src": None, "tgt": None}
    await message.answer("Привет! Выбери язык ОРИГИНАЛА:", reply_markup=get_lang_kb())
    await state.set_state(BotStates.choosing_lang)

@dp.callback_query(BotStates.choosing_lang)
async def set_lang(cb: types.CallbackQuery, state: FSMContext):
    code = cb.data.split("_")[1]
    user_data[cb.from_user.id]["src"] = code
    await cb.message.edit_text(f"Выбран: {LANGS[code]}. Теперь язык ПЕРЕВОДА:", reply_markup=get_lang_kb())
    await state.set_state(BotStates.choosing_goal)

@dp.callback_query(BotStates.choosing_goal)
async def set_goal(cb: types.CallbackQuery, state: FSMContext):
    goal = cb.data.split("_")[1]
    user_data[cb.from_user.id]["goal"] = goal
    await cb.message.edit_text(f"Цель: {GOALS[goal]}. Выбери стиль:", reply_markup=get_style_kb())
    await state.set_state(BotStates.choosing_style)

@dp.callback_query(BotStates.choosing_style)
async def set_style(cb: types.CallbackQuery, state: FSMContext):
    style = cb.data.split("_")[1]
    user_data[cb.from_user.id]["style"] = style
    cfg = user_data[cb.from_user.id]
    
    await cb.message.edit_text(
        f"✅ Готово!\n📝 С {LANGS[cfg['src']]} на {LANGS[cfg['tgt']]}\n"
        f"🎯 Для: {GOALS[cfg['goal']]},  Стиль: {STYLES[cfg['style']]}\n\n"
        f"Отправляй текст для перевода!"
    )
    await state.set_state(BotStates.waiting_for_text)

@dp.message(BotStates.waiting_for_text)
async def translate_text(message: types.Message):
    cfg = user_data[message.from_user.id]
    text = message.text
    
    context_prefix = f"[Context: {cfg['goal']}, Tone: {cfg['style']}] "
    full_text = context_prefix + text
    
    try:
        translator = GoogleTranslator(source=cfg['src'], target=cfg['tgt'])
        result = translator.translate(full_text)
        
        if result.startswith("[Context:"):
            result = result.split("] ", 1)[-1]
            
        cfg['last_text'] = text
        cfg['last_result'] = result
        
        await message.answer(
            f"📥 Оригинал: {text}\n\n📤 Перевод: {result}",
            reply_markup=get_action_kb()
        )
    except Exception as e:
        await message.answer(f"⚠️ Ошибка перевода: {e}")

@dp.callback_query(F.data.in_(["swap", "change_lang", "change_style", "copy"]))
async def handle_actions(cb: types.CallbackQuery, state: FSMContext):
    cfg = user_data[cb.from_user.id]
    
    if cb.data == "swap":
        cfg['src'], cfg['tgt'] = cfg['tgt'], cfg['src']
        await cb.answer("🔄 Языки поменяны местами! Отправь текст.")
    elif cb.data == "change_lang":
        await cb.message.edit_text("Выбери новый язык оригинала:", reply_markup=get_lang_kb())
        await state.set_state(BotStates.choosing_lang)
    elif cb.data == "change_style":
        await cb.message.edit_text("Выбери новый стиль:", reply_markup=get_style_kb())
        await state.set_state(BotStates.choosing_style)
    elif cb.data == "copy":
        await cb.answer("📋 Перевод скопирован (нажми на сообщение и удерживай)")

# --- WEBHOOK (ИСПРАВЛЕНО ДЛЯ AIOGRAM 3) ---
async def handle_webhook(request: web.Request):
    try:
        update_data = await request.json()
        update = types.Update(**update_data)
        # ✅ В aiogram 3.x обновление передаётся в диспетчер
        await dp.feed_update(bot, update)
        return web.Response()
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return web.Response(status=500)

async def on_startup():
    if WEBHOOK_HOST:
        await bot.set_webhook(WEBHOOK_URL)
        logging.info(f"🔗 Webhook установлен: {WEBHOOK_URL}")

async def main():
    await on_startup()
    
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()
    
    logging.info("🚀 Bot is running on Render...")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await runner.cleanup()

if __name__ == "__main__":
    if not WEBHOOK_HOST:
        logging.info("Running locally with polling...")
        asyncio.run(dp.start_polling(bot))
    else:
        asyncio.run(main())