import os
import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from yandex_music import Client
import yt_dlp

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в переменных окружения Render.")
YANDEX_TOKEN = os.environ.get("YANDEX_TOKEN", "")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ---------- веб-сервер, чтобы Render видел открытый порт ----------
async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info(f"Web server on port {port}")


# ---------- Яндекс Музыка (синхронные функции, вызываются через to_thread) ----------
def yandex_get_liked(token):
    client = Client(token).init()
    return client.users_likes_tracks().tracks


def yandex_get_playlist(token, user_login, kind):
    client = Client(token).init()
    playlist = client.users_playlists(int(kind), user_login)
    return playlist.tracks


def yandex_track_info(short_track):
    """Возвращает (название, артисты, путь к обложке или None)."""
    track = short_track.fetch_track()
    title = track.title
    artists = ", ".join(a.name for a in track.artists)
    cover_path = None
    try:
        cover_path = os.path.join(DOWNLOAD_DIR, f"cover_{track.id}.jpg")
        track.download_cover(cover_path, size="200x200")
    except Exception:
        cover_path = None
    return title, artists, cover_path


# ---------- поиск и скачивание аудио ----------
def download_track_public(artist, title):
    clean_query = f"{artist} - {title}".replace('"', "").replace("'", "")
    base_opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "noplaylist": True,
        # пропускаем слишком длинные видео (миксы, альбомы целиком)
        "match_filter": yt_dlp.utils.match_filter_func("duration < 600"),
    }
    queries = [
        f"scsearch1:{clean_query}",
        f"ytsearch1:{clean_query} audio",
        f"ytsearch1:{clean_query}",
    ]
    for q in queries:
        try:
            with yt_dlp.YoutubeDL(base_opts) as ydl:
                info = ydl.extract_info(q, download=True)
                if not info:
                    continue
                if "entries" in info:
                    entries = [e for e in info["entries"] if e]
                    if not entries:
                        continue
                    info = entries[0]
                mp3 = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
                if os.path.exists(mp3):
                    return mp3
        except Exception as e:
            logging.warning(f"Ошибка поиска '{q}': {e}")
            continue
    return None


# ---------- общая логика переноса ----------
async def transfer(message: Message, tracks, limit=None):
    total_all = len(tracks)
    if limit:
        tracks = tracks[-limit:]
    total = len(tracks)

    hours = round(total * 20 / 3600, 1)
    await message.answer(
        f"Найдено треков: {total_all}. В работе: {total}.\n"
        f"Примерное время: {hours} ч. Одно сообщение с прогрессом будет обновляться."
    )
    progress = await message.answer("Начинаю...")

    ok, failed = 0, []
    for i, short_track in enumerate(tracks, 1):
        cover = None
        file_path = None
        try:
            title, artists, cover = await asyncio.to_thread(yandex_track_info, short_track)
            try:
                await progress.edit_text(f"[{i}/{total}] Ищу: {artists} — {title}")
            except Exception:
                pass

            file_path = await asyncio.to_thread(download_track_public, artists, title)
            if file_path:
                await message.answer_audio(
                    audio=FSInputFile(file_path),
                    title=title,
                    performer=artists,
                    thumbnail=FSInputFile(cover) if cover and os.path.exists(cover) else None,
                )
                ok += 1
            else:
                failed.append(f"{artists} — {title}")
        except Exception as e:
            logging.exception("Ошибка на треке")
            failed.append(f"трек #{i} ({e})")
        finally:
            for p in (file_path, cover):
                if p and os.path.exists(p):
                    os.remove(p)

        # пауза, чтобы Telegram не ограничил бота за частые отправки
        await asyncio.sleep(3)
        if i % 5 == 0:
            await asyncio.sleep(10)

    report = f"Готово. Загружено: {ok} из {total}."
    if failed:
        report += "\n\nНе удалось найти:\n" + "\n".join(failed[:40])
        if len(failed) > 40:
            report += f"\n...и ещё {len(failed) - 40}"
    await message.answer(report)


# ---------- команды ----------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я переношу треки из Яндекс Музыки в Telegram.\n\n"
        "/download — весь плейлист «Мне нравится»\n"
        "/download 3 — только последние 3 трека (для проверки)\n"
        "/playlist логин номер — публичный плейлист. Ссылка вида "
        "music.yandex.ru/users/ЛОГИН/playlists/НОМЕР"
    )


@dp.message(Command("download"))
async def cmd_download(message: Message):
    if not YANDEX_TOKEN:
        await message.answer("Не задан YANDEX_TOKEN в Render.")
        return
    args = message.text.split()
    limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    try:
        await message.answer("Получаю список «Мне нравится»...")
        tracks = await asyncio.to_thread(yandex_get_liked, YANDEX_TOKEN)
        await transfer(message, tracks, limit)
    except Exception as e:
        await message.answer(f"Ошибка при работе с Яндекс Музыкой: {e}")


@dp.message(Command("playlist"))
async def cmd_playlist(message: Message):
    args = message.text.split()
    if len(args) < 3 or not args[2].isdigit():
        await message.answer("Формат: /playlist логин номер [сколько]")
        return
    limit = int(args[3]) if len(args) > 3 and args[3].isdigit() else None
    try:
        await message.answer("Получаю плейлист...")
        tracks = await asyncio.to_thread(yandex_get_playlist, YANDEX_TOKEN, args[1], args[2])
        await transfer(message, tracks, limit)
    except Exception as e:
        await message.answer(f"Ошибка при получении плейлиста: {e}")


async def main():
    asyncio.create_task(start_web_server())
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
