import os
import gc
import re
import io
import time
import shutil
import random
import zipfile
import tarfile
import platform
import subprocess
import urllib.request
import html
import asyncio
import difflib
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
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


# ---------- память (на бесплатном Render всего 512 МБ) ----------
def mem_report():
    """Строка с расходом памяти: весь контейнер и сам бот (для логов и /diag)."""
    total = own = None
    for path in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        try:
            with open(path) as f:
                total = int(f.read().strip()) / 1048576
            break
        except Exception:
            pass
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    own = int(line.split()[1]) / 1024
                    break
    except Exception:
        pass
    parts = []
    if total is not None:
        parts.append(f"всего {total:.0f} МБ из 512")
    if own is not None:
        parts.append(f"сам бот {own:.0f} МБ")
    return ", ".join(parts) or "нет данных"


def trim_memory():
    """Освобождает память, которую Python уже не использует, но не вернул системе."""
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в переменных окружения Render.")
# Токен Яндекса нужен только для чтения публичных плейлистов
YANDEX_TOKEN = os.environ.get("YANDEX_TOKEN", "")

# Увеличенный таймаут: на слабом бесплатном сервере отправка файла может идти долго
bot = Bot(
    token=BOT_TOKEN,
    session=AiohttpSession(timeout=300),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------- YouTube: куки (необязательно) и защита от блокировки ----------
# Если на Render добавлен Secret File с именем cookies.txt, бот будет использовать его
# для YouTube. Без него YouTube с адресов хостингов обычно блокирует запросы.
COOKIE_FILE = None
for _src in ("/etc/secrets/cookies.txt", "cookies.txt"):
    if os.path.exists(_src):
        try:
            COOKIE_FILE = os.path.join(DOWNLOAD_DIR, "yt_cookies.txt")
            shutil.copy(_src, COOKIE_FILE)  # копия, потому что yt-dlp может её перезаписывать
            logging.info("Найден файл cookies, YouTube будет использовать его")
        except Exception as _e:
            COOKIE_FILE = None
            logging.warning(f"Не удалось подготовить cookies: {_e}")
        break

YT_BLOCK_MINUTES = 120     # на сколько пропускать YouTube после отказа
YT_BLOCKED_UNTIL = 0.0


def yt_is_blocked():
    return time.time() < YT_BLOCKED_UNTIL


def note_yt_error(message, youtube_context=False):
    """
    Если YouTube блокирует сервер (просит войти или отвечает 403), на время отключаем его,
    чтобы не тратить время на заведомо неудачные запросы.
    youtube_context=True: сообщение относится именно к запросу к YouTube.
    """
    global YT_BLOCKED_UNTIL
    text = str(message)
    blocked = "Sign in to confirm" in text or "not a bot" in text
    if youtube_context and "403" in text and "Forbidden" in text:
        blocked = True
    if blocked:
        minutes = 30 if WARP_PROXY else YT_BLOCK_MINUTES
        if not yt_is_blocked():
            logging.warning(f"YouTube блокирует сервер, отключаю его на {minutes} мин")
        YT_BLOCKED_UNTIL = time.time() + minutes * 60
        return True
    return False


class YtLogger:
    """Тихий логгер для yt-dlp: не засоряет логи, но замечает блокировку YouTube."""

    def __init__(self, youtube=False):
        self.youtube = youtube

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        if not note_yt_error(msg, self.youtube):
            logging.warning(f"yt-dlp: {str(msg)[:200]}")


# ---------- JavaScript-движок для YouTube ----------
# Без него YouTube отдаёт yt-dlp только картинки, и на каждом видео возникает ошибка
# "Requested format is not available". Движок Deno скачивается один раз при запуске бота.
JS_OPTS = {}


def ensure_js_runtime():
    """Скачивает (если нужно) Deno и подключает его к yt-dlp. Ошибки не должны ронять бота."""
    global JS_OPTS
    # ограничиваем память, которую может занять JavaScript-движок (иначе Render перезапускает бота)
    os.environ.setdefault("DENO_V8_FLAGS", "--max-old-space-size=128")
    try:
        bin_dir = os.path.abspath("bin")
        deno_path = shutil.which("deno") or os.path.join(bin_dir, "deno")
        if not os.path.exists(deno_path):
            os.makedirs(bin_dir, exist_ok=True)
            arch = "aarch64" if platform.machine().lower() in ("aarch64", "arm64") else "x86_64"
            url = (
                "https://github.com/denoland/deno/releases/latest/download/"
                f"deno-{arch}-unknown-linux-gnu.zip"
            )
            zip_path = os.path.join(bin_dir, "deno.zip")
            logging.info("Скачиваю JavaScript-движок Deno для YouTube...")
            urllib.request.urlretrieve(url, zip_path)
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(bin_dir)
            os.remove(zip_path)
            deno_path = os.path.join(bin_dir, "deno")
            os.chmod(deno_path, 0o755)

        version = subprocess.run(
            [deno_path, "--version"], capture_output=True, text=True, timeout=30
        ).stdout.strip().split("\n")[0]
        logging.info(f"JavaScript-движок готов: {version}")

        # настройки формируем тем же разбором, что и у командной строки yt-dlp:
        # так структура всегда подходит к установленной версии
        parsed = yt_dlp.parse_options(["--js-runtimes", f"deno:{deno_path}"])
        ydl_opts = parsed[-1]
        JS_OPTS = {k: ydl_opts[k] for k in ("js_runtimes",) if k in ydl_opts}
        logging.info(f"Настройки JS для yt-dlp: {JS_OPTS}")
    except (Exception, SystemExit) as e:
        JS_OPTS = {}
        logging.warning(f"Не удалось подключить JavaScript-движок: {e}")


# ---------- обход блокировки YouTube через бесплатный Cloudflare WARP ----------
# YouTube блокирует адреса хостингов (в том числе Render). Бот при запуске сам скачивает
# программы wgcf и wireproxy, получает бесплатный WARP-аккаунт Cloudflare и пускает через него
# ТОЛЬКО запросы к YouTube. Telegram и Яндекс работают как раньше.
# Если есть свой прокси, его можно указать в переменной окружения YT_PROXY (например socks5h://...).
WARP_PORT = 40000
WARP_PROXY = None            # адрес прокси, когда он готов к работе
WARP_STATUS = "не запускался"
_warp_proc = None
USER_PROXY = os.environ.get("YT_PROXY", "").strip() or None


def _warp_arch():
    return "arm64" if platform.machine().lower() in ("aarch64", "arm64") else "amd64"


def _wgcf_url(arch):
    """Ссылка на свежую версию wgcf (узнаём номер версии через редирект GitHub)."""
    version = None
    try:
        req = urllib.request.Request(
            "https://github.com/ViRb3/wgcf/releases/latest", headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            tag = resp.geturl().rstrip("/").split("/")[-1]  # например v2.2.29
            if re.fullmatch(r"v\d+\.\d+\.\d+", tag):
                version = tag[1:]
    except Exception as e:
        logging.warning(f"Не удалось узнать версию wgcf: {e}")
    version = version or "2.2.29"
    return f"https://github.com/ViRb3/wgcf/releases/download/v{version}/wgcf_{version}_linux_{arch}"


def warp_selftest(proxy):
    """Проверяет, отвечает ли YouTube через прокси. Возвращает число найденных результатов."""
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": True,
        "ignore_no_formats_error": True,
        "socket_timeout": 30,
        "logger": CaptureLogger(),
        "proxy": proxy,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("ytsearch2:Bruno Mars Talking to the Moon", download=False)
    return len([e for e in ((info or {}).get("entries") or []) if e])


def ensure_warp():
    """Запускает WARP-прокси для YouTube. Любые ошибки только отключают прокси, бот продолжает работать."""
    global WARP_PROXY, WARP_STATUS, YT_BLOCKED_UNTIL, _warp_proc
    if USER_PROXY:
        WARP_PROXY = USER_PROXY
        WARP_STATUS = "используется свой прокси (YT_PROXY)"
        logging.info(WARP_STATUS)
        return
    try:
        bin_dir = os.path.abspath("bin")
        work_dir = os.path.join(bin_dir, "warp")
        os.makedirs(work_dir, exist_ok=True)
        arch = _warp_arch()

        wireproxy = os.path.join(bin_dir, "wireproxy")
        if not os.path.exists(wireproxy):
            logging.info("Скачиваю wireproxy для WARP...")
            tgz = os.path.join(bin_dir, "wireproxy.tar.gz")
            urllib.request.urlretrieve(
                f"https://github.com/pufferffish/wireproxy/releases/latest/download/wireproxy_linux_{arch}.tar.gz",
                tgz,
            )
            with tarfile.open(tgz) as t:
                member = next(m for m in t.getmembers() if os.path.basename(m.name) == "wireproxy")
                member.name = "wireproxy"
                t.extract(member, bin_dir)
            os.remove(tgz)
            os.chmod(wireproxy, 0o755)

        wgcf = os.path.join(bin_dir, "wgcf")
        if not os.path.exists(wgcf):
            logging.info("Скачиваю wgcf для WARP...")
            urllib.request.urlretrieve(_wgcf_url(arch), wgcf)
            os.chmod(wgcf, 0o755)

        profile = os.path.join(work_dir, "wgcf-profile.conf")
        if not os.path.exists(profile):
            last_err = ""
            for attempt in range(3):
                r = subprocess.run(
                    [wgcf, "register", "--accept-tos"], cwd=work_dir,
                    capture_output=True, text=True, timeout=90,
                )
                last_err = (r.stdout + r.stderr)[-300:]
                if os.path.exists(os.path.join(work_dir, "wgcf-account.toml")):
                    break
                time.sleep(5 * (attempt + 1))
            r = subprocess.run(
                [wgcf, "generate"], cwd=work_dir, capture_output=True, text=True, timeout=60
            )
            last_err = (r.stdout + r.stderr)[-300:] or last_err
            if not os.path.exists(profile):
                raise RuntimeError(f"не удалось получить профиль WARP: {last_err}")

        with open(profile, encoding="utf-8") as f:
            conf = f.read()
        conf_path = os.path.join(work_dir, "wireproxy.conf")
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write(conf + f"\n[Socks5]\nBindAddress = 127.0.0.1:{WARP_PORT}\n")

        warp_env = dict(os.environ, GOMEMLIMIT="48MiB", GOGC="50")
        _warp_proc = subprocess.Popen(
            [wireproxy, "-c", conf_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=warp_env,
        )
        time.sleep(4)
        if _warp_proc.poll() is not None:
            raise RuntimeError("wireproxy сразу завершился")

        proxy = f"socks5h://127.0.0.1:{WARP_PORT}"
        found = warp_selftest(proxy)
        if found > 0:
            WARP_PROXY = proxy
            YT_BLOCKED_UNTIL = 0.0  # раз YouTube отвечает через WARP, снимаем блокировку
            WARP_STATUS = f"работает (тестовый поиск на YouTube: {found} рез.)"
        else:
            WARP_STATUS = "запущен, но YouTube через него не отвечает"
            _warp_proc.terminate()
        logging.info(f"WARP: {WARP_STATUS}")
    except (Exception, SystemExit) as e:
        WARP_STATUS = f"не удалось запустить: {str(e)[:200]}"
        logging.warning(f"WARP: {WARP_STATUS}")


def use_warp(opts, is_youtube):
    """Добавляет прокси в настройки yt-dlp, но только для запросов к YouTube."""
    global WARP_PROXY, WARP_STATUS
    if WARP_PROXY and _warp_proc is not None and _warp_proc.poll() is not None:
        WARP_PROXY = None
        WARP_STATUS = "процесс WARP остановился"
        logging.warning("WARP: процесс остановился, YouTube снова идёт напрямую")
    if is_youtube and WARP_PROXY:
        opts["proxy"] = WARP_PROXY
    return opts


def with_retry(fn, *args, tries=4, delay=3):
    """Повторяет вызов при временных сбоях (например, 429 «слишком много запросов» от Яндекса)."""
    for attempt in range(tries):
        try:
            return fn(*args)
        except Exception as e:
            if attempt == tries - 1:
                raise
            logging.warning(f"Сбой {getattr(fn, '__name__', fn)} (попытка {attempt + 1}/{tries}): {e}")
            time.sleep(delay * (attempt + 1))


# ---------- настройки (можно менять) ----------

# Идеальная разница длительности (в секундах) между Яндексом и найденным файлом.
MAX_DURATION_DIFF = 7

# Запасной вариант: файлы с разницей до этого значения берутся, если идеальных нет,
# и подписываются предупреждением.
RELAXED_DURATION_DIFF = 40

# Сколько секунд максимум искать один трек. Если за это время подходящая версия не найдена,
# бот переходит к следующему треку. Меньше число: быстрее, но реже находит редкие треки.
TRACK_TIME_LIMIT = 120

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


def fuzzy_hits(query_words, found_words):
    """Сколько слов запроса есть среди найденных (с учётом небольших отличий в написании)."""
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
    return hits


def fuzzy_overlap(query_words, found_words):
    """Доля слов запроса, которые есть среди найденных."""
    if not query_words:
        return 0.0
    return fuzzy_hits(query_words, found_words) / len(query_words)


# Служебные слова, которые не говорят об исполнителе
STOP_WORDS = {skeleton(w) for w in ("the", "and", "feat", "ft", "vs", "with")}


# Слова, которые обычно означают "не оригинал"
BAD_WORDS = norm_words(
    "remix cover slowed reverb sped speed speedup spedup nightcore karaoke "
    "instrumental mashup boosted edit bootleg flip rework acoustic live extended tiktok "
    "ремикс кавер минус караоке "
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
def yandex_get_playlist(token, login=None, kind=None, uuid=None):
    """
    Возвращает (название плейлиста, список треков).
    Два формата ссылок:
      старый: music.yandex.ru/users/ЛОГИН/playlists/НОМЕР  -> login + kind
      новый:  music.yandex.ru/playlists/lk.xxxxxxxx-...     -> uuid
    """
    client = Client(token).init() if token else Client().init()

    if uuid:
        playlist = None
        # пробуем идентификатор целиком (с "lk.") и без префикса
        for candidate in dict.fromkeys([uuid, uuid.split(".", 1)[-1]]):
            try:
                playlist = client.playlist(candidate)
            except Exception as e:
                logging.warning(f"Плейлист {candidate} не открылся: {e}")
                playlist = None
            if playlist:
                break
        if not playlist:
            raise ValueError("Плейлист по этой ссылке не найден")
    else:
        playlist = client.users_playlists(int(kind), login)
        if isinstance(playlist, list):
            playlist = playlist[0]

    title = getattr(playlist, "title", None) or "Плейлист"
    declared = getattr(playlist, "track_count", None)  # сколько треков заявляет сам Яндекс

    # Яндекс по-разному отдаёт список в разных запросах (часть треков может теряться),
    # поэтому собираем несколько вариантов и берём самый полный
    candidates = {"playlist.tracks": list(playlist.tracks or [])}
    try:
        candidates["fetch_tracks"] = list(playlist.fetch_tracks() or [])
    except Exception as e:
        logging.info(f"fetch_tracks недоступен: {e}")
    try:
        owner_uid = getattr(getattr(playlist, "owner", None), "uid", None)
        kind_value = getattr(playlist, "kind", None)
        if owner_uid and kind_value is not None:
            other = client.users_playlists(kind_value, owner_uid)
            if isinstance(other, list):
                other = other[0] if other else None
            if other is not None:
                candidates["users_playlists"] = list(other.tracks or [])
    except Exception as e:
        logging.info(f"users_playlists недоступен: {e}")

    sizes = {name: len(items) for name, items in candidates.items()}
    logging.info(f"Плейлист '{title}': заявлено Яндексом={declared}, получено={sizes}")
    tracks = list(max(candidates.values(), key=len))

    # «Мне нравится» (kind=3): Яндекс отдаёт от новых к старым, разворачиваем
    if getattr(playlist, "kind", None) == 3 and LIKES_OLDEST_FIRST:
        tracks.reverse()
    return title, tracks, declared


def yandex_track_info(short_track):
    """Возвращает (название, артисты, путь к обложке или None, длительность в секундах или None)."""
    track = short_track.fetch_track()
    title = track.title
    # у Яндекса версия трека (Remix, Slowed, Live...) лежит в отдельном поле
    version = getattr(track, "version", None)
    if version and version.lower() not in title.lower():
        title = f"{title} ({version})"
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
TRANSLIT_READ = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def translit_text(text):
    """Читаемая латиница для русского текста; None, если в тексте нет кириллицы."""
    low = (text or "").lower()
    if not re.search(r"[а-яё]", low):
        return None
    out = "".join(TRANSLIT_READ.get(ch, ch) for ch in low)
    return re.sub(r"\s+", " ", out).strip()



def squash(text):
    """Слитное написание без пробелов и знаков: 'Bruno Mars' и 'brunomars' становятся одинаковыми."""
    return skeleton(re.sub(r"[\W_]+", "", (text or "").lower()))


def score_entry(entry, artist_words, first_artist_words, title_core_words, title_full_words,
                artists_text="", first_artist_text=""):
    """Оценка того, насколько найденный результат похож на нужный трек."""
    found_title = entry.get("title") or ""
    uploader = entry.get("uploader") or entry.get("channel") or ""
    found_words = norm_words(found_title + " " + uploader)

    title_part = fuzzy_overlap(title_core_words, found_words)
    artist_part = fuzzy_overlap(artist_words, found_words)

    # исполнитель должен быть в названии файла или в имени канала, иначе это
    # скорее всего чужой кавер или другой трек с таким же названием.
    # Достаточно половины значимых слов или двух совпавших слов (длинные названия групп
    # часто сокращают).
    core_artist = {w for w in artist_words if w not in STOP_WORDS} or set(artist_words)
    core_first = {w for w in first_artist_words if w not in STOP_WORDS} or set(first_artist_words)
    artist_hits = fuzzy_hits(core_artist, found_words)
    first_hits = fuzzy_hits(core_first, found_words)
    artist_ok = bool(
        (core_artist and (artist_hits >= 2 or artist_hits / len(core_artist) >= 0.5))
        or (core_first and first_hits / len(core_first) >= 0.5)
    )

    # исполнитель, записанный слитно («brunomars», «dopeclvb» как имя канала): сравниваем
    # слитные написания, иначе официальные загрузки артистов отбрасывались
    squashed_found = squash(found_title + " " + uploader)
    squashed_uploader = squash(uploader)
    uploader_is_artist = False
    for name in (first_artist_text, artists_text):
        sq = squash(name)
        if len(sq) >= 4 and sq in squashed_found:
            artist_ok = True
        if len(sq) >= 4 and sq in squashed_uploader:
            uploader_is_artist = True

    score = title_part * 2 + artist_part
    if uploader_is_artist:
        score += 0.3  # официальная загрузка от самого исполнителя

    # автоматические каналы YouTube «Исполнитель - Topic» хранят оригинальные студийные версии
    if artist_ok and uploader.strip().lower().endswith("topic"):
        score += 0.4

    # слова-пометки (ремикс, кавер, ускорено...), которых нет в оригинальном названии
    found_title_words = norm_words(found_title)
    bad = (found_title_words & BAD_WORDS) - title_full_words

    # небольшой штраф за лишние слова в названии (предпочитаем самое "чистое" название)
    extra = found_title_words - artist_words - title_full_words
    score -= 0.1 * min(len(extra), 4)

    return score, title_part, bad, artist_ok


def is_mostly_latin(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isascii()) / len(letters) > 0.7


def search_candidates(query, artist_words, first_artist_words, title_core_words, title_full_words, orig_duration,
                      artists_text="", first_artist_text=""):
    """
    Ищет по запросу. Возвращает два списка кандидатов:
    точные (оценка, разница_длительности, результат) и
    запасные (оценка, разница_длительности, результат, причина, есть_пометка_ремикса).
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "ignoreerrors": True,
        "skip_download": True,
        "logger": YtLogger(query.startswith("ytsearch")),
    }
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    # для поиска нужны только название, канал и длительность, а не форматы видео
    opts["ignore_no_formats_error"] = True
    if query.startswith("ytsearch"):
        opts["extract_flat"] = True  # быстро и без проверки форматов каждого видео
        use_warp(opts, True)
    strict, relaxed = [], []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if not info:
            logging.info("   поиск ничего не вернул (источник мог отказать или результатов нет)")
            return strict, relaxed
        entries = [e for e in (info.get("entries") or []) if e]
        logging.info(f"   найдено результатов: {len(entries)}")
        for entry in entries:
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

            score, title_part, bad, artist_ok = score_entry(
                entry, artist_words, first_artist_words, title_core_words, title_full_words,
                artists_text, first_artist_text,
            )
            if diff is not None and diff <= 3:
                score += 0.5
            logging.info(
                f"   кандидат: '{entry.get('title')}' ({entry.get('uploader') or entry.get('channel')}) "
                f"оценка={score:.2f} название={title_part:.2f} исполнитель={'да' if artist_ok else 'нет'} "
                f"разница={diff}"
            )

            if title_part < 0.6:
                continue
            if not artist_ok:
                logging.info("      отброшен: исполнитель не подтверждён")
                continue

            if bad:
                reason = "в названии есть пометка вроде remix/cover/ускорено"
                relaxed.append((score, diff or 0, entry, reason, True))
            elif orig_duration and diff is None:
                reason = "не удалось сверить длительность"
                relaxed.append((score, 0, entry, reason, False))
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
    is_youtube = "youtube.com" in url or "youtu.be" in url
    opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "noplaylist": True,
        "logger": YtLogger(is_youtube),
    }
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    opts.update(JS_OPTS)
    use_warp(opts, is_youtube)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None
            mp3 = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
            if os.path.exists(mp3):
                return mp3
    except Exception as e:
        if not note_yt_error(e, is_youtube):
            logging.warning(f"Ошибка скачивания {url}: {e}")
    return None


def download_track_public(artists, title, orig_duration=None, allow_relaxed=True):
    """
    Главная функция: ищет лучший вариант трека и скачивает его.
    Возвращает (путь к файлу или None, предупреждение или None).
    allow_relaxed=False («точный» режим): сомнительные варианты не отправляются совсем.
    """
    logging.info(f"Ищу: {artists} — {title} (длительность {orig_duration}, сомнительные: {allow_relaxed})")
    first_artist = artists.split(",")[0].strip()

    # название без скобок: "Трек (feat. Кто-то)" -> "Трек"
    title_core = re.sub(r"[\(\[].*?[\)\]]", " ", title).strip()
    if not title_core:
        title_core = title

    artist_words = norm_words(artists)
    first_artist_words = norm_words(first_artist)
    title_core_words = norm_words(title_core)
    title_full_words = norm_words(title)

    # варианты запроса без повторов; SoundCloud сейчас основной рабочий источник,
    # поэтому для него пробуем три формулировки
    query_texts = []
    variants = [
        f"{artists} - {title}",
        f"{first_artist} - {title_core}",
    ]
    # на SoundCloud русские исполнители часто подписаны латиницей
    latin_variant = translit_text(f"{first_artist} {title_core}")
    if latin_variant:
        variants.append(latin_variant)
    variants.append(f"{title_core} {first_artist}")
    # только название: исполнителя всё равно проверяет оценка результата
    if len(title_core) >= 5:
        variants.append(title_core)
    for text in variants:
        if text and text not in query_texts:
            query_texts.append(text)

    # для YouTube второй запрос выводит автоматические «Topic»-каналы с оригиналами
    yt_texts = query_texts[:1] + [f"{first_artist} {title_core} topic"]

    sources = [("scsearch8", query_texts), ("ytsearch4", yt_texts)]
    # зарубежные треки на YouTube находятся заметно лучше, но только если YouTube не блокирует сервер
    if is_mostly_latin(f"{artists} {title}") and not yt_is_blocked():
        sources.reverse()
    relaxed_pool = {}

    # общий лимит времени на один трек, чтобы бот не зависал на ненайденных
    deadline = time.time() + TRACK_TIME_LIMIT

    for source, texts in sources:
        for text in texts:
            if time.time() > deadline:
                logging.info(f"Лимит времени на трек ({TRACK_TIME_LIMIT} с) исчерпан")
                break
            if source.startswith("ytsearch") and yt_is_blocked():
                logging.info("YouTube сейчас блокирует сервер, пропускаю")
                break
            query = f"{source}:{text}"
            logging.info(f"Запрос: {query}")
            strict, relaxed = search_candidates(
                query, artist_words, first_artist_words, title_core_words, title_full_words, orig_duration,
                artists, first_artist,
            )
            for cand in relaxed:
                url = cand[2].get("webpage_url") or cand[2].get("url")
                if url and url not in relaxed_pool:
                    relaxed_pool[url] = cand
            # пробуем скачать до 3 лучших точных результатов
            for score, diff, entry in strict[:2]:
                url = entry.get("webpage_url") or entry.get("url")
                if not url:
                    continue
                logging.info(f"Выбран: '{entry.get('title')}' (оценка {score:.2f})")
                path = download_url(url)
                if path:
                    return path, None

    # запасной вариант (только в режиме «максимум»): заметная разница длительности или пометка ремикса
    if not allow_relaxed:
        return None, None
    pool = sorted(relaxed_pool.values(), key=lambda c: (c[4], c[1], -c[0]))
    for score, diff, entry, reason, is_bad in pool[:2]:
        if time.time() > deadline + 60:
            break
        url = entry.get("webpage_url") or entry.get("url")
        logging.info(f"Запасной вариант: '{entry.get('title')}' ({reason})")
        path = download_url(url)
        if path:
            return path, f"{reason}, возможно другая версия"

    return None, None


# ---------- диагностика источников (команда /diag) ----------
class CaptureLogger:
    """Собирает сообщения yt-dlp, чтобы показать их в диагностике."""

    def __init__(self, youtube=False):
        self.msgs = []
        self.youtube = youtube

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.msgs.append(str(msg))

    def error(self, msg):
        self.msgs.append(str(msg))
        note_yt_error(msg, self.youtube)


def _short(text, limit=220):
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def run_diagnostics(query):
    """Проверяет YouTube и SoundCloud на одном запросе и возвращает отчёт текстом."""
    lines = []
    try:
        version = yt_dlp.version.__version__
    except Exception:
        version = "неизвестна"
    lines.append(f"yt-dlp: версия {version}")
    lines.append(f"JS-движок: {'подключён' if JS_OPTS else 'НЕ подключён'}")
    lines.append(f"Cookies: {'есть' if COOKIE_FILE else 'нет'}")
    lines.append(f"WARP-прокси для YouTube: {WARP_STATUS}")
    lines.append(f"Память: {mem_report()}")
    lines.append(f"YouTube помечен как заблокированный: {'да' if yt_is_blocked() else 'нет'}")
    lines.append(f"Запрос: {query}")
    lines.append("")

    # --- YouTube: поиск ---
    cap = CaptureLogger(youtube=True)
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": True,
        "ignore_no_formats_error": True,
        "socket_timeout": 30,
        "logger": cap,
    }
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    use_warp(opts, True)
    entries = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch4:{query}", download=False)
        entries = [e for e in ((info or {}).get("entries") or []) if e]
    except Exception as e:
        cap.msgs.append(str(e))
    lines.append(f"YouTube, поиск: результатов {len(entries)}")
    for e in entries[:3]:
        lines.append(
            f"  • {_short(e.get('title'), 60)} — {_short(e.get('channel') or e.get('uploader'), 40)} "
            f"({e.get('duration')} с)"
        )
    for m in cap.msgs[:2]:
        lines.append(f"  ! {_short(m)}")

    # --- YouTube: доступны ли форматы у первого видео (без скачивания) ---
    if entries:
        url = entries[0].get("url") or entries[0].get("webpage_url")
        cap2 = CaptureLogger(youtube=True)
        o2 = {
            "quiet": True,
            "skip_download": True,
            "format": "bestaudio/best",
            "noplaylist": True,
            "socket_timeout": 45,
            "logger": cap2,
        }
        if COOKIE_FILE:
            o2["cookiefile"] = COOKIE_FILE
        o2.update(JS_OPTS)
        use_warp(o2, True)
        try:
            with yt_dlp.YoutubeDL(o2) as ydl:
                inf = ydl.extract_info(url, download=False)
            fmts = (inf or {}).get("formats") or []
            audio = [f for f in fmts if f.get("acodec") not in (None, "none")]
            lines.append(f"YouTube, видео 1: форматов {len(fmts)}, из них с аудио {len(audio)}")
        except Exception as e:
            cap2.msgs.append(str(e))
            lines.append("YouTube, видео 1: форматы получить НЕ удалось")
        for m in cap2.msgs[:2]:
            lines.append(f"  ! {_short(m)}")
    lines.append("")

    # --- SoundCloud: поиск ---
    cap3 = CaptureLogger()
    o3 = {
        "quiet": True,
        "skip_download": True,
        "ignoreerrors": True,
        "ignore_no_formats_error": True,
        "socket_timeout": 30,
        "logger": cap3,
    }
    sc_entries = []
    try:
        with yt_dlp.YoutubeDL(o3) as ydl:
            info = ydl.extract_info(f"scsearch4:{query}", download=False)
        sc_entries = [e for e in ((info or {}).get("entries") or []) if e]
    except Exception as e:
        cap3.msgs.append(str(e))
    lines.append(f"SoundCloud, поиск: результатов {len(sc_entries)}")
    for e in sc_entries[:3]:
        lines.append(
            f"  • {_short(e.get('title'), 60)} — {_short(e.get('uploader'), 30)} ({e.get('duration')} с)"
        )
    drm = sum(1 for m in cap3.msgs if "DRM" in m)
    if drm:
        lines.append(f"  ! {drm} рез. закрыты защитой (DRM)")
    return "\n".join(lines)


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
    "Просто отправьте мне ссылку на плейлист.\n\n"
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
        "🎯 <b>Точный режим</b> (по умолчанию): бот присылает только трек, в котором совпали "
        "название, исполнитель и длительность. Если такого нет, трек попадёт в список «не найдено».\n\n"
        "🌐 <b>Режим «максимум»</b>: бот дополнительно присылает похожие варианты (другая длительность, "
        "возможный ремикс). Они подписаны ⚠️ и перечислены в итоговом сообщении. "
        "Переключатель находится в карточке плейлиста.\n\n"
        "Зарубежные треки бот ищет в первую очередь на YouTube, русские на SoundCloud."
    ),
    "help": (
        "❓ <b>Помощь</b>\n\n"
        "<b>Где взять ссылку?</b>\n"
        "Откройте плейлист в Яндекс Музыке, нажмите «Поделиться» и скопируйте ссылку. "
        "Можно также скопировать адрес из адресной строки браузера. "
        "Всё, что идёт после знака «?», можно не убирать.\n\n"
        "<b>«Мне нравится»</b>\n"
        "Откройте плейлист и нажмите «Поделиться». В настройках приватности "
        "Яндекс Музыки плейлист должен быть открыт.\n\n"
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
    if session.get("mode", "exact") == "exact":
        mode_line = (
            "🎯 <b>Режим: точный.</b> Бот присылает только проверенные совпадения "
            "(тот же исполнитель, почти та же длительность). Сомнительное пропускает."
        )
    else:
        mode_line = (
            "🌐 <b>Режим: максимум.</b> Найдётся больше, но часть треков может оказаться "
            "другой версией. Такие треки будут подписаны ⚠️."
        )
    declared = session.get("declared")
    count_note = ""
    if declared and declared != session["total"]:
        count_note = (
            f"ℹ️ Яндекс указывает {declared}, а список отдал {session['total']}: "
            "часть треков могла быть удалена или скрыта.\n"
        )
    return (
        f"📀 <b>{html.escape(session['title'])}</b>\n"
        f"🎵 Треков: <b>{session['total']}</b>\n"
        f"{count_note}"
        f"⏱ Весь плейлист: {fmt_time(session['total'])}\n\n"
        f"{mode_line}\n\n"
        "Как переносить?\n"
        "• <b>Частями</b> — удобнее для больших плейлистов\n"
        "• <b>Сразу всё</b> — бот работает на сервере, можно закрыть Telegram\n\n"
        "<i>Поиск лучшей версии каждого трека занимает время, это нормально 🙂</i>"
    )


def card_kb(session):
    total = session["total"]
    part_end = min(PART_SIZE, total)
    test_n = min(5, total)
    if session.get("mode", "exact") == "exact":
        mode_btn = btn("🎯 Режим: точный (сменить)", "mode")
    else:
        mode_btn = btn("🌐 Режим: максимум (сменить)", "mode")
    return kb(
        [btn(f"📦 Частями: 1–{part_end}", f"run:1-{part_end}")],
        [btn(f"▶️ Сразу всё ({total})", f"run:1-{total}")],
        [btn("✏️ Свой диапазон", "custom"), btn(f"🎲 Проверка: {test_n} случайных", "rand")],
        [mode_btn],
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

# старый формат: music.yandex.ru/users/ЛОГИН/playlists/НОМЕР
LINK_RE = re.compile(
    r"music\.yandex\.(?:ru|com|by|kz|uz)/users/([^/\s?#]+)/playlists/(\d+)", re.IGNORECASE
)
# новый формат: music.yandex.ru/playlists/lk.xxxxxxxx-xxxx-... (параметры после ? игнорируются)
NEW_LINK_RE = re.compile(
    r"music\.yandex\.(?:ru|com|by|kz|uz)/playlists/([\w.\-]+)", re.IGNORECASE
)


# ---------- выполнение переноса ----------
async def safe_edit(chat_id, message_id, text, reply_markup=None):
    try:
        await bot.edit_message_text(
            text=text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
        )
    except Exception:
        pass


async def send_audio_retry(chat_id, file_path, title, artists, cover, caption):
    """Отправляет трек, при сетевых сбоях повторяет попытку до 3 раз."""
    last_error = None
    for attempt in range(3):
        try:
            await bot.send_audio(
                chat_id,
                audio=FSInputFile(file_path),
                title=title,
                performer=artists,
                thumbnail=FSInputFile(cover) if cover and os.path.exists(cover) else None,
                caption=caption,
            )
            return
        except TelegramRetryAfter as e:
            last_error = e
            await asyncio.sleep(e.retry_after + 1)
        except TelegramNetworkError as e:
            last_error = e
            logging.warning(f"Сбой отправки (попытка {attempt + 1}/3): {e}")
            await asyncio.sleep(5)
    raise last_error


async def run_job(chat_id, user_id, start, end, picks=None):
    """picks: список номеров треков (для случайной проверки); иначе берётся диапазон start–end."""
    session = sessions.get(user_id)
    if not session:
        await bot.send_message(chat_id, "Сессия устарела. Отправьте ссылку на плейлист заново.")
        return

    stop_event = stop_events[user_id]
    numbers = list(picks) if picks else list(range(start, end + 1))
    selected = [session["tracks"][n - 1] for n in numbers]
    allow_relaxed = session.get("mode", "exact") == "max"
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
    last_done = numbers[0] - 1 if numbers else start - 1
    processed = 0
    stopped = False

    try:
        async with WORK_LOCK:
            if user_id in queue_ids:
                queue_ids.remove(user_id)

            for offset, short_track in enumerate(selected):
                if stop_event.is_set():
                    stopped = True
                    break

                number = numbers[offset]
                cover = None
                file_path = None
                artists = title = ""
                try:
                    title, artists, cover, duration = await asyncio.to_thread(
                        with_retry, yandex_track_info, short_track
                    )

                    if picks:
                        where = f"🎲 Случайная проверка: трек №{number} ({offset + 1} из {count})"
                    else:
                        where = f"Трек <b>{number}</b> из {end} (часть {start}–{end})"
                    await safe_edit(
                        chat_id,
                        status.message_id,
                        f"⏳ <b>Идёт перенос</b>\n"
                        f"📀 {title_escaped}\n\n"
                        f"{progress_bar(offset, count)} {int(100 * offset / count)}%\n"
                        f"{where}\n\n"
                        f"🎵 {html.escape(artists)} — {html.escape(title)}\n\n"
                        f"✅ Загружено: {ok}   🔍 Не найдено: {len(failed)}\n"
                        "<i>Поиск лучшей версии может занимать несколько минут. "
                        "Telegram можно закрыть.</i>",
                        reply_markup=STOP_KB,
                    )

                    file_path, note = await asyncio.to_thread(
                        download_track_public, artists, title, duration, allow_relaxed
                    )
                    if file_path:
                        await send_audio_retry(
                            chat_id,
                            file_path,
                            title,
                            artists,
                            cover,
                            f"⚠️ {html.escape(note)}" if note else None,
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
                processed += 1
                trim_memory()
                logging.info(f"Память после трека №{number}: {mem_report()}")

                # пауза, чтобы Telegram не ограничил бота за частые отправки
                await asyncio.sleep(3)
                if (offset + 1) % 5 == 0:
                    await asyncio.sleep(10)
    except Exception:
        logging.exception("Сбой задачи переноса")
    finally:
        if user_id in queue_ids:
            queue_ids.remove(user_id)

    # --- итог ---
    if stopped:
        await safe_edit(chat_id, status.message_id, f"⏹ Перенос остановлен на треке №{last_done}.")
        header = f"⏹ <b>Остановлено</b> на №{last_done}"
    elif picks:
        await safe_edit(chat_id, status.message_id, "✅ Случайная проверка обработана.")
        header = "🎲 <b>Случайная проверка готова</b>"
    else:
        await safe_edit(chat_id, status.message_id, f"✅ Часть {start}–{end} обработана.")
        header = f"✅ <b>Часть {start}–{end} готова</b>"

    lines = [header, f"🎵 Загружено: <b>{ok}</b> из {max(processed, 0)}"]
    if flagged:
        lines.append(f"⚠️ Возможно другая версия: {len(flagged)}")
    if failed:
        lines.append(f"🔍 Не найдено: {len(failed)}")
        if yt_is_blocked():
            lines.append(
                "ℹ️ YouTube сейчас не отвечает серверу бота, поэтому часть зарубежных треков "
                "могла не найтись. Это не связано с вашим плейлистом."
            )

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
    if picks:
        rows.append([btn("🎲 Ещё случайные", "rand")])
    elif stopped and last_done < end:
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


async def start_job(chat_id, user_id, start, end, picks=None):
    task = active_jobs.get(user_id)
    if task and not task.done():
        await bot.send_message(chat_id, "⏳ Перенос уже идёт. Остановить его можно командой /stop.")
        return
    stop_events[user_id] = asyncio.Event()
    active_jobs[user_id] = asyncio.create_task(run_job(chat_id, user_id, start, end, picks))


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


@dp.message(Command("diag"))
async def cmd_diag(message: Message):
    """Скрытая команда проверки источников: /diag или /diag Исполнитель - Название"""
    parts = (message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else "Bruno Mars - Talking to the Moon"
    wait = await message.answer("🔧 Проверяю источники, это может занять до минуты...")
    try:
        report = await asyncio.to_thread(run_diagnostics, query)
    except Exception as e:
        logging.exception("Сбой диагностики")
        report = f"Диагностика упала: {e}"
    try:
        await wait.edit_text(html.escape(report)[:3900])
    except Exception:
        await message.answer(html.escape(report)[:3900])


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

    elif data == "rand":
        session = sessions.get(user_id)
        if not session:
            await show("Сессия устарела. Отправьте ссылку на плейлист заново.", menu_kb())
            return
        n = min(5, session["total"])
        picks = sorted(random.sample(range(1, session["total"] + 1), n))
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await start_job(message.chat.id, user_id, 1, 1, picks)

    elif data == "mode":
        session = sessions.get(user_id)
        if not session:
            await show("Сессия устарела. Отправьте ссылку на плейлист заново.", menu_kb())
            return
        session["mode"] = "max" if session.get("mode", "exact") == "exact" else "exact"
        await show(card_text(session), card_kb(session))

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
async def handle_link(message: Message, login=None, kind=None, uuid=None):
    user_id = message.from_user.id
    wait = await message.answer("🔎 Открываю плейлист...")
    try:
        title, tracks, declared = await asyncio.to_thread(
            with_retry, yandex_get_playlist, YANDEX_TOKEN, login, kind, uuid, tries=2
        )
    except Exception as e:
        logging.warning(f"Не удалось открыть плейлист {login}/{kind}/{uuid}: {e}")
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
        "declared": declared,
        "awaiting_range": False,
        "mode": "exact",
    }
    session = sessions[user_id]
    await wait.edit_text(card_text(session), reply_markup=card_kb(session))


@dp.message(F.text)
async def on_text(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    match = LINK_RE.search(text)
    if match:
        await handle_link(message, login=match.group(1), kind=match.group(2))
        return

    new_match = NEW_LINK_RE.search(text)
    if new_match:
        await handle_link(message, uuid=new_match.group(1))
        return

    if "music.yandex" in text.lower():
        await message.answer(
            "🤔 Не вижу в ссылке плейлиста.\n\n"
            "Откройте плейлист в Яндекс Музыке, нажмите «Поделиться» и отправьте мне скопированную ссылку.",
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
    await asyncio.to_thread(ensure_js_runtime)
    # WARP запускается в фоне, чтобы бот отвечал сразу
    asyncio.create_task(asyncio.to_thread(ensure_warp))
    await bot.delete_webhook(drop_pending_updates=True)
    await setup_bot_profile()
    logging.info("Polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
