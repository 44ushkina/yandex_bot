import os
import re
import asyncio
import difflib
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

# ---------- настройки (можно менять) ----------

# Идеальная разница длительности (в секундах) между Яндексом и найденным файлом.
# Такие файлы бот берёт в первую очередь.
MAX_DURATION_DIFF = 7

# Запасной вариант: если идеальных не нашлось, бот берёт файл с разницей до этого значения,
# но подписывает его предупреждением. Так находится больше треков.
RELAXED_DURATION_DIFF = 40

# Сколько треков предлагать в подсказке следующей части
PART_SIZE = 100

# True: №1 в списке «Мне нравится» = самый первый (старый) лайк
LIKES_OLDEST_FIRST = True


# ---------- нормализация текста для сравнения названий ----------
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "",
    "ы": "i", "ь": "", "э": "e", "ю": "u", "я": "a",
}

SKELETON_REPLACEMENTS = [
    ("kh", "h"), ("ts", "c"), ("zh", "z"), ("sh", "s"), ("ch", "c"),
    ("ya", "a"), ("yu", "u"), ("ye", "e"), ("yo", "e"),
    ("y", "i"), ("j", "i"), ("w", "v"), ("x", "ks"), ("q", "k"),
]


def skeleton(word):
    """Приводит слово к упрощённому латинскому виду, чтобы сравнивать русский и английский."""
    word = "".join(TRANSLIT.get(ch, ch) for ch in word)
    for a, b in SKELETON_REPLACEMENTS:
        word = word.replace(a, b)
    word = re.sub(r"(.)\1+", r"\1", word)
    return word


def norm_words(text):
    """Множество упрощённых слов из текста."""
    if not text:
        return set()
    text = text.lower().replace("_", " ")
    text = re.sub(r"[^\w\s]", " ", text)
    # разделяем буквы и цифры: "100iz10" -> "100 iz 10"
    text = re.sub(r"(?<=\d)(?=[^\W\d_])|(?<=[^\W\d_])(?=\d)", " ", text)
    return {skeleton(w) for w in text.split()}


def fuzzy_overlap(query_words, found_words):
    """Доля слов запроса, которые есть среди найденных (с учётом небольших отличий в написании)."""
    if not query_words:
        return 0.0
    hits = 0
    for qw in query_words:
        if qw in found_words:
            hits += 1
            continue
        if len(qw) >= 4:
            for fw in found_words:
                if difflib.SequenceMatcher(None, qw, fw).ratio() >= 0.8:
                    hits += 1
                    break
    return hits / len(query_words)


# Слова, которые обычно означают "не оригинал"
BAD_WORDS = norm_words(
    "remix cover slowed reverb sped speed speedup spedup nightcore karaoke "
    "instrumental mashup boosted ремикс кавер минус караоке "
    "ускоренный ускоренная ускорено ускорен замедленный замедленная"
)


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
    tracks = list(client.users_likes_tracks().tracks)
    if LIKES_OLDEST_FIRST:
        tracks.reverse()
    return tracks


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
def score_entry(entry, artist_words, title_core_words, title_full_words):
    """Оценка того, насколько найденный результат похож на нужный трек."""
    found_title = entry.get("title") or ""
    uploader = entry.get("uploader") or entry.get("channel") or ""
    found_words = norm_words(found_title + " " + uploader)

    title_part = fuzzy_overlap(title_core_words, found_words)
    artist_part = fuzzy_overlap(artist_words, found_words)

    score = title_part * 2 + artist_part

    # слова-пометки (ремикс, кавер, ускорено...), которых нет в оригинальном названии
    found_title_words = norm_words(found_title)
    bad = (found_title_words & BAD_WORDS) - title_full_words

    # небольшой штраф за лишние слова в названии (предпочитаем самое "чистое" название)
    extra = found_title_words - artist_words - title_full_words
    score -= 0.1 * min(len(extra), 4)

    return score, title_part, bad


