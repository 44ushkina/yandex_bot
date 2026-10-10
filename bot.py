import asyncio
import logging
import os
import traceback

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.filters import CommandStart
from yandex_music import Client


# Токен Telegram берётся из настроек Render
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN в настройках Environment на Render"
    )

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# Веб-сервер для Render
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

    logging.info("Web server successfully started on port %s", port)


# Команда /start
@dp.message(CommandStart())
async def cmd_start(message: Message):
    auth_url = (
        "https://oauth.yandex.ru/authorize"
        "?response_type=token"
        "&client_id=23cabbbdc6cd418abb4b39c32c41195d"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 Получить OAuth-токен",
                    url=auth_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎵 Запустить поиск",
                    callback_data="start_search",
                )
            ],
        ]
    )

    text = (
        "Привет! Я бот для поиска недоступных треков "
        "из Яндекс Музыки.\n\n"
        "Для проверки отправь мне OAuth-токен Яндекса "
        "или нажми кнопку запуска, если уже отправляла его."
    )

    await message.answer(text, reply_markup=keyboard)


# Получение OAuth-токена от пользователя
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_token(message: Message):
    token = message.text.strip()
    user_id = message.from_user.id

    # Сохраняем токен в файл на сервере
    token_file = f"token_{user_id}.txt"

    with open(token_file, "w", encoding="utf-8") as file:
        file.write(token)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎵 Найти недоступные треки",
                    callback_data="start_search",
                )
            ]
        ]
    )

    await message.answer(
        "✅ Текст сохранён.\n\n"
        "Нажми кнопку ниже, чтобы начать проверку.",
        reply_markup=keyboard,
    )


# Проверка плейлиста «Мне нравится»
@dp.callback_query(F.data == "start_search")
async def start_search(callback: CallbackQuery):
    await callback.answer()

    user_id = callback.from_user.id
    token_file = f"token_{user_id}.txt"

    if not os.path.exists(token_file):
        await callback.message.answer(
            "⚠️ Сначала отправь мне OAuth-токен Яндекса."
        )
        return

    with open(token_file, "r", encoding="utf-8") as file:
        token = file.read().strip()

    if not token:
        await callback.message.answer(
            "⚠️ Файл токена пуст. Отправь токен заново."
        )
        return

    await callback.message.edit_text(
        "⏳ Подключаюсь к Яндекс Музыке..."
    )

    try:
        client = Client(token)
        client.init()

        await callback.message.edit_text(
            "📥 Загружаю плейлист «Мне нравится»..."
        )

        likes = client.users_likes_tracks()
        track_ids = []

        if likes and getattr(likes, "tracks", None):
            track_ids = [
                str(track.id)
                for track in likes.tracks
                if getattr(track, "id", None) is not None
            ]

        if not track_ids and getattr(client, "me", None):
            uid = client.me.account.uid
            likes = client.users_likes_tracks(uid)

            if likes and getattr(likes, "tracks", None):
                track_ids = [
                    str(track.id)
                    for track in likes.tracks
                    if getattr(track, "id", None) is not None
                ]

        if not track_ids:
            await callback.message.edit_text(
                "❌ Не удалось получить треки из «Мне нравится». "
                "Список пуст или Яндекс не вернул данные."
            )
            return

        await callback.message.edit_text(
            f"🔍 Получено треков: {len(track_ids)}.\n"
            "Проверяю доступность..."
        )

        deleted_tracks = []
        batch_size = 100

        for i in range(0, len(track_ids), batch_size):
            batch = track_ids[i:i + batch_size]
            tracks_info = client.tracks(batch)

            for item in tracks_info or []:
                # Помечаем трек только если API явно сообщил,
                # что он недоступен.
                if item is not None and item.available is False:
                    artists = getattr(item, "artists", None) or []
                    artists_names = ", ".join(
                        artist.name
                        for artist in artists
                        if getattr(artist, "name", None)
                    ) or "Неизвестный исполнитель"

                    title = getattr(item, "title", None) or "Без названия"
                    version = getattr(item, "version", None)

                    if version:
                        title += f" ({version})"

                    deleted_tracks.append(
                        f"{artists_names} — {title}"
                    )

        filename = f"deleted_{user_id}.txt"

        with open(filename, "w", encoding="utf-8") as file:
            file.write(
                "=== НЕДОСТУПНЫЕ ТРЕКИ ИЗ ЯНДЕКС МУЗЫКИ ===\n\n"
            )

            if deleted_tracks:
                file.write("\n".join(deleted_tracks))
                file.write("\n")
            else:
                file.write(
                    "Среди полученных данных недоступные треки "
                    "не обнаружены.\n"
                )

        await callback.message.answer_document(
            FSInputFile(filename),
            caption=(
                "✅ Проверка завершена.\n"
                f"Получено треков: {len(track_ids)}\n"
                f"Недоступных найдено: {len(deleted_tracks)}"
            ),
        )

        os.remove(filename)

    except Exception as error:
        logging.error(
            "Ошибка проверки:\n%s",
            traceback.format_exc(),
        )

        await callback.message.answer(
            "❌ При проверке возникла ошибка: "
            f"{type(error).__name__}: {error}"
        )


async def main():
    await bot.delete_webhook(drop_pending_updates=True)

    logging.info("🤖 Запуск веб-сервера и бота...")

    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot, polling_timeout=30),
    )


if __name__ == "__main__":
    asyncio.run(main())
