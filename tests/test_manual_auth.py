from __future__ import annotations

from urllib.parse import quote

import pytest

from gift_bot.markets.manual_auth import AuthMissing, ManualAuthStore, normalize
from tests.conftest import FakeMarket, make_settings
from tests.test_panel import make_client

INIT = 'query_id=AAE&user=%7B%22id%22%3A1%7D&auth_date=1760000000&hash=abc123'


@pytest.mark.parametrize("raw", [
    INIT,
    f"tma {INIT}",
    f'"{INIT}"',
    quote(INIT, safe=""),
    f"https://web.telegram.org/#tgWebAppData={quote(INIT, safe='')}&tgWebAppVersion=8.0",
])
def test_normalize_accepts_any_form(raw):
    assert normalize(raw) == INIT or "auth_date=1760000000" in normalize(raw)


def test_normalize_rejects_garbage():
    with pytest.raises(ValueError):
        normalize("привет")


def test_settings_without_api_use_manual_mode():
    s = make_settings(TG_API_ID=None, TG_API_HASH=None, TG_SESSION_STRING=None)
    assert s.auto_auth is False


async def test_store_roundtrip(env):
    e = await env()
    store = ManualAuthStore(e.db)
    with pytest.raises(AuthMissing):
        await store.get("portals")
    await store.set("portals", f"tma {INIT}")
    assert await store.get("portals") == INIT
    st = await store.status()
    assert st["portals"]["set"] and not st["tonnel"]["set"]


async def test_missing_auth_notifies_once(env):
    e = await env()

    class NoAuth(FakeMarket):
        async def recent_sales(self, limit=50):
            raise AuthMissing("нет данных")

    e.trader.markets["portals"] = NoAuth("portals")
    import asyncio
    task = asyncio.create_task(e.trader._market_loop("portals"))
    await asyncio.sleep(0.05)
    task.cancel()
    assert sum("нет данных входа" in t for t in e.sent) == 1


async def test_panel_saves_auth(env):
    e = await env()
    client, sent = await make_client(e)
    try:
        r = await client.post("/api/auth", json={"market": "mrkt", "data": INIT}, headers={"X-Panel": "1"})
        assert r.status == 200 and "mrkt" in sent[-1]
        o = await (await client.get("/api/overview")).json()
        assert o["auth"]["markets"]["mrkt"]["set"]
        r = await client.post("/api/auth", json={"market": "mrkt", "data": "мусор"}, headers={"X-Panel": "1"})
        assert r.status == 400
    finally:
        await client.close()
