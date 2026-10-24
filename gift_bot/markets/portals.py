"""Portals (portal-market.com) через библиотеку aportalsmp."""
from __future__ import annotations

import logging

from aportalsmp import gifts as pm

from gift_bot.markets.base import MarketAdapter
from gift_bot.models import CollectionInfo, Listing, Sale, parse_dt, per_mille_to_pct

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
        model_rarity=per_mille_to_pct(g.model_rarity),
        backdrop_rarity=per_mille_to_pct(g.backdrop_rarity),
        symbol_rarity=per_mille_to_pct(g.symbol_rarity),
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

    async def collections_info(self) -> list[CollectionInfo]:
        res = await self.with_auth(lambda a: pm.collections(limit=500, authData=a))
        return [CollectionInfo(name=c.get("name", ""), supply=int(c.get("supply") or 0) or None)
                for c in getattr(res, "_collections", []) if c.get("name")]

    # --- торговля ---
    async def balance(self) -> float:
        from aportalsmp import account
        return (await self.with_auth(lambda a: account.myBalances(authData=a))).balance

    async def buy(self, listing: Listing) -> str:
        await self.once(lambda a: pm.buy(nft_id=listing.listing_id, price=listing.price, authData=a))
        return listing.listing_id  # у Portals id подарка после покупки не меняется

    async def list_for_sale(self, item_id: str, price: float) -> None:
        res = await self.once(lambda a: pm.sale(nft_id=item_id, price=float(price), authData=a))
        failed = getattr(res, "failed_nfts", None)
        if failed:
            raise RuntimeError(f"portals: не удалось выставить {item_id}: {failed}")

    async def change_price(self, item_id: str, price: float) -> None:
        await self.once(lambda a: pm.changePrice(nft_id=item_id, price=float(price), authData=a))

    async def owned_ids(self) -> set[str]:
        ids: set[str] = set()
        for listed in (True, False):
            res = await self.with_auth(lambda a, l=listed: pm.myPortalsGifts(limit=100, listed=l, authData=a))
            ids |= {str(g.id) for g in res}
        return ids
