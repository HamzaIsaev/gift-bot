"""Настройки из админ-панели: изменение, границы, профили, сброс, сохранение после перезапуска."""
from __future__ import annotations

import pytest

from gift_bot.risk.limits import RiskManager
from gift_bot.settings_registry import ITEMS, SettingsService
from tests.conftest import make_settings
from tests.test_panel import make_client

H = {"X-Panel": "1"}


def test_every_item_has_explanation_and_reads():
    s = make_settings()
    svc = SettingsService(s, None, RiskManager(None, s))
    snap = svc.snapshot()
    keys = {i["key"] for g in snap["groups"] for i in g["items"]}
    assert keys == {i.key for i in ITEMS}
    assert all(len(i.help) > 30 and i.title for i in ITEMS)
    spread = next(i for g in snap["groups"] for i in g["items"] if i["key"] == "max_spread")
    assert spread["value"] == 40 and spread["unit"] == "%"          # 0.40 показывается как 40 %


async def test_change_applies_live_and_survives_restart(env):
    e = await env()
    client, sent = await make_client(e)
    try:
        r = await client.post("/api/settings", json={"key": "min_profit_pct", "value": "15"}, headers=H)
        assert r.status == 200 and e.s.min_profit_pct == 15 and "Минимальная ожидаемая прибыль" in sent[-1]
        r = await client.post("/api/settings", json={"key": "max_spread", "value": 25}, headers=H)
        assert e.s.max_spread == pytest.approx(0.25)
        r = await client.post("/api/settings", json={"key": "fee:portals:sell", "value": 3}, headers=H)
        assert e.s.fees("portals").sell == pytest.approx(0.03)
        r = await client.post("/api/settings", json={"key": "limit:max_trade", "value": 7}, headers=H)
        assert e.risk.limits.max_trade_ton == 7
        data = await r.json()
        item = next(i for g in data["groups"] for i in g["items"] if i["key"] == "min_profit_pct")
        assert item["changed"] and item["value"] == 15 and item["default"] == 12
    finally:
        await client.close()

    fresh = make_settings()                                        # «перезапуск»: новые настройки из .env
    svc = SettingsService(fresh, e.db, e.risk)
    await svc.load()
    assert fresh.min_profit_pct == 15 and fresh.max_spread == pytest.approx(0.25)
    assert fresh.fees("portals").sell == pytest.approx(0.03)
    assert make_settings().fees("portals").sell == pytest.approx(0.05)  # общий default не испорчен


async def test_bounds_presets_reset_and_danger(env):
    e = await env()
    client, _ = await make_client(e)
    try:
        r = await client.post("/api/settings", json={"key": "min_profit_pct", "value": 500}, headers=H)
        assert r.status == 400 and "допустимо" in (await r.json())["error"]
        r = await client.post("/api/settings", json={"preset": "careful"}, headers=H)
        assert (await r.json())["active_preset"] == "careful" and e.s.min_profit_pct == 18
        r = await client.post("/api/settings", json={"reset": "all"}, headers=H)
        assert (await r.json())["active_preset"] == "balanced" and e.s.min_profit_pct == 12
        r = await client.post("/api/settings", json={"key": "strategy_v2_live", "value": True}, headers=H)
        assert r.status == 400 and not e.s.strategy_v2_live
        r = await client.post("/api/settings", json={"key": "strategy_v2_live", "value": True, "confirm": True},
                              headers=H)
        assert r.status == 200 and e.s.strategy_v2_live
    finally:
        await client.close()
