
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

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DATA_DIR = Path("/tmp/yandex_music_bot")
DATA_DIR.mkdir(parents=True, exist_ok=True)

auth_in_progress = set()
search_in_progress = set()


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


def check_tracks(access_token):
    client = Client(access_token)
    client.init()

    likes = client.users_likes_tracks()
    short_tracks = extract_short_tracks(likes)

    if not short_tracks and getattr(client, "me", None):
        uid = client.me.account.uid
        likes = client.users_likes_tracks(uid)
        short_tracks = extract_short_tracks(likes)

    if not short_tracks:
        return 0, [], [], []

    short_by_id = dict(short_tracks)
    ids = list(short_by_id.keys())

    unavailable = []
    unresolved = []
    checked_ids = set()
    batch_size = 100

    for start in range(0, len(ids), batch_size):
        batch = ids[start:start + batch_size]

        try:
            tracks = client.tracks(batch) or []
        except Exception as error:
            logging.warning(
                "Ошибка при проверке пачки: %s",
                type(error).__name__,
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

            if available is False:
                unavailable.append(format_track(track, track_id))
                continue

            if available is True:
                continue

            # Если API не дал однозначного статуса,
            # пробуем получить полную информацию о треке.
            short_track = short_by_id.get(track_id)

            if short_track is None:
                unresolved.append(
                    format_track(track, track_id)
                    + " [нет исходной записи]"
                )
                continue

            try:
                detailed_track = short_track.fetch_track()

                if detailed_track is None:
                    unresolved.append(
                        format_track(short_track, track_id)
                        + " [подробная информация не получена]"
                    )
                    continue

                detailed_available = getattr(
                    detailed_track, "available", None
                )

                if detailed_available is False:
                    unavailable.append(
                        format_track(detailed_track, track_id)
                    )
                elif detailed_available is not True:
                    unresolved.append(
                        format_track(detailed_track, track_id)
                        + " [статус доступности не определён]"
                    )

            except Exception as error:
                unresolved.append(
                    format_track(short_track, track_id)
                    + f" [ошибка подробного запроса: "
                    f"{type(error).__name__}]"
                )

            time.sleep(0.05)

        # Если API совсем не вернул ID, это не означает,
        # что трек точно удалён.
        for track_id in batch:
            if track_id not in found:
                unresolved.append(
                    format_track(short_by_id[track_id], track_id)
                    + " [API не вернул трек]"
                )

    # Убираем повторения, сохраняя порядок.
    unavailable = list(dict.fromkeys(unavailable))
    unresolved = list(dict.fromkeys(unresolved))

    return len(ids), unavailable, unresolved, len(checked_ids)


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
        total, unavailable, unresolved, checked = (
            await asyncio.to_thread(check_tracks, access_token)
        )

        report_path = DATA_DIR / f"report_{user_id}.txt"

        with report_path.open("w", encoding="utf-8") as file:
            file.write("ОТЧЁТ ПРОВЕРКИ ЯНДЕКС МУЗЫКИ\n\n")
            file.write(f"Уникальных треков в списке: {total}\n")
            file.write(f"Треков, полученных через API: {checked}\n")
            file.write(
                f"API явно пометил недоступными: {len(unavailable)}\n"
            )
            file.write(f"Не удалось однозначно проверить: {len(unresolved)}\n\n")

            file.write("НЕДОСТУПНЫЕ ПО ДАННЫМ API:\n")
            file.write(
                "\n".join(unavailable)
                if unavailable
                else "Не обнаружены"
            )

            file.write("\n\nТРЕБУЮТ ДОПОЛНИТЕЛЬНОЙ ПРОВЕРКИ:\n")
            file.write(
                "\n".join(unresolved)
                if unresolved
                else "Нет"
            )

            file.write(
                "\n\nПримечание: отсутствие трека в ответе API "
                "не доказывает, что он удалён."
            )

        await bot.send_document(
            chat_id=user_id,
            document=FSInputFile(report_path),
            caption=(
                "✅ Проверка завершена.\n"
                f"Всего в «Мне нравится»: {total}\n"
                f"Получено API: {checked}\n"
                f"Явно недоступных: {len(unavailable)}\n"
                f"Не удалось однозначно проверить: {len(unresolved)}"
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
