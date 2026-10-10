
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
