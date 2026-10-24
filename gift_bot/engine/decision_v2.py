"""Решение о покупке v2: справедливая цена, фундаментальность, ликвидность, EV, Келли.

decide() — чистая функция от снимка рынка, поэтому одинаково работает в боте и в бэктесте.
Порядок проверок (первая не пройденная — причина пропуска):
  новая коллекция (блок) → fair → ликвидность → фундаментальность → EV → размер позиции.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from gift_bot.models import Listing
from gift_bot.pricing.analytics import (ExpectedValue, FairValue, Features, Liquidity, SalePoint,
                                        daily_volatility, expected_value, fair_value, floor_depth,
                                        fundamental_score, kelly_size, liquidity, p_sale,
                                        robust_median, trend)

NEW_MAX_VOL_DISCOUNT = 0.20
MIN_LOSS_SHARE = 0.05   # для Келли: минимальный ожидаемый убыток при неудаче — 5% цены


@dataclass
class MarketSnapshot:
    """Всё, что известно о модели в момент решения (кандидат исключён из стаканов)."""

    now: datetime
    listing: Listing
    sales: list[tuple[str, float, datetime]]       # (площадка, цена сделки, время) — все площадки, 30 дней
    floors: dict[str, list[float]]                 # площадка -> цены лотов по возрастанию
    supply: int | None = None
    upgraded: int | None = None
    created_at: datetime | None = None             # дата выхода коллекции, если известна
    first_seen: datetime | None = None             # когда бот впервые увидел коллекцию
    watch_started: datetime | None = None          # когда бот начал наблюдать рынок
    capital: float = 100.0
    collection_exposure: float = 0.0              # уже вложено в эту коллекцию, TON


@dataclass
class SellOption:
    market: str
    ask: float          # цена выставления
    net: float          # на руки после комиссий (и переноса, если площадка другая)


@dataclass
class DecisionV2:
    buy: bool
    reason: str | None
    fair_net: float | None = None
    fair_gross: float | None = None                # fair в ценах витрины площадки кандидата
    fv: FairValue | None = None
    score: float | None = None
    score_parts: dict = field(default_factory=dict)
    liq: Liquidity | None = None
    ev: ExpectedValue | None = None
    cost: float = 0.0
    flip: SellOption | None = None                 # продажа на той же площадке (исполняется)
    best: SellOption | None = None                 # лучшая площадка (если другая — арбитраж-сигнал)
    required_pct: float = 0.0
    age_days: float | None = None
    is_new: bool = False
    size_limit: float | None = None
    vol: float = 0.0

    def details(self) -> dict:
        """Для лога/панели: почему принято решение."""
        d = {"reason": self.reason, "fair": self.fair_net, "score": self.score, "score_parts": self.score_parts,
             "required_pct": self.required_pct, "new_collection": self.is_new, "age_days": self.age_days,
             "vol_daily": round(self.vol, 4)}
        if self.fv:
            d.update(sales_median=self.fv.sales_median, floor_depth=self.fv.floor_depth,
                     weight_sales=self.fv.weight_sales, discount=self.fv.discount, trend=self.fv.trend,
                     discount_parts=self.fv.parts)
        if self.liq:
            d.update(sales_24h=self.liq.sales_24h, sales_7d=self.liq.sales_7d, volume_7d=self.liq.volume_7d,
                     active_listings=self.liq.active_listings, days_to_sell=self.liq.days_to_sell,
                     spread=self.liq.spread)
        if self.ev:
            d.update(ev=self.ev.ev, ev_pct=self.ev.ev_pct, p_sale=self.ev.p, risk=self.ev.risk)
        if self.flip:
            d.update(sell_market=self.flip.market, ask=self.flip.ask, net=self.flip.net)
        if self.size_limit is not None:
            d["size_limit"] = round(self.size_limit, 2)
        return {k: (None if isinstance(v, float) and math.isinf(v) else v) for k, v in d.items()}


def collection_age(snap: MarketSnapshot) -> float | None:
    """Возраст коллекции в днях; None — коллекция существовала до начала наблюдения (зрелая)."""
    if snap.created_at:
        return (snap.now - snap.created_at).total_seconds() / 86400
    if snap.first_seen and snap.watch_started and \
            (snap.first_seen - snap.watch_started).total_seconds() > 86400:
        return (snap.now - snap.first_seen).total_seconds() / 86400
    return None


@dataclass
class MarketState:
    fair_net: float | None
    fv: FairValue
    liq: Liquidity
    vol: float


def assess(snap: MarketSnapshot, cfg, is_new: bool = False) -> MarketState:
    """Справедливая цена и ликвидность модели по всем площадкам (без решения о покупке)."""
    markets = set(snap.floors) | {s[0] for s in snap.sales} | {snap.listing.market}
    sell_fee = {m: cfg.fees(m).sell for m in markets}
    points = [SalePoint(price * (1 - sell_fee[m]), ts) for m, price, ts in snap.sales]
    window = cfg.new_window_days if is_new else 7
    med7, n7 = robust_median(points, snap.now, window, cfg.sales_half_life_days, cfg.mad_k)
    med30, n30 = robust_median(points, snap.now, 30, cfg.sales_half_life_days, cfg.mad_k)
    floor_px, _ = floor_depth(sorted(p * (1 - sell_fee[m]) for m, ps in snap.floors.items() for p in ps))
    vol = daily_volatility(points, snap.now)
    fv = fair_value(med7, n7, med30, n30, floor_px, trend(points, snap.now), cfg)
    fair = fv.fair
    if fair is not None and is_new:  # доп. дисконт за волатильность молодой коллекции
        extra = min(vol * cfg.new_vol_discount_k, NEW_MAX_VOL_DISCOUNT)
        fv.parts["новая коллекция"] = round(extra, 4)
        fair *= 1 - extra
    active = sum(len(ps) for ps in snap.floors.values())
    liq = liquidity(points, snap.now, active, floor_px, fv.sales_median or med30)
    return MarketState(fair, fv, liq, vol)


def reprice_step_mult(days_to_sell: float) -> float:
    """Ликвидная модель — снижаем цену медленнее (×0.5), неликвидная — быстрее (до ×1.5)."""
    if math.isinf(days_to_sell) or days_to_sell >= 5:
        return 1.5
    if days_to_sell <= 1:
        return 0.5
    return 0.5 + (days_to_sell - 1) / 4


def decide(snap: MarketSnapshot, cfg, fees, base_required_pct: float) -> DecisionV2:
    """fees — объект с buy_cost(market, price), sell_net(market, ask), min_ask(market, cost)."""
    lst = snap.listing
    sell_fee = {m: cfg.fees(m).sell for m in set(snap.floors) | {s[0] for s in snap.sales} | {lst.market}}
    cost = fees.buy_cost(lst.market, lst.price)
    d = DecisionV2(buy=False, reason=None, cost=round(cost, 4))

    # 1. Новые коллекции
    d.age_days = collection_age(snap)
    if d.age_days is not None:
        d.age_days = round(d.age_days, 2)
        if d.age_days < cfg.new_collection_block_days:
            d.reason = f"новая коллекция: {d.age_days:.1f} дн < {cfg.new_collection_block_days:g} — не покупаем"
            return d
        d.is_new = d.age_days < cfg.new_collection_days

    # 2–3. Справедливая цена и ликвидность (всё в TON на руки)
    st = assess(snap, cfg, d.is_new)
    d.fv, d.vol, d.liq = st.fv, st.vol, st.liq
    if st.fair_net is None:
        d.reason = "нет данных о цене (ни продаж, ни лотов)"
        return d
    fair = st.fair_net
    d.fair_net = round(fair, 4)
    d.fair_gross = round(fair / (1 - sell_fee[lst.market]), 4)

    if (reason := d.liq.reject_reason(cfg)):
        d.reason = reason
        return d
    if d.is_new and d.liq.sales_7d < cfg.new_min_sales:
        d.reason = f"новая коллекция: продаж за 7д {d.liq.sales_7d} < {cfg.new_min_sales}"
        return d

    # 4. Фундаментальность
    d.score, d.score_parts = fundamental_score(Features(
        supply=snap.supply, upgraded=snap.upgraded, model_rarity=lst.model_rarity,
        backdrop_rarity=lst.backdrop_rarity, symbol_rarity=lst.symbol_rarity,
        age_days=d.age_days, vol_daily=d.vol))
    if d.score < cfg.min_score:
        d.reason = f"фундаментальность {d.score:.0f} < {cfg.min_score:.0f}"
        return d

    # 5. Где продавать: min(fair, 2-й лот площадки) чуть ниже рынка
    options = []
    for m in sorted(set(snap.floors) | {lst.market}):
        fair_m = fair / (1 - sell_fee[m])
        book = sorted(snap.floors.get(m, []))
        ref = min(fair_m, book[1] if len(book) > 1 else (book[0] if book else fair_m))
        ask = round(ref * (1 - cfg.undercut_pct / 100), 2)
        net = fees.sell_net(m, ask) - (cfg.transfer_cost_ton if m != lst.market else 0)
        options.append(SellOption(m, ask, round(net, 4)))
    d.flip = next(o for o in options if o.market == lst.market)
    d.best = max(options, key=lambda o: o.net)

    # 6. Ожидаемая прибыль с поправкой на риск
    hold = min(d.liq.days_to_sell, cfg.max_days_to_sell)
    d.ev = expected_value(cost, d.flip.net, p_sale(d.liq.days_to_sell, cfg.max_days_to_sell), d.vol, hold)
    d.required_pct = round(base_required_pct * (cfg.new_profit_mult if d.is_new else 1), 2)
    if d.ev.ev_pct < d.required_pct:
        d.reason = f"EV {d.ev.ev_pct:+.1f}% < порога {d.required_pct:g}%"
        return d

    # 7. Размер позиции: дробный Келли и концентрация по коллекции
    loss = cost * max(d.vol * math.sqrt(max(hold, 0.25)), MIN_LOSS_SHARE)
    d.size_limit = kelly_size(d.ev.p, d.flip.net - cost, loss, snap.capital, cfg.kelly_fraction)
    if d.is_new:
        d.size_limit *= cfg.new_size_mult
    if cost > d.size_limit:
        d.reason = f"limit:kelly — цена {cost:.2f} > допустимых {d.size_limit:.2f} TON"
        return d
    cap = cfg.max_collection_share * snap.capital
    if snap.collection_exposure + cost > cap:
        d.reason = f"limit:concentration — в коллекции уже {snap.collection_exposure:.2f}, лимит {cap:.2f} TON"
        return d

    d.buy = True
    return d
