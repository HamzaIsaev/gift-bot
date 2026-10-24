"""Tonnel (market.tonnel.network) через библиотеку tonnelmp.

tonnelmp синхронная — вызовы уходят в отдельный поток, чтобы не блокировать бота.
"""
from __future__ import annotations

import asyncio
import logging

import tonnelmp

from gift_bot.markets.base import MarketAdapter, MarketError
from gift_bot.models import Listing, Sale, clean_attr, parse_dt, rarity_pct

log = logging.getLogger(__name__)


def _check(res):
    if isinstance(res, dict) and res.get("status") == "error":
        raise MarketError(f"tonnel: {res.get('message', res)}")
    return res


def _listing(d: dict) -> Listing | None:
    price = float(d.get("price") or 0)
    if price <= 0 or not d.get("name"):
        return None
    return Listing(
        market="tonnel",
        listing_id=str(d["gift_id"]),
        collection=d["name"],
        model=clean_attr(d.get("model")),
        backdrop=clean_attr(d.get("backdrop")),
        symbol=clean_attr(d.get("symbol")),
        number=d.get("gift_num"),
        price=price,
        model_rarity=rarity_pct(d.get("model")),
        backdrop_rarity=rarity_pct(d.get("backdrop")),
        symbol_rarity=rarity_pct(d.get("symbol")),
    )


class TonnelMarket(MarketAdapter):
    name = "tonnel"

    def __init__(self, init_data_provider, proxies: dict | None = None):
        super().__init__(init_data_provider)
        self._proxies = proxies

    async def _call(self, fn, **kw):
        return _check(await asyncio.to_thread(fn, proxies=self._proxies, **kw))

    async def latest_listings(self, limit: int = 30) -> list[Listing]:
        res = await self.with_auth(lambda a: self._call(
            tonnelmp.getGifts, sort="latest", limit=min(limit, 30), authData=a))
        return [x for x in map(_listing, res or []) if x]

    async def recent_sales(self, limit: int = 50) -> list[Sale]:
        res = await self.with_auth(lambda a: self._call(
            tonnelmp.saleHistory, authData=a, type="SALE", limit=min(limit, 50), sort="latest"))
        out = []
        for d in res or []:
            name = d.get("name") or d.get("gift_name")
            price = float(d.get("price") or 0)
            if not name or not d.get("model") or price <= 0:
                continue
            ts = d.get("timestamp") or d.get("time") or d.get("created_at")
            out.append(Sale(
                market="tonnel",
                sale_id=str(d.get("_id") or f"{d.get('gift_id')}:{ts}"),
                collection=name, model=clean_attr(d["model"]),
                backdrop=clean_attr(d.get("backdrop")), price=price, sold_at=parse_dt(ts),
            ))
        return out

    async def lowest_listings(self, collection: str, model: str, limit: int = 3) -> list[Listing]:
        res = await self.with_auth(lambda a: self._call(
            tonnelmp.getGifts, gift_name=collection, model=model, sort="price_asc", limit=limit, authData=a))
        return [x for x in map(_listing, res or []) if x]

    # --- торговля ---
    async def balance(self) -> float:
        res = await self.with_auth(lambda a: self._call(tonnelmp.info, authData=a))
        return float(res.get("balance") or res.get("TON") or 0) if isinstance(res, dict) else 0.0

    async def _my(self, listed: bool) -> list[dict]:
        return await self.with_auth(lambda a: self._call(tonnelmp.myGifts, listed=listed, limit=30, authData=a)) or []

    async def buy(self, listing: Listing) -> str:
        await self.once(lambda a: self._call(
            tonnelmp.buyGift, gift_id=int(listing.listing_id), price=listing.price, authData=a))
        # После покупки подарок появляется в инвентаре — ищем его по номеру, id может смениться
        for _ in range(5):
            for g in await self._my(listed=False):
                if g.get("gift_num") == listing.number and g.get("name") == listing.collection:
                    return str(g["gift_id"])
            await asyncio.sleep(3)
        return listing.listing_id

    async def list_for_sale(self, item_id: str, price: float) -> None:
        try:
            await self.once(lambda a: self._call(
                tonnelmp.listForSale, gift_id=int(item_id), price=float(price), authData=a))
        except MarketError:
            # Некоторые подарки нужно «разблокировать» для продажи (0.1 TON) — пробуем один раз
            await self.once(lambda a: self._call(tonnelmp.unlockListing, gift_id=int(item_id), authData=a))
            await self.once(lambda a: self._call(
                tonnelmp.listForSale, gift_id=int(item_id), price=float(price), authData=a))

    async def change_price(self, item_id: str, price: float) -> None:
        await self.once(lambda a: self._call(tonnelmp.cancelSale, gift_id=int(item_id), authData=a))
        await self.list_for_sale(item_id, price)

    async def owned_ids(self) -> set[str]:
        return {str(g["gift_id"]) for listed in (True, False) for g in await self._my(listed)}
