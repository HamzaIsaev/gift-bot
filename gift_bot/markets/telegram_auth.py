"""Получение initData мини-приложений площадок через Telegram-аккаунт бота.

Одна Pyrogram-сессия (строка из .env) на все площадки, поэтому логиниться
нужно один раз — скриптом scripts/login.py.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import unquote

from pyrogram import Client
from pyrogram.raw.functions.messages import RequestAppWebView
from pyrogram.raw.types import InputBotAppShortName, InputUser

log = logging.getLogger(__name__)


class TelegramAuth:
    def __init__(self, api_id: int, api_hash: str, session_string: str):
        self._client = Client(
            "gift_bot", api_id=api_id, api_hash=api_hash,
            session_string=session_string, in_memory=True, no_updates=True,
        )
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        if not self._started:
            await self._client.start()
            self._started = True

    async def stop(self) -> None:
        if self._started:
            await self._client.stop()
            self._started = False

    async def init_data(self, bot_username: str, app_short_name: str, platform: str = "android") -> str:
        async with self._lock:
            await self.start()
            peer = await self._client.resolve_peer(bot_username)
            bot = InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
            web_view = await self._client.invoke(
                RequestAppWebView(
                    peer=peer,
                    app=InputBotAppShortName(bot_id=bot, short_name=app_short_name),
                    platform=platform,
                )
            )
        data = unquote(web_view.url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0])
        log.info("Получен свежий initData для @%s", bot_username)
        return data
