
import asyncio
import json
import logging
import os
import traceback
from pathlib import Path

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
export_in_progress = set()


def token_path(user_id):
    return DATA_DIR / f"token_{user_id}.txt"


def keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 Войти через Яндекс",
                    callback_data="auth_yandex",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❤️ Выгрузить «Мне нравится»",
                    callback_data="export_likes",
                )
            ],
        ]
    )


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

    logging.info("Web server started on port %s", port)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Выгрузка списка треков из Яндекс Музыки.\n\n"
        "1. Нажми «Войти через Яндекс».\n"
        "2. Подтверди вход по ссылке и коду.\n"
        "3. Нажми «Выгрузить „Мне нравится“».\n\n"
        "Бот отправит JSON с названиями, исполнителями, "
        "ID, длительностью и обложками, если эти данные "
        "удастся получить через API.",
        reply_markup=keyboard(),
    )


@dp.callback_query(F.data == "auth_yandex")
async def auth_yandex(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id

    if callback.message.chat.type != "private":
        await callback.message.answer(
            "Используй бота в личном чате."
        )
        return

    if user_id in auth_in_progress:
        await callback.message.answer(
            "Авторизация уже выполняется."
        )
        return

    auth_in_progress.add(user_id)
    await callback.message.answer(
        "⏳ Запрашиваю код авторизации..."
    )

    loop = asyncio.get_running_loop()

    def on_code(code):
        text = (
            "🔐 Подтверди вход в Яндекс.\n\n"
            f"Ссылка: {code.verification_url}\n"
            f"Код: {code.user_code}\n\n"
            f"Срок действия: около {code.expires_in} секунд.\n"
            "Не пересылай код другим людям."
        )

        future = asyncio.run_coroutine_threadsafe(
            bot.send_message(user_id, text),
            loop,
        )
        future.result(timeout=20)

    def authenticate():
        temporary_client = Client()
        auth_token = temporary_client.device_auth(
            on_code=on_code
        )

        client = Client(auth_token.access_token)
        client.init()

        return auth_token.access_token

    try:
        access_token = await asyncio.to_thread(authenticate)

        path = token_path(user_id)
        path.write_text(
            access_token,
            encoding="utf-8",
        )

        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

        await callback.message.answer(
            "✅ Вход подтверждён. Теперь нажми "
            "«Выгрузить „Мне нравится“».",
            reply_markup=keyboard(),
        )

    except Exception as error:
        logging.exception("Ошибка авторизации")
        await callback.message.answer(
            f"❌ Ошибка авторизации: "
            f"{type(error).__name__}: {error}"
        )

    finally:
        auth_in_progress.discard(user_id)


def get_value(obj, key, default=None):
    if obj is None:
        return default

    if isinstance(obj, dict):
        return obj.get(key, default)

    return getattr(obj, key, default)


def as_list(obj):
    if obj is None:
        return []

    if isinstance(obj, (list, tuple)):
        return list(obj)

    try:
        return list(obj)
    except TypeError:
        return [obj]


def unwrap_track(obj):
    """
    Извлекает трек из обёртки, если API её возвращает.
    """
    current = obj

    for _ in range(4):
        if current is None:
            break

        nested = get_value(current, "track")

        if nested is None or nested is current:
            break

        current = nested

    return current


def get_track_id(track):
    track_id = get_value(track, "id")

    if track_id is None:
        track_id = get_value(track, "track_id")

    if track_id is None:
        return None

    # Иногда ID представлен в формате "ID:ID_альбома".
    return str(track_id).split(":")[0]


def get_artists(track):
    result = []

    for artist in as_list(
        get_value(track, "artists")
    ):
        name = (
            get_value(artist, "name")
            or get_value(artist, "title")
        )

        if name:
            result.append(str(name))

    return result


def get_cover_url(track):
    albums = as_list(
        get_value(track, "albums")
    )

    for album in albums:
        cover = (
            get_value(album, "cover_uri")
            or get_value(album, "coverUri")
        )

        if cover:
            return (
                "https://"
                + str(cover).replace("%%", "400x400")
            )

    cover = (
        get_value(track, "cover_uri")
        or get_value(track, "coverUri")
    )

    if not cover:
        return None

    cover = str(cover)

    if cover.startswith("http://") or cover.startswith("https://"):
        return cover

    return "https://" + cover.replace("%%", "400x400")


def summarize_track(track):
    track = unwrap_track(track)

    return {
        "id": get_track_id(track),
        "title": get_value(track, "title"),
        "artists": get_artists(track),
        "duration_ms": get_value(track, "duration_ms"),
        "cover_url": get_cover_url(track),
    }


def get_liked_items(client):
    response = client.users_likes_tracks()

    items = get_value(response, "tracks")

    if items is None:
        items = response

    return as_list(items)


def get_full_tracks(client, track_ids):
    """
    Запрашивает полные записи треков небольшими партиями.
    """
    found = {}
    errors = []

    for start in range(0, len(track_ids), 50):
        batch = track_ids[start:start + 50]

        try:
            response = client.tracks(batch) or []

            for item in as_list(response):
                track = unwrap_track(item)
                track_id = get_track_id(track)

                if track_id is not None:
                    found[track_id] = track

        except Exception as error:
            logging.exception(
                "Ошибка получения партии треков"
            )

            errors.append({
                "ids": batch,
                "error": type(error).__name__,
                "message": str(error)[:300],
            })

    return found, errors


def create_report(access_token, output_path):
    client = Client(access_token)
    client.init()

    liked_items = get_liked_items(client)

    short_tracks = {}
    track_ids = []

    for item in liked_items:
        track = unwrap_track(item)
        summary = summarize_track(track)
        track_id = summary["id"]

        if not track_id:
            continue

        if track_id not in short_tracks:
            short_tracks[track_id] = track
            track_ids.append(track_id)

    full_tracks, errors = get_full_tracks(
        client,
        track_ids,
    )

    rows = []

    for track_id in track_ids:
        short_info = summarize_track(
            short_tracks[track_id]
        )

        full_track = full_tracks.get(track_id)

        if full_track is not None:
            full_info = summarize_track(full_track)
        else:
            full_info = {}

        # Если полная запись не содержит поля,
        # используем данные из исходного списка лайков.
        row = {
            "id": (
                full_info.get("id")
                or short_info.get("id")
                or track_id
            ),
            "title": (
                full_info.get("title")
                or short_info.get("title")
            ),
            "artists": (
                full_info.get("artists")
                or short_info.get("artists")
                or []
            ),
            "duration_ms": (
                full_info.get("duration_ms")
                or short_info.get("duration_ms")
            ),
            "cover_url": (
                full_info.get("cover_url")
                or short_info.get("cover_url")
            ),
            "full_record_found": (
                full_track is not None
            ),
        }

        rows.append(row)

    metadata_found = sum(
        1
        for row in rows
        if row["title"] or row["artists"]
    )

    report = {
        "source": "Мне нравится",
        "track_count": len(rows),
        "metadata_found_count": metadata_found,
        "metadata_missing_count": (
            len(rows) - metadata_found
        ),
        "tracks": rows,
        "errors": errors,
        "note": (
            "Отчёт содержит метаданные, возвращённые API. "
            "Он не читает локальные загрузки и базу данных "
            "приложения Яндекс Музыки на iPhone. "
            "Недоступность записи через API сама по себе "
            "не доказывает, что трек удалён."
        ),
    }

    with open(
        output_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            report,
            file,
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    return report


@dp.callback_query(F.data == "export_likes")
async def export_likes(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id

    if callback.message.chat.type != "private":
        await callback.message.answer(
            "Используй бота в личном чате."
        )
        return

    if user_id in export_in_progress:
        await callback.message.answer(
            "⏳ Выгрузка уже выполняется."
        )
        return

    path = token_path(user_id)

    if not path.exists():
        await callback.message.answer(
            "Сначала нажми «Войти через Яндекс».",
            reply_markup=keyboard(),
        )
        return

    access_token = path.read_text(
        encoding="utf-8"
    ).strip()

    if not access_token:
        await callback.message.answer(
            "Токен пустой. Войди через Яндекс заново."
        )
        return

    export_in_progress.add(user_id)

    status = await callback.message.answer(
        "⏳ Получаю список лайков и названия треков. "
        "Это может занять несколько минут."
    )

    report_path = (
        DATA_DIR / f"playlist_{user_id}.json"
    )

    try:
        report = await asyncio.to_thread(
            create_report,
            access_token,
            report_path,
        )

        if (
            report["track_count"] > 0
            and report["metadata_found_count"] == 0
        ):
            await status.answer(
                "⚠️ Получены ID треков, но названия "
                "и исполнители не найдены. Отправляю "
                "отчёт для дальнейшей диагностики."
            )

        await bot.send_document(
            chat_id=user_id,
            document=FSInputFile(report_path),
            caption=(
                f"Выгрузка завершена.\n"
                f"Всего треков: {report['track_count']}\n"
                f"Метаданные найдены: "
                f"{report['metadata_found_count']}\n"
                f"Метаданные отсутствуют: "
                f"{report['metadata_missing_count']}"
            ),
        )

        await status.answer(
            "Готово. JSON-файл отправлен выше."
        )

    except Exception as error:
        logging.error(
            "Ошибка выгрузки: %s\n%s",
            type(error).__name__,
            traceback.format_exc(),
        )

        await status.answer(
            f"❌ Ошибка: {type(error).__name__}: {error}"
        )

    finally:
        export_in_progress.discard(user_id)

        try:
            report_path.unlink(missing_ok=True)
        except OSError:
            pass


async def main():
    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await asyncio.gather(
        start_web_server(),
        dp.start_polling(
            bot,
            polling_timeout=30,
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
