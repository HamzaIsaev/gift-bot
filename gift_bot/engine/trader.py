"""Главный цикл: мониторинг лотов и продаж, сигналы, ведение позиций."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import timedelta
from typing import Awaitable, Callable

from gift_bot.bot import texts
from gift_bot.config import Settings
from gift_bot.engine.decision_v2 import MarketState, assess, reprice_step_mult
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
COLLECTIONS_EVERY_SEC = 6 * 3600
FOLLOW_FLOOR_AFTER = timedelta(minutes=30)


class Trader:
    def __init__(self, settings: Settings, db: Database, markets: dict[str, MarketAdapter],
                 pricer: Pricer, strategy: Strategy, risk: RiskManager,
                 executor: Executor | dict[str, Executor], notify: Notify,
                 on_auth_missing: Callable[[], Awaitable[None]] | None = None):
        self.s = settings
        self.db = db
        self.markets = markets
        self.pricer = pricer
        self.strategy = strategy
        self.risk = risk
        # Исполнители по режимам: virtual — наблюдение, real — боевой
        self.executors = executor if isinstance(executor, dict) else {executor.mode_db: executor}
        self._buy_lock = asyncio.Lock()
        self.notify = notify
        self.on_auth_missing = on_auth_missing
        self._seen: dict[str, deque[str]] = {m: deque(maxlen=SEEN_MAX) for m in markets}
        self._seen_set: dict[str, set[str]] = {m: set() for m in markets}
        self._warmed: set[str] = set()
        self._errors: dict[str, int] = {m: 0 for m in markets}
        self._auth_warned: set[str] = set()
        self._exit_warned: set[int] = set()   # по каким позициям уже прислали сигнал на выход

    @property
    def executor(self) -> Executor | None:
        return self.executors.get(self.risk.mode_db)

    @property
    def virtual(self) -> bool:
        return self.risk.mode_db == "virtual"

    # --- запуск ---
    async def run(self) -> None:
        tasks = [asyncio.create_task(self._market_loop(m), name=f"market:{m}") for m in self.markets]
        tasks.append(asyncio.create_task(self._positions_loop(), name="positions"))
        tasks.append(asyncio.create_task(self._collections_loop(), name="collections"))
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
                if self.on_auth_missing:  # есть сессия Telegram Web — пробуем обновить вход сами
                    asyncio.create_task(self.on_auth_missing())
                elif market not in self._auth_warned:
                    self._auth_warned.add(market)
                    await self.notify(f"🔑 {market}: нет данных входа. Отправьте /auth — там инструкция.")
                await asyncio.sleep(60)
                continue
            except Exception as e:
                self._errors[market] += 1
                if self.on_auth_missing and self._errors[market] >= 3:  # возможно, вход устарел
                    asyncio.create_task(self.on_auth_missing())
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
            await self._remember_market_data(lst)
            await self.process_listing(lst)

    async def _remember_market_data(self, lst: Listing) -> None:
        """История для анализа/бэктеста и учёт коллекций (тираж, когда впервые увидели)."""
        try:
            await self.db.record_listing(lst)
            await self.db.upsert_collection(lst.collection_key, lst.collection,
                                            supply=lst.coll_supply, upgraded=lst.coll_upgraded)
        except Exception as e:
            log.debug("market data: %s", e)

    async def _collections_loop(self) -> None:
        """Раз в 6 часов: тираж и дата выхода коллекций с площадок, которые это отдают."""
        while True:
            for name, market in self.markets.items():
                try:
                    for c in await market.collections_info():
                        await self.db.upsert_collection(c.key, c.name, supply=c.supply,
                                                        upgraded=c.upgraded, created_at=c.created_at)
                except Exception as e:
                    log.debug("collections %s: %s", name, e)
            await asyncio.sleep(COLLECTIONS_EVERY_SEC)

    # --- обработка лота ---
    async def process_listing(self, lst: Listing) -> None:
        if self.risk.stopped or self.executor is None:
            return
        if await self.db.is_our_item(lst.market, lst.listing_id):
            return  # это наш собственный выставленный подарок
        ref = await self.pricer.reference_price(lst)
        if ref is not None and lst.price >= ref * PRE_FILTER_RATIO:
            return
        ev = await self.strategy.evaluate(lst)
        if ev.best is None and ev.arbitrage is None:
            return
        if ev.arbitrage:
            await self._record(ev.arbitrage, ev, "signal", None)
            await self.notify(texts.arbitrage_signal(ev.arbitrage, virtual=self.virtual))
        if ev.best is None:
            return
        if ev.reject_reason:
            await self._record(ev.best, ev, "skip", ev.reject_reason)
            return
        async with self._buy_lock:  # лимиты проверяются и сделка проходит по одной за раз
            reason = await self.risk.check_trade(ev.best.buy_cost)
            if not reason and not self.virtual:
                try:
                    bal = await self.executor.balance(lst.market)
                except Exception as e:
                    bal, reason = 0.0, f"balance_error: {e}"
                if not reason and bal < ev.best.buy_cost:
                    reason = f"limit:balance — на {lst.market} {bal:.2f} TON < {ev.best.buy_cost:.2f}"
            if reason:
                await self._record(ev.best, ev, "skip", reason)
                return
            executor = self.executor
            try:
                pos_id = await executor.buy_and_list(ev.best, 0)
            except Exception as e:
                log.warning("Покупка не удалась %s: %s", lst.title, e)
                await self._record(ev.best, ev, "skip", f"buy_failed: {str(e)[:150]}", force=True)
                if not self.virtual:
                    await self.notify(texts.buy_failed(ev.best, e))
                return
            signal_id = await self._record(ev.best, ev, "buy", None, force=True)
            await self.db.link_signal(pos_id, signal_id)
        await self.notify(texts.bought(ev.best, pos_id, virtual=executor.mode_db == "virtual"))

    async def _record(self, plan: Plan, ev: Evaluation, decision: str, reason: str | None,
                      force: bool = False) -> int | None:
        discount = (1 - plan.listing.price / plan.market_price) * 100 if plan.market_price else 0
        if not force and decision == "skip" and discount < self.s.log_signal_min_discount_pct \
                and not (reason or "").startswith("limit:"):
            return None
        lst = plan.listing
        extra: dict = {"strategy": "v1"}
        if ev.v2 is not None:  # v2: всё, на чём основано решение — для панели и анализа
            d = ev.v2
            extra = {"strategy": "v2", "fair": d.fair_gross, "score": d.score,
                     "sales_7d": d.liq.sales_7d if d.liq else None,
                     "days_to_sell": d.liq.days_to_sell if d.liq and d.liq.days_to_sell != float("inf") else None,
                     "ev_pct": d.ev.ev_pct if d.ev else None,
                     "details": json.dumps(d.details(), ensure_ascii=False, default=str)}
        return await self.db.add_signal(
            mode=self.risk.mode_db, kind=plan.kind, market=lst.market,
            target_market=plan.target_market, listing_id=lst.listing_id, model_key=lst.key,
            title=lst.title, price=lst.price, buy_cost=plan.buy_cost, market_price=plan.market_price,
            floor_price=plan.view.floor, sales_median=plan.view.sales_median,
            sales_count=plan.view.sales_count, expected_net=plan.expected_net,
            profit_pct=plan.profit_pct, required_pct=ev.required_pct, decision=decision, reason=reason,
            **extra,
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
        # Ведём позиции обоих режимов: смена режима не бросает уже купленные подарки
        for pos in await self.db.open_positions():
            executor = self.executors.get(pos["mode"])
            if executor is None:
                continue
            try:
                sold = await executor.sold_price(pos)
                if sold is not None:
                    await self._close(pos, sold)
                    continue
                new_ask = await self._next_ask(pos)
                if new_ask is not None:
                    await executor.reprice(pos, new_ask)
                    await self.notify(texts.repriced(pos, new_ask, virtual=pos["mode"] == "virtual"))
            except Exception as e:
                log.warning("Позиция #%s: %s", pos["id"], e)
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
        pos_listing = Listing(market=pos["market"], listing_id=pos["listing_id"], collection=pos["collection"],
                              model=pos["model"], backdrop="", symbol="", number=None, price=ask)
        step = self.s.reprice_step_pct
        if self.strategy.use_v2() and since_reprice >= FOLLOW_FLOOR_AFTER:
            state = await self._market_state(pos, pos_listing)
            if state is not None:
                step *= reprice_step_mult(state.liq.days_to_sell)  # ликвид — медленнее, неликвид — быстрее
        candidate = None
        if since_reprice >= timedelta(hours=self.s.reprice_after_hours):
            candidate = ask * (1 - step / 100)
        if self.s.adaptive and since_reprice >= FOLLOW_FLOOR_AFTER:
            lows = await self.pricer.lowest(pos["market"], pos_listing)
            ours = {pos["listing_id"], pos["item_id"]}
            others = [x.price for x in lows if x.listing_id not in ours]
            if others:
                market_floor = min(others)
                if market_floor < ask:
                    follow = market_floor * (1 - self.s.undercut_pct / 100)
                    candidate = min(candidate or ask, follow)
        if candidate is None:
            return None
        return round(max(candidate, min_price), 2)

    async def _market_state(self, pos: sqlite_row, pos_listing: Listing) -> MarketState | None:
        """Fair и ликвидность модели позиции; заодно сигнал на выход, если fair упал и тренд вниз."""
        try:
            snap = await self.strategy.snapshot(pos_listing)
            snap.floors = {m: [p for p in ps if p != pos["ask_price"]] for m, ps in snap.floors.items()}
            state = assess(snap, self.s)
        except Exception as e:
            log.debug("market state #%s: %s", pos["id"], e)
            return None
        if state.fair_net is not None and pos["id"] not in self._exit_warned:
            fair_gross = state.fair_net / (1 - self.s.fees(pos["market"]).sell)
            drop = (1 - fair_gross / pos["buy_cost"]) * 100
            if drop >= self.s.exit_drop_pct and state.fv.trend < 0:
                self._exit_warned.add(pos["id"])
                await self.notify(texts.exit_signal(pos, fair_gross, drop, state.fv.trend,
                                                    virtual=pos["mode"] == "virtual"))
        return state

    async def _close(self, pos: sqlite_row, sold: float) -> None:
        net = self.pricer.sell_net(pos["market"], sold)
        pnl = round(net - pos["buy_cost"], 4)
        await self.db.close_position(pos["id"], sold, round(net, 4), pnl)
        await self.notify(texts.sold(pos, sold, net, pnl, virtual=pos["mode"] == "virtual"))
