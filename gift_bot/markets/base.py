"""Общий интерфейс площадки.

В режиме наблюдения нужны только методы чтения. Методы покупки/выставления
появятся вместе с боевым режимом — до этого их в коде нет вообще.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from gift_bot.models import CollectionInfo, Listing, Sale

AUTH_TTL_SEC = 30 * 60
FORCED_REFRESH_MIN_SEC = 60  # не дёргать Telegram чаще раза в минуту при сетевых сбоях


class MarketError(Exception):
    pass


class MarketAdapter(ABC):
    name: str

    def __init__(self, init_data_provider: Callable[[], Awaitable[str]]):
        self._provide_init_data = init_data_provider
        self._auth: str | None = None
        self._auth_at = 0.0

    async def auth(self, force: bool = False) -> str:
        """initData живёт ограниченное время — обновляем по таймеру или после ошибки."""
        age = time.monotonic() - self._auth_at
        if force and age < FORCED_REFRESH_MIN_SEC:
            force = False
        if force or not self._auth or age > AUTH_TTL_SEC:
            self._auth = await self._provide_init_data()
            self._auth_at = time.monotonic()
        return self._auth

    def reset_auth(self) -> None:
        """Забыть данные входа — следующий запрос возьмёт свежие."""
        self._auth = None
        self._auth_at = 0.0

    async def with_auth(self, call: Callable[[str], Awaitable]):
        """Выполнить запрос; при ошибке один раз обновить авторизацию и повторить."""
        try:
            return await call(await self.auth())
        except Exception:
            return await call(await self.auth(force=True))

    async def once(self, call: Callable[[str], Awaitable]):
        """Для торговых операций: без повтора — иначе можно купить дважды."""
        return await call(await self.auth())

    @abstractmethod
    async def latest_listings(self, limit: int = 30) -> list[Listing]:
        """Самые свежие лоты."""

    @abstractmethod
    async def recent_sales(self, limit: int = 50) -> list[Sale]:
        """Последние продажи на площадке."""

    @abstractmethod
    async def lowest_listings(self, collection: str, model: str, limit: int = 3) -> list[Listing]:
        """Самые дешёвые лоты конкретной модели (для расчёта floor)."""

    async def collections_info(self) -> list[CollectionInfo]:
        """Тираж/дата создания коллекций, если площадка их отдаёт."""
        return []

    # --- торговля (боевой режим) ---
    async def balance(self) -> float:
        """Свободный баланс на площадке, TON."""
        raise NotImplementedError

    async def buy(self, listing: Listing) -> str:
        """Купить лот. Возвращает id подарка в нашем инвентаре на площадке."""
        raise NotImplementedError

    async def list_for_sale(self, item_id: str, price: float) -> None:
        raise NotImplementedError

    async def change_price(self, item_id: str, price: float) -> None:
        raise NotImplementedError

    async def owned_ids(self) -> set[str]:
        """id всех наших подарков на площадке (выставленных и нет)."""
        raise NotImplementedError
