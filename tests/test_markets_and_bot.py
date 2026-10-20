from __future__ import annotations

from amrkt.models import Gift
from aportalsmp.classes.Objects import PortalsGift

from gift_bot.bot.app import ControlBot
from gift_bot.markets import mrkt, portals, tonnel
from tests.conftest import listing


def test_portals_listing_parsing():
    g = PortalsGift({
        "id": "uuid-1", "name": "Toy Bear", "price": "12.5", "external_collection_number": 35531,
        "attributes": [{"type": "model", "value": "Wizard", "rarity_per_mille": 15},
                       {"type": "backdrop", "value": "Black", "rarity_per_mille": 20},
                       {"type": "symbol", "value": "Star", "rarity_per_mille": 2}],
    })
    x = portals._listing(g)
    assert (x.listing_id, x.collection, x.model, x.price, x.number) == ("uuid-1", "Toy Bear", "Wizard", 12.5, 35531)


def test_tonnel_listing_parsing():
    x = tonnel._listing({
        "gift_num": 35531, "gift_id": 4840785, "name": "Toy Bear", "model": "Zebra (1.5%)",
        "symbol": "Rabbit (0.2%)", "backdrop": "Burgundy (2%)", "price": 12.9,
    })
    assert (x.listing_id, x.model, x.backdrop, x.price) == ("4840785", "Zebra", "Burgundy", 12.9)
    assert x.key == "toy bear|zebra"


def test_mrkt_listing_parsing():
    g = Gift.model_validate({
        "id": "m1", "collectionName": "ToyBear", "collectionTitle": "Toy Bear", "modelTitle": "Wizard",
        "backdropName": "Black", "number": 7, "salePrice": 12_000_000_000, "salePriceWithoutFee": 11_400_000_000,
    })
    x = mrkt._listing(g)
    assert (x.collection, x.model, x.price, x.raw_seller_net) == ("Toy Bear", "Wizard", 12.0, 11.4)


async def test_status_text(env):
    e = await env()
    e.markets["portals"].lows["Toy Bear|Wizard"] = [listing(lid="F1", price=13.0)]
    await e.trader.process_listing(listing(price=9.0))
    bot = ControlBot(e.s, e.db, e.risk)
    text = await bot.status_text()
    assert "наблюдение" in text and "Открытые позиции:</b> 1 / 5" in text
    await bot.bot.session.close()
