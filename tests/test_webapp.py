"""Вход в панель из Telegram Mini App."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from gift_bot.panel.webapp_auth import verify_init_data
from tests.test_panel import make_client

TOKEN = "123:secret-token-value"   # как в make_settings


def signed(user_id: int, token: str = TOKEN, age: int = 0) -> str:
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "AAE",
              "user": json.dumps({"id": user_id, "first_name": "G"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_verify_init_data():
    assert verify_init_data(signed(1), TOKEN) == 1
    assert verify_init_data(signed(1, token="other:token"), TOKEN) is None      # чужая подпись
    assert verify_init_data(signed(1, age=2 * 86400), TOKEN) is None            # устарела
    assert verify_init_data(signed(1).replace("first_name", "first_nam"), TOKEN) is None
    assert verify_init_data("garbage", TOKEN) is None


async def test_panel_accepts_admin_from_telegram_only(env):
    e = await env(PANEL_PASSWORD="pa$$word")
    client, _ = await make_client(e)
    try:
        assert (await client.get("/app")).status == 200                          # страница открыта
        assert (await client.get("/")).status == 401                             # в браузере — пароль
        ok = {"X-Tg-Init-Data": signed(1)}                                       # 1 — админ
        assert (await client.get("/api/overview", headers=ok)).status == 200
        stranger = {"X-Tg-Init-Data": signed(999)}
        assert (await client.get("/api/overview", headers=stranger)).status == 401
        r = await client.post("/api/stop", headers={**ok, "X-Panel": "1"})
        assert r.status == 200 and e.risk.stopped
    finally:
        await client.close()
