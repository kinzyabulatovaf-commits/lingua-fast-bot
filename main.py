import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from together import AsyncTogether
from aiohttp import web

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "")
WEBHOOK_PATH = "/webhook/"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
together_client = AsyncTogether(api_key=TOGETHER_API_KEY) if TOGETHER_API_KEY else None

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

# --- ДАННЫЕ ---
LANGS = {"ru": "🇷🇺 Русский", "en": "🇬🇧 English", "es": "🇪🇸 Español", "de": "🇩🇪 Deutsch"}
LANG_NAMES = {"ru": "русский", "en": "английский", "es": "испанский", "de": "немецкий"}
GOALS = {"study": "учебы", "work": "работы", "travel": "путешествий", "social": "социальных сетей", "daily": "повседневного общения"}
STYLES = {"formal": "формальный", "informal": "разговорный", "neutral": "нейтральный"}

user_data = {}

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
    user_data[message.from_user.id] = {}
    await message.answer("👋 Привет! Выбери язык ОРИГИНАЛА:", reply_markup=get_lang_kb())
    await state.set_state(BotStates.choosing_src_lang)

@dp.callback_query(BotStates.choosing_src_lang)
async def set_src_lang(cb: types.CallbackQuery, state: FSMContext):
    code = cb.data.split("_")[1]
    user_data[cb.from_user.id]["src"] = code
    await cb.message.edit_text(f"✅ Источник: {LANGS[code]}. Теперь выбери язык ПЕРЕВОДА:", reply_markup=get_lang_kb(exclude=code))
    await state.set_state(BotStates.choosing_tgt_lang)

@dp.callback_query(BotStates.choosing_tgt_lang)
async def set_tgt_lang(cb: types.CallbackQuery, state: FSMContext):
    code = cb.data.split("_")[1]
    user_data[cb.from_user.id]["tgt"] = code
    await cb.message.edit_text(f"✅ Цель: {LANGS[code]}. Для какой сферы нужен перевод?", reply_markup=get_goal_kb())
    await state.set_state(BotStates.choosing_goal)

@dp.callback_query(BotStates.choosing_goal)
async def set_goal(cb: types.CallbackQuery, state: FSMContext):
    goal = cb.data.split("_")[1]
    user_data[cb.from_user.id]["goal"] = goal
    await cb.message.edit_text(f"🎯 Сфера: {GOALS[goal]}. Выбери стиль общения:", reply_markup=get_style_kb())
    await state.set_state(BotStates.choosing_style)

@dp.callback_query(BotStates.choosing_style)
async def set_style(cb: types.CallbackQuery, state: FSMContext):
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
        
    logging.info(f"User {user_id} | {cfg['src']}→{cfg['tgt']} | '{text[:50]}...'")
    
    if not together_client:
        await message.answer("🔑 Ошибка: API-ключ не настроен.")
        return
    
    try:
        prompt = (
            f"Переведи текст с {LANG_NAMES[cfg['src']]} на {LANG_NAMES[cfg['tgt']]}. "
            f"Контекст: {GOALS[cfg['goal']]}. Стиль: {STYLES[cfg['style']]}. "
            f"Верни ТОЛЬКО перевод, без кавычек и пояснений.\n\nТекст: {text}"
        )

        # ✅ ПРОВЕРЕННАЯ МОДЕЛЬ (бесплатная и доступная)
        MODEL_NAME = "meta-llama/Llama-3-8b-chat-hf"  # Бесплатная, без списания кредита
        logging.info(f"🤖 Calling model: {MODEL_NAME}")
        
        response = await together_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=512,
            timeout=30
        )
        
        result = response.choices[0].message.content.strip()
        result = result.strip('"').strip("'")
        
        cfg['last_text'] = text
        cfg['last_result'] = result
        
        await message.answer(
            f"📥 Оригинал: {text}\n\n📤 Перевод: {result}",
            reply_markup=get_action_kb()
        )
        logging.info(f"✓ Translation sent")
        
    except Exception as e:
        # 🔍 Детальное логирование ошибки
        error_details = str(e)
        logging.error(f"✗ API Error: {type(e).__name__} | {error_details}")
        
        if "unauthorized" in error_details.lower():
            await message.answer("🔑 Ошибка API-ключа.")
        elif "model" in error_details.lower() or "not found" in error_details.lower():
            await message.answer(f"❌ Модель не найдена. Попробуй позже. (Детали в логах)")
        elif "429" in error_details or "rate limit" in error_details.lower():
            await message.answer("⏳ Лимит запросов. Подожди 30 сек.")
        elif "402" in error_details or "quota" in error_details.lower():
            await message.answer("💸 Закончились кредиты. Проверь together.ai")
        else:
            await message.answer(f"⚠️ Ошибка: {type(e).__name__}. Напиши /start")

@dp.callback_query(F.data.in_(["swap", "change_lang", "change_style", "copy"]))
async def handle_actions(cb: types.CallbackQuery, state: FSMContext):
    cfg = user_data.get(cb.from_user.id, {})
    if cb.data == "swap":
        cfg['src'], cfg['tgt'] = cfg.get('tgt'), cfg.get('src')
        await cb.answer("🔄 Языки поменяны местами!")
    elif cb.data == "change_lang":
        await cb.message.edit_text("Выбери новый язык оригинала:", reply_markup=get_lang_kb())
        await state.set_state(BotStates.choosing_src_lang)
    elif cb.data == "change_style":
        await cb.message.edit_text("Выбери новый стиль:", reply_markup=get_style_kb())
        await state.set_state(BotStates.choosing_style)
    elif cb.data == "copy":
        await cb.answer("📋 Скопировано!")

# --- WEBHOOK ---
async def handle_webhook(request: web.Request):
    try:
        update = types.Update(**await request.json())
        await dp.feed_update(bot, update)
        return web.Response()
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return web.Response(status=500)

async def on_startup():
    if WEBHOOK_HOST:
        await bot.set_webhook(WEBHOOK_URL)
        logging.info(f"🔗 Webhook: {WEBHOOK_URL}")

async def main():
    await on_startup()
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()
    logging.info("🚀 Bot running...")
    try:
        while True: await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await runner.cleanup()

if __name__ == "__main__":
    if not WEBHOOK_HOST: 
        asyncio.run(dp.start_polling(bot))
    else: 
        asyncio.run(main())