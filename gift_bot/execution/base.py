"""Исполнение сделок.

VirtualExecutor — режим наблюдения (ничего не покупает), RealExecutor — боевой режим.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from gift_bot.engine.strategy import Plan
from gift_bot.markets.base import MarketAdapter
from gift_bot.models import parse_dt
from gift_bot.storage.db import Database, sqlite_row

log = logging.getLogger(__name__)


class Executor(ABC):
    mode_db: str

    def __init__(self, db: Database):
        self.db = db

    @abstractmethod
    async def buy_and_list(self, plan: Plan, signal_id: int) -> int:
        """Купить лот и сразу выставить по plan.ask_price. Возвращает id позиции."""

    @abstractmethod
    async def reprice(self, pos: sqlite_row, new_ask: float) -> None:
        """Изменить цену выставленного подарка."""

    @abstractmethod
    async def sold_price(self, pos: sqlite_row) -> float | None:
        """Если подарок продан — цена продажи, иначе None."""


class VirtualExecutor(Executor):
    """Ничего не покупает: записывает позицию и считает её проданной,
    когда на площадке проходит продажа той же модели не дешевле нашей цены
    (наш лот был бы дешевле и ушёл бы первым)."""

    mode_db = "virtual"

    async def buy_and_list(self, plan: Plan, signal_id: int) -> int:
        lst = plan.listing
        return await self.db.open_position(
            mode=self.mode_db, market=plan.target_market, listing_id=lst.listing_id,
            model_key=lst.key, title=lst.title, collection=lst.collection, model=lst.model,
            buy_price=lst.price, buy_cost=plan.buy_cost, market_price=plan.market_price,
            ask_price=plan.ask_price, min_price=plan.min_price, signal_id=signal_id,
        )

    async def reprice(self, pos: sqlite_row, new_ask: float) -> None:
        await self.db.reprice(pos["id"], new_ask)

    async def sold_price(self, pos: sqlite_row) -> float | None:
        sales = await self.db.sales_after(pos["model_key"], pos["market"], parse_dt(pos["last_reprice_at"]))
        if any(s["price"] >= pos["ask_price"] for s in sales):
            return pos["ask_price"]
        return None


class RealExecutor(Executor):
    """Реальные сделки. Каждая торговая операция — один запрос без повторов.

    Покупка только на площадке лота и перепродажа там же (арбитраж — только сигнал).
    Продажа определяется по инвентарю: подарка больше нет у нас на площадке.
    Чтобы случайный пустой ответ API не закрыл позицию, нужно два таких ответа подряд.
    """

    mode_db = "real"
    SOLD_CONFIRMATIONS = 2

    def __init__(self, db: Database, markets: dict[str, MarketAdapter]):
        super().__init__(db)
        self.markets = markets
        self._missing: dict[int, int] = {}

    async def balance(self, market: str) -> float:
        return await self.markets[market].balance()

    async def buy_and_list(self, plan: Plan, signal_id: int) -> int:
        lst = plan.listing
        item_id = await self.markets[lst.market].buy(lst)  # исключение — покупка не состоялась
        pos_id = await self.db.open_position(
            mode=self.mode_db, market=lst.market, listing_id=lst.listing_id, item_id=item_id,
            model_key=lst.key, title=lst.title, collection=lst.collection, model=lst.model,
            buy_price=lst.price, buy_cost=plan.buy_cost, market_price=plan.market_price,
            ask_price=plan.ask_price, min_price=plan.min_price, signal_id=signal_id, listed=0,
        )
        await self.ensure_listed(await self.db.get_position(pos_id))
        return pos_id

    async def ensure_listed(self, pos: sqlite_row) -> bool:
        """Выставить купленный подарок, если ещё не выставлен (повторяется в цикле позиций:
        у некоторых подарков после покупки бывает блокировка на перепродажу)."""
        if pos["listed"]:
            return True
        try:
            await self.markets[pos["market"]].list_for_sale(pos["item_id"], pos["ask_price"])
        except Exception as e:
            log.warning("Не удалось выставить #%s: %s", pos["id"], e)
            return False
        await self.db.mark_listed(pos["id"])
        return True

    async def reprice(self, pos: sqlite_row, new_ask: float) -> None:
        if pos["listed"]:
            await self.markets[pos["market"]].change_price(pos["item_id"], new_ask)
        await self.db.reprice(pos["id"], new_ask)

    async def sold_price(self, pos: sqlite_row) -> float | None:
        if not pos["listed"]:
            await self.ensure_listed(pos)
            return None
        owned = await self.markets[pos["market"]].owned_ids()
        if pos["item_id"] in owned:
            self._missing.pop(pos["id"], None)
            return None
        self._missing[pos["id"]] = self._missing.get(pos["id"], 0) + 1
        if self._missing[pos["id"]] < self.SOLD_CONFIRMATIONS:
            return None
        self._missing.pop(pos["id"], None)
        return pos["ask_price"]
