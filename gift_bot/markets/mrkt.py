"""MRKT (tgmrkt.io).

Модели данных берём из библиотеки amrkt, а HTTP-клиент свой, асинхронный:
в amrkt запросы синхронные и перед каждым делается лишний запрос проверки токена.
"""
from __future__ import annotations

import logging

from amrkt.models import Balance, FeedResponse, Gift, GiftCollection, GiftList, PurchaseResult, SearchParams
from curl_cffi.requests import AsyncSession

from gift_bot.markets.base import MarketAdapter, MarketError
from datetime import timezone

from gift_bot.models import CollectionInfo, Listing, Sale, parse_dt, per_mille_to_pct, utcnow

log = logging.getLogger(__name__)

API_URL = "https://api.tgmrkt.io/api/v1"
NANO = 1_000_000_000
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://cdn.tgmrkt.io",
    "Referer": "https://cdn.tgmrkt.io/",
}


def _listing(g: Gift) -> Listing | None:
    if not g.sale_price or not g.collection_name:
        return None
    return Listing(
        market="mrkt",
        listing_id=g.id,
        collection=g.collection_title or g.collection_name,
        model=g.model_title or g.model_name or "",
        backdrop=g.backdrop_name or "",
        symbol=g.symbol_name or "",
        number=g.number,
        price=g.sale_price / NANO,
        raw_seller_net=(g.sale_price_without_fee / NANO) if g.sale_price_without_fee else None,
        model_rarity=per_mille_to_pct(g.model_rarity_per_mille),
        backdrop_rarity=per_mille_to_pct(g.backdrop_rarity_per_mille),
        symbol_rarity=per_mille_to_pct(g.symbol_rarity_per_mille),
        coll_supply=g.max_upgraded_count or None,
        coll_upgraded=g.total_upgraded_count or None,
    )


class MrktMarket(MarketAdapter):
    name = "mrkt"

    def __init__(self, init_data_provider, proxy: str | None = None):
        super().__init__(init_data_provider)
        self._proxy = proxy
        self._token: str | None = None

    def reset_auth(self) -> None:
        super().reset_auth()
        self._token = None

    async def _login(self, force: bool) -> str:
        if self._token and not force:
            return self._token
        init_data = await self.auth(force=force)
        async with AsyncSession(impersonate="chrome124", proxy=self._proxy, timeout=15) as s:
            r = await s.post(f"{API_URL}/auth", json={"data": init_data}, headers=HEADERS)
        if r.status_code != 200 or not r.json().get("token"):
            raise MarketError(f"mrkt auth failed: {r.status_code}")
        self._token = r.json()["token"]
        return self._token

    async def _post(self, path: str, payload: dict, retry: bool = True) -> dict:
        token = await self._login(force=False)
        async with AsyncSession(impersonate="chrome124", proxy=self._proxy, timeout=15) as s:
            r = await s.post(f"{API_URL}{path}", json=payload,
                             headers={**HEADERS, "Authorization": token})
        if r.status_code == 401 and retry:
            await self._login(force=True)
            return await self._post(path, payload, retry=False)
        if r.status_code != 200:
            raise MarketError(f"mrkt {path}: {r.status_code}")
        return r.json()

    async def _search(self, **kw) -> list[Listing]:
        data = await self._post("/gifts/saling", SearchParams(**kw).to_api_dict())
        return [x for x in map(_listing, GiftList.model_validate(data).items) if x]

    async def latest_listings(self, limit: int = 30) -> list[Listing]:
        return await self._search(ordering="Date", low_to_high=False, count=limit)

    async def recent_sales(self, limit: int = 50) -> list[Sale]:
        feed = FeedResponse.model_validate(await self._post("/feed", {}))
        out = []
        for it in feed.items:
            if it.type != "sale" or not it.amount:
                continue
            g = it.gift
            out.append(Sale(
                market="mrkt", sale_id=it.id,
                collection=g.collection_title or g.collection_name or "",
                model=g.model_title or g.model_name or "",
                backdrop=g.backdrop_name or "",
                price=it.amount / NANO,
                sold_at=(it.date.replace(tzinfo=it.date.tzinfo or timezone.utc) if it.date else utcnow()),
            ))
        return out[:limit]

    async def lowest_listings(self, collection: str, model: str, limit: int = 3) -> list[Listing]:
        return await self._search(collection_names=[collection], model_names=[model],
                                  ordering="Price", low_to_high=True, count=limit)

    async def collections_info(self) -> list[CollectionInfo]:
        """Дата создания коллекции; тираж и число улучшенных — из лотов этой коллекции."""
        token = await self._login(force=False)
        async with AsyncSession(impersonate="chrome124", proxy=self._proxy, timeout=15) as s:
            r = await s.get(f"{API_URL}/gifts/collections", headers={**HEADERS, "Authorization": token})
        if r.status_code != 200:
            raise MarketError(f"mrkt collections: {r.status_code}")
        out = []
        for raw in r.json() or []:
            try:
                c = GiftCollection.model_validate(raw)
            except Exception:
                continue
            out.append(CollectionInfo(name=c.title or c.name, created_at=parse_dt(c.created_at)))
        return out

    # --- торговля ---
    async def _get(self, path: str) -> dict:
        token = await self._login(force=False)
        async with AsyncSession(impersonate="chrome124", proxy=self._proxy, timeout=15) as s:
            r = await s.get(f"{API_URL}{path}", headers={**HEADERS, "Authorization": token})
        if r.status_code == 401:
            await self._login(force=True)
            return await self._get(path)
        if r.status_code != 200:
            raise MarketError(f"mrkt {path}: {r.status_code}")
        return r.json()

    async def balance(self) -> float:
        return Balance.model_validate(await self._get("/balance")).hard / NANO

    async def buy(self, listing: Listing) -> str:
        data = await self._post("/gifts/buy", {"Ids": [listing.listing_id]})
        results = [PurchaseResult.model_validate(x) for x in (data if isinstance(data, list) else [])]
        if not results:
            raise MarketError("mrkt: покупка не подтверждена")
        gift = results[0].user_gift
        return gift.id if gift else listing.listing_id

    async def list_for_sale(self, item_id: str, price: float) -> None:
        await self._post("/gifts/sale", {"ids": [item_id], "prices": [int(round(price * NANO))]})

    async def change_price(self, item_id: str, price: float) -> None:
        await self._post("/gifts/sale/change-price", {"ids": [item_id], "prices": [int(round(price * NANO))]})

    async def owned_ids(self) -> set[str]:
        data = await self._post("/gifts", SearchParams(count=100, low_to_high=False).to_api_dict())
        return {g.id for g in GiftList.model_validate(data).items}
