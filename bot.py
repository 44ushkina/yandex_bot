
import asyncio
import logging
import os
import time
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# Диагностика: библиотека будет сообщать в Render Logs о новых полях API.
logging.getLogger("yandex_music").setLevel(logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DATA_DIR = Path("/tmp/yandex_music_bot")
DATA_DIR.mkdir(parents=True, exist_ok=True)

auth_in_progress = set()
search_in_progress = set()


def make_yandex_client(token=None):
    """Создаёт клиент с диагностикой неизвестных полей API, если версия поддерживает её."""
    try:
        return Client(token, report_unknown_fields=True)
    except TypeError:
        # Совместимость со старой версией yandex-music.
        logging.warning(
            "Установленная версия yandex-music не поддерживает "
            "report_unknown_fields; диагностика неизвестных полей отключена."
        )
        return Client(token)


def token_path(user_id):
    return DATA_DIR / f"token_{user_id}.txt"


def get_keyboard():
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
                    text="🎵 Найти недоступные треки",
                    callback_data="start_search",
                )
            ],
        ]
    )


async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def start_web_server():
    app = web.Application()
    app.add_routes([web.get("/", handle_ping)])

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logging.info("Web server started on port %s", port)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я помогу проверить треки из «Мне нравится».\n\n"
        "Сначала войди через Яндекс, затем запусти проверку.",
        reply_markup=get_keyboard(),
    )


@dp.callback_query(F.data == "auth_yandex")
async def auth_yandex(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id

    if callback.message.chat.type != "private":
        await callback.message.answer(
            "Для безопасности запусти бота в личном чате."
        )
        return

    if user_id in auth_in_progress:
        await callback.message.answer(
            "Авторизация уже запущена. Проверь предыдущие сообщения."
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
            f"1. Открой ссылку: {code.verification_url}\n"
            f"2. Введи код: {code.user_code}\n\n"
            f"Код действует примерно {code.expires_in} секунд.\n"
            "Не пересылай этот код другим людям."
        )

        future = asyncio.run_coroutine_threadsafe(
            bot.send_message(user_id, text),
            loop,
        )
        future.result(timeout=20)

    def authenticate():
        client = make_yandex_client()
        token = client.device_auth(on_code=on_code)

        client = make_yandex_client(token.access_token)
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
            "✅ Вход подтверждён!\n"
            "Теперь нажми «Найти недоступные треки».",
            reply_markup=get_keyboard(),
        )

    except Exception as error:
        logging.error(
            "Ошибка авторизации: %s\n%s",
            type(error).__name__,
            traceback.format_exc(),
        )

        await callback.message.answer(
            "❌ Не получилось завершить вход: "
            f"{type(error).__name__}: {error}\n\n"
            "Попробуй ещё раз немного позже."
        )

    finally:
        auth_in_progress.discard(user_id)


def extract_short_tracks(likes):
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
        result.append((track_id, track))

    return result


def format_track(track, track_id):
    artists = getattr(track, "artists", None) or []

    artist_names = ", ".join(
        artist.name
        for artist in artists
        if getattr(artist, "name", None)
    ) or "Неизвестный исполнитель"

    title = getattr(track, "title", None) or "Без названия"

    version = getattr(track, "version", None)
    if version:
        title += f" ({version})"

    return f"{artist_names} — {title} [ID: {track_id}]"


def value_text(value):
    if value is None:
        return "null"
    return str(value)


