
import asyncio
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

# Файлы токенов сохраняются на диске Render.
# При перезапуске бесплатного сервиса они могут пропасть.
DATA_DIR = Path("/tmp/yandex_music_bot")
DATA_DIR.mkdir(parents=True, exist_ok=True)

auth_in_progress = set()


def token_path(user_id):
    return DATA_DIR / f"token_{user_id}.txt"


def get_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔐 Войти через Яндекс",
            callback_data="auth_yandex",
        )],
        [InlineKeyboardButton(
            text="🎵 Найти недоступные треки",
            callback_data="start_search",
        )],
    ])


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
        # Выводим в Telegram только ссылку и код подтверждения.
        # Сам токен никогда не отправляем в чат.
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
        # Проверяем, что токен действительно работает.
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
            "✅ Вход подтверждён! Токен сохранён на сервере.\n"
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


def extract_track_ids(likes):
    items = getattr(likes, "tracks", None)
    if items is None:
        items = likes

    ids = []
    for item in items or []:
        track = getattr(item, "track", None) or item
        track_id = getattr(track, "id", None)
        if track_id is not None:
            ids.append(str(track_id))
    return list(dict.fromkeys(ids))


def check_tracks(access_token):
    client = Client(access_token)
    client.init()

    likes = client.users_likes_tracks()
    ids = extract_track_ids(likes)

    if not ids and getattr(client, "me", None):
        uid = client.me.account.uid
        likes = client.users_likes_tracks(uid)
        ids = extract_track_ids(likes)

    if not ids:
        return 0, [], []

    unavailable = []
    unresolved = []
    batch_size = 100

    for start in range(0, len(ids), batch_size):
        batch = ids[start:start + batch_size]
        result = client.tracks(batch) or []
        found = set()

        for track in result:
            if track is None:
                continue

            track_id = getattr(track, "id", None)
            if track_id is not None:
                found.add(str(track_id))

            if getattr(track, "available", None) is False:
                artists = getattr(track, "artists", None) or []
                artist_names = ", ".join(
                    a.name for a in artists if getattr(a, "name", None)
                ) or "Неизвестный исполнитель"
                title = getattr(track, "title", None) or "Без названия"
                version = getattr(track, "version", None)
                if version:
                    title += f" ({version})"
                unavailable.append(f"{artist_names} — {title}")

        # Если API не вернул ID, отмечаем его отдельно,
        # а не объявляем удалённым без подтверждения.
        unresolved.extend(track_id for track_id in batch
                          if track_id not in found)

    return len(ids), unavailable, unresolved


@dp.callback_query(F.data == "start_search")
async def start_search(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id

    if callback.message.chat.type != "private":
        await callback.message.answer(
            "Для безопасности используй бота в личном чате."
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

    status = await callback.message.answer(
        "⏳ Загружаю «Мне нравится» и проверяю треки..."
    )

    try:
        total, unavailable, unresolved = await asyncio.to_thread(
            check_tracks, access_token
        )

        report_path = DATA_DIR / f"report_{user_id}.txt"
        with report_path.open("w", encoding="utf-8") as file:
            file.write("ОТЧЁТ ПРОВЕРКИ ЯНДЕКС МУЗЫКИ\n\n")
            file.write(f"Треков в списке: {total}\n")
            file.write(f"API явно пометил недоступными: {len(unavailable)}\n")
            file.write(f"Не удалось проверить: {len(unresolved)}\n\n")

            file.write("НЕДОСТУПНЫЕ ПО ДАННЫМ API:\n")
            file.write("\n".join(unavailable) if unavailable else "Не обнаружены")
            file.write("\n\nID, которые API не вернул:\n")
            file.write("\n".join(unresolved) if unresolved else "Нет")

        await bot.send_document(
            chat_id=user_id,
            document=FSInputFile(report_path),
            caption=(
                f"Проверка завершена.\n"
                f"Получено треков: {total}\n"
                f"Явно недоступных: {len(unavailable)}\n"
                f"Не удалось проверить: {len(unresolved)}"
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


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot, polling_timeout=30),
    )


if __name__ == "__main__":
    asyncio.run(main())
