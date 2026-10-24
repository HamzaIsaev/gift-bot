from __future__ import annotations

import pytest

from gift_bot.config import Settings
from gift_bot.engine.strategy import Strategy
from gift_bot.engine.trader import Trader
from gift_bot.execution.base import VirtualExecutor
from gift_bot.markets.base import MarketAdapter
from gift_bot.models import Listing, Sale, utcnow
from gift_bot.pricing.valuation import Pricer
from gift_bot.risk.limits import RiskManager
from gift_bot.storage.db import Database


def make_settings(**over) -> Settings:
    base = dict(
        BOT_TOKEN="123:secret-token-value", ADMIN_IDS=[1], TG_API_ID=1,
        TG_API_HASH="hash-secret-value", TG_SESSION_STRING="session-secret-value",
        ENABLED_MARKETS=["portals", "tonnel"], FEES_PORTALS={"buy": 0.0, "sell": 0.05},
        FEES_TONNEL={"buy": 0.10, "sell": 0.0}, NETWORK_FEE_TON=0.0, TRANSFER_COST_TON=0.5,
        STRATEGY_V2=False,  # старые тесты проверяют логику v1; v2 — в test_decision_v2.py
    )
    base.update(over)
    return Settings(_env_file=None, **base)


def listing(market="portals", lid="L1", price=10.0, collection="Toy Bear", model="Wizard") -> Listing:
    return Listing(market=market, listing_id=lid, collection=collection, model=model,
                   backdrop="Black", symbol="Star", number=1, price=price)


def sale(market="portals", sid="S1", price=12.0, collection="Toy Bear", model="Wizard", when=None) -> Sale:
    return Sale(market=market, sale_id=sid, collection=collection, model=model,
                backdrop="Black", price=price, sold_at=when or utcnow())


class FakeMarket(MarketAdapter):
    def __init__(self, name: str):
        async def provider() -> str:
            return "init"
        super().__init__(provider)
        self.name = name
        self.listings: list[Listing] = []
        self.sales: list[Sale] = []
        self.lows: dict[str, list[Listing]] = {}

    async def latest_listings(self, limit: int = 30):
        return list(self.listings)

    async def recent_sales(self, limit: int = 50):
        return list(self.sales)

    async def lowest_listings(self, collection, model, limit=3):
        return self.lows.get(f"{collection}|{model}", [])[:limit]


class Env:
    def __init__(self, db, settings, markets, risk, pricer, strategy, trader, sent):
        self.db, self.s, self.markets, self.risk = db, settings, markets, risk
        self.pricer, self.strategy, self.trader, self.sent = pricer, strategy, trader, sent


@pytest.fixture
async def env(tmp_path):
    async def build(**over) -> Env:
        s = make_settings(**over)
        db = Database(str(tmp_path / "t.sqlite3"))
        await db.connect()
        markets = {m: FakeMarket(m) for m in s.enabled_markets}
        risk = RiskManager(db, s)
        await risk.load()
        pricer = Pricer(db, markets, s)
        strategy = Strategy(db, pricer, risk, s)
        sent: list[str] = []

        async def notify(text: str) -> None:
            sent.append(text)

        trader = Trader(s, db, markets, pricer, strategy, risk, VirtualExecutor(db), notify)
        built.append(db)
        return Env(db, s, markets, risk, pricer, strategy, trader, sent)

    built: list[Database] = []
    yield build
    for db in built:
        await db.close()
