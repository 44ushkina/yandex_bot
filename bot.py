import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from yandex_music import Client
import yt_dlp

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Получение токена из переменных окружения Render
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден токен бота! Проверьте переменную окружения BOT_TOKEN в Render.")

# Замените на ваш реальный токен Яндекс Музыки (можно тоже вынести в env, но для теста можно указать строкой)
YANDEX_TOKEN = os.environ.get("YANDEX_TOKEN", "ваш_токен_яндекс_музыки")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Настройки для yt-dlp (скачивание аудио в mp3)
def download_track_from_youtube(artist: str, title: str, output_path: str = "downloads"):
    os.makedirs(output_path, exist_ok=True)
    query = f"ytsearch1:{artist} - {title} official audio"
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': os.path.join(output_path, '%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            # Получаем реальный путь к скачанному mp3 файлу
            filename = ydl.prepare_filename(info)
            mp3_filename = os.path.splitext(filename)[0] + ".mp3"
            if os.path.exists(mp3_filename):
                return mp3_filename
    except Exception as e:
        logging.error(f"Ошибка при скачивании через yt-dlp ({artist} - {title}): {e}")
    return None


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот для переноса треков из Яндекс Музыки в Telegram без цензуры.\n\n"
        "📌 **Как пользоваться:**\n"
        "Отправь команду `/download` — по умолчанию я предложу настроить выгрузку.\n"
        "Или укажи количество треков с конца, например: `/download 3` (чтобы протестировать быстро)."
    )


@dp.message(Command("download"))
async def cmd_download(message: Message):
    # Парсим аргумент команды (например, "/download 5" -> 5)
    args = message.text.split()
    limit = None
    if len(args) > 1 and args[1].isdigit():
        limit = int(args[1])

    await message.answer("🔄 Подключаюсь к Яндекс Музыке и получаю список треков из плейлиста «Мне нравится»...")

    try:
        # Авторизация в Яндекс Музыке
        client = Client(YANDEX_TOKEN).init()
        liked_tracks = client.users_likes_tracks().tracks
        
        total_count = len(liked_tracks)
        
        # Если задан лимит (для теста), берем последние N треков
        if limit:
            tracks_to_process = liked_tracks[-limit:]
            await message.answer(f"🧪 Режим теста: выбрано последних треков для скачивания: {len(tracks_to_process)} из {total_count}.")
        else:
            tracks_to_process = liked_tracks
            # Расчет примерного времени (в среднем 5-7 секунд на трек)
            estimated_hours = round((total_count * 6) / 3600, 1)
            await message.answer(
                f"⚠️ **Внимание! В вашем плейлисте найдено треков: {total_count}.**\n\n"
                f"⏳ Поскольку скачивание и поиск оригиналов без цензуры через сторонние источники занимают время (около 5-7 секунд на трек), полный перенос займет **примерно {estimated_hours} часа(ов)**.\n\n"
                f"📦 Процесс будет идти порциями (по 5 треков), чтобы сервер не упал от нагрузки. Скоро начнем!"
            )

        # Настройки порционной отправки
        BATCH_SIZE = 5
        batch_count = 0

        for i, short_track in enumerate(tracks_to_process, 1):
            track = short_track.fetch_track()
            title = track.title
            artists = ", ".join([artist.name for artist in track.artists])
            
            await message.answer(f"🔍 [{i}/{len(tracks_to_process)}] Ищу оригинал: <b>{artists} — {title}</b>", parse_mode="HTML")
            
            # Скачиваем трек без цензуры
            file_path = download_track_from_youtube(artists, title)
            
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
                    # Удаляем файл с сервера, чтобы не забивать диск
                    if os.path.exists(file_path):
                        os.remove(file_path)
            else:
                await message.answer(f"⚠️ Не удалось найти или скачать трек: {artists} — {title}")

            # Пауза между порциями для разгрузки процессора и сети
            batch_count += 1
            if batch_count >= BATCH_SIZE and i < len(tracks_to_process):
                batch_count = 0
                await message.answer("☕ Сделал небольшую паузу (пакет из 5 треков обработан), продолжаю...")
                await asyncio.sleep(5)

        await message.answer("🎉 Выгрузка выбранных треков завершена!")

    except Exception as e:
        await message.answer(f"❌ Произошла ошибка при работе с Яндекс Музыкой: {e}")


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
