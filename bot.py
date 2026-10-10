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
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from yandex_music import Client
import yt_dlp

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Получение токенов из переменных окружения Render
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден токен бота! Проверьте переменную окружения BOT_TOKEN в Render.")

YANDEX_TOKEN = os.environ.get("YANDEX_TOKEN", "")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Путь для вебхука Telegram
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}" if RENDER_EXTERNAL_URL else ""

# Функция безопасного поиска трека
def search_youtube_via_api(query: str):
    instances = [
        "https://vid.priv.au",
        "https://invidious.privacyredirect.com",
        "https://inv.nadeko.net"
    ]
    encoded_query = urllib.parse.quote(query)
    
    for instance in instances:
        try:
            url = f"{instance}/api/v1/search?q={encoded_query}&type=video"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=3) as response:
                data = json.loads(response.read().decode())
                if data and isinstance(data, list) and len(data) > 0:
                    video_id = data[0].get('videoId')
                    if video_id:
                        return f"https://www.youtube.com/watch?v={video_id}"
        except Exception:
            continue
    return None

def download_track_clean(artist: str, title: str, output_path: str = "downloads"):
    os.makedirs(output_path, exist_ok=True)
    clean_query = f"{artist} - {title}".replace('"', '').replace("'", "")
    
    video_url = search_youtube_via_api(clean_query)
    if not video_url:
        video_url = f"ytsearch1:{clean_query}"

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
        'extractor_args': {'youtube': {'player_client': ['android', 'tv']}},
        'ignoreerrors': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            if not info:
                return None
            
            if 'entries' in info:
                entries = list(info['entries'])
                if not entries or entries[0] is None:
                    return None
                info = entries[0]
            
            filename = ydl.prepare_filename(info)
            mp3_filename = os.path.splitext(filename)[0] + ".mp3"
            if os.path.exists(mp3_filename):
                return mp3_filename
    except Exception as e:
        logging.warning(f"Ошибка скачивания '{clean_query}': {e}")
        
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

async def on_startup(bot: Bot):
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        logging.info(f"Webhook successfully set to {WEBHOOK_URL}")
    else:
        logging.warning("RENDER_EXTERNAL_URL is missing! Webhook could not be set automatically.")

async def main():
    app = web.Application()
    
    # Регистрация вебхук-обработчика aiogram в aiohttp
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    app.router.add_get("/", lambda request: web.Response(text="Bot is running via Webhooks!"))

    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    
    dp.startup.register(on_startup)
    
    await site.start()
    logging.info(f"Webhook server started on port {port}")
    
    # Бесконечное удержание процесса активным
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
