"""Боевой режим на подставной площадке: покупка, выставление, продажа, защита."""
from __future__ import annotations

import pytest

from gift_bot.execution.base import RealExecutor, VirtualExecutor
from gift_bot.risk.limits import LIVE
from tests.conftest import FakeMarket, listing, sale


class TradingMarket(FakeMarket):
    def __init__(self, name):
        super().__init__(name)
        self.bal = 100.0
        self.owned: set[str] = set()
        self.calls: list[tuple] = []
        self.fail_buy = False
        self.fail_list = 0

    async def balance(self):
        return self.bal

    async def buy(self, lst):
        self.calls.append(("buy", lst.listing_id))
        if self.fail_buy:
            raise RuntimeError("лот уже продан")
        self.bal -= lst.price
        item = f"own-{lst.listing_id}"
        self.owned.add(item)
        return item

    async def list_for_sale(self, item_id, price):
        self.calls.append(("list", item_id, price))
        if self.fail_list:
            self.fail_list -= 1
            raise RuntimeError("подарок заблокирован для продажи")

    async def change_price(self, item_id, price):
        self.calls.append(("price", item_id, price))

    async def owned_ids(self):
        return set(self.owned)


async def live_env(env, **over):
    e = await env(**over)
    m = TradingMarket("portals")
    e.markets["portals"] = m
    e.pricer.markets["portals"] = m
    e.trader.markets["portals"] = m
    e.trader.executors = {"virtual": VirtualExecutor(e.db), "real": RealExecutor(e.db, e.markets)}
    m.lows["Toy Bear|Wizard"] = [listing(lid="F1", price=13.0), listing(lid="F2", price=14.0)]
    await e.db.add_sales([sale(sid=f"S{i}", price=13.0) for i in range(3)])
    await e.risk.set_mode(LIVE)
    return e, m


async def test_live_buy_lists_and_sells(env):
    e, m = await live_env(env)
    await e.trader.process_listing(listing(lid="L1", price=10.0))
    [pos] = await e.db.open_positions("real")
    assert pos["item_id"] == "own-L1" and pos["listed"] == 1
    assert m.calls == [("buy", "L1"), ("list", "own-L1", 12.87)]
    assert "ВИРТУАЛЬНО" not in e.sent[-1] and "Покупка" in e.sent[-1]

    m.owned.discard("own-L1")                      # подарок купили
    await e.trader.manage_positions()
    assert await e.db.open_positions("real")       # одного пустого ответа мало
    await e.trader.manage_positions()
    assert await e.db.open_positions("real") == []
    assert await e.db.realized_pnl("real") == pytest.approx(12.87 * 0.95 - 10.0, abs=1e-3)


async def test_buy_failure_does_not_open_position(env):
    e, m = await live_env(env)
    m.fail_buy = True
    await e.trader.process_listing(listing(lid="L1", price=10.0))
    assert await e.db.open_positions() == []
    assert m.calls == [("buy", "L1")]              # ровно одна попытка, без повтора
    assert "Покупка не удалась" in e.sent[-1]


async def test_not_enough_balance_skips(env):
    e, m = await live_env(env)
    m.bal = 3.0
    await e.trader.process_listing(listing(lid="L1", price=10.0))
    assert m.calls == [] and await e.db.open_positions() == []
    rows = await e.db.recent_signals()
    assert rows[0]["reason"].startswith("limit:balance")


async def test_listing_is_retried_until_it_works(env):
    e, m = await live_env(env)
    m.fail_list = 1
    await e.trader.process_listing(listing(lid="L1", price=10.0))
    [pos] = await e.db.open_positions("real")
    assert pos["listed"] == 0
    await e.trader.manage_positions()
    [pos] = await e.db.open_positions("real")
    assert pos["listed"] == 1


async def test_own_listing_is_ignored(env):
    e, m = await live_env(env)
    await e.trader.process_listing(listing(lid="L1", price=10.0))
    m.calls.clear()
    await e.trader.process_listing(listing(lid="own-L1", price=9.0))
    assert m.calls == []


async def test_stop_blocks_live_trading(env):
    e, m = await live_env(env)
    await e.risk.set_stopped(True)
    await e.trader.process_listing(listing(lid="L1", price=10.0))
    assert m.calls == []


async def test_max_trade_limit_in_live(env):
    e, m = await live_env(env, MAX_TRADE_TON=5)
    await e.trader.process_listing(listing(lid="L1", price=10.0))
    assert m.calls == []


async def test_panel_live_requires_confirmation(env):
    from tests.test_panel import make_client
    e, _ = await live_env(env)
    await e.risk.set_mode("observe")
    client, sent = await make_client(e)
    try:
        h = {"X-Panel": "1"}
        assert (await client.post("/api/mode", json={"mode": "live"}, headers=h)).status == 400
        assert (await client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=h)).status == 200
        assert e.risk.mode == LIVE and "Боевой режим включён" in sent[-1]
    finally:
        await client.close()
