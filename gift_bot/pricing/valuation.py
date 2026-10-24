"""Рыночная цена модели подарка и расчёт выгоды сделки с учётом комиссий."""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from dataclasses import dataclass

from gift_bot.config import Settings
from gift_bot.markets.base import MarketAdapter
from gift_bot.models import Listing
from gift_bot.storage.db import Database

log = logging.getLogger(__name__)

FLOOR_CACHE_TTL = 180.0


@dataclass
class MarketView:
    """Что известно о модели на конкретной площадке."""

    market: str
    floor: float | None          # самый дешёвый лот, не считая кандидата
    sales_median: float | None
    sales_count: int
    sales_cv: float              # коэффициент вариации цен продаж (волатильность)

    @property
    def market_price(self) -> float | None:
        """Рыночная цена: среднее floor и медианы продаж, если продаж достаточно."""
        if self.floor and self.sales_median:
            return (self.floor + self.sales_median) / 2
        return self.floor or self.sales_median

    def ask_price(self, undercut_pct: float) -> float | None:
        """Цена выставления: чуть ниже рынка и не выше floor, чтобы продаться первыми."""
        mp = self.market_price
        if mp is None:
            return None
        ref = min(mp, self.floor) if self.floor else mp
        return round(ref * (1 - undercut_pct / 100), 2)


class Pricer:
    def __init__(self, db: Database, markets: dict[str, MarketAdapter], settings: Settings):
        self.db = db
        self.markets = markets
        self.s = settings
        self._floor_cache: dict[tuple[str, str], tuple[float, list[Listing]]] = {}
        self._sem = asyncio.Semaphore(3)

    # --- комиссии ---
    def buy_cost(self, market: str, price: float) -> float:
        return price * (1 + self.s.fees(market).buy) + self.s.network_fee_ton

    def sell_net(self, market: str, ask: float) -> float:
        return ask * (1 - self.s.fees(market).sell) - self.s.network_fee_ton

    def min_ask(self, market: str, cost: float) -> float:
        """Минимальная цена выставления, при которой сделка в ноль."""
        return round((cost + self.s.network_fee_ton) / (1 - self.s.fees(market).sell), 2)

    # --- данные рынка ---
    def cached_floor(self, market: str, key: str) -> list[Listing] | None:
        hit = self._floor_cache.get((market, key))
        if hit and time.monotonic() - hit[0] < FLOOR_CACHE_TTL:
            return hit[1]
        return None

    async def lowest(self, market: str, listing: Listing) -> list[Listing]:
        cached = self.cached_floor(market, listing.key)
        if cached is not None:
            return cached
        async with self._sem:
            try:
                res = await self.markets[market].lowest_listings(
                    listing.collection, listing.model, limit=self.s.floor_depth_limit)
            except Exception as e:
                log.warning("floor %s %s: %s", market, listing.key, e)
                return []
        self._floor_cache[(market, listing.key)] = (time.monotonic(), res)
        try:  # снимок стакана — для анализа и бэктеста
            await self.db.add_floor_snapshot(market, listing.key, [x.price for x in res])
        except Exception as e:
            log.debug("floor snapshot: %s", e)
        return res

    async def _sales_stats(self, key: str, market: str) -> tuple[float | None, int, float]:
        prices = await self.db.recent_sale_prices(key, self.s.sales_window_days, market=market)
        if len(prices) < self.s.min_sales_for_blend:
            return None, len(prices), 0.0
        mean = statistics.fmean(prices)
        cv = statistics.pstdev(prices) / mean if mean else 0.0
        return statistics.median(prices), len(prices), cv

    async def views(self, listing: Listing) -> dict[str, MarketView]:
        """Состояние модели на всех площадках (кандидат исключён из floor)."""
        names = list(self.markets)
        lows = await asyncio.gather(*(self.lowest(m, listing) for m in names))
        out = {}
        for m, lst in zip(names, lows):
            others = [x.price for x in lst
                      if not (x.market == listing.market and x.listing_id == listing.listing_id)]
            median, n, cv = await self._sales_stats(listing.key, m)
            out[m] = MarketView(market=m, floor=min(others) if others else None,
                                sales_median=median, sales_count=n, sales_cv=cv)
        return out

    async def reference_price(self, listing: Listing) -> float | None:
        """Дешёвая оценка без сетевых запросов — для отсева заведомо неинтересных лотов."""
        cached = self.cached_floor(listing.market, listing.key)
        refs = []
        if cached:
            others = [x.price for x in cached if x.listing_id != listing.listing_id]
            if others:
                refs.append(min(others))
        median, _, _ = await self._sales_stats(listing.key, listing.market)
        if median:
            refs.append(median)
        return max(refs) if refs else None
