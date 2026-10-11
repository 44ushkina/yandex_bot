import os
import re
import io
import html
import asyncio
import difflib
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)
from yandex_music import Client
import yt_dlp

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в переменных окружения Render.")
# Токен Яндекса нужен только для чтения публичных плейлистов
YANDEX_TOKEN = os.environ.get("YANDEX_TOKEN", "")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------- настройки (можно менять) ----------

# Идеальная разница длительности (в секундах) между Яндексом и найденным файлом.
MAX_DURATION_DIFF = 7

# Запасной вариант: файлы с разницей до этого значения берутся, если идеальных нет,
# и подписываются предупреждением.
RELAXED_DURATION_DIFF = 40

# Размер одной части при переносе частями
PART_SIZE = 100

# True: в плейлисте «Мне нравится» №1 = самый первый (старый) лайк
LIKES_OLDEST_FIRST = True

# Если в итоге список проблемных треков длиннее этого числа, он отправляется файлом
MAX_INLINE_LIST = 10


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
def yandex_get_playlist(token, user_login, kind):
    """Возвращает (название плейлиста, список треков)."""
    client = Client(token).init() if token else Client().init()
    kind = int(kind)
    playlist = client.users_playlists(kind, user_login)
    if isinstance(playlist, list):
        playlist = playlist[0]
    tracks = list(playlist.tracks or [])
    if not tracks:
        tracks = list(playlist.fetch_tracks() or [])
    title = getattr(playlist, "title", None) or "Плейлист"
    # «Мне нравится» (kind=3): Яндекс отдаёт от новых к старым, разворачиваем
    if kind == 3 and LIKES_OLDEST_FIRST:
        tracks.reverse()
    return title, tracks


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
    Ищет по запросу. Возвращает два списка кандидатов:
    точные (оценка, разница_длительности, результат) и
    запасные (оценка, разница_длительности, результат, причина, есть_пометка_ремикса).
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

    # запасной вариант: файлы с заметной разницей длительности или пометкой ремикса
    pool = sorted(relaxed_pool.values(), key=lambda c: (c[4], c[1], -c[0]))
    for score, diff, entry, reason, is_bad in pool[:3]:
        url = entry.get("webpage_url") or entry.get("url")
        logging.info(f"Запасной вариант: '{entry.get('title')}' ({reason})")
        path = download_url(url)
        if path:
            return path, f"{reason}, возможно другая версия"

    return None, None


# ---------- разбор диапазона ----------
def parse_selection(arg_text, total):
    """
    Возвращает (начало, конец), нумерация с 1, или None, если запись непонятна.
      "5"      -> первые 5 треков
      "1-100"  -> треки с 1-го по 100-й
    """
    arg_text = (arg_text or "").strip()
    if not arg_text:
        return None

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


# ---------- тексты интерфейса ----------
MENU_TEXT = (
    "👋 <b>Привет!</b>\n\n"
    "Я переношу плейлисты из Яндекс Музыки в Telegram: название, исполнитель, "
    "обложка и сам трек.\n\n"
    "<b>Как начать</b>\n"
    "Просто отправьте мне ссылку на плейлист, например:\n"
    "<code>music.yandex.ru/users/ЛОГИН/playlists/НОМЕР</code>\n\n"
    "Нужна помощь со ссылкой? Загляните в раздел «Помощь» ниже 👇"
)

