from __future__ import annotations

import base64

from aiohttp.test_utils import TestClient, TestServer

from gift_bot.panel.server import Panel
from tests.conftest import listing


async def make_client(e) -> tuple[TestClient, list[str]]:
    sent: list[str] = []

    async def notify(text: str) -> None:
        sent.append(text)

    panel = Panel(e.s, e.db, e.risk, notify)
    client = TestClient(TestServer(panel.app))
    await client.start_server()
    return client, sent


async def test_overview_and_tabs(env):
    e = await env()
    e.markets["portals"].lows["Toy Bear|Wizard"] = [listing(lid="F1", price=13.0)]
    await e.trader.process_listing(listing(price=9.0))
    client, _ = await make_client(e)
    try:
        assert "Gift Bot" in await (await client.get("/")).text()
        o = await (await client.get("/api/overview")).json()
        assert o["mode"] == "observe" and len(o["positions"]) == 1 and len(o["daily"]) == 7
        assert len(await (await client.get("/api/signals")).json()) == 1
        assert await (await client.get("/api/trades")).json() == []
    finally:
        await client.close()


async def test_actions_require_panel_header(env):
    e = await env()
    client, sent = await make_client(e)
    try:
        assert (await client.post("/api/stop")).status == 403          # чужой сайт не сможет нажать «Стоп»
        assert (await client.post("/api/stop", headers={"X-Panel": "1"})).status == 200
        assert e.risk.stopped and "Остановлено" in sent[-1]
        r = await client.post("/api/limits", json={"name": "max_trade", "value": "25"}, headers={"X-Panel": "1"})
        assert r.status == 200 and e.risk.limits.max_trade_ton == 25
        r = await client.post("/api/mode", json={"mode": "live"}, headers={"X-Panel": "1"})
        assert r.status == 400 and e.risk.mode == "observe"
    finally:
        await client.close()


async def test_password_protection(env):
    e = await env(PANEL_PASSWORD="pa$$word")
    client, _ = await make_client(e)
    try:
        assert (await client.get("/api/overview")).status == 401
        auth = "Basic " + base64.b64encode(b"admin:pa$$word").decode()
        assert (await client.get("/api/overview", headers={"Authorization": auth})).status == 200
    finally:
        await client.close()
