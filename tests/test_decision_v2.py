"""Решения v2: покупка только недооценённых, ликвидных и фундаментально сильных подарков."""
from __future__ import annotations

from datetime import timedelta

import pytest

from gift_bot.engine.decision_v2 import MarketSnapshot, decide
from gift_bot.models import utcnow
from gift_bot.pricing.valuation import Pricer
from tests.conftest import listing, make_settings, sale

NOW = utcnow()


def good_listing(price=10.0, **kw):
    lst = listing(price=price, **kw)
    lst.model_rarity, lst.backdrop_rarity, lst.symbol_rarity = 0.8, 1.0, 0.5
    return lst


def snap(lst=None, sales=None, floors=None, **kw) -> MarketSnapshot:
    sales = sales if sales is not None else [("portals", 13.0, NOW - timedelta(hours=12 * i)) for i in range(14)]
    floors = floors if floors is not None else {"portals": [13.0, 13.5, 14.0], "tonnel": [14.5]}
    base = dict(supply=20_000, upgraded=8_000, capital=100.0)
    base.update(kw)
    return MarketSnapshot(now=NOW, listing=lst or good_listing(), sales=sales, floors=floors, **base)


def run(s=None, cfg=None, base=12.0):
    cfg = cfg or make_settings(STRATEGY_V2=True)
    return decide(s or snap(), cfg, Pricer(None, {}, cfg), base)


def test_undervalued_liquid_gift_is_bought():
    d = run()
    assert d.buy, d.reason
    assert 12 < d.fair_net < 13.2                      # fair между медианой и глубиной стакана
    assert d.flip.ask <= 13.5 * 0.99 + 1e-9            # не выше 2-го лота
    assert d.ev.ev_pct >= 12 and d.score >= 45
    det = d.details()
    assert {"fair", "score", "sales_7d", "ev_pct", "days_to_sell"} <= set(det)


def test_price_near_fair_is_skipped():
    d = run(snap(good_listing(price=12.3)))
    assert not d.buy and d.reason.startswith("EV")


def test_illiquid_model_is_skipped():
    few = [("portals", 13.0, NOW - timedelta(days=d)) for d in (1, 3)]
    d = run(snap(sales=few))
    assert not d.buy and "неликвид" in d.reason


def test_wide_spread_is_skipped():
    d = run(snap(floors={"portals": [25.0, 26.0, 27.0]}))
    assert not d.buy and "спред" in d.reason


def test_bait_listing_does_not_set_floor():
    """Один демпинговый лот в стакане не тянет fair вниз — берётся глубина 2–3-го лота."""
    with_bait = run(snap(floors={"portals": [6.0, 13.5, 14.0]}))
    assert with_bait.fv.floor_depth == pytest.approx((13.5 + 14.0) / 2 * 0.95)


def test_weak_fundamentals_skipped():
    weak = listing(price=10.0)
    weak.model_rarity, weak.backdrop_rarity, weak.symbol_rarity = 8, 8, 6
    cfg = make_settings(STRATEGY_V2=True, MIN_SCORE=60)
    assert run(cfg=cfg).score > 80                                     # сильный подарок проходит
    d = run(snap(weak, supply=2_000_000, upgraded=1_500_000), cfg=cfg)
    assert not d.buy and "фундаментальность" in d.reason
    assert set(d.score_parts) >= {"модель", "тираж"}


def test_outlier_sale_ignored():
    sales = [("portals", 13.0, NOW - timedelta(hours=12 * i)) for i in range(14)] + [("portals", 300.0, NOW)]
    assert run(snap(sales=sales)).fv.sales_median == pytest.approx(13 * 0.95)


def test_new_collection_blocked_then_cautious():
    blocked = run(snap(created_at=NOW - timedelta(days=1)))
    assert not blocked.buy and "не покупаем" in blocked.reason
    young = run(snap(created_at=NOW - timedelta(days=7)))
    assert young.is_new and young.required_pct == pytest.approx(12 * 1.5)
    mature = run(snap(created_at=NOW - timedelta(days=60)))
    assert not mature.is_new and mature.required_pct == 12


def test_collection_seen_since_start_is_mature():
    watch = NOW - timedelta(days=2)
    known = run(snap(first_seen=watch, watch_started=watch))           # была до старта бота
    assert known.age_days is None and not known.is_new
    appeared = run(snap(first_seen=NOW - timedelta(days=1), watch_started=NOW - timedelta(days=10)))
    assert appeared.age_days == pytest.approx(1, abs=0.01) and "не покупаем" in appeared.reason


def test_kelly_and_concentration_limits():
    small = run(snap(capital=20.0))
    assert not small.buy and small.reason.startswith("limit:kelly")
    crowded = run(snap(collection_exposure=25.0))
    assert not crowded.buy and crowded.reason.startswith("limit:concentration")


def test_falling_trend_discounts_fair():
    falling = [("portals", 20.0 - i * 0.5, NOW - timedelta(days=13 - i)) for i in range(14)]
    d = run(snap(sales=falling, floors={"portals": [13.5, 14.0, 14.2]}))
    assert d.fv.trend < 0 and "падающий тренд" in d.fv.parts


async def test_trader_uses_v2_in_observe_and_logs_details(env):
    e = await env(STRATEGY_V2=True)
    m = e.markets["portals"]
    m.lows["Toy Bear|Wizard"] = [listing(lid=f"F{i}", price=p) for i, p in enumerate((13.0, 13.5, 14.0))]
    await e.db.add_sales([sale(sid=f"S{i}", price=13.0, when=NOW - timedelta(hours=12 * i)) for i in range(14)])
    await e.db.upsert_collection("toy bear", "Toy Bear", supply=20_000, upgraded=8_000)
    await e.trader.process_listing(good_listing(lid="L1"))
    [pos] = await e.db.open_positions("virtual")
    [sig] = await e.db.recent_signals()
    assert sig["strategy"] == "v2" and sig["fair"] and sig["score"] and sig["ev_pct"] and sig["sales_7d"] == 14
    assert pos["ask_price"] <= 13.5


async def test_v2_not_used_in_live_by_default(env):
    e = await env(STRATEGY_V2=True)
    await e.risk.set_mode("live")
    assert not e.strategy.use_v2()
    e2 = await env(STRATEGY_V2=True, STRATEGY_V2_LIVE=True)
    await e2.risk.set_mode("live")
    assert e2.strategy.use_v2()


def test_report_skip_breakdown():
    from gift_bot.reports.weekly import skip_breakdown
    rows = skip_breakdown(["неликвид: продаж 2 < 5", "неликвид: спред 50%", "EV +3% < порога 12%",
                           "limit:kelly — цена 10 > 5", "фундаментальность 30 < 45"])
    assert rows[0] == ("неликвид", 2)
    assert dict(rows) == {"неликвид": 2, "прибыль ниже порога": 1, "размер позиции (Келли)": 1,
                          "слабая фундаментальность": 1}


async def test_weekly_report_lists_skips(env):
    from gift_bot.reports.weekly import build_report
    e = await env()
    for reason in ("неликвид: продаж 1 < 5", "неликвид: спред 60%"):
        await e.db.add_signal(mode="virtual", kind="flip", market="portals", target_market="portals",
                              listing_id="x", model_key="k", title="t", price=1, buy_cost=1, market_price=1,
                              sales_count=0, expected_net=1, profit_pct=0, required_pct=12,
                              decision="skip", reason=reason, ts=(NOW - timedelta(days=1)).isoformat())
    text = await build_report(e.db, e.risk, 100)
    assert "Отсеяно сделок: 2" in text and "неликвид: 2" in text
