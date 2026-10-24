"""Формулы торговой математики v2."""
from __future__ import annotations

import math
from datetime import timedelta

import pytest

from gift_bot.models import utcnow
from gift_bot.pricing.analytics import (Features, SalePoint, daily_volatility, decay_weights,
                                        expected_value, fair_value, floor_depth, fundamental_score,
                                        kelly_size, liquidity, mad_mask, p_sale, robust_median, trend,
                                        weighted_median)
from tests.conftest import make_settings

NOW = utcnow()
S = make_settings()


def pts(prices, days_ago=None):
    days_ago = days_ago or [0] * len(prices)
    return [SalePoint(p, NOW - timedelta(days=d)) for p, d in zip(prices, days_ago)]


def test_decay_and_weighted_median():
    assert decay_weights([0, 3, 6], 3) == pytest.approx([1, 0.5, 0.25])
    assert weighted_median([1, 2, 100], [1, 1, 1]) == 2
    assert weighted_median([10, 20], [0.1, 1]) == 20      # свежая продажа перевешивает


def test_mad_drops_outliers():
    assert mad_mask([10, 10.5, 9.8, 10.2, 50], 3) == [True, True, True, True, False]


def test_robust_median_ignores_outlier_and_old():
    p = pts([10, 10.2, 9.9, 100, 5], [0, 1, 2, 1, 40])    # 100 — выброс, 5 — вне окна
    med, n = robust_median(p, NOW, 7, 3, 3)
    assert 9.9 <= med <= 10.2 and n == 3


def test_floor_depth_skips_bait():
    assert floor_depth([5, 10, 11, 30]) == (10.5, 4)       # 5 — приманка
    assert floor_depth([7]) == (7, 1)
    assert floor_depth([]) == (None, 0)


def test_trend_detects_fall():
    falling = pts([20 - i for i in range(14)], list(range(13, -1, -1)))  # от 20 до 7 за 2 недели
    assert trend(falling, NOW) < -0.1
    flat = pts([10] * 14, list(range(14)))
    assert trend(flat, NOW) == pytest.approx(0, abs=1e-9)


def test_fair_value_weights_and_discounts():
    many = fair_value(10, 30, 10, 60, 12, 0.0, S)
    assert many.weight_sales == 0.8 and many.fair == pytest.approx(0.8 * 10 + 0.2 * 12)
    thin = fair_value(None, 1, 10, 2, 12, 0.0, S)
    assert thin.weight_sales < 0.4 and "мало продаж" in thin.parts
    down = fair_value(10, 30, 10, 60, 10, -0.2, S)
    assert down.discount == pytest.approx(0.10) and down.fair == pytest.approx(9.0)
    assert fair_value(None, 0, None, 0, None, 0.0, S).fair is None


def test_liquidity_filters():
    p = pts([10] * 14, [i / 2 for i in range(14)])        # 2 продажи в день
    liq = liquidity(p, NOW, active_listings=4, floor_px=10, median=10)
    assert liq.sales_7d == 14 and liq.days_to_sell == pytest.approx(2)
    assert liq.reject_reason(S) is None
    dead = liquidity(pts([10, 10], [1, 2]), NOW, 30, 10, 10)
    assert "неликвид" in dead.reject_reason(S)
    wide = liquidity(p, NOW, 4, floor_px=5, median=10)
    assert "спред" in wide.reject_reason(S)


def test_ev_and_probability():
    assert p_sale(0.5, 7) > p_sale(5, 7) > p_sale(math.inf, 7)
    ev = expected_value(cost=10, net_sell=12, p=0.9, vol_daily=0.0, hold_days=2)
    assert ev.ev == pytest.approx(1.8) and ev.ev_pct == pytest.approx(18)
    risky = expected_value(10, 12, 0.9, vol_daily=0.1, hold_days=4)
    assert risky.ev == pytest.approx(1.8 - 0.1 * 2 * 10)


def test_kelly():
    assert kelly_size(0.9, gain=2, loss=1, capital=100, fraction=0.25) == pytest.approx((0.9 - 0.1 / 2) * 25)
    assert kelly_size(0.3, gain=1, loss=1, capital=100, fraction=0.25) == 0


def test_fundamental_score_breakdown():
    rare = Features(supply=8_000, upgraded=3_000, model_rarity=0.4, backdrop_rarity=0.5,
                    symbol_rarity=0.2, age_days=90, vol_daily=0.01)
    common = Features(supply=900_000, upgraded=500_000, model_rarity=6, backdrop_rarity=6,
                      symbol_rarity=4, age_days=1, vol_daily=0.2)
    hi, parts = fundamental_score(rare)
    lo, _ = fundamental_score(common)
    assert hi == 100 and lo < 25 and set(parts) >= {"модель", "тираж", "стабильность"}


def test_daily_volatility_bounds():
    stable = pts([10] * 20, list(range(20)))
    assert daily_volatility(stable, NOW) < 0.01
    assert daily_volatility([], NOW) == 0.05
