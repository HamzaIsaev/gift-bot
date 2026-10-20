"""Portals (portal-market.com) через библиотеку aportalsmp."""
from __future__ import annotations

import logging

from aportalsmp import gifts as pm

from gift_bot.markets.base import MarketAdapter
from gift_bot.models import Listing, Sale, parse_dt

log = logging.getLogger(__name__)


def _listing(g) -> Listing:
    return Listing(
        market="portals",
        listing_id=str(g.id),
        collection=g.name or "",
        model=g.model or "",
        backdrop=g.backdrop or "",
        symbol=g.symbol or "",
        number=int(g.tg_id) if g.tg_id else None,
        price=g.price,
    )


class PortalsMarket(MarketAdapter):
    name = "portals"

    async def latest_listings(self, limit: int = 30) -> list[Listing]:
        res = await self.with_auth(lambda a: pm.search(sort="latest", limit=limit, authData=a))
        return [_listing(g) for g in res if g.price > 0]

    async def recent_sales(self, limit: int = 50) -> list[Sale]:
        res = await self.with_auth(
            lambda a: pm.marketActivity(sort="latest", limit=limit, activityType="buy", authData=a)
        )
        out = []
        for act in res:
            nft = act.nft
            if not nft.name or not nft.model or act.amount <= 0:
                continue
            out.append(Sale(
                market="portals",
                sale_id=str(act.toDict().get("id") or f"{nft.id}:{act.created_at}"),
                collection=nft.name, model=nft.model, backdrop=nft.backdrop or "",
                price=act.amount, sold_at=parse_dt(act.created_at),
            ))
        return out

    async def lowest_listings(self, collection: str, model: str, limit: int = 3) -> list[Listing]:
        try:
            res = await self.with_auth(lambda a: pm.search(
                sort="price_asc", limit=limit, gift_name=collection, model=model, authData=a))
        except Exception as e:  # новая коллекция может отсутствовать в справочнике библиотеки
            log.debug("portals floor %s/%s: %s", collection, model, e)
            return []
        return [_listing(g) for g in res if g.price > 0]
