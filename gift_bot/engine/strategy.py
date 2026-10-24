"""Оценка лота: выгодно ли купить и где потом продать.

Адаптивный порог прибыли (если ADAPTIVE=true):
  * неликвидная модель (мало продаж за окно) — порог выше;
  * ликвидная (много продаж) — порог чуть ниже;
  * волатильная (большой разброс цен продаж) — порог выше;
  * модель, на которой бот недавно терял деньги, — пропускается;
  * если сегодня уже убыток больше половины дневного лимита — порог выше.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from gift_bot.config import Settings
from gift_bot.engine.decision_v2 import DecisionV2, MarketSnapshot, decide
from gift_bot.models import Listing, parse_dt, utcnow
from gift_bot.pricing.valuation import MarketView, Pricer
from gift_bot.risk.limits import RiskManager
from gift_bot.storage.db import Database

LIQUID_SALES = 20
MAX_VOLATILITY_EXTRA = 15.0
TIGHTEN_EXTRA = 3.0


@dataclass
class Plan:
    kind: str                # flip | arbitrage
    listing: Listing
    target_market: str
    buy_cost: float
    market_price: float
    ask_price: float
    min_price: float
    expected_net: float
    profit_pct: float
    view: MarketView

    @property
    def profit_ton(self) -> float:
        return self.expected_net - self.buy_cost


@dataclass
class Evaluation:
    best: Plan | None             # лучший вариант (может не пройти порог)
    required_pct: float
    reject_reason: str | None     # почему не сигнал (None — сигнал)
    arbitrage: Plan | None = None  # отдельный межплощадочный сигнал, если есть
    v2: DecisionV2 | None = None  # подробности решения v2 (fair, score, ликвидность, EV)


class Strategy:
    def __init__(self, db: Database, pricer: Pricer, risk: RiskManager, settings: Settings):
        self.db = db
        self.pricer = pricer
        self.risk = risk
        self.s = settings

    async def required_pct(self, view: MarketView, key: str) -> tuple[float, str | None]:
        base = self.s.min_profit_pct
        if not self.s.adaptive:
            return base, None
        n, pnl = await self.db.model_pnl(key, self.risk.mode_db, self.s.loser_cooldown_days)
        if n >= 2 and pnl < 0:
            return base, f"adaptive: на этой модели убыток {pnl:.2f} TON за {self.s.loser_cooldown_days} дн."
        req = base
        if view.sales_count < self.s.min_sales_for_blend:
            req += self.s.illiquid_extra_pct
        elif view.sales_count >= LIQUID_SALES:
            req -= self.s.liquid_bonus_pct
        req += min(self.s.volatility_k * view.sales_cv, MAX_VOLATILITY_EXTRA)
        if await self.risk.daily_pnl() < -self.risk.limits.max_daily_loss_ton / 2:
            req += TIGHTEN_EXTRA
        return round(req, 2), None

    async def required_pct_v2(self, key: str) -> tuple[float, str | None]:
        """Адаптивный порог для v2: ликвидность и волатильность v2 учитывает сама (фильтры и риск в EV),
        здесь остаются убыточные модели и осторожность при дневной просадке."""
        base = self.s.min_profit_pct
        if not self.s.adaptive:
            return base, None
        n, pnl = await self.db.model_pnl(key, self.risk.mode_db, self.s.loser_cooldown_days)
        if n >= 2 and pnl < 0:
            return base, f"adaptive: на этой модели убыток {pnl:.2f} TON за {self.s.loser_cooldown_days} дн."
        if await self.risk.daily_pnl() < -self.risk.limits.max_daily_loss_ton / 2:
            base += TIGHTEN_EXTRA
        return base, None

    def _plan(self, listing: Listing, view: MarketView, cost: float) -> Plan | None:
        ask = view.ask_price(self.s.undercut_pct)
        if not ask:
            return None
        net = self.pricer.sell_net(view.market, ask)
        kind = "flip" if view.market == listing.market else "arbitrage"
        if kind == "arbitrage":
            net -= self.s.transfer_cost_ton
        return Plan(
            kind=kind, listing=listing, target_market=view.market, buy_cost=round(cost, 4),
            market_price=round(view.market_price, 4), ask_price=ask,
            min_price=self.pricer.min_ask(view.market, cost + (self.s.transfer_cost_ton if kind == "arbitrage" else 0)),
            expected_net=round(net, 4), profit_pct=round((net - cost) / cost * 100, 2), view=view,
        )

    def use_v2(self) -> bool:
        """v2 — сначала только в режиме наблюдения; в боевом — после STRATEGY_V2_LIVE=true."""
        if not self.s.strategy_v2:
            return False
        return self.risk.mode_db == "virtual" or self.s.strategy_v2_live

    async def evaluate(self, listing: Listing) -> Evaluation:
        if self.use_v2():
            return await self.evaluate_v2(listing)
        return await self.evaluate_v1(listing)

    # --- v2 ---
    async def capital(self) -> float:
        """Капитал для Келли и концентрации."""
        mode = self.risk.mode_db
        in_positions = sum(p["buy_cost"] for p in await self.db.open_positions(mode))
        if mode == "virtual":
            return self.s.virtual_balance_ton + await self.db.realized_pnl(mode)
        total = in_positions
        for m in self.pricer.markets.values():
            try:
                total += await m.balance()
            except Exception:
                pass
        return total

    async def snapshot(self, listing: Listing, now: datetime | None = None) -> MarketSnapshot:
        now = now or utcnow()
        names = list(self.pricer.markets)
        lows = await asyncio.gather(*(self.pricer.lowest(m, listing) for m in names))
        ours = {listing.listing_id}
        floors = {m: sorted(x.price for x in lst if x.listing_id not in ours) for m, lst in zip(names, lows)}
        sales = [(r["market"], r["price"], parse_dt(r["sold_at"]))
                 for r in await self.db.sales_rows(listing.key, 30, until=now)]
        coll = await self.db.get_collection(listing.collection_key)
        watch = await self.db.get_setting("db_created_at")
        exposure = sum(p["buy_cost"] for p in await self.db.open_positions(self.risk.mode_db)
                       if p["collection"].lower() == listing.collection.lower())
        return MarketSnapshot(
            now=now, listing=listing, sales=sales, floors=floors,
            supply=coll["supply"] if coll else None, upgraded=coll["upgraded"] if coll else None,
            created_at=parse_dt(coll["created_at"]) if coll and coll["created_at"] else None,
            first_seen=parse_dt(coll["first_seen"]) if coll else None,
            watch_started=parse_dt(watch) if watch else None,
            capital=await self.capital(), collection_exposure=exposure)

    async def evaluate_v2(self, listing: Listing) -> Evaluation:
        snap = await self.snapshot(listing)
        own_view = MarketView(listing.market, floor=(snap.floors.get(listing.market) or [None])[0],
                              sales_median=None, sales_count=0, sales_cv=0.0)
        base, block = await self.required_pct_v2(listing.key)
        d = decide(snap, self.s, self.pricer, base)
        if d.fair_net is None or d.flip is None:
            return Evaluation(None, base, block or d.reason or "нет данных", v2=d)
        view = MarketView(listing.market, floor=own_view.floor,
                          sales_median=d.fv.sales_median if d.fv else None,
                          sales_count=d.liq.sales_7d if d.liq else 0, sales_cv=d.vol)
        plan = Plan(
            kind="flip", listing=listing, target_market=listing.market, buy_cost=d.cost,
            market_price=d.fair_gross, ask_price=d.flip.ask,
            min_price=self.pricer.min_ask(listing.market, d.cost),
            expected_net=d.flip.net, profit_pct=round((d.flip.net - d.cost) / d.cost * 100, 2), view=view)
        arb = None
        if d.best and d.best.market != listing.market and d.best.net > d.flip.net and \
                (d.best.net - d.cost) / d.cost * 100 >= d.required_pct:
            arb = Plan(kind="arbitrage", listing=listing, target_market=d.best.market, buy_cost=d.cost,
                       market_price=d.fair_gross, ask_price=d.best.ask,
                       min_price=self.pricer.min_ask(d.best.market, d.cost + self.s.transfer_cost_ton),
                       expected_net=d.best.net, profit_pct=round((d.best.net - d.cost) / d.cost * 100, 2),
                       view=view)
        reason = block or (None if d.buy else d.reason)
        return Evaluation(plan, d.required_pct or base, reason, arb, v2=d)

    # --- v1 ---
    async def evaluate_v1(self, listing: Listing) -> Evaluation:
        cost = self.pricer.buy_cost(listing.market, listing.price)
        views = await self.pricer.views(listing)
        plans = [p for v in views.values() if (p := self._plan(listing, v, cost))]
        own = views[listing.market]
        required, block = await self.required_pct(own, listing.key)

        flip = next((p for p in plans if p.kind == "flip"), None)
        arbs = sorted((p for p in plans if p.kind == "arbitrage"), key=lambda p: p.profit_pct, reverse=True)
        arb = arbs[0] if arbs and arbs[0].profit_pct >= required else None

        if flip is None:
            return Evaluation(None, required, "нет данных о рыночной цене", arb)
        if block:
            return Evaluation(flip, required, block, None)
        if flip.profit_pct < required:
            return Evaluation(flip, required, f"прибыль {flip.profit_pct}% < порога {required}%", arb)
        return Evaluation(flip, required, None, arb)