def describe_api_flags(track, track_id):
    """Return diagnostic flags only; these flags do not alone prove removal."""
    flags = [
        f"available={value_text(getattr(track, 'available', None))}",
        f"available_for_premium_users={value_text(getattr(track, 'available_for_premium_users', None))}",
        f"available_full_without_permission={value_text(getattr(track, 'available_full_without_permission', None))}",
    ]

    albums = getattr(track, "albums", None) or []
    album_flags = []
    for album in albums[:5]:
        album_flags.append(
            "album["
            + str(getattr(album, "id", "?"))
            + "] available="
            + value_text(getattr(album, "available", None))
            + ",partially="
            + value_text(getattr(album, "available_partially", None))
            + ",mobile="
            + value_text(getattr(album, "available_for_mobile", None))
        )

    if album_flags:
        flags.extend(album_flags)

    substituted = getattr(track, "substituted", None)
    matched = getattr(track, "matched_track", None)
    if substituted is not None:
        flags.append(f"substituted={getattr(substituted, 'id', '?')}")
    if matched is not None:
        flags.append(f"matched_track={getattr(matched, 'id', '?')}")

    return f"{format_track(track, track_id)} | " + " | ".join(flags)


def check_tracks(access_token):
    client = make_yandex_client(access_token)
    client.init()

    likes = client.users_likes_tracks()
    short_tracks = extract_short_tracks(likes)

    if not short_tracks and getattr(client, "me", None):
        uid = client.me.account.uid
        likes = client.users_likes_tracks(uid)
        short_tracks = extract_short_tracks(likes)

    if not short_tracks:
        return {
            "total": 0,
            "checked": 0,
            "unavailable": [],
            "unresolved": [],
            "diagnostics": [],
            "counts": {},
        }

    short_by_id = dict(short_tracks)
    ids = list(short_by_id.keys())

    unavailable = []
    unresolved = []
    diagnostics = []
    checked_ids = set()
    counts = {
        "available_true": 0,
        "available_false": 0,
        "available_null": 0,
        "premium_true": 0,
        "premium_false": 0,
        "premium_null": 0,
        "album_unavailable_or_partial": 0,
        "has_substitution_or_match": 0,
    }
    batch_size = 100

    for start in range(0, len(ids), batch_size):
        batch = ids[start:start + batch_size]

        try:
            tracks = client.tracks(batch) or []
        except Exception as error:
            logging.warning(
                "Ошибка при проверке пачки %s-%s: %s",
                start + 1, start + len(batch), type(error).__name__,
            )
            unresolved.extend(
                format_track(short_by_id[track_id], track_id)
                + f" [ошибка пачки: {type(error).__name__}]"
                for track_id in batch
            )
            continue

        found = set()

        for track in tracks:
            if track is None:
                continue

            track_id = getattr(track, "id", None)
            if track_id is None:
                continue

            track_id = str(track_id)
            found.add(track_id)
            checked_ids.add(track_id)

            available = getattr(track, "available", None)
            premium = getattr(track, "available_for_premium_users", None)

            if available is True:
                counts["available_true"] += 1
            elif available is False:
                counts["available_false"] += 1
            else:
                counts["available_null"] += 1

            if premium is True:
                counts["premium_true"] += 1
            elif premium is False:
                counts["premium_false"] += 1
            else:
                counts["premium_null"] += 1

            albums = getattr(track, "albums", None) or []
            album_issue = any(
                getattr(album, "available", None) is False
                or getattr(album, "available_partially", None) is True
                for album in albums
            )
            if album_issue:
                counts["album_unavailable_or_partial"] += 1

            if (
                getattr(track, "substituted", None) is not None
                or getattr(track, "matched_track", None) is not None
            ):
                counts["has_substitution_or_match"] += 1

            # Keep a diagnostic record for each track so we can inspect
            # what the API actually says, without assuming a flag proves deletion.
            diagnostics.append(describe_api_flags(track, track_id))

            if available is False:
                unavailable.append(format_track(track, track_id))
            elif available is None:
                unresolved.append(
                    format_track(track, track_id)
                    + " [available отсутствует в ответе API]"
                )

            # If API marks the track available but the album flags look unusual,
            # list it for review, not as a confirmed unavailable track.
            if available is True and album_issue:
                unresolved.append(
                    format_track(track, track_id)
                    + " [проверить флаги альбома; это не подтверждает удаление]"
                )

        for track_id in batch:
            if track_id not in found:
                unresolved.append(
                    format_track(short_by_id[track_id], track_id)
                    + " [API не вернул трек]"
                )

    unavailable = list(dict.fromkeys(unavailable))
    unresolved = list(dict.fromkeys(unresolved))
    diagnostics = list(dict.fromkeys(diagnostics))

    return {
        "total": len(ids),
        "checked": len(checked_ids),
        "unavailable": unavailable,
        "unresolved": unresolved,
        "diagnostics": diagnostics,
        "counts": counts,
    }


