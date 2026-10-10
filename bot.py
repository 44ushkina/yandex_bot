
import asyncio
import json
import logging
import os
import re
import traceback
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from yandex_music import Client

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в Environment на Render")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DATA_DIR = Path("/tmp/yandex_music_bot")
DATA_DIR.mkdir(parents=True, exist_ok=True)

auth_in_progress = set()
work_in_progress = set()


def token_path(user_id):
    return DATA_DIR / f"token_{user_id}.txt"


def keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔐 Войти через Яндекс",
            callback_data="auth_yandex"
        )],
        [InlineKeyboardButton(
            text="❤️ Получить «Мне нравится»",
            callback_data="my_likes"
        )],
    ])


async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info("Web server started on port %s", port)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎵 Получение списка треков Яндекс Музыки.\n\n"
        "1. Нажми «Войти через Яндекс».\n"
        "2. Для лайков нажми соответствующую кнопку.\n"
        "3. Для публичного плейлиста просто отправь ссылку сообщением.\n\n"
        "Бот соберёт названия, исполнителей, длительность и ссылки "
        "на обложки. Этот этап не скачивает аудио.",
        reply_markup=keyboard()
    )


@dp.callback_query(F.data == "auth_yandex")
async def auth_yandex(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id

    if callback.message.chat.type != "private":
        await callback.message.answer("Используй бота в личном чате.")
        return

    if user_id in auth_in_progress:
        await callback.message.answer("Авторизация уже запущена.")
        return

    auth_in_progress.add(user_id)
    await callback.message.answer("⏳ Запрашиваю код авторизации...")
    loop = asyncio.get_running_loop()

    def on_code(code):
        text = (
            "🔐 Подтверди вход в Яндекс.\n\n"
            f"Ссылка: {code.verification_url}\n"
            f"Код: {code.user_code}\n\n"
            f"Срок действия: около {code.expires_in} секунд.\n"
            "Не пересылай код другим людям."
        )
        asyncio.run_coroutine_threadsafe(
            bot.send_message(user_id, text), loop
        ).result(timeout=20)

    def authenticate():
        temp_client = Client()
        auth_token = temp_client.device_auth(on_code=on_code)
        client = Client(auth_token.access_token)
        client.init()
        return auth_token.access_token

    try:
        access_token = await asyncio.to_thread(authenticate)
        path = token_path(user_id)
        path.write_text(access_token, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

        await callback.message.answer(
            "✅ Вход подтверждён.",
            reply_markup=keyboard()
        )
    except Exception as error:
        logging.exception("Ошибка авторизации")
        await callback.message.answer(
            f"❌ Ошибка: {type(error).__name__}: {error}"
        )
    finally:
        auth_in_progress.discard(user_id)


def value(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def artist_names(track):
    artists = value(track, "artists") or []
    result = []
    for artist in artists:
        name = value(artist, "name") or value(artist, "title")
        if name:
            result.append(str(name))
    return result


def cover_url(track):
    albums = value(track, "albums") or []
    for album in albums:
        uri = value(album, "cover_uri")
        if uri:
            return "https://" + str(uri).replace(
                "%%", "400x400"
            ).lstrip("/")
    return None


def track_info(track):
    return {
        "id": str(value(track, "id", "")),
        "title": value(track, "title"),
        "artists": artist_names(track),
        "duration_ms": value(track, "duration_ms"),
        "cover_url": cover_url(track),
    }


def parse_playlist_url(url):
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()

    if host not in {
        "music.yandex.ru",
        "music.yandex.com",
        "music.yandex.kz",
        "music.yandex.by",
        "music.yandex.uz",
    }:
        return None

    match = re.search(
        r"/users/([^/]+)/playlists/(\d+)",
        parsed.path
    )
    if not match:
        return None

    return match.group(1), int(match.group(2))


def get_playlist_tracks(access_token, username, playlist_kind):
    client = Client(access_token)
    client.init()

    playlist = client.users_playlists(
        playlist_kind, username
    )

    tracks = value(playlist, "tracks") or []
    result = []
    seen = set()

    for item in tracks:
        track = value(item, "track") or item
        track_id = value(track, "id")
        if track_id is None:
            continue

        track_id = str(track_id)
        if track_id in seen:
            continue

        seen.add(track_id)
        result.append(track_info(track))

    return result


def get_liked_tracks(access_token):
    client = Client(access_token)
    client.init()

    likes = client.users_likes_tracks()
    items = value(likes, "tracks") or likes or []

    result = []
    seen = set()

    for item in items:
        track = value(item, "track") or item
        track_id = value(track, "id")
        if track_id is None:
            continue

        track_id = str(track_id)
        if track_id in seen:
            continue

        seen.add(track_id)
        result.append(track_info(track))

    return result


async def send_report(message, tracks, source):
    report_path = DATA_DIR / f"playlist_{message.from_user.id}.json"

    report = {
        "source": source,
        "track_count": len(tracks),
        "tracks": tracks,
        "note": (
            "Это список метаданных, а не аудиофайлы. "
            "Наличие записи в списке не означает, что аудио "
            "доступно для скачивания."
        ),
    }

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    await message.answer(
        f"✅ Получено треков: {len(tracks)}.\n"
        "Отправляю JSON со списком и ссылками на обложки."
    )
    await message.answer_document(
        FSInputFile(report_path),
        caption=f"Список треков: {source}"
    )
    report_path.unlink(missing_ok=True)


@dp.callback_query(F.data == "my_likes")
async def my_likes(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id

    if user_id in work_in_progress:
        await callback.message.answer("⏳ Уже обрабатываю запрос.")
        return

    path = token_path(user_id)
    if not path.exists():
        await callback.message.answer(
            "Сначала нажми «Войти через Яндекс».",
            reply_markup=keyboard()
        )
        return

    work_in_progress.add(user_id)
    status = await callback.message.answer(
        "⏳ Получаю список понравившихся треков..."
    )

    try:
        token = path.read_text(encoding="utf-8").strip()
        tracks = await asyncio.to_thread(get_liked_tracks, token)
        await send_report(callback.message, tracks, "Мне нравится")
        await status.answer("Готово.")
    except Exception as error:
        logging.error("%s\n%s", error, traceback.format_exc())
        await status.answer(
            f"❌ Не удалось получить лайки: "
            f"{type(error).__name__}: {error}"
        )
    finally:
        work_in_progress.discard(user_id)


@dp.message(F.text)
async def playlist_link(message: Message):
    url = message.text.strip()
    parsed = parse_playlist_url(url)

    if not parsed:
        await message.answer(
            "Отправь публичную ссылку вида:\n"
            "https://music.yandex.ru/users/USERNAME/playlists/123"
        )
        return

    user_id = message.from_user.id
    path = token_path(user_id)

    if not path.exists():
        await message.answer(
            "Сначала войди через Яндекс с помощью кнопки /start."
        )
        return

    if user_id in work_in_progress:
        await message.answer("⏳ Уже обрабатываю запрос.")
        return

    username, playlist_kind = parsed
    work_in_progress.add(user_id)
    status = await message.answer("⏳ Читаю публичный плейлист...")

    try:
        token = path.read_text(encoding="utf-8").strip()
        tracks = await asyncio.to_thread(
            get_playlist_tracks,
            token,
            username,
            playlist_kind
        )
        await send_report(
            message, tracks, f"{username}/playlists/{playlist_kind}"
        )
        await status.answer("Готово.")
    except Exception as error:
        logging.error("%s\n%s", error, traceback.format_exc())
        await status.answer(
            f"❌ Ошибка плейлиста: {type(error).__name__}: {error}"
        )
    finally:
        work_in_progress.discard(user_id)


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot, polling_timeout=30)
    )


if __name__ == "__main__":
    asyncio.run(main())
