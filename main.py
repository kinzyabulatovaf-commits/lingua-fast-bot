import os
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from deep_translator import GoogleTranslator
from aiohttp import web

# --- НАСТРОЙКИ ---
# Токен мы будем брать из переменной окружения на Render, но для локалки можно оставить здесь
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_ОТ_BOTFATHER")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "") # Адрес Render (заполнится сам)
WEBHOOK_PATH = "/webhook/"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# --- ДАННЫЕ ---
LANGS = {"ru": "🇺 Русский", "en": "🇧 English", "es": "🇸 Español", "de": "🇪 Deutsch"}
GOALS = {"study": "Учеба", "work": "Работа", "travel": "Путешествие", "social": "Соцсети", "daily": "Повседневное"}
STYLES = {"formal": "Формальный", "informal": "Разговорный", "neutral": "Нейтральный"}

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
         InlineKeyboardButton(text="📋 Копировать", callback_data="copy")],
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
    await state.set_state(BotStates.choosing_goal) # Сразу переходим к целям

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
    
    # Сохраняем финальные настройки
    cfg = user_data[cb.from_user.id]
    await cb.message.edit_text(
        f"✅ Готово!\n📝 С {LANGS[cfg['src']]} на {LANGS[cfg['tgt']]}\n"
        f" Для: {GOALS[cfg['goal']]}, Стиль: {STYLES[cfg['style']]}\n\n"
        f"Отправляй текст для перевода!"
    )
    await state.set_state(BotStates.waiting_for_text)

@dp.message(BotStates.waiting_for_text)
async def translate_text(message: types.Message):
    cfg = user_data[message.from_user.id]
    text = message.text
    
    # Магия контекста: добавляем инструкции прямо в текст для переводчика
    # Google Translate увидит это и постарается адаптировать
    context_prefix = f"[Context: {cfg['goal']}, Tone: {cfg['style']}] "
    full_text = context_prefix + text
    
    try:
        translator = GoogleTranslator(source=cfg['src'], target=cfg['tgt'])
        result = translator.translate(full_text)
        
        # Убираем служебный префикс из результата, если он остался
        if result.startswith("[Context:"):
            result = result.split("] ", 1)[-1]
            
        # Сохраняем для кнопки "Назад"
        cfg['last_text'] = text
        cfg['last_result'] = result
        
        await message.answer(
            f" Оригинал: {text}\n\n"
            f"📤 Перевод: {result}",
            reply_markup=get_action_kb()
        )
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

# --- ОБРАБОТКА КНОПОК ДЕЙСТВИЙ ---
@dp.callback_query(F.data.in_(["swap", "change_lang", "change_style", "copy"]))
async def handle_actions(cb: types.CallbackQuery, state: FSMContext):
    cfg = user_data[cb.from_user.id]
    
    if cb.data == "swap":
        # Меняем языки местами
        cfg['src'], cfg['tgt'] = cfg['tgt'], cfg['src']
        await cb.answer("Языки поменяны местами! Отправь текст.")
        
    elif cb.data == "change_lang":
        await cb.message.edit_text("Выбери новый язык оригинала:", reply_markup=get_lang_kb())
        await state.set_state(BotStates.choosing_lang)
        
    elif cb.data == "change_style":
        await cb.message.edit_text("Выбери новый стиль:", reply_markup=get_style_kb())
        await state.set_state(BotStates.choosing_style)
        
    elif cb.data == "copy":
        await cb.answer("Перевод скопирован в буфер (нажми на сообщение и удерживай)")

# --- WEBHOOK SETUP (Для Render) ---
async def on_startup(bot):
    if WEBHOOK_HOST:
        await bot.set_webhook(WEBHOOK_URL)
        print(f"Webhook set to {WEBHOOK_URL}")

# Запуск через aiohttp (нужен для Render)
async def main_app():
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    return app

async def handle_webhook(request: web.Request):
    update = types.Update(**await request.json())
    await bot.process_update(update)
    return web.Response()

if __name__ == "__main__":
    # Если запускаем локально без WEBHOOK_HOST, используем polling
    if not WEBHOOK_HOST:
        dp.startup.register(on_startup)
        asyncio.run(dp.start_polling(bot))
    else:
        # Если есть WEBHOOK_HOST (Render), запускаем веб-сервер
        from aiohttp import web
        import asyncio
        
        async def run_web_app():
            runner = web.AppRunner(await main_app())
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
            await site.start()
            
            # Имитируем запуск бота
            await on_startup(bot)
            print("Bot is running on Render...")
            
            # Блокируем цикл, чтобы процесс не умер
            while True:
                await asyncio.sleep(3600)
                
        asyncio.run(run_web_app())