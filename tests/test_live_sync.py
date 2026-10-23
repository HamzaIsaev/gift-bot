"""Живая синхронизация панелей: изменение из любого места сразу приходит всем подписчикам."""
from __future__ import annotations

import asyncio

from tests.test_panel import make_client
from tests.test_webapp import signed


async def read_event(resp) -> str:
    """Следующее событие из потока (пропуская ping-комментарии)."""
    while True:
        line = (await asyncio.wait_for(resp.content.readline(), timeout=3)).decode().strip()
        if line.startswith("data:"):
            return line[5:].strip()


async def test_change_in_one_panel_reaches_another(env):
    e = await env(PANEL_PASSWORD="pa$$word")
    client, _ = await make_client(e)
    try:
        tg = {"X-Tg-Init-Data": signed(1)}                      # «мини-приложение»
        stream = await client.get("/api/events", headers=tg)
        assert stream.status == 200
        assert await read_event(stream) == "hello"

        # «Браузер» меняет лимит — мини-приложение сразу получает событие
        import base64
        basic = {"Authorization": "Basic " + base64.b64encode(b"a:pa$$word").decode(), "X-Panel": "1"}
        r = await client.post("/api/limits", json={"name": "max_trade", "value": "7"}, headers=basic)
        assert r.status == 200
        assert await read_event(stream) == "settings"

        # Команда бота (/stop) меняет состояние напрямую — событие тоже приходит
        await e.risk.set_stopped(True)
        assert await read_event(stream) == "settings"
        stream.close()
    finally:
        await client.close()


async def test_events_require_auth(env):
    e = await env(PANEL_PASSWORD="pa$$word")
    client, _ = await make_client(e)
    try:
        assert (await client.get("/api/events")).status == 401
        assert (await client.get("/api/events", headers={"X-Tg-Init-Data": signed(999)})).status == 401
    finally:
        await client.close()
