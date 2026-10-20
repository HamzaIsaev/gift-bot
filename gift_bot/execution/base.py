"""Исполнение сделок.

Сейчас есть только виртуальный исполнитель (режим наблюдения). Реальный
появится после подтверждения боевого режима и будет реализовывать тот же интерфейс.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from gift_bot.engine.strategy import Plan
from gift_bot.models import parse_dt
from gift_bot.storage.db import Database, sqlite_row


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