def search_candidates(query, artist_words, title_core_words, title_full_words, orig_duration):
    """
    Ищет по запросу. Возвращает два списка кандидатов вида (оценка, разница_длительности, результат):
    точные (длительность почти совпадает) и запасные (длительность отличается заметнее).
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "skip_download": True,
    }
    strict, relaxed = [], []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if not info:
            return strict, relaxed
        for entry in info.get("entries") or []:
            if not entry:
                continue
            found_duration = entry.get("duration")
            if found_duration and found_duration > 600:
                continue  # слишком длинное: микс, альбом целиком

            diff = None
            if found_duration and orig_duration:
                diff = abs(found_duration - orig_duration)
                if diff > RELAXED_DURATION_DIFF:
                    logging.info(
                        f"   отброшен: '{entry.get('title')}' длительность отличается на {diff:.0f} с"
                    )
                    continue

            score, title_part, bad = score_entry(entry, artist_words, title_core_words, title_full_words)
            if diff is not None and diff <= 3:
                score += 0.5
            logging.info(
                f"   кандидат: '{entry.get('title')}' оценка={score:.2f} "
                f"название={title_part:.2f} разница={diff}"
            )
            if title_part >= 0.6 and score >= 2.0:
                if bad:
                    reason = "в названии есть пометка вроде remix/cover/ускорено"
                    relaxed.append((score, diff or 0, entry, reason, True))
                elif diff is not None and diff > MAX_DURATION_DIFF:
                    reason = f"длительность отличается от Яндекса на {diff:.0f} с"
                    relaxed.append((score, diff, entry, reason, False))
                else:
                    strict.append((score, diff or 0, entry))
    except Exception as e:
        logging.warning(f"Ошибка поиска '{query}': {e}")
    strict.sort(key=lambda x: x[0], reverse=True)
    return strict, relaxed


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
    """
    Главная функция: ищет лучший вариант трека и скачивает его.
    Возвращает (путь к файлу или None, предупреждение или None).
    """
    logging.info(f"Ищу: {artists} — {title} (длительность {orig_duration})")
    first_artist = artists.split(",")[0].strip()

    # название без скобок: "Трек (feat. Кто-то)" -> "Трек"
    title_core = re.sub(r"[\(\[].*?[\)\]]", " ", title).strip()
    if not title_core:
        title_core = title

    artist_words = norm_words(artists)
    title_core_words = norm_words(title_core)
    title_full_words = norm_words(title)

    # варианты запроса: от самого точного к более простым (без повторов)
    query_texts = []
    for text in (
        f"{artists} - {title}",
        f"{first_artist} - {title_core}",
        f"{title_core} {first_artist}",
    ):
        if text not in query_texts:
            query_texts.append(text)

    sources = ["scsearch8", "ytsearch5"]
    relaxed_pool = {}

    for source in sources:
        for text in query_texts:
            query = f"{source}:{text}"
            logging.info(f"Запрос: {query}")
            strict, relaxed = search_candidates(
                query, artist_words, title_core_words, title_full_words, orig_duration
            )
            for cand in relaxed:
                url = cand[2].get("webpage_url") or cand[2].get("url")
                if url and url not in relaxed_pool:
                    relaxed_pool[url] = cand
            # пробуем скачать до 3 лучших точных результатов
            for score, diff, entry in strict[:3]:
                url = entry.get("webpage_url") or entry.get("url")
                if not url:
                    continue
                logging.info(f"Выбран: '{entry.get('title')}' (оценка {score:.2f})")
                path = download_url(url)
                if path:
                    return path, None

    # запасной вариант: файлы с заметной разницей длительности
    pool = sorted(relaxed_pool.values(), key=lambda c: (c[4], c[1], -c[0]))
    for score, diff, entry, reason, is_bad in pool[:3]:
        url = entry.get("webpage_url") or entry.get("url")
        logging.info(f"Запасной вариант: '{entry.get('title')}' ({reason})")
        path = download_url(url)
        if path:
            return path, f"{reason}, возможно другая версия"

    return None, None


# ---------- выбор части плейлиста ----------
def parse_selection(arg_text, total):
    """
    Разбирает, какие треки обрабатывать. Возвращает (начало, конец), нумерация с 1,
    или None, если запись непонятна.
      пусто    -> все треки
      "5"      -> первые 5 треков (для проверки)
      "1-100"  -> треки с 1-го по 100-й
    """
    arg_text = (arg_text or "").strip()
    if not arg_text:
        return 1, total

    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", arg_text)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
    elif arg_text.isdigit():
        start, end = 1, int(arg_text)
    else:
        return None

    end = min(end, total)
    if start < 1 or start > end:
        return None
    return start, end


# ---------- общая логика переноса ----------
async def transfer(message: Message, all_tracks, start, end, command_name, extra_args=""):
    total_all = len(all_tracks)
    selected = all_tracks[start - 1:end]
    count = len(selected)

    hours = round(count * 60 / 3600, 1)
    await message.answer(
        f"Всего в плейлисте: {total_all}. В работе: с {start} по {end} ({count} шт.).\n"
        f"Примерное время: около {hours} ч. Одно сообщение с прогрессом будет обновляться."
    )
    progress = await message.answer("Начинаю...")

    ok, failed, flagged = 0, [], []
    for offset, short_track in enumerate(selected):
        number = start + offset  # номер трека в плейлисте
        cover = None
        file_path = None
        try:
            title, artists, cover, duration = await asyncio.to_thread(yandex_track_info, short_track)
            try:
                await progress.edit_text(f"№{number} (до {end}): {artists} — {title}")
            except Exception:
                pass

            file_path, note = await asyncio.to_thread(download_track_public, artists, title, duration)
            if file_path:
                await message.answer_audio(
                    audio=FSInputFile(file_path),
                    title=title,
                    performer=artists,
                    thumbnail=FSInputFile(cover) if cover and os.path.exists(cover) else None,
                    caption=f"⚠️ {note}" if note else None,
                )
                ok += 1
                if note:
                    flagged.append(f"№{number}: {artists} — {title} ({note})")
            else:
                failed.append(f"№{number}: {artists} — {title}")
        except Exception as e:
            logging.exception("Ошибка на треке")
            failed.append(f"№{number}: ошибка ({e})")
        finally:
            for p in (file_path, cover):
                if p and os.path.exists(p):
                    os.remove(p)

        # пауза, чтобы Telegram не ограничил бота за частые отправки
        await asyncio.sleep(3)
        if (offset + 1) % 5 == 0:
            await asyncio.sleep(10)

    report = f"Готово: треки с {start} по {end}. Загружено: {ok} из {count}."
    if flagged:
        report += "\n\nВозможно не та версия (проверьте на слух):\n" + "\n".join(flagged[:30])
    if failed:
        report += "\n\nНе удалось найти:\n" + "\n".join(failed[:40])
        if len(failed) > 40:
            report += f"\n...и ещё {len(failed) - 40}"
    if end < total_all:
        next_start = end + 1
        next_end = min(end + PART_SIZE, total_all)
        report += f"\n\nСледующая часть:\n/{command_name} {extra_args}{next_start}-{next_end}".replace("  ", " ")
    else:
        report += "\n\nПлейлист закончился."
    await message.answer(report)


# ---------- команды ----------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я переношу треки из Яндекс Музыки в Telegram.\n\n"
        "/download — весь плейлист «Мне нравится»\n"
        "/download 3 — первые 3 трека (для проверки)\n"
        "/download 1-100 — треки с 1-го по 100-й (перенос частями)\n\n"
        "/playlist логин номер — публичный плейлист целиком\n"
        "/playlist логин номер 1-100 — часть плейлиста\n"
        "Ссылка плейлиста: music.yandex.ru/users/ЛОГИН/playlists/НОМЕР"
    )


@dp.message(Command("download"))
async def cmd_download(message: Message):
    if not YANDEX_TOKEN:
        await message.answer("Не задан YANDEX_TOKEN в Render.")
        return
    args = message.text.split()
    arg_text = " ".join(args[1:])
    try:
        await message.answer("Получаю список «Мне нравится»...")
        tracks = await asyncio.to_thread(yandex_get_liked, YANDEX_TOKEN)
        selection = parse_selection(arg_text, len(tracks))
        if selection is None:
            await message.answer(
                f"Не поняла диапазон. В плейлисте {len(tracks)} треков. "
                "Примеры: /download 5 или /download 1-100"
            )
            return
        start, end = selection
        await transfer(message, tracks, start, end, "download")
    except Exception as e:
        await message.answer(f"Ошибка при работе с Яндекс Музыкой: {e}")


@dp.message(Command("playlist"))
async def cmd_playlist(message: Message):
    args = message.text.split()
    if len(args) < 3 or not args[2].isdigit():
        await message.answer("Формат: /playlist логин номер [5 или 1-100]")
        return
    login, kind = args[1], args[2]
    arg_text = " ".join(args[3:])
    try:
        await message.answer("Получаю плейлист...")
        tracks = await asyncio.to_thread(yandex_get_playlist, YANDEX_TOKEN, login, kind)
        selection = parse_selection(arg_text, len(tracks))
        if selection is None:
            await message.answer(
                f"Не поняла диапазон. В плейлисте {len(tracks)} треков. "
                f"Примеры: /playlist {login} {kind} 5 или /playlist {login} {kind} 1-100"
            )
            return
        start, end = selection
        await transfer(message, tracks, start, end, "playlist", extra_args=f"{login} {kind} ")
    except Exception as e:
        await message.answer(f"Ошибка при получении плейлиста: {e}")


async def main():
    asyncio.create_task(start_web_server())
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