INFO_TEXTS = {
    "how": (
        "📖 <b>Как это работает</b>\n\n"
        "1️⃣ Вы присылаете ссылку на открытый плейлист Яндекс Музыки.\n"
        "2️⃣ Бот читает список: названия, исполнителей, обложки.\n"
        "3️⃣ Для каждого трека бот ищет лучшую версию на открытых музыкальных площадках.\n"
        "4️⃣ Готовые треки с обложками приходят вам в этот чат.\n\n"
        "Качество звука: MP3, 192 кбит/с. Используйте бот только для личного архива."
    ),
    "time": (
        "⏱ <b>Время и работа в фоне</b>\n\n"
        "Перенос большого плейлиста занимает много времени: в среднем около минуты на трек. "
        "100 треков это примерно 1,5–2 часа.\n\n"
        "🌙 <b>Бот работает на сервере.</b> Нажмите «Перенести», затем можно закрыть Telegram "
        "или выключить телефон: треки будут приходить в чат сами, а когда вы вернётесь, всё уже будет на месте.\n\n"
        "📦 Для больших плейлистов удобнее переносить <b>частями по 100 треков</b>: "
        "после каждой части бот предложит следующую одной кнопкой.\n\n"
        "⏹ Остановить перенос можно в любой момент кнопкой или командой /stop."
    ),
    "search": (
        "🔎 <b>О поиске</b>\n\n"
        "Бот не берёт первый попавшийся файл. Он сравнивает название, исполнителя и длительность, "
        "чтобы найти именно оригинал, а не ремикс, кавер или ускоренную версию.\n\n"
        "Поэтому поиск редких или малоизвестных треков может занимать несколько минут: "
        "бот пробует разные варианты запроса и разные площадки, чтобы выбрать лучшую версию.\n\n"
        "⚠️ Если точной версии нет, бот может прислать похожую, но обязательно подпишет это под треком. "
        "Список таких случаев будет в итоговом сообщении."
    ),
    "help": (
        "❓ <b>Помощь</b>\n\n"
        "<b>Где взять ссылку?</b>\n"
        "Откройте плейлист в Яндекс Музыке в браузере и скопируйте адрес из адресной строки. "
        "Он должен выглядеть так:\n"
        "<code>music.yandex.ru/users/ЛОГИН/playlists/НОМЕР</code>\n\n"
        "<b>«Мне нравится»</b>\n"
        "Ссылка заканчивается на <code>/playlists/3</code>. В настройках приватности "
        "Яндекс Музыки плейлисты должны быть открыты.\n\n"
        "<b>Бот пишет, что не может открыть плейлист</b>\n"
        "Скорее всего, плейлист закрыт для посторонних. Откройте доступ и отправьте ссылку снова.\n\n"
        "<b>Команды</b>\n"
        "/start — главное меню\n"
        "/stop — остановить перенос\n"
        "/help — эта справка"
    ),
}


# ---------- кнопки ----------
def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def menu_kb():
    return kb(
        [btn("📖 Как это работает", "info:how:menu"), btn("⏱ Время и фон", "info:time:menu")],
        [btn("🔎 О поиске", "info:search:menu"), btn("❓ Помощь", "info:help:menu")],
    )


def fmt_time(count):
    minutes = count
    if minutes < 60:
        return f"около {int(minutes)} мин"
    return f"около {minutes / 60:.1f} ч"


def card_text(session):
    return (
        f"📀 <b>{html.escape(session['title'])}</b>\n"
        f"🎵 Треков: <b>{session['total']}</b>\n"
        f"⏱ Весь плейлист: {fmt_time(session['total'])}\n\n"
        "Как переносить?\n"
        "• <b>Частями</b> — удобнее для больших плейлистов\n"
        "• <b>Сразу всё</b> — бот работает на сервере, можно закрыть Telegram\n\n"
        "<i>Поиск лучшей версии каждого трека занимает время, это нормально 🙂</i>"
    )


def card_kb(session):
    total = session["total"]
    part_end = min(PART_SIZE, total)
    test_end = min(5, total)
    return kb(
        [btn(f"📦 Частями: 1–{part_end}", f"run:1-{part_end}")],
        [btn(f"▶️ Сразу всё ({total})", f"run:1-{total}")],
        [btn("✏️ Свой диапазон", "custom"), btn(f"🧪 Проверка: {test_end}", f"run:1-{test_end}")],
        [btn("⏱ Как долго?", "info:time:card"), btn("❌ Отмена", "cancel")],
    )


def back_kb(back):
    return kb([btn("⬅️ Назад", f"back:{back}")])


STOP_KB = kb([btn("⏹ Остановить", "stop")])


def progress_bar(done, total, width=10):
    if total <= 0:
        return "░" * width
    filled = int(width * done / total)
    return "▓" * filled + "░" * (width - filled)


