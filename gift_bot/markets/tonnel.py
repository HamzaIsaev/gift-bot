"""Tonnel (market.tonnel.network) через библиотеку tonnelmp.

tonnelmp синхронная — вызовы уходят в отдельный поток, чтобы не блокировать бота.
"""
from __future__ import annotations

import asyncio
import logging

import tonnelmp

from gift_bot.markets.base import MarketAdapter, MarketError
from gift_bot.models import Listing, Sale, clean_attr, parse_dt

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
