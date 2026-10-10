
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
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, FSInputFile,
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
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔐 Войти через Яндекс",
            callback_data="auth_yandex",
        )],
        [InlineKeyboardButton(
            text="🔬 Собрать диагностический отчёт",
            callback_data="start_search",
        )],
    ])


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="Bot is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info("Web server started on port %s", port)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Диагностический режим Яндекс Музыки.\n\n"
        "Сначала войди через Яндекс, затем собери отчёт.",
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
        asyncio.run_coroutine_threadsafe(
            bot.send_message(user_id, text), loop
        ).result(timeout=20)

    def authenticate():
        client = Client()
        token = client.device_auth(on_code=on_code)
        client = Client(token.access_token)
        client.init()
        return token.access_token

    try:
        access_token = await asyncio.to_thread(authenticate)
        path = token_path(user_id)
        path.write_text(access_token, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        await callback.message.answer(
            "✅ Вход подтверждён. Теперь запусти диагностику.",
            reply_markup=keyboard(),
        )
    except Exception as error:
        logging.exception("Ошибка авторизации")
        await callback.message.answer(
            f"❌ Ошибка авторизации: {type(error).__name__}: {error}"
        )
    finally:
        auth_in_progress.discard(user_id)


def as_dict(obj):
    """Преобразует объект библиотеки в словарь, если это возможно."""
    if obj is None:
        return None
    method = getattr(obj, "to_dict", None)
    if callable(method):
        try:
            result = method()
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    if isinstance(obj, dict):
        return obj
    return {"_repr": repr(obj)}


def extract_likes(likes):
    items = getattr(likes, "tracks", None)
    if items is None:
        items = likes

    result = []
    seen = set()

    for item in items or []:
        track = getattr(item, "track", None) or item
        track_id = getattr(track, "id", None)
        if track_id is None:
            continue
        track_id = str(track_id)
        if track_id in seen:
            continue
        seen.add(track_id)
        result.append({
            "id": track_id,
            "short_object": track,
            "like_object": item,
        })
    return result


def get_value(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def inspect_record(obj):
    data = as_dict(obj)
    if not isinstance(data, dict):
        data = {"_value": str(data)}

    # Дополнительно фиксируем важные поля напрямую:
    # они могут отсутствовать в сериализованном словаре.
    fields = (
        "id", "title", "available", "available_for_premium_users",
        "available_full_without_permission", "matched_track",
        "substituted", "albums", "artists", "duration_ms",
        "storage_dir", "type", "content_warning",
    )
    extra = {}
    for key in fields:
        try:
            value = get_value(obj, key)
            if value is not None:
                extra[key] = as_dict(value) if not isinstance(
                    value, (str, int, float, bool, list, dict)
                ) else value
        except Exception:
            pass

    return {"data": data, "important_fields": extra}


def extract_linked_id(obj):
    value = get_value(obj, "matched_track")
    if value is None:
        value = get_value(obj, "substituted")

    if value is None:
        return None

    if isinstance(value, (str, int)):
        candidate = str(value).strip()
    elif isinstance(value, dict):
        candidate = value.get("id") or value.get("track_id")
        if candidate is None:
            return None
        candidate = str(candidate).strip()
    else:
        candidate = getattr(value, "id", None)
        if candidate is None:
            candidate = getattr(value, "track_id", None)
        if candidate is None:
            return None
        candidate = str(candidate).strip()

    return candidate if candidate and candidate != "None" else None


def run_diagnostics(access_token, output_path):
    client = Client(access_token)
    client.init()

    likes = client.users_likes_tracks()
    liked = extract_likes(likes)

    if not liked and getattr(client, "me", None):
        likes = client.users_likes_tracks(client.me.account.uid)
        liked = extract_likes(likes)

    records = {item["id"]: item for item in liked}
    ids = list(records)
    full_tracks = {}
    errors = []
    checked_ids = set()

    # Запрашиваем полные объекты пачками.
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        try:
            response = client.tracks(batch) or []
        except Exception as error:
            errors.append({
                "stage": "bulk_tracks",
                "ids": batch,
                "error": type(error).__name__,
                "message": str(error),
            })
            continue

        for track in response:
            track_id = get_value(track, "id")
            if track_id is None:
                continue
            track_id = str(track_id)
            full_tracks[track_id] = track
            checked_ids.add(track_id)

    # Записываем данные всех лайков, включая краткую запись и полную,
    # если API её вернул.
    output_records = []
    linked_ids = set()
    unavailable_ids = []

    for track_id, item in records.items():
        short_obj = item["short_object"]
        full_obj = full_tracks.get(track_id)
        short_info = inspect_record(short_obj)
        full_info = inspect_record(full_obj) if full_obj else None

        available = get_value(full_obj, "available") if full_obj else None
        if available is False:
            unavailable_ids.append(track_id)

        linked_id = extract_linked_id(full_obj or short_obj)
        if linked_id and linked_id != track_id:
            linked_ids.add(linked_id)

        output_records.append({
            "liked_track_id": track_id,
            "liked_track_short": short_info,
            "liked_track_full": full_info,
            "api_returned_full_track": full_obj is not None,
            "available": available,
            "matched_or_substituted_id": linked_id,
        })

    # Повторно запрашиваем недоступные ID по одному.
    # Это может выявить различия между пакетным и одиночным ответом.
    individual_checks = []
    for track_id in unavailable_ids:
        item = records[track_id]
        result = {
            "id": track_id,
            "liked_track": inspect_record(item["short_object"]),
        }
        try:
            response = client.tracks([track_id]) or []
            if response:
                track = response[0]
                result["single_request"] = inspect_record(track)
                result["single_request_available"] = get_value(track, "available")
            else:
                result["single_request"] = None
                result["single_request_available"] = None
                result["note"] = "API не вернул запись при одиночном запросе"
        except Exception as error:
            result["error"] = type(error).__name__
            result["message"] = str(error)
        individual_checks.append(result)

    # Отдельно проверяем ID, связанные через matched_track/substituted.
    linked_checks = []
    linked_list = sorted(linked_ids)
    for start in range(0, len(linked_list), 100):
        batch = linked_list[start:start + 100]
        try:
            response = client.tracks(batch) or []
            returned = {str(get_value(track, "id")): track for track in response
                        if get_value(track, "id") is not None}
            for linked_id in batch:
                track = returned.get(linked_id)
                linked_checks.append({
                    "id": linked_id,
                    "returned": track is not None,
                    "record": inspect_record(track) if track is not None else None,
                    "available": get_value(track, "available") if track is not None else None,
                })
        except Exception as error:
            errors.append({
                "stage": "linked_tracks",
                "ids": batch,
                "error": type(error).__name__,
                "message": str(error),
            })

    report = {
        "report_type": "yandex_music_diagnostic",
        "warning": (
            "available=False не доказывает удаление. "
            "Данные показывают только то, что вернул API на момент проверки."
        ),
        "summary": {
            "unique_liked_tracks": len(ids),
            "full_tracks_returned": len(checked_ids),
            "available_false": len(unavailable_ids),
            "unavailable_ids": unavailable_ids,
            "linked_ids_found": len(linked_ids),
            "linked_ids": sorted(linked_ids),
            "errors_count": len(errors),
        },
        "tracks": output_records,
        "individual_checks_for_unavailable": individual_checks,
        "checks_for_linked_ids": linked_checks,
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
        await callback.message.answer("⏳ Диагностика уже выполняется.")
        return

    path = token_path(user_id)
    if not path.exists():
        await callback.message.answer(
            "Сначала нажми «Войти через Яндекс».", reply_markup=keyboard()
        )
        return

    access_token = path.read_text(encoding="utf-8").strip()
    if not access_token:
        await callback.message.answer("Токен пустой. Войди через Яндекс заново.")
        return

    search_in_progress.add(user_id)
    status = await callback.message.answer(
        "⏳ Собираю подробные данные по лайкам.\n"
        "Не закрывай чат; большая коллекция может проверяться несколько минут."
    )

    report_path = DATA_DIR / f"diagnostic_{user_id}.json"
    try:
        await asyncio.to_thread(run_diagnostics, access_token, report_path)

        await bot.send_document(
            chat_id=user_id,
            document=FSInputFile(report_path),
            caption=(
                "✅ Диагностика завершена.\n"
                "Файл содержит данные по всем лайкам, ответы API "
                "и отдельные проверки недоступных и связанных ID.\n"
                "Это диагностический отчёт, а не готовый список удалённых треков."
            ),
        )
        report_path.unlink(missing_ok=True)
    except Exception as error:
        logging.error(
            "Ошибка диагностики: %s\n%s",
            type(error).__name__, traceback.format_exc()
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
