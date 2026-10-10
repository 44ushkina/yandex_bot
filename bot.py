import os
import re
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

# Слова, которые обычно означают "не оригинал"
BAD_WORDS = {
    "remix", "cover", "slowed", "reverb", "sped", "speed", "nightcore",
    "karaoke", "instrumental", "mashup", "8d", "boosted",
    "ремикс", "кавер", "минус", "караоке",
}


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
    """Возвращает (название, артисты, путь к обложке или None, длительность в секундах или None)."""
    track = short_track.fetch_track()
    title = track.title
    artists = ", ".join(a.name for a in track.artists)
    duration = None
    if track.duration_ms:
        duration = track.duration_ms / 1000
    cover_path = None
    try:
        cover_path = os.path.join(DOWNLOAD_DIR, f"cover_{track.id}.jpg")
        track.download_cover(cover_path, size="200x200")
    except Exception:
        cover_path = None
    return title, artists, cover_path, duration


# ---------- умный поиск аудио ----------
def norm_words(text):
    """Разбивает текст на множество слов (маленькими буквами, без знаков)."""
    if not text:
        return set()
    return set(re.sub(r"[^\w\s]", " ", text.lower()).split())


def score_entry(entry, artist_words, title_words, orig_duration):
    """Оценка того, насколько найденный результат похож на нужный трек."""
    found_title = entry.get("title") or ""
    uploader = entry.get("uploader") or entry.get("channel") or ""
    found_words = norm_words(found_title + " " + uploader)

    if title_words:
        title_part = len(title_words & found_words) / len(title_words)
    else:
        title_part = 0.0
    if artist_words:
        artist_part = len(artist_words & found_words) / len(artist_words)
    else:
        artist_part = 0.0

    score = title_part * 2 + artist_part

    # штраф за ремиксы, каверы и т.п., если этого слова нет в оригинальном названии
    found_title_words = norm_words(found_title)
    bad = (found_title_words & BAD_WORDS) - title_words
    score -= 0.7 * len(bad)

    # сравнение длительности
    found_duration = entry.get("duration")
    if found_duration and orig_duration:
        diff = abs(found_duration - orig_duration)
        if diff <= 10:
            score += 0.5
        elif diff > max(30, orig_duration * 0.25):
            score -= 1.5

    return score, title_part


def search_candidates(query, artist_words, title_words, orig_duration):
    """Ищет по запросу и возвращает список (оценка, результат), лучшие первыми."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "skip_download": True,
    }
    result = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if not info:
            return result
        for entry in info.get("entries") or []:
            if not entry:
                continue
            duration = entry.get("duration")
            if duration and duration > 600:
                continue  # слишком длинное: микс, альбом целиком
            score, title_part = score_entry(entry, artist_words, title_words, orig_duration)
            logging.info(f"   кандидат: '{entry.get('title')}' оценка={score:.2f} совпадение названия={title_part:.2f}")
            if title_part >= 0.6 and score >= 2.0:
                result.append((score, entry))
    except Exception as e:
        logging.warning(f"Ошибка поиска '{query}': {e}")
    result.sort(key=lambda x: x[0], reverse=True)
    return result


def download_url(url):
    """Скачивает конкретную ссылку и возвращает путь к mp3 или None."""
    opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None
            mp3 = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
            if os.path.exists(mp3):
                return mp3
    except Exception as e:
        logging.warning(f"Ошибка скачивания {url}: {e}")
    return None


def download_track_public(artists, title, orig_duration=None):
    """Главная функция: ищет лучший вариант трека и скачивает его."""
    logging.info(f"Ищу: {artists} — {title}")
    first_artist = artists.split(",")[0].strip()
    artist_words = norm_words(artists)
    title_words = norm_words(title)

    # варианты запроса: от самого точного к более простым
    query_texts = [f"{artists} - {title}"]
    if first_artist and first_artist != artists:
        query_texts.append(f"{first_artist} - {title}")
    query_texts.append(f"{title} {first_artist}")

    sources = ["scsearch5", "ytsearch5"]

    for source in sources:
        for text in query_texts:
            query = f"{source}:{text}"
            logging.info(f"Запрос: {query}")
            candidates = search_candidates(query, artist_words, title_words, orig_duration)
            # пробуем скачать до 3 лучших результатов
            for score, entry in candidates[:3]:
                url = entry.get("webpage_url") or entry.get("url")
                if not url:
                    continue
                logging.info(f"Выбран: '{entry.get('title')}' (оценка {score:.2f})")
                path = download_url(url)
                if path:
                    return path
    return None


# ---------- общая логика переноса ----------
async def transfer(message: Message, tracks, limit=None):
    total_all = len(tracks)
    if limit:
        tracks = tracks[-limit:]
    total = len(tracks)

    hours = round(total * 30 / 3600, 1)
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
            title, artists, cover, duration = await asyncio.to_thread(yandex_track_info, short_track)
            try:
                await progress.edit_text(f"[{i}/{total}] Ищу: {artists} — {title}")
            except Exception:
                pass

            file_path = await asyncio.to_thread(download_track_public, artists, title, duration)
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
