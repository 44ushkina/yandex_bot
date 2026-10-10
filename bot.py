
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
search_in_progress = set()


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
                    text="🔎 Проверить треки",
                    callback_data="start_search",
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
        "Проверка треков из «Мне нравится» в Яндекс Музыке.\n\n"
        "1. Нажми «Войти через Яндекс».\n"
        "2. Подтверди авторизацию.\n"
        "3. Нажми «Проверить треки».\n\n"
        "Бот отправит JSON-отчёт. Он не читает загрузки "
        "из памяти iPhone.",
        reply_markup=keyboard(),
    )


@dp.callback_query(F.data == "auth_yandex")
async def auth_yandex(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id

    if callback.message.chat.type != "private":
        await callback.message.answer(
            "Для безопасности используй бота в личном чате."
        )
        return

    if user_id in auth_in_progress:
        await callback.message.answer(
            "Авторизация уже запущена. Дождись её завершения."
        )
        return

    auth_in_progress.add(user_id)
    await callback.message.answer(
        "⏳ Запрашиваю код авторизации у Яндекса..."
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
        future.result(timeout=30)

    def authenticate():
        temporary_client = Client()
        auth_token = temporary_client.device_auth(on_code=on_code)

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
            "✅ Авторизация завершена. Теперь нажми "
            "«Проверить треки».",
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


def value(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def artist_names(track):
    artists = value(track, "artists") or []
    result = []

    for artist in artists:
        name = value(artist, "name") or value(artist, "title")
        if name:
            result.append(str(name))

    return result


def to_plain(obj):
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, (list, tuple)):
        return [to_plain(item) for item in obj]

    if isinstance(obj, dict):
        return {
            str(key): to_plain(item)
            for key, item in obj.items()
        }

    method = getattr(obj, "to_dict", None)
    if callable(method):
        try:
            result = method()
            if isinstance(result, dict):
                return to_plain(result)
        except Exception:
            pass

    return str(obj)


def track_summary(track):
    if track is None:
        return None

    return {
        "id": str(value(track, "id"))
        if value(track, "id") is not None else None,
        "title": value(track, "title"),
        "artists": artist_names(track),
        "available": value(track, "available"),
        "available_for_premium_users": value(
            track, "available_for_premium_users"
        ),
        "error": value(track, "error"),
        "track_source": value(track, "track_source"),
        "real_id": value(track, "real_id"),
        "matched_track": to_plain(value(track, "matched_track")),
        "substituted": to_plain(value(track, "substituted")),
        "albums": to_plain(value(track, "albums")),
        "duration_ms": value(track, "duration_ms"),
    }


def extract_likes(likes):
    items = getattr(likes, "tracks", None)
    if items is None:
        items = likes

    result = []
    seen = set()

    for item in items or []:
        track = getattr(item, "track", None) or item
        track_id = value(track, "id")

        if track_id is None:
            continue

        track_id = str(track_id)

        if track_id in seen:
            continue

        seen.add(track_id)
        result.append({
            "id": track_id,
            "track": track,
        })

    return result


def collect_linked_ids(track):
    found = set()
    if track is None:
        return found

    def walk(obj, depth=0):
        if obj is None or depth > 8:
            return

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                walk(item, depth + 1)
            return

        if isinstance(obj, dict):
            for key, item in obj.items():
                key_lower = str(key).lower()

                if (
                    key_lower in {"id", "track_id", "real_id"}
                    and item is not None
                ):
                    candidate = str(item).strip()
                    if candidate.isdigit():
                        found.add(candidate)

                if isinstance(item, (dict, list, tuple)):
                    walk(item, depth + 1)
            return

        for key in ("id", "track_id", "real_id"):
            candidate = value(obj, key)
            if candidate is not None:
                candidate = str(candidate).strip()
                if candidate.isdigit():
                    found.add(candidate)

        for key in ("matched_track", "substituted"):
            walk(value(obj, key), depth + 1)

    walk(value(track, "matched_track"))
    walk(value(track, "substituted"))

    return found


def fetch_bulk(client, ids, errors, stage):
    result = {}

    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]

        try:
            response = client.tracks(batch) or []

            for track in response:
                track_id = value(track, "id")
                if track_id is not None:
                    result[str(track_id)] = track

        except Exception as error:
            errors.append({
                "stage": stage,
                "ids": batch,
                "error": type(error).__name__,
                "message": str(error)[:400],
            })

    return result


def fetch_single(client, track_id):
    response = client.tracks([str(track_id)]) or []

    for track in response:
        returned_id = value(track, "id")
        if returned_id is not None and str(returned_id) == str(track_id):
            return track

    return response[0] if response else None


def run_diagnostics(access_token, output_path):
    client = Client(access_token)
    client.init()

    likes = client.users_likes_tracks()
    liked = extract_likes(likes)

    if not liked and getattr(client, "me", None):
        likes = client.users_likes_tracks(client.me.account.uid)
        liked = extract_likes(likes)

    liked_by_id = {item["id"]: item for item in liked}
    liked_ids = list(liked_by_id)

    errors = []
    full_tracks = fetch_bulk(
        client, liked_ids, errors, "bulk_liked_tracks"
    )

    single_results = {}

    for track_id in liked_ids:
        if track_id in full_tracks:
            continue

        try:
            track = fetch_single(client, track_id)

            if track is not None:
                full_tracks[track_id] = track
                single_results[track_id] = "returned"
            else:
                single_results[track_id] = "not_returned"

        except Exception as error:
            single_results[track_id] = "request_error"
            errors.append({
                "stage": "single_liked_track",
                "id": track_id,
                "error": type(error).__name__,
                "message": str(error)[:400],
            })

    linked_ids = set()

    for item in liked_by_id.values():
        linked_ids.update(collect_linked_ids(item["track"]))

    for track in full_tracks.values():
        linked_ids.update(collect_linked_ids(track))

    linked_ids.difference_update(liked_ids)

    linked_tracks = {}
    pending = set(linked_ids)

    for depth in range(2):
        batch_ids = sorted(pending - set(linked_tracks))
        if not batch_ids:
            break

        fetched = fetch_bulk(
            client,
            batch_ids,
            errors,
            f"linked_tracks_pass_{depth + 1}",
        )
        linked_tracks.update(fetched)

        missing_ids = [
            track_id for track_id in batch_ids
            if track_id not in fetched
        ]

        for track_id in missing_ids:
            try:
                track = fetch_single(client, track_id)
                if track is not None:
                    linked_tracks[track_id] = track

            except Exception as error:
                errors.append({
                    "stage": "single_linked_track",
                    "id": track_id,
                    "error": type(error).__name__,
                    "message": str(error)[:400],
                })

        new_ids = set()

        for track in linked_tracks.values():
            new_ids.update(collect_linked_ids(track))

        new_ids.difference_update(liked_ids)
        new_ids.difference_update(linked_tracks.keys())
        pending = new_ids
        linked_ids.update(new_ids)

    liked_rows = []
    candidates = []

    for track_id, item in liked_by_id.items():
        short = item["track"]
        full = full_tracks.get(track_id)

        liked_rows.append({
            "id": track_id,
            "short_record": track_summary(short),
            "full_record": track_summary(full),
            "full_record_returned": full is not None,
            "single_request_result": single_results.get(track_id),
            "linked_ids": sorted(
                collect_linked_ids(short)
                | collect_linked_ids(full)
            ),
        })

        if full is None:
            candidates.append({
                "id": track_id,
                "title": value(short, "title"),
                "artists": artist_names(short),
                "status": "NOT_RETURNED",
                "explanation": (
                    "Полная запись не получена. Это может быть "
                    "ошибка API, а не удаление."
                ),
            })

        elif value(full, "available") is False:
            candidates.append({
                "id": track_id,
                "title": value(full, "title"),
                "artists": artist_names(full),
                "status": "AVAILABLE_FALSE",
                "explanation": (
                    "API сообщает available=false. Причина "
                    "может быть связана с регионом, правами "
                    "или состоянием каталога."
                ),
            })

        elif value(full, "error"):
            candidates.append({
                "id": track_id,
                "title": value(full, "title"),
                "artists": artist_names(full),
                "status": "API_ERROR_FIELD",
                "explanation": (
                    "У записи заполнено поле error; требуется "
                    "проверка."
                ),
            })

    linked_rows = []

    for track_id in sorted(linked_ids):
        track = linked_tracks.get(track_id)

        linked_rows.append({
            "id": track_id,
            "returned": track is not None,
            "record": track_summary(track),
        })

        if track is None:
            candidates.append({
                "id": track_id,
                "title": None,
                "artists": [],
                "status": "LINKED_NOT_RETURNED",
                "explanation": (
                    "Связанный ID не вернулся из API. "
                    "Это не доказывает удаление."
                ),
            })

        elif value(track, "available") is False:
            candidates.append({
                "id": track_id,
                "title": value(track, "title"),
                "artists": artist_names(track),
                "status": "LINKED_AVAILABLE_FALSE",
                "explanation": (
                    "Связанная запись имеет available=false; "
                    "нужно проверить причину."
                ),
            })

    report = {
        "report_type": "yandex_music_track_availability_check",
        "warning": (
            "Отчёт не читает локальную базу приложения "
            "на iPhone. Он не может обнаружить треки, "
            "полностью отсутствующие в «Мне нравится» "
            "и в связанных записях API. Статус кандидата "
            "не доказывает удаление."
        ),
        "summary": {
            "liked_tracks_count": len(liked_ids),
            "full_records_returned": len(full_tracks),
            "liked_records_not_returned": sum(
                1 for track_id in liked_ids
                if track_id not in full_tracks
            ),
            "linked_ids_found": len(linked_ids),
            "linked_records_returned": len(linked_tracks),
            "candidates_for_review": len(candidates),
            "errors_count": len(errors),
        },
        "candidates_for_review": candidates,
        "liked_tracks": liked_rows,
        "linked_catalog_records": linked_rows,
        "errors": errors,
    }

    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(
            report,
            file,
            ensure_ascii=False,
            indent=2,
            default=str,
        )


@dp.callback_query(F.data == "start_search")
async def start_search(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id

    if callback.message.chat.type != "private":
        await callback.message.answer(
            "Используй бота в личном чате."
        )
        return

    if user_id in search_in_progress:
        await callback.message.answer(
            "⏳ Проверка уже выполняется."
        )
        return

    path = token_path(user_id)

    if not path.exists():
        await callback.message.answer(
            "Сначала нажми «Войти через Яндекс».",
            reply_markup=keyboard(),
        )
        return

    access_token = path.read_text(encoding="utf-8").strip()

    if not access_token:
        await callback.message.answer(
            "Токен пустой. Войди через Яндекс заново."
        )
        return

    search_in_progress.add(user_id)

    status = await callback.message.answer(
        "⏳ Проверяю лайки и связанные записи каталога. "
        "Это может занять несколько минут."
    )

    report_path = DATA_DIR / f"diagnostic_{user_id}.json"

    try:
        await asyncio.to_thread(
            run_diagnostics,
            access_token,
            report_path,
        )

        await bot.send_document(
            chat_id=user_id,
            document=FSInputFile(report_path),
            caption=(
                "✅ Проверка завершена. В JSON есть список "
                "кандидатов и подробные записи для проверки.\n\n"
                "Это не окончательный список удалённых треков: "
                "недоступность через API не всегда означает удаление."
            ),
        )

        report_path.unlink(missing_ok=True)
        await status.answer("Готово. Отчёт отправлен.")

    except Exception as error:
        logging.error(
            "Ошибка диагностики: %s\n%s",
            type(error).__name__,
            traceback.format_exc(),
        )
        await status.answer(
            f"❌ Ошибка: {type(error).__name__}: {error}"
        )

    finally:
        search_in_progress.discard(user_id)


async def main():
    await bot.delete_webhook(drop_pending_updates=True)

    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot, polling_timeout=30),
    )


if __name__ == "__main__":
    asyncio.run(main())
