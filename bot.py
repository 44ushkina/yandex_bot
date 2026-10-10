import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import CommandStart
from yandex_music import Client

# Твой токен бота
BOT_TOKEN = "8685013659:AAErk618IFRBUROGOzP8jKJzJs9A2rp2AH8"

# Настраиваем логирование, чтобы видеть всё, что происходит
logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_start_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Найти удаленные треки", callback_data="start_search")]
    ])

@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "Привет! Я бот для быстрого поиска удаленных треков из Яндекс Музыки.\n\n"
        "Чтобы я мог проверить твои лайкнутые треки, мне нужен **OAuth-токен** от твоего аккаунта Яндекс Музыки.\n\n"
        "🔗 **Как его получить:**\n"
        "1. Перейди по этой ссылке: https://oauth.yandex.ru/authorize?response_type=token&client_id=23cabbbdc6cd491ba4b8ec639eb437c5\n"
        "2. Нажми «Разрешить».\n"
        "3. Из адресной строки или экрана скопируй длинный токен.\n"
        "4. Просто отправь этот токен мне ответным сообщением!"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_token(message: Message):
    token = message.text.strip()
    
    if len(token) < 10 or " " in token:
        await message.answer("❌ Это не похоже на токен. Пожалуйста, отправь токен, полученный по ссылке из команды /start.")
        return

    user_id = message.from_user.id
    with open(f"token_{user_id}.txt", "w") as f:
        f.write(token)
        
    await message.answer(
        "✅ Токен успешно принят!\n\nНажми кнопку ниже, чтобы я проверил твой плейлист «Мне нравится».",
        reply_markup=get_start_keyboard()
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
        client = Client(token).init()
        
        await callback.message.edit_text("📥 Загружаю плейлист «Мне нравится»...")
        
        likes = client.users_likes_tracks()
        track_ids = [str(track.id) for track in likes.tracks] if likes and hasattr(likes, 'tracks') else []
        
        if not track_ids:
            try:
                liked_tracks = client.users_likes_tracks(client.user.uid)
                track_ids = [str(t.id) for t in liked_tracks.tracks]
            except Exception:
                pass

        if not track_ids:
            await callback.message.edit_text("❌ Не удалось найти треки в «Мне нравится» или токен недействителен.")
            return

        await callback.message.edit_text(f"🔍 Найдено {len(track_ids)} треков в лайках. Сверяю статус с серверами...")

        deleted_tracks = []
        batch_size = 100
        
        for i in range(0, len(track_ids), batch_size):
            batch = track_ids[i:i+batch_size]
            tracks_info = client.tracks(batch)
            
            for item in tracks_info:
                if not item.available:
                    artists_names = ", ".join([a.name for a in item.artists]) if item.artists else "Неизвестный исполнитель"
                    title = item.title
                    if item.version:
                        title += f" ({item.version})"
                    deleted_tracks.append(f"{artists_names} — {title}")

        filename = f"deleted_{user_id}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write("=== ТВОИ УДАЛЕННЫЕ ТРЕКИ ИЗ ЯНДЕКС МУЗЫКИ ===\n\n")
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
        await callback.message.answer(f"❌ Произошла ошибка: {str(e)}\n\nВозможно, токен недействителен или устарел.")

async def main():
    # Очищаем зависшие вебхуки и принудительно запускаем polling с короткими таймаутами
    await bot.delete_webhook(drop_pending_updates=True)
    print("🤖 Бот запущен и ожидает сообщения в Telegram...")
    await dp.start_polling(bot, polling_timeout=30)

if __name__ == "__main__":
    asyncio.run(main())