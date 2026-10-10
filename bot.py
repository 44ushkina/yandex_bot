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
    text = (
        "Привет! Я бот для поиска удаленных треков из Яндекс Музыки.\n\n"
        "Поскольку Яндекс заблокировал сторонние OAuth-приложения, мы используем надежный вход по куке сессии.\n\n"
        "👉 **Как получить куку Session_id:**\n"
        "1. Открой music.yandex.ru в браузере (ты должна быть авторизована).\n"
        "2. Нажми `F12` -> вкладка **Application** (Приложение) -> **Cookies** -> `https://yandex.ru`.\n"
        "3. Найди параметр **Session_id** и скопируй его значение.\n"
        "4. Отправь скопированное значение мне ответным сообщением!"
    )
    await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_token(message: Message):
    cookie_value = message.text.strip()
    
    if len(cookie_value) < 15 or " " in cookie_value:
        await message.answer("❌ Это не похоже на Session_id. Пожалуйста, скопируй точное значение куки из браузера.")
        return

    user_id = message.from_user.id
    with open(f"token_{user_id}.txt", "w") as f:
        f.write(cookie_value)
        
    search_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Найти удаленные треки", callback_data="start_search")]
    ])
    
    await message.answer(
        "✅ Кука успешно принята!\n\nНажми кнопку ниже, чтобы запустить проверку плейлиста.",
        reply_markup=search_keyboard
    )

@dp.callback_query(F.data == "start_search")
async def start_search(callback: CallbackQuery):
    user_id = callback.from_user.id
    token_file = f"token_{user_id}.txt"
    
    if not os.path.exists(token_file):
        await callback.message.answer("⚠️ Сначала отправь мне свою куку Session_id!")
        return
        
    with open(token_file, "r") as f:
        session_id = f.read().strip()
        
    await callback.message.edit_text("⏳ Подключаюсь к аккаунту Яндекс Музыки через сессию...")
    
    try:
        # Передаем Session_id напрямую в клиент
        client = Client(session_id)
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
        await callback.message.answer(f"❌ Ошибка авторизации: {str(e)}")

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    
    logging.info("🤖 Запуск веб-сервера и бота...")
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot, polling_timeout=30)
    )

if __name__ == "__main__":
    asyncio.run(main())