# ---------- состояние ----------
sessions = {}      # user_id -> данные о плейлисте
active_jobs = {}   # user_id -> asyncio.Task
stop_events = {}   # user_id -> asyncio.Event
WORK_LOCK = asyncio.Lock()  # перенос идёт по одному плейлисту за раз
queue_ids = []     # кто ждёт очереди

LINK_RE = re.compile(
    r"music\.yandex\.(?:ru|com|by|kz|uz)/users/([^/\s?#]+)/playlists/(\d+)", re.IGNORECASE
)


# ---------- выполнение переноса ----------
async def safe_edit(chat_id, message_id, text, reply_markup=None):
    try:
        await bot.edit_message_text(
            text=text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
        )
    except Exception:
        pass


async def run_job(chat_id, user_id, start, end):
    session = sessions.get(user_id)
    if not session:
        await bot.send_message(chat_id, "Сессия устарела. Отправьте ссылку на плейлист заново.")
        return

    stop_event = stop_events[user_id]
    selected = session["tracks"][start - 1:end]
    count = len(selected)
    total_all = session["total"]
    title_escaped = html.escape(session["title"])

    # --- очередь ---
    if WORK_LOCK.locked():
        queue_ids.append(user_id)
        status = await bot.send_message(
            chat_id,
            f"⏳ Сейчас идёт перенос для другого пользователя.\n"
            f"Вы в очереди, перед вами: <b>{len(queue_ids)}</b>.\n"
            "Начну сам, как только освобожусь. Telegram можно закрыть.",
            reply_markup=STOP_KB,
        )
    else:
        status = await bot.send_message(chat_id, "🚀 Запускаю перенос...", reply_markup=STOP_KB)

    ok, failed, flagged = 0, [], []
    last_done = start - 1
    stopped = False

    try:
        async with WORK_LOCK:
            if user_id in queue_ids:
                queue_ids.remove(user_id)

            for offset, short_track in enumerate(selected):
                if stop_event.is_set():
                    stopped = True
                    break

                number = start + offset
                cover = None
                file_path = None
                artists = title = ""
                try:
                    title, artists, cover, duration = await asyncio.to_thread(
                        yandex_track_info, short_track
                    )

                    await safe_edit(
                        chat_id,
                        status.message_id,
                        f"⏳ <b>Идёт перенос</b>\n"
                        f"📀 {title_escaped}\n\n"
                        f"{progress_bar(offset, count)} {int(100 * offset / count)}%\n"
                        f"Трек <b>{number}</b> из {end} (часть {start}–{end})\n\n"
                        f"🎵 {html.escape(artists)} — {html.escape(title)}\n\n"
                        f"✅ Загружено: {ok}   🔍 Не найдено: {len(failed)}\n"
                        "<i>Поиск лучшей версии может занимать несколько минут. "
                        "Telegram можно закрыть.</i>",
                        reply_markup=STOP_KB,
                    )

                    file_path, note = await asyncio.to_thread(
                        download_track_public, artists, title, duration
                    )
                    if file_path:
                        await bot.send_audio(
                            chat_id,
                            audio=FSInputFile(file_path),
                            title=title,
                            performer=artists,
                            thumbnail=FSInputFile(cover) if cover and os.path.exists(cover) else None,
                            caption=f"⚠️ {html.escape(note)}" if note else None,
                        )
                        ok += 1
                        if note:
                            flagged.append(f"№{number}: {artists} — {title} ({note})")
                    else:
                        failed.append(f"№{number}: {artists} — {title}")
                except Exception as e:
                    logging.exception("Ошибка на треке")
                    failed.append(f"№{number}: {artists or 'трек'} {title} (ошибка: {e})")
                finally:
                    for p in (file_path, cover):
                        if p and os.path.exists(p):
                            os.remove(p)

                last_done = number

                # пауза, чтобы Telegram не ограничил бота за частые отправки
                await asyncio.sleep(3)
                if (offset + 1) % 5 == 0:
                    await asyncio.sleep(10)
    except Exception:
        logging.exception("Сбой задачи переноса")
    finally:
        if user_id in queue_ids:
            queue_ids.remove(user_id)

    processed = last_done - start + 1

    # --- итог ---
    if stopped:
        await safe_edit(chat_id, status.message_id, f"⏹ Перенос остановлен на треке №{last_done}.")
        header = f"⏹ <b>Остановлено</b> на №{last_done}"
    else:
        await safe_edit(chat_id, status.message_id, f"✅ Часть {start}–{end} обработана.")
        header = f"✅ <b>Часть {start}–{end} готова</b>"

    lines = [header, f"🎵 Загружено: <b>{ok}</b> из {max(processed, 0)}"]
    if flagged:
        lines.append(f"⚠️ Возможно другая версия: {len(flagged)}")
    if failed:
        lines.append(f"🔍 Не найдено: {len(failed)}")

    problem_lines = []
    if flagged:
        problem_lines.append("⚠️ Возможно не та версия (проверьте на слух):")
        problem_lines.extend(flagged)
    if failed:
        if problem_lines:
            problem_lines.append("")
        problem_lines.append("🔍 Не найдено:")
        problem_lines.extend(failed)

    send_file = len(flagged) + len(failed) > MAX_INLINE_LIST
    if problem_lines and not send_file:
        lines.append("")
        lines.extend(html.escape(x) for x in problem_lines)
    if send_file:
        lines.append("\n📎 Подробный список в файле ниже.")

    rows = []
    if stopped and last_done < end:
        rows.append([btn(f"▶️ Продолжить с №{last_done + 1}", f"run:{last_done + 1}-{end}")])
    elif end < total_all:
        next_start = end + 1
        next_end = min(end + PART_SIZE, total_all)
        rows.append([btn(f"➡️ Следующая часть {next_start}–{next_end}", f"run:{next_start}-{next_end}")])
    else:
        lines.append("\n🎉 Плейлист закончился!")
    rows.append([btn("🏠 Меню", "back:menu")])

    await bot.send_message(chat_id, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    if send_file:
        data = "\n".join(problem_lines).encode("utf-8")
        await bot.send_document(
            chat_id, BufferedInputFile(data, filename="report.txt"), caption="Список проблемных треков"
        )


async def start_job(chat_id, user_id, start, end):
    task = active_jobs.get(user_id)
    if task and not task.done():
        await bot.send_message(chat_id, "⏳ Перенос уже идёт. Остановить его можно командой /stop.")
        return
    stop_events[user_id] = asyncio.Event()
    active_jobs[user_id] = asyncio.create_task(run_job(chat_id, user_id, start, end))


# ---------- команды ----------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(MENU_TEXT, reply_markup=menu_kb())


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(INFO_TEXTS["help"], reply_markup=back_kb("menu"))


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    user_id = message.from_user.id
    task = active_jobs.get(user_id)
    if task and not task.done():
        stop_events[user_id].set()
        await message.answer("⏹ Останавливаю после текущего трека...")
    else:
        await message.answer("Сейчас нет активного переноса.")


# ---------- кнопки (callback) ----------
@dp.callback_query()
async def on_callback(cb: CallbackQuery):
    data = cb.data or ""
    user_id = cb.from_user.id
    message = cb.message
    await cb.answer()

    async def show(text, markup):
        try:
            await message.edit_text(text, reply_markup=markup)
        except Exception:
            pass

    if data.startswith("info:"):
        _, key, back = data.split(":")
        await show(INFO_TEXTS.get(key, "—"), back_kb(back))

    elif data.startswith("back:"):
        back = data.split(":")[1]
        session = sessions.get(user_id)
        if back == "card" and session:
            await show(card_text(session), card_kb(session))
        else:
            await show(MENU_TEXT, menu_kb())

    elif data.startswith("run:"):
        session = sessions.get(user_id)
        if not session:
            await show("Сессия устарела. Отправьте ссылку на плейлист заново.", menu_kb())
            return
        start_s, end_s = data[4:].split("-")
        start, end = int(start_s), min(int(end_s), session["total"])
        if start < 1 or start > end:
            return
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await start_job(message.chat.id, user_id, start, end)

    elif data == "custom":
        session = sessions.get(user_id)
        if not session:
            await show("Сессия устарела. Отправьте ссылку на плейлист заново.", menu_kb())
            return
        session["awaiting_range"] = True
        await show(
            f"✏️ <b>Свой диапазон</b>\n\n"
            f"В плейлисте {session['total']} треков. Отправьте номера, например:\n"
            "<code>1-100</code> — с первого по сотый\n"
            "<code>101-200</code> — со 101-го по 200-й\n"
            "<code>10</code> — первые 10 треков",
            kb([btn("⬅️ Назад", "back:card")]),
        )

    elif data == "stop":
        task = active_jobs.get(user_id)
        if task and not task.done():
            stop_events[user_id].set()
            await show("⏹ Останавливаю после текущего трека...", None)

    elif data == "cancel":
        sessions.pop(user_id, None)
        await show(MENU_TEXT, menu_kb())


# ---------- обработка ссылок и текста ----------
async def handle_link(message: Message, login, kind):
    user_id = message.from_user.id
    wait = await message.answer("🔎 Открываю плейлист...")
    try:
        title, tracks = await asyncio.to_thread(yandex_get_playlist, YANDEX_TOKEN, login, kind)
    except Exception as e:
        logging.warning(f"Не удалось открыть плейлист {login}/{kind}: {e}")
        await wait.edit_text(
            "❌ <b>Не получилось открыть плейлист.</b>\n\n"
            "Проверьте, что плейлист открыт для всех, а ссылка скопирована полностью. "
            "Подробнее в «Помощь».",
            reply_markup=kb([btn("❓ Помощь", "info:help:menu")]),
        )
        return

    if not tracks:
        await wait.edit_text("В этом плейлисте нет треков или он закрыт.")
        return

    sessions[user_id] = {
        "title": title,
        "tracks": tracks,
        "total": len(tracks),
        "awaiting_range": False,
    }
    session = sessions[user_id]
    await wait.edit_text(card_text(session), reply_markup=card_kb(session))


@dp.message(F.text)
async def on_text(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    match = LINK_RE.search(text)
    if match:
        await handle_link(message, match.group(1), match.group(2))
        return

    if "music.yandex" in text.lower():
        await message.answer(
            "🤔 Не вижу в ссылке логин и номер плейлиста.\n\n"
            "Откройте плейлист в браузере и скопируйте адрес из адресной строки. "
            "Он должен выглядеть так:\n"
            "<code>music.yandex.ru/users/ЛОГИН/playlists/НОМЕР</code>",
            reply_markup=kb([btn("❓ Помощь", "info:help:menu")]),
        )
        return

    session = sessions.get(user_id)
    if session and session.get("awaiting_range"):
        selection = parse_selection(text, session["total"])
        if selection is None:
            await message.answer(
                f"Не поняла диапазон. В плейлисте {session['total']} треков.\n"
                "Пример: <code>1-100</code>"
            )
            return
        session["awaiting_range"] = False
        await start_job(message.chat.id, user_id, selection[0], selection[1])
        return

    await message.answer(
        "Отправьте ссылку на плейлист из Яндекс Музыки или откройте меню 👇",
        reply_markup=kb([btn("🏠 Меню", "back:menu")]),
    )


async def setup_bot_profile():
    """Меню команд и описание бота (то, что люди видят в Telegram)."""
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="help", description="Помощь и как получить ссылку"),
                BotCommand(command="stop", description="Остановить перенос"),
            ]
        )
        await bot.set_my_short_description(
            short_description="Перенос плейлистов из Яндекс Музыки в Telegram"
        )
        await bot.set_my_description(
            description=(
                "Переношу плейлисты из Яндекс Музыки в Telegram: название, исполнитель, обложка и трек. "
                "Отправьте ссылку на плейлист, и я пришлю музыку в этот чат."
            )
        )
    except Exception as e:
        logging.warning(f"Не удалось настроить профиль бота: {e}")


async def main():
    asyncio.create_task(start_web_server())
    await bot.delete_webhook(drop_pending_updates=True)
    await setup_bot_profile()
    logging.info("Polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
