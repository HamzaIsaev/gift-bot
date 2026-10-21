"""Ручной вход на площадки (план Б, без api_id).

Данные входа (initData мини-приложения) копируются из Telegram Web в браузере
и отправляются боту командой /auth <площадка> <данные> или через админ-панель.
Хранятся в БД; со временем устаревают — тогда бот попросит обновить.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote

from gift_bot.markets.base import MarketError
from gift_bot.models import utcnow
from gift_bot.storage.db import Database

MARKETS = ("portals", "tonnel", "mrkt")

# Подсказка: что выполнить в консоли браузера внутри мини-приложения
CONSOLE_SNIPPET = "copy(Telegram.WebApp.initData)"


class AuthMissing(MarketError):
    """Нет данных входа для площадки — нужно отправить /auth."""


def normalize(raw: str) -> str:
    """Принимает initData в любом виде: как есть, с 'tma ', в кавычках, URL-кодированным
    или целой ссылкой с tgWebAppData=. Возвращает чистую строку initData."""
    data = raw.strip().strip("'\"` ")
    if data.lower().startswith("tma "):
        data = data[4:].strip()
    if "tgWebAppData=" in data:
        data = data.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0]
    if "auth_date" not in data and "%3D" in data:
        data = unquote(data)
    if "auth_date%3D" in data:
        data = unquote(data)
    params = parse_qs(data)
    if "auth_date" not in params or "hash" not in params:
        raise ValueError("это не похоже на данные входа (нет auth_date и hash)")
    return data


def auth_date(data: str) -> datetime:
    ts = int(parse_qs(data)["auth_date"][0])
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class ManualAuthStore:
    def __init__(self, db: Database):
        self.db = db

    async def get(self, market: str) -> str:
        data = await self.db.get_setting(f"auth:{market}")
        if not data:
            raise AuthMissing(f"нет данных входа для {market} — отправьте боту /auth {market}")
        return data

    async def set(self, market: str, raw: str) -> datetime:
        if market not in MARKETS:
            raise ValueError(f"неизвестная площадка, доступны: {', '.join(MARKETS)}")
        data = normalize(raw)
        await self.db.set_setting(f"auth:{market}", data)
        return auth_date(data)

    async def status(self) -> dict[str, dict]:
        out = {}
        for m in MARKETS:
            data = await self.db.get_setting(f"auth:{m}")
            if not data:
                out[m] = {"set": False, "age_hours": None}
                continue
            age = (utcnow() - auth_date(data)).total_seconds() / 3600
            out[m] = {"set": True, "age_hours": round(age, 1)}
        return out


def user_name(data: str) -> str:
    """Имя аккаунта из initData — чтобы показать, под кем выполнен вход."""
    try:
        user = json.loads(parse_qs(data)["user"][0])
        return user.get("username") or user.get("first_name") or str(user.get("id"))
    except Exception:
        return "?"
