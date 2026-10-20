"""Оценка лота: выгодно ли купить и где потом продать.

Адаптивный порог прибыли (если ADAPTIVE=true):
  * неликвидная модель (мало продаж за окно) — порог выше;
  * ликвидная (много продаж) — порог чуть ниже;
  * волатильная (большой разброс цен продаж) — порог выше;
  * модель, на которой бот недавно терял деньги, — пропускается;
  * если сегодня уже убыток больше половины дневного лимита — порог выше.
"""
from __future__ import annotations

from dataclasses import dataclass

from gift_bot.config import Settings
from gift_bot.models import Listing
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

    async def evaluate(self, listing: Listing) -> Evaluation:
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
