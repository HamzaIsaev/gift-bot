"""Торговая математика v2 — чистые функции без сети и БД.

Все цены здесь — TON «на руки» продавцу (после комиссии площадки), чтобы данные
разных площадок были сопоставимы. Функции используются и ботом, и бэктестом.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class SalePoint:
    price: float          # TON на руки продавцу
    ts: datetime


# ---------- устойчивые статистики ----------

def decay_weights(ages_days: list[float], half_life_days: float) -> list[float]:
    """Свежие продажи весят больше: вес падает вдвое каждые half_life дней."""
    return [0.5 ** (max(a, 0.0) / half_life_days) for a in ages_days]


def weighted_median(values: list[float], weights: list[float]) -> float | None:
    if not values:
        return None
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def mad_mask(values: list[float], k: float) -> list[bool]:
    """True — значение не выброс: |x − медиана| ≤ k·MAD (MAD масштабирован под σ)."""
    if len(values) < 3:
        return [True] * len(values)
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values) * 1.4826
    if mad == 0:
        return [abs(v - med) <= med * 0.01 for v in values]
    return [abs(v - med) <= k * mad for v in values]


def robust_median(points: list[SalePoint], now: datetime, window_days: float,
                  half_life_days: float, mad_k: float) -> tuple[float | None, int]:
    """Медиана продаж за окно: выбросы по MAD отброшены, свежие продажи весят больше.
    Возвращает (медиана, сколько продаж учтено)."""
    since = now - timedelta(days=window_days)
    pts = [p for p in points if since <= p.ts <= now]
    if not pts:
        return None, 0
    keep = mad_mask([p.price for p in pts], mad_k)
    pts = [p for p, ok in zip(pts, keep) if ok]
    ages = [(now - p.ts).total_seconds() / 86400 for p in pts]
    return weighted_median([p.price for p in pts], decay_weights(ages, half_life_days)), len(pts)


def floor_depth(prices: list[float]) -> tuple[float | None, int]:
    """Цена «глубины» стакана: не 1-й лот (приманка/демпинг), а среднее 2-го и 3-го.
    Возвращает (цена, сколько лотов в стакане)."""
    p = sorted(prices)
    if not p:
        return None, 0
    if len(p) == 1:
        return p[0], 1
    if len(p) == 2:
        return p[1], 2
    return (p[1] + p[2]) / 2, len(p)


def daily_medians(points: list[SalePoint], now: datetime, days: int) -> list[float]:
    """Медиана продаж по дням (старые → новые); дни без продаж заполняются предыдущим значением."""
    buckets: dict[int, list[float]] = {}
    for p in points:
        d = (now - p.ts).days
        if 0 <= d < days:
            buckets.setdefault(d, []).append(p.price)
    out, last = [], None
    for d in range(days - 1, -1, -1):
        if d in buckets:
            last = statistics.median(buckets[d])
        if last is not None:
            out.append(last)
    return out


def ema(values: list[float], span: int) -> float | None:
    if not values:
        return None
    alpha = 2 / (span + 1)
    acc = values[0]
    for v in values[1:]:
        acc = alpha * v + (1 - alpha) * acc
    return acc


def trend(points: list[SalePoint], now: datetime, fast: int = 3, slow: int = 14) -> float:
    """EMA(3д) / EMA(14д) − 1: < 0 — цена падает."""
    series = daily_medians(points, now, slow)
    if len(series) < fast + 1:
        return 0.0
    f, s = ema(series[-fast:], fast), ema(series, slow)
    return (f / s - 1) if s else 0.0


def daily_volatility(points: list[SalePoint], now: datetime, days: int = 30) -> float:
    """Дневная волатильность: σ логарифмических изменений дневной медианы.
    Мало данных — грубая оценка через разброс цен продаж."""
    series = daily_medians(points, now, days)
    changes = [math.log(b / a) for a, b in zip(series, series[1:]) if a > 0 and b > 0 and a != b]
    if len(changes) >= 5:
        return statistics.pstdev(changes)
    prices = [p.price for p in points if (now - p.ts).days < days]
    if len(prices) >= 3:
        mean = statistics.fmean(prices)
        return (statistics.pstdev(prices) / mean) / math.sqrt(7) if mean else 0.05
    return 0.05  # данных нет — считаем умеренно рискованным


# ---------- справедливая цена ----------

@dataclass
class FairValue:
    fair: float | None
    sales_median: float | None
    floor_depth: float | None
    weight_sales: float
    discount: float               # итоговый дисконт (0.07 = −7%)
    trend: float
    parts: dict = field(default_factory=dict)


def fair_value(med7: float | None, n7: int, med30: float | None, n30: int,
               floor_px: float | None, trend_ratio: float, cfg) -> FairValue:
    """fair = смесь медианы продаж и глубины floor. Чем меньше продаж, тем больше вес floor
    и сильнее дисконт; падающий тренд даёт дополнительный дисконт."""
    if med7 is not None and n7 >= cfg.min_sales_for_blend:
        sales = 0.7 * med7 + 0.3 * med30 if med30 else med7
    else:
        sales = med30
    n_eff = n7 + 0.3 * max(n30 - n7, 0)
    w = min(n_eff / (n_eff + cfg.fair_sales_k), cfg.fair_max_sales_weight) if sales else 0.0

    if sales is None and floor_px is None:
        return FairValue(None, None, None, 0.0, 0.0, trend_ratio)
    base = (w * sales + (1 - w) * floor_px) if (sales and floor_px) else (sales or floor_px)

    disc = 0.0
    parts = {}
    if n_eff < cfg.min_sales_for_blend:
        parts["мало продаж"] = cfg.fair_thin_discount
        disc += cfg.fair_thin_discount
    if trend_ratio < 0:
        t = min(-trend_ratio * cfg.fair_trend_k, cfg.fair_trend_max_discount)
        parts["падающий тренд"] = round(t, 4)
        disc += t
    return FairValue(round(base * (1 - disc), 4), sales, floor_px, round(w, 3), round(disc, 4),
                     round(trend_ratio, 4), parts)


# ---------- ликвидность ----------

@dataclass
class Liquidity:
    sales_24h: int
    sales_7d: int
    volume_7d: float
    active_listings: int
    days_to_sell: float
    spread: float                 # (медиана − floor) / медиана, по модулю

    def reject_reason(self, cfg) -> str | None:
        if self.sales_7d < cfg.min_sales_7d:
            return f"неликвид: продаж за 7д {self.sales_7d} < {cfg.min_sales_7d}"
        if self.volume_7d < cfg.min_volume_ton:
            return f"неликвид: оборот {self.volume_7d:.0f} < {cfg.min_volume_ton:.0f} TON"
        if self.days_to_sell > cfg.max_days_to_sell:
            return f"неликвид: продажа ~{self.days_to_sell:.1f} дн > {cfg.max_days_to_sell}"
        if self.spread > cfg.max_spread:
            return f"неликвид: спред {self.spread:.0%} > {cfg.max_spread:.0%}"
        return None


def liquidity(points: list[SalePoint], now: datetime, active_listings: int,
              floor_px: float | None, median: float | None) -> Liquidity:
    s24 = sum(1 for p in points if now - p.ts <= timedelta(days=1))
    week = [p for p in points if now - p.ts <= timedelta(days=7)]
    per_day = len(week) / 7
    days = active_listings / per_day if per_day > 0 else float("inf")
    spread = abs(median - floor_px) / median if (median and floor_px) else 1.0
    return Liquidity(s24, len(week), round(sum(p.price for p in week), 2), active_listings,
                     round(days, 2), round(spread, 4))


# ---------- ожидаемая прибыль и размер позиции ----------

def p_sale(days_to_sell: float, horizon_days: float) -> float:
    """Вероятность продать в разумный срок: быстро продаётся → почти 1, медленно → меньше."""
    if math.isinf(days_to_sell):
        return 0.1
    return max(0.1, min(0.97, 1 - days_to_sell / (2 * horizon_days)))


@dataclass
class ExpectedValue:
    ev: float
    ev_pct: float
    p: float
    risk: float
    hold_days: float


def expected_value(cost: float, net_sell: float, p: float, vol_daily: float, hold_days: float) -> ExpectedValue:
    """EV = (продажа − покупка)·P(продажи) − риск, риск ≈ σ·√дней·цена."""
    hold = max(hold_days, 0.25)
    risk = vol_daily * math.sqrt(hold) * cost
    ev = (net_sell - cost) * p - risk
    return ExpectedValue(round(ev, 4), round(ev / cost * 100, 2) if cost else 0.0, round(p, 3),
                         round(risk, 4), round(hold, 2))


def kelly_size(p: float, gain: float, loss: float, capital: float, fraction: float) -> float:
    """Дробный Келли: доля капитала f* = p − (1−p)/b, b = выигрыш/проигрыш."""
    if gain <= 0 or loss <= 0 or capital <= 0:
        return 0.0
    f = p - (1 - p) / (gain / loss)
    return max(0.0, f * fraction * capital)


# ---------- фундаментальность ----------

def _scale_log(x: float | None, best: float, worst: float, unknown: float = 0.5) -> float:
    """1.0 при x ≤ best, 0.2 при x ≥ worst, между — по логарифмической шкале."""
    if x is None or x <= 0:
        return unknown
    if x <= best:
        return 1.0
    if x >= worst:
        return 0.2
    return 1.0 - 0.8 * (math.log(x / best) / math.log(worst / best))


@dataclass
class Features:
    supply: int | None = None            # тираж коллекции
    upgraded: int | None = None          # сколько улучшено до NFT
    model_rarity: float | None = None    # % владельцев с такой моделью
    backdrop_rarity: float | None = None
    symbol_rarity: float | None = None
    age_days: float | None = None        # None — коллекция старше, чем мы наблюдаем
    vol_daily: float = 0.05


FUNDAMENTAL_WEIGHTS = {"модель": 20, "фон": 8, "символ": 7, "тираж": 15, "улучшено": 10,
                       "возраст": 15, "стабильность": 25}


def fundamental_score(f: Features) -> tuple[float, dict[str, float]]:
    """0–100 и разбивка по факторам (каждый 0–1)."""
    age = 0.8 if f.age_days is None else min(1.0, 0.3 + 0.7 * f.age_days / 60)
    stability = max(0.0, min(1.0, 1 - (f.vol_daily - 0.02) / 0.13))
    parts = {
        "модель": _scale_log(f.model_rarity, 0.5, 5.0),
        "фон": _scale_log(f.backdrop_rarity, 0.5, 5.0),
        "символ": _scale_log(f.symbol_rarity, 0.3, 3.0),
        "тираж": _scale_log(f.supply, 10_000, 500_000),
        "улучшено": _scale_log(f.upgraded, 5_000, 300_000),
        "возраст": round(age, 3),
        "стабильность": round(stability, 3),
    }
    parts = {k: round(v, 3) for k, v in parts.items()}
    score = sum(FUNDAMENTAL_WEIGHTS[k] * v for k, v in parts.items()) / sum(FUNDAMENTAL_WEIGHTS.values())
    return round(score * 100, 1), parts
