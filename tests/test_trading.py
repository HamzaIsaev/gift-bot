from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from gift_bot.logging_setup import RedactFilter
from gift_bot.models import clean_attr, model_key, utcnow
from gift_bot.pricing.valuation import MarketView
from gift_bot.reports.weekly import build_report
from gift_bot.storage.db import iso
from tests.conftest import listing, sale

def test_model_key_strips_rarity():
    assert clean_attr("Zebra (1.5%)") == "Zebra"
    assert model_key("Toy Bear", "Zebra (1.5%)") == model_key("toy bear", "Zebra")


def test_market_view_price_and_ask():
    v = MarketView("portals", floor=12.0, sales_median=14.0, sales_count=5, sales_cv=0.0)
    assert v.market_price == 13.0
    assert v.ask_price(1.0) == 11.88       # не выше floor, минус 1%
    v2 = MarketView("portals", floor=None, sales_median=None, sales_count=0, sales_cv=0.0)
    assert v2.market_price is None and v2.ask_price(1.0) is None


async def seed_market(e, floor=13.0, sales=(13.0, 13.0, 13.0), sales_age_h=0):
    p = e.markets["portals"]
    p.lows["Toy Bear|Wizard"] = [listing(lid="F1", price=floor), listing(lid="F2", price=floor + 1)]
    when = utcnow() - timedelta(hours=sales_age_h)
    await e.db.add_sales([sale(sid=f"S{i}", price=x, when=when) for i, x in enumerate(sales)])


async def test_cheap_listing_gives_flip_signal(env):
    e = await env()
    await seed_market(e)
    ev = await e.strategy.evaluate(listing(price=10.0))
    assert ev.reject_reason is None
    plan = ev.best
    # ask = 13 * 0.99 = 12.87; на руки 12.87 * 0.95 = 12.2265; прибыль 22.3%
    assert plan.ask_price == 12.87
    assert plan.profit_pct == pytest.approx(22.27, abs=0.01)
    assert plan.min_price == pytest.approx(10 / 0.95, abs=0.01)


async def test_small_discount_is_rejected(env):
    e = await env()
    await seed_market(e)
    ev = await e.strategy.evaluate(listing(price=11.5))
    assert ev.reject_reason and "порога" in ev.reject_reason


async def test_illiquid_model_needs_bigger_discount(env):
    e = await env()
    await seed_market(e, sales=())                    # продаж нет — модель неликвидна
    ev = await e.strategy.evaluate(listing(price=10.5))
    assert ev.required_pct == 12 + 8
    assert ev.reject_reason                          # 17% прибыли < 20% порога


async def test_arbitrage_signal_between_markets(env):
    e = await env()
    await seed_market(e, floor=10.5, sales=())
    e.markets["tonnel"].lows["Toy Bear|Wizard"] = [listing("tonnel", "T1", 20.0)]
    ev = await e.strategy.evaluate(listing(price=10.0))
    assert ev.arbitrage is not None
    assert ev.arbitrage.target_market == "tonnel"
    # 20*0.99=19.8 - 0.5 перенос = 19.3 на руки при затратах 10
    assert ev.arbitrage.profit_ton == pytest.approx(9.3)


async def test_trader_opens_virtual_position_only_for_new_listings(env):
    e = await env()
    await seed_market(e)
    p = e.markets["portals"]
    p.listings = [listing(lid="OLD", price=5.0)]
    await e.trader._market_loop_once("portals")        # прогрев: старые лоты не трогаем
    assert await e.db.open_positions() == []
    p.listings.append(listing(lid="NEW", price=10.0))
    await e.trader._market_loop_once("portals")
    pos = await e.db.open_positions("virtual")
    assert len(pos) == 1 and pos[0]["listing_id"] == "NEW"
    assert "ВИРТУАЛЬНО" in e.sent[-1] and "Покупка" in e.sent[-1]


async def test_limits_block_and_are_logged(env):
    e = await env(MAX_TRADE_TON=5)
    await seed_market(e)
    await e.trader.process_listing(listing(price=10.0))
    assert await e.db.open_positions() == []
    since, until = utcnow() - timedelta(hours=1), utcnow() + timedelta(hours=1)
    assert await e.db.count_skipped(since, until) == 1


async def test_stop_blocks_everything(env):
    e = await env()
    await seed_market(e)
    await e.risk.set_stopped(True)
    await e.trader.process_listing(listing(price=10.0))
    assert await e.db.open_positions() == []


async def test_virtual_sale_closes_position_with_profit(env):
    e = await env()
    await seed_market(e)
    await e.trader.process_listing(listing(price=10.0))
    [pos] = await e.db.open_positions()
    await e.db.add_sales([sale(sid="BUYER", price=13.0, when=utcnow() + timedelta(seconds=1))])
    await e.trader.manage_positions()
    assert await e.db.open_positions() == []
    assert await e.db.realized_pnl("virtual") == pytest.approx(12.87 * 0.95 - 10.0, abs=1e-3)
    assert "Продано" in e.sent[-1]


async def test_reprice_never_below_breakeven(env):
    e = await env(REPRICE_STEP_PCT=50, ADAPTIVE=False)
    await seed_market(e, sales_age_h=8)                # продажи были до нашего выставления
    await e.trader.process_listing(listing(price=10.0))
    [pos] = await e.db.open_positions()
    old = iso(utcnow() - timedelta(hours=7))
    await e.db.conn.execute("UPDATE positions SET last_reprice_at=?", (old,))
    await e.trader.manage_positions()
    [pos] = await e.db.open_positions()
    assert pos["ask_price"] == pytest.approx(pos["min_price"])


async def test_daily_loss_halts_trading(env):
    e = await env(MAX_DAILY_LOSS_TON=1)
    await seed_market(e)
    await e.trader.process_listing(listing(price=10.0))
    [pos] = await e.db.open_positions()
    await e.db.close_position(pos["id"], 9.0, 8.55, -1.45)
    await e.trader.manage_positions()
    assert "лимит убытка" in e.sent[-1]
    assert (await e.risk.check_trade(1.0)).startswith("limit:daily_loss")


async def test_weekly_report_builds(env):
    e = await env()
    text = await build_report(e.db, e.risk, 100)
    assert "Отчёт за неделю" in text and "Сделок: <b>0</b>" in text


def test_secrets_are_redacted():
    f = RedactFilter(["super-secret-token"])
    rec = logging.LogRecord("x", logging.INFO, "", 0, "token=%s auth=tma query_id=AAA&hash=bbb", ("super-secret-token",), None)
    f.filter(rec)
    assert "super-secret-token" not in rec.msg and "query_id" not in rec.msg
