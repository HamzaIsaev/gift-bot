"""Главный цикл: мониторинг лотов и продаж, сигналы, ведение позиций."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import timedelta
from typing import Awaitable, Callable

from gift_bot.bot import texts
from gift_bot.config import Settings
from gift_bot.engine.strategy import Evaluation, Plan, Strategy
from gift_bot.execution.base import Executor
from gift_bot.markets.base import MarketAdapter
from gift_bot.markets.manual_auth import AuthMissing
from gift_bot.models import Listing, parse_dt, utcnow
from gift_bot.pricing.valuation import Pricer
from gift_bot.risk.limits import RiskManager
from gift_bot.storage.db import Database, sqlite_row

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]
SEEN_MAX = 5000
PRE_FILTER_RATIO = 0.95      # лот дороже 95% известной цены — даже не считаем
POSITIONS_EVERY_SEC = 60
FOLLOW_FLOOR_AFTER = timedelta(minutes=30)


class Trader:
    def __init__(self, settings: Settings, db: Database, markets: dict[str, MarketAdapter],
                 pricer: Pricer, strategy: Strategy, risk: RiskManager,
                 executor: Executor, notify: Notify):
        self.s = settings
        self.db = db
        self.markets = markets
        self.pricer = pricer
        self.strategy = strategy
        self.risk = risk
        self.executor = executor
        self.notify = notify
        self._seen: dict[str, deque[str]] = {m: deque(maxlen=SEEN_MAX) for m in markets}
        self._seen_set: dict[str, set[str]] = {m: set() for m in markets}
        self._warmed: set[str] = set()
        self._errors: dict[str, int] = {m: 0 for m in markets}
        self._auth_warned: set[str] = set()

    # --- запуск ---
    async def run(self) -> None:
        tasks = [asyncio.create_task(self._market_loop(m), name=f"market:{m}") for m in self.markets]
        tasks.append(asyncio.create_task(self._positions_loop(), name="positions"))
        await asyncio.gather(*tasks)

    def _remember(self, market: str, listing_id: str) -> bool:
        """True — если лот новый."""
        if listing_id in self._seen_set[market]:
            return False
        dq = self._seen[market]
        if len(dq) == dq.maxlen:
            self._seen_set[market].discard(dq[0])
        dq.append(listing_id)
        self._seen_set[market].add(listing_id)
        return True

    async def _market_loop(self, market: str) -> None:
        while True:
            try:
                await self._market_loop_once(market)
                self._auth_warned.discard(market)
                if self._errors[market] >= 5:
                    await self.notify(f"✅ {market}: связь восстановлена")
                self._errors[market] = 0
            except asyncio.CancelledError:
                raise
            except AuthMissing:
                if market not in self._auth_warned:
                    self._auth_warned.add(market)
                    await self.notify(f"🔑 {market}: нет данных входа. Отправьте /auth — там инструкция.")
                await asyncio.sleep(60)
                continue
            except Exception as e:
                self._errors[market] += 1
                log.warning("%s: ошибка цикла (%d подряд): %s", market, self._errors[market], e)
                if self._errors[market] == 5:
                    await self.notify(f"⚠️ {market}: 5 ошибок подряд — {type(e).__name__}: {str(e)[:200]}\n"
                                      f"Если вход ручной, возможно, устарели данные: обновите /auth {market}")
                await asyncio.sleep(min(60, self.s.poll_interval_sec * self._errors[market]))
            await asyncio.sleep(self.s.poll_interval_sec)

    async def _market_loop_once(self, market: str) -> None:
        adapter = self.markets[market]
        await self.db.add_sales(await adapter.recent_sales())
        listings = await adapter.latest_listings()
        fresh = [x for x in listings if self._remember(market, x.listing_id)]
        if market not in self._warmed:
            # первый проход — только запоминаем витрину, чтобы не принять старые лоты за новые
            self._warmed.add(market)
            log.info("%s: стартовая витрина %d лотов", market, len(listings))
            return
        for lst in fresh:
            await self.process_listing(lst)

    # --- обработка лота ---
    async def process_listing(self, lst: Listing) -> None:
        if self.risk.stopped:
            return
        ref = await self.pricer.reference_price(lst)
        if ref is not None and lst.price >= ref * PRE_FILTER_RATIO:
            return
        ev = await self.strategy.evaluate(lst)
        if ev.best is None and ev.arbitrage is None:
            return
        if ev.arbitrage:
            await self._record(ev.arbitrage, ev, "signal", None)
            await self.notify(texts.arbitrage_signal(ev.arbitrage, virtual=self.executor.mode_db == "virtual"))
        if ev.best is None:
            return
        if ev.reject_reason:
            await self._record(ev.best, ev, "skip", ev.reject_reason)
            return
        reason = await self.risk.check_trade(ev.best.buy_cost)
        if reason:
            await self._record(ev.best, ev, "skip", reason)
            return
        signal_id = await self._record(ev.best, ev, "buy", None, force=True)
        pos_id = await self.executor.buy_and_list(ev.best, signal_id)
        await self.notify(texts.bought(ev.best, pos_id, virtual=self.executor.mode_db == "virtual"))

    async def _record(self, plan: Plan, ev: Evaluation, decision: str, reason: str | None,
                      force: bool = False) -> int | None:
        discount = (1 - plan.listing.price / plan.market_price) * 100 if plan.market_price else 0
        if not force and decision == "skip" and discount < self.s.log_signal_min_discount_pct \
                and not (reason or "").startswith("limit:"):
            return None
        lst = plan.listing
        return await self.db.add_signal(
            mode=self.executor.mode_db, kind=plan.kind, market=lst.market,
            target_market=plan.target_market, listing_id=lst.listing_id, model_key=lst.key,
            title=lst.title, price=lst.price, buy_cost=plan.buy_cost, market_price=plan.market_price,
            floor_price=plan.view.floor, sales_median=plan.view.sales_median,
            sales_count=plan.view.sales_count, expected_net=plan.expected_net,
            profit_pct=plan.profit_pct, required_pct=ev.required_pct, decision=decision, reason=reason,
        )

    # --- позиции ---
    async def _positions_loop(self) -> None:
        while True:
            try:
                await self.manage_positions()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка обработки позиций")
            await asyncio.sleep(POSITIONS_EVERY_SEC)

    async def manage_positions(self) -> None:
        if self.risk.stopped:  # /stop останавливает всё, включая переоценку
            return
        for pos in await self.db.open_positions(self.executor.mode_db):
            sold = await self.executor.sold_price(pos)
            if sold is not None:
                await self._close(pos, sold)
                continue
            new_ask = await self._next_ask(pos)
            if new_ask is not None:
                await self.executor.reprice(pos, new_ask)
                await self.notify(texts.repriced(pos, new_ask, virtual=pos["mode"] == "virtual"))
        if await self.risk.check_daily_loss():
            await self.notify(texts.daily_loss_hit(await self.risk.daily_pnl(), self.risk.limits.max_daily_loss_ton))

    async def _next_ask(self, pos: sqlite_row) -> float | None:
        """Снижение цены: по таймеру на шаг, а если рынок ушёл ниже — сразу под floor.
        Никогда не ниже цены безубыточности (min_price)."""
        ask, min_price = pos["ask_price"], pos["min_price"]
        if ask <= min_price:
            return None
        now = utcnow()
        since_reprice = now - parse_dt(pos["last_reprice_at"])
        candidate = None
        if since_reprice >= timedelta(hours=self.s.reprice_after_hours):
            candidate = ask * (1 - self.s.reprice_step_pct / 100)
        if self.s.adaptive and since_reprice >= FOLLOW_FLOOR_AFTER:
            lows = await self.pricer.lowest(pos["market"], Listing(
                market=pos["market"], listing_id=pos["listing_id"], collection=pos["collection"],
                model=pos["model"], backdrop="", symbol="", number=None, price=ask))
            others = [x.price for x in lows if x.listing_id != pos["listing_id"]]
            if others:
                market_floor = min(others)
                if market_floor < ask:
                    follow = market_floor * (1 - self.s.undercut_pct / 100)
                    candidate = min(candidate or ask, follow)
        if candidate is None:
            return None
        return round(max(candidate, min_price), 2)

    async def _close(self, pos: sqlite_row, sold: float) -> None:
        net = self.pricer.sell_net(pos["market"], sold)
        pnl = round(net - pos["buy_cost"], 4)
        await self.db.close_position(pos["id"], sold, round(net, 4), pnl)
        await self.notify(texts.sold(pos, sold, net, pnl, virtual=pos["mode"] == "virtual"))