@dp.callback_query(F.data == "start_search")
async def start_search(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id

    if callback.message.chat.type != "private":
        await callback.message.answer(
            "Для безопасности используй бота в личном чате."
        )
        return

    if user_id in search_in_progress:
        await callback.message.answer(
            "⏳ Проверка уже идёт. Дождись отчёта."
        )
        return

    path = token_path(user_id)

    if not path.exists():
        await callback.message.answer(
            "Сначала нажми «Войти через Яндекс».",
            reply_markup=get_keyboard(),
        )
        return

    access_token = path.read_text(encoding="utf-8").strip()

    if not access_token:
        await callback.message.answer(
            "Токен не найден. Войди через Яндекс заново."
        )
        return

    search_in_progress.add(user_id)

    status = await callback.message.answer(
        "⏳ Загружаю «Мне нравится» и проверяю треки.\n"
        "Большая коллекция может проверяться несколько минут."
    )

    try:
        result = await asyncio.to_thread(check_tracks, access_token)
        total = result["total"]
        checked = result["checked"]
        unavailable = result["unavailable"]
        unresolved = result["unresolved"]
        diagnostics = result["diagnostics"]
        counts = result["counts"]

        report_path = DATA_DIR / f"report_{user_id}.txt"

        with report_path.open("w", encoding="utf-8") as file:
            file.write("ОТЧЁТ ДИАГНОСТИКИ ЯНДЕКС МУЗЫКИ\n\n")
            file.write(f"Уникальных треков в списке: {total}\n")
            file.write(f"Треков, полученных через API: {checked}\n")
            file.write(f"API явно пометил недоступными: {len(unavailable)}\n")
            file.write(f"Неоднозначных/подозрительных записей: {len(unresolved)}\n\n")

            file.write("СВОДКА ПОЛЕЙ API:\n")
            for key, value in counts.items():
                file.write(f"{key}: {value}\n")

            file.write("НЕДОСТУПНЫЕ ПО ДАННЫМ API:\n")
            file.write("\n".join(unavailable) if unavailable else "Не обнаружены")

            file.write("\n\nНЕОДНОЗНАЧНЫЕ ИЛИ ПОДОЗРИТЕЛЬНЫЕ ЗАПИСИ:\n")
            file.write("\n".join(unresolved) if unresolved else "Нет")

            file.write("\n\nДИАГНОСТИЧЕСКИЕ ФЛАГИ ДЛЯ ВСЕХ ТРЕКОВ:\n")
            file.write("\n".join(diagnostics) if diagnostics else "Нет данных")

            file.write(
                "\n\nВажно: диагностические флаги альбома/подстановки "
                "не доказывают удаление трека; они нужны для дальнейшего анализа."
            )

        await bot.send_document(
            chat_id=user_id,
            document=FSInputFile(report_path),
            caption=(
                "✅ Диагностика завершена.\n"
                f"Всего: {total}; получено API: {checked}.\n"
                f"Явно недоступных: {len(unavailable)}; "
                f"неоднозначных/подозрительных: {len(unresolved)}.\n"
                "В отчёте есть сводка дополнительных полей API."
            ),
        )

        report_path.unlink(missing_ok=True)

    except Exception as error:
        logging.error(
            "Ошибка проверки: %s\n%s",
            type(error).__name__,
            traceback.format_exc(),
        )

        await status.answer(
            f"❌ Ошибка проверки: {type(error).__name__}: {error}"
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
