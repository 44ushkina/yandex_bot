import asyncio
import json
import logging
import os
import traceback
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
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
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Войти через Яндекс", callback_data="auth_yandex")],
        [InlineKeyboardButton(text="🔎 Проверить треки", callback_data="start_search")],
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
        "Проверка доступности треков в Яндекс Музыке.\n\n"
        "Нажми «Войти через Яндекс», затем «Проверить треки».\n"
        "Отчёт проверяет лайки и связанные с ними записи каталога.",
        reply_markup=keyboard(),
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
        asyncio.run_coroutine_threadsafe(bot.send_message(user_id, text), loop).result(timeout=20)

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
        await callback.message.answer("✅ Вход подтверждён. Теперь нажми «Проверить треки».", reply_markup=keyboard())
    except Exception as error:
        logging.exception("Ошибка авторизации")
        await callback.message.answer(f"❌ Ошибка авторизации: {type(error).__name__}: {error}")
    finally:
        auth_in_progress.discard(user_id)


def value(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def to_plain(obj):
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [to_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    method = getattr(obj, "to_dict", None)
    if callable(method):
        try:
            result = method()
            if isinstance(result, dict):
                return to_plain(result)
        except Exception:
            pass
    return {"_repr": str(obj)}


def artist_names(track):
    artists = value(track, "artists") or []
    names = []
    for artist in artists:
        name = value(artist, "name") or value(artist, "title")
        if name:
            names.append(str(name))
    return names


def track_summary(track):
    if track is None:
        return None
    return {
        "id": str(value(track, "id")) if value(track, "id") is not None else None,
        "title": value(track, "title"),
        "artists": artist_names(track),
        "available": value(track, "available"),
        "error": value(track, "error"),
        "track_source": value(track, "track_source"),
        "real_id": value(track, "real_id"),
        "matched_track": to_plain(value(track, "matched_track")),
        "substituted": to_plain(value(track, "substituted")),
        "albums": to_plain(value(track, "albums")),
        "duration_ms": value(track, "duration_ms"),
        "storage_dir": value(track, "storage_dir"),
        "filename": value(track, "filename"),
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
        result.append({"id": track_id, "short": track, "like_object": item})
    return result


def collect_ids_from_link_fields(track):
    """Извлекает ID из matched_track и substituted, в том числе из списков/словарей."""
    found = set()
    if track is None:
        return found

    def walk(obj, key_hint=None, depth=0):
        if obj is None or depth > 8:
            return
        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                walk(item, key_hint, depth + 1)
            return
        if isinstance(obj, dict):
            for key, item in obj.items():
                key_lower = str(key).lower()
                if key_lower in {"id", "track_id", "real_id"} and item is not None:
                    candidate = str(item).strip()
                    if candidate.isdigit():
                        found.add(candidate)
                elif key_lower in {"matched_track", "substituted", "track", "tracks", "track_id", "id"}:
                    walk(item, key_lower, depth + 1)
                elif isinstance(item, (dict, list, tuple)):
                    walk(item, key_lower, depth + 1)
            return
        if isinstance(obj, (str, int)):
            candidate = str(obj).strip()
            if key_hint in {"matched_track", "substituted", "track_id", "id"} and candidate.isdigit():
                found.add(candidate)
            return
        for key in ("id", "track_id", "real_id"):
            candidate = value(obj, key)
            if candidate is not None and str(candidate).strip().isdigit():
                found.add(str(candidate).strip())
        for key in ("matched_track", "substituted"):
            walk(value(obj, key), key, depth + 1)

    walk(value(track, "matched_track"), "matched_track")
    walk(value(track, "substituted"), "substituted")
    return found


def fetch_bulk(client, ids, errors, stage):
    """Возвращает полученные полные записи; ошибки записывает в отчёт."""
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
            errors.append({"stage": stage, "ids": batch, "error": type(error).__name__, "message": str(error)[:500]})
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
    full_tracks = fetch_bulk(client, liked_ids, errors, "bulk_liked_tracks")

    # Если пакетный запрос пропустил лайк, пробуем запросить его отдельно.
    single_fallback = {}
    for track_id in liked_ids:
        if track_id in full_tracks:
            continue
        try:
            track = fetch_single(client, track_id)
            if track is not None:
                full_tracks[track_id] = track
                single_fallback[track_id] = "returned_on_single_request"
            else:
                single_fallback[track_id] = "not_returned_even_individually"
        except Exception as error:
            single_fallback[track_id] = "single_request_error"
            errors.append({"stage": "single_liked_track", "id": track_id, "error": type(error).__name__, "message": str(error)[:500]})

    # Собираем все связанные ID и проверяем их отдельно от лайков.
    linked_ids = set()
    for track_id, item in liked_by_id.items():
        linked_ids.update(collect_ids_from_link_fields(item["short"]))
        linked_ids.update(collect_ids_from_link_fields(full_tracks.get(track_id)))
    linked_ids.difference_update(liked_ids)

    linked_tracks = {}
    # Два прохода: первоначально найденные связи, затем связи в найденных записях.
    pending = set(linked_ids)
    for depth in range(2):
        batch_ids = sorted(pending - set(linked_tracks))
        if not batch_ids:
            break
        got = fetch_bulk(client, batch_ids, errors, f"linked_bulk_depth_{depth + 1}")
        linked_tracks.update(got)
        missing = [x for x in batch_ids if x not in got]
        for track_id in missing:
            try:
                track = fetch_single(client, track_id)
                if track is not None:
                    linked_tracks[track_id] = track
            except Exception as error:
                errors.append({"stage": "single_linked_track", "id": track_id, "error": type(error).__name__, "message": str(error)[:500]})
        new_ids = set()
        for track in linked_tracks.values():
            new_ids.update(collect_ids_from_link_fields(track))
        new_ids.difference_update(liked_ids)
        new_ids.difference_update(linked_tracks.keys())
        pending = new_ids
        linked_ids.update(new_ids)

    liked_rows = []
    candidates = []
    for track_id, item in liked_by_id.items():
        short = item["short"]
        full = full_tracks.get(track_id)
        info = track_summary(full) if full is not None else None
        row = {
            "liked_id": track_id,
            "title_from_liked_entry": value(short, "title"),
            "artists_from_liked_entry": artist_names(short),
            "short_record": track_summary(short),
            "full_record": info,
            "full_record_returned": full is not None,
            "single_fallback_result": single_fallback.get(track_id),
            "linked_ids": sorted(collect_ids_from_link_fields(short) | collect_ids_from_link_fields(full)),
        }
        liked_rows.append(row)
        if full is None or value(full, "available") is False or value(full, "error"):
            candidates.append({
                "source": "liked_track",
                "id": track_id,
                "title": value(full, "title") if full is not None else value(short, "title"),
                "artists": artist_names(full if full is not None else short),
                "available": value(full, "available") if full is not None else None,
                "error": value(full, "error") if full is not None else None,
                "track_source": value(full, "track_source") if full is not None else value(short, "track_source"),
                "reason": "not_returned" if full is None else ("available_false" if value(full, "available") is False else "api_error_field"),
            })

    linked_rows = []
    for track_id in sorted(linked_ids):
        track = linked_tracks.get(track_id)
        summary = track_summary(track) if track is not None else None
        linked_rows.append({"id": track_id, "returned": track is not None, "record": summary})
        if track is None or value(track, "available") is False or value(track, "error"):
            candidates.append({
                "source": "linked_catalog_record",
                "id": track_id,
                "title": value(track, "title") if track is not None else None,
                "artists": artist_names(track) if track is not None else [],
                "available": value(track, "available") if track is not None else None,
                "error": value(track, "error") if track is not None else None,
                "track_source": value(track, "track_source") if track is not None else None,
                "reason": "not_returned" if track is None else ("available_false" if value(track, "available") is False else "api_error_field"),
            })

    report = {
        "report_type": "yandex_music_expanded_diagnostic",
        "warning": (
            "Это проверка только лайков и записей, связанных с ними в ответах API. "
            "Она НЕ может найти треки, полностью отсутствующие в списке лайков, "
            "и не читает локальные загрузки/базу приложения на iPhone. "
            "Недоступность через API не всегда доказывает удаление."
        ),
        "summary": {
            "liked_tracks_count": len(liked_ids),
            "full_records_returned_after_fallback": len(full_tracks),
            "liked_records_not_returned": sum(1 for x in liked_ids if x not in full_tracks),
            "linked_catalog_ids_found": len(linked_ids),
            "linked_records_returned": len(linked_tracks),
            "candidate_records": len(candidates),
            "errors_count": len(errors),
        },
        "candidates_for_review": candidates,
        "liked_tracks": liked_rows,
        "linked_catalog_records": linked_rows,
        "errors": errors,
    }
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2, default=str)


@dp.callback_query(F.data == "start_search")
async def start_search(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    if callback.message.chat.type != "private":
        await callback.message.answer("Используй бота в личном чате.")
        return
    if user_id in search_in_progress:
        await callback.message.answer("⏳ Проверка уже выполняется.")
        return

    path = token_path(user_id)
    if not path.exists():
        await callback.message.answer("Сначала нажми «Войти через Яндекс».", reply_markup=keyboard())
        return
    access_token = path.read_text(encoding="utf-8").strip()
    if not access_token:
        await callback.message.answer("Токен пустой. Войди через Яндекс заново.")
        return

    search_in_progress.add(user_id)
    status = await callback.message.answer(
        "⏳ Проверяю лайки, отдельно перепроверяю пропущенные записи и связанные ID.\n"
        "Это может занять несколько минут."
    )
    report_path = DATA_DIR / f"diagnostic_{user_id}.json"
    try:
        await asyncio.to_thread(run_diagnostics, access_token, report_path)
        await bot.send_document(
            chat_id=user_id,
            document=FSInputFile(report_path),
            caption=(
                "✅ Проверка завершена. Файл содержит расширенный отчёт.\n"
                "Важно: он не может видеть локальные загрузки iPhone или треки, полностью отсутствующие в лайках."
            ),
        )
        report_path.unlink(missing_ok=True)
        await status.answer("Готово. Отчёт отправлен отдельным сообщением.")
    except Exception as error:
        logging.error("Ошибка диагностики: %s\n%s", type(error).__name__, traceback.format_exc())
        await status.answer(f"❌ Ошибка: {type(error).__name__}: {error}")
    finally:
        search_in_progress.discard(user_id)


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(start_web_server(), dp.start_polling(bot, polling_timeout=30))


if __name__ == "__main__":
    asyncio.run(main())
