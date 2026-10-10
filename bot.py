import os
import asyncio
import logging
import urllib.request
import urllib.parse
import json
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from yandex_music import Client
import yt_dlp

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Получение токенов из переменных окружения Render
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден токен бота! Проверьте переменную окружения BOT_TOKEN в Render.")

YANDEX_TOKEN = os.environ.get("YANDEX_TOKEN", "")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Заглушка веб-сервера для удержания порта Render (Web Service)
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server started and listening on port {port}")

# Устойчивая функция поиска и скачивания через оптимизированный профиль yt-dlp
def download_track_clean(artist: str, title: str, output_path: str = "downloads"):
    os.makedirs(output_path, exist_ok=True)
    clean_query = f"{artist} - {title}".replace('"', '').replace("'", "")
    
    # Настройки yt-dlp с минимальным набором параметров, чтобы избежать конфликтов плееров
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': os.path.join(output_path, '%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        # Отключаем лишние проверки и используем обходные эмуляторы
        'ignoreerrors': True,
        'geo_bypass': True,
    }
    
    # Пробуем последовательно разные поисковые префиксы без сложных заголовков
    queries = [
        f"ytsearch1:{clean_query} audio",
        f"scsearch1:{clean_query}",
        f"ytsearch1:{clean_query}"
    ]
    
    for q in queries:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(q, download=True)
                if not info:
                    continue
                # Если это результативный плейлист/поиск, берем первый элемент
                if 'entries' in info:
                    entries = list(info['entries'])
                    if not entries:
                        continue
                    info = entries[0]
                
                filename = ydl.prepare_filename(info)
                mp3_filename = os.path.splitext(filename)[0] + ".mp3"
                if os.path.exists(mp3_filename):
                    return mp3_filename
        except Exception as e:
            logging.warning(f"Попытка поиска по запросу '{q}' не удалась: {e}")
            continue
            
    return None

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот для переноса треков из Яндекс Музыки в Telegram без цензуры.\n\n"
        "📌 **Как пользоваться:**\n"
        "Отправь команду `/download` для полного переноса или укажи количество треков с конца, например: `/download 1` (для быстрой проверки)."
    )

@dp.message(Command("download"))
async def cmd_download(message: Message):
    args = message.text.split()
    limit = None
    if len(args) > 1 and args[1].isdigit():
        limit = int(args[1])

    if not YANDEX_TOKEN:
        await message.answer("❌ Ошибка: не задан YANDEX_TOKEN в переменных окружения Render!")
        return

    await message.answer("🔄 Подключаюсь к Яндекс Музыке и получаю список треков из плейлиста «Мне нравится»...")

    try:
        client = Client(YANDEX_TOKEN).init()
        liked_tracks = client.users_likes_tracks().tracks
        total_count = len(liked_tracks)
        
        if limit:
            tracks_to_process = liked_tracks[-limit:]
            await message.answer(f"🧪 Режим теста: выбрано последних треков для скачивания: {len(tracks_to_process)} из {total_count}.")
        else:
            tracks_to_process = liked_tracks
            estimated_hours = round((total_count * 6) / 3600, 1)
            await message.answer(
                f"⚠️ **Внимание! В вашем плейлисте найдено треков: {total_count}.**\n\n"
                f"⏳ Полный перенос займет **примерно {estimated_hours} часа(ов)**.\n"
                f"📦 Процесс будет идти порциями (по 5 треков)."
            )

        BATCH_SIZE = 5
        batch_count = 0

        for i, short_track in enumerate(tracks_to_process, 1):
            track = short_track.fetch_track()
            title = track.title
            artists = ", ".join([artist.name for artist in track.artists])
            
            await message.answer(f"🔍 [{i}/{len(tracks_to_process)}] Ищу оригинал: <b>{artists} — {title}</b>", parse_mode="HTML")
            
            file_path = download_track_clean(artists, title)
            
            if file_path and os.path.exists(file_path):
                try:
                    audio_file = FSInputFile(file_path)
                    await message.answer_audio(
                        audio=audio_file,
                        title=title,
                        performer=artists,
                        caption=f"✅ Трек {i} успешно выгружен без цензуры"
                    )
                except Exception as e:
                    await message.answer(f"❌ Ошибка отправки в Telegram: {e}")
                finally:
                    if os.path.exists(file_path):
                        os.remove(file_path)
            else:
                await message.answer(f"⚠️ Не удалось найти или скачать трек: {artists} — {title}")

            batch_count += 1
            if batch_count >= BATCH_SIZE and i < len(tracks_to_process):
                batch_count = 0
                await message.answer("☕ Пауза (пакет из 5 треков обработан)...")
                await asyncio.sleep(5)

        await message.answer("🎉 Выгрузка выбранных треков завершена!")

    except Exception as e:
        await message.answer(f"❌ Произошла ошибка при работе с Яндекс Музыкой: {e}")

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
