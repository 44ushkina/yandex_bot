import asyncio
import logging
import os
import traceback
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import CommandStart
from yandex_music import Client

BOT_TOKEN = "8685013659:AAErk618IFRBUROGOzP8jKJzJs9A2rp2AH8"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- МИНИ-СЕРВЕР ДЛЯ RENDER ---
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/', handle_ping)])
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server successfully started on port {port}")
# ------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    # Прямая официальная ссылка на получение токена через Яндекс Паспорт (без кастомных сломанных client_id)
    auth_url = "https://oauth.yandex.ru/authorize?response_type=token&client_id=23cabbbdc6cd491ba4b8ec639eb437c5"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Получить OAuth-токен", url=auth_url)],
        [InlineKeyboardButton(text="ℹ️ Как получить токен вручную?", callback_data="help_token")]
    ])
    
    text = (
        "Привет! Я бот для поиска удаленных треков из Яндекс Музыки.\n\n"
        "Чтобы я мог проверить твои лайки, мне нужен твой **OAuth-токен**.\n\n"
        "👉 **Как получить:**\n"
        "1. Нажми кнопку ниже.\n"
        "2. Скопируй выданный токен (начинается с `y0_`).\n"
        "3. Отправь его мне сюда ответным сообщением!"
    )
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(F.data == "help_token")
async def help_token(callback: CallbackQuery):
    await callback.message.answer(
        "💡 **Инструкция по получению токена:**\n\n"
        "Если кнопка авторизации вызывает ошибку:\n"
        "1. Открой в браузере сайт Яндекс Музыки и убедись, что ты вошел в аккаунт.\n"
        "2. Перейди по ссылке: `https://oauth.yandex.ru/authorize?response_type=token&client_id=23cabbbdc6cd491ba4b8ec639eb437c5`\n"
        "3. Разреши доступ и скопируй длинную строчку символов из адресной строки или страницы.\n"
        "4. Вернись в чат со мной и отправь этот токен.",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_token(message: Message):
    token = message.text.strip()
    
    if len(token) < 10 or " " in token:
        await message.answer("❌ Это не похоже на токен. Пожалуйста, отправь токен, начинающийся с `y0_`.")
        return

    user_id = message.from_user.id
    with open(f"token_{user_id}.txt", "w") as f:
        f.write(token)
        
    search_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Найти удаленные треки", callback_data="start_search")]
    ])
    
    await message.answer(
        "✅ Токен успешно принят!\n\nНажми кнопку ниже, чтобы запустить проверку плейлиста.",
        reply_markup=search_keyboard
    )

@dp.callback_query(F.data == "start_search")
async def start_search(callback: CallbackQuery):
    user_id = callback.from_user.id
    token_file = f"token_{user_id}.txt"
    
    if not os.path.exists(token_file):
        await callback.message.answer("⚠️ Сначала отправь мне свой OAuth-токен!")
        return
        
    with open(token_file, "r") as f:
        token = f.read().strip()
        
    await callback.message.edit_text("⏳ Подключаюсь к аккаунту Яндекс Музыки...")
    
    try:
        client = Client(token)
        client.init()
        
        await callback.message.edit_text("📥 Загружаю плейлист «Мне нравится»...")
        
        likes = client.users_likes_tracks()
        track_ids = [str(track.id) for track in likes.tracks] if likes and hasattr(likes, 'tracks') else []
        
        if not track_ids and client.me:
            liked_tracks = client.users_likes_tracks(client.me.account.uid)
            track_ids = [str(t.id) for t in liked_tracks.tracks] if liked_tracks and hasattr(liked_tracks, 'tracks') else []

        if not track_ids:
            await callback.message.edit_text("❌ В твоем плейлисте «Мне нравится» не найдено треков.")
            return

        await callback.message.edit_text(f"🔍 Проверяю статус {len(track_ids)} треков...")

        deleted_tracks = []
        batch_size = 100
        
        for i in range(0, len(track_ids), batch_size):
            batch = track_ids[i:i+batch_size]
            tracks_info = client.tracks(batch)
            
            for item in tracks_info:
                if item and not item.available:
                    artists_names = ", ".join([a.name for a in item.artists]) if item.artists else "Неизвестный исполнитель"
                    title = item.title
                    if item.version:
                        title += f" ({item.version})"
                    deleted_tracks.append(f"{artists_names} — {title}")

        filename = f"deleted_{user_id}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write("=== УДАЛЕННЫЕ ТРЕКИ ИЗ ЯНДЕКС МУЗЫКИ ===\n\n")
            if deleted_tracks:
                for t in deleted_tracks:
                    f.write(f"{t}\n")
            else:
                f.write("Удаленных треков не найдено!\n")

        file_to_send = FSInputFile(filename)
        await callback.message.answer_document(
            file_to_send, 
            caption=f"🎉 Готово! Найдено удаленных треков: {len(deleted_tracks)}"
        )
        
        os.remove(filename)
        
    except Exception as e:
        err_msg = traceback.format_exc()
        logging.error(f"DETAILED ERROR:\n{err_msg}")
        await callback.message.answer("❌ Ошибка авторизации. Убедись, что токен скопирован правильно и полностью.")

async def main():
    # Сбрасываем вебхук и старые соединения, чтобы избежать конфликтов
    await bot.delete_webhook(drop_pending_updates=True)
    
    logging.info("🤖 Запуск веб-сервера и бота...")
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot, polling_timeout=30)
    )

if __name__ == "__main__":
    asyncio.run(main())
