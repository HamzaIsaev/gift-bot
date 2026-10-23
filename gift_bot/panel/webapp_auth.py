"""Проверка входа из Telegram Mini App.

Telegram подписывает initData токеном бота — подделать её без токена нельзя.
Алгоритм: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_SEC = 24 * 3600


def verify_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE_SEC) -> int | None:
    """Возвращает Telegram id пользователя, если подпись верна и данные свежие, иначе None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return int(json.loads(pairs["user"])["id"])
    except (KeyError, ValueError, TypeError):
        return None
