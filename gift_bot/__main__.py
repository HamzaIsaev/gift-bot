"""Точка входа: python -m gift_bot"""
from __future__ import annotations

import asyncio
import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from gift_bot.bot.app import ControlBot
from gift_bot.config import Settings
from gift_bot.engine.strategy import Strategy
from gift_bot.engine.trader import Trader
from gift_bot.execution.base import VirtualExecutor
from gift_bot.logging_setup import setup_logging
from gift_bot.markets.base import MarketAdapter
from gift_bot.markets.mrkt import MrktMarket
from gift_bot.markets.portals import PortalsMarket
from gift_bot.markets.telegram_auth import TelegramAuth
from gift_bot.markets.tonnel import TonnelMarket
from gift_bot.pricing.valuation import Pricer
from gift_bot.reports.weekly import build_report
from gift_bot.risk.limits import LIVE, OBSERVE, RiskManager
from gift_bot.storage.db import Database

log = logging.getLogger("gift_bot")


def build_markets(s: Settings, tg: TelegramAuth) -> dict[str, MarketAdapter]:
    proxy = s.http_proxy.get_secret_value() if s.http_proxy else None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    async def portals_auth() -> str:
        return "tma " + await tg.init_data(s.portals_bot, s.portals_app, platform="desktop")

    async def tonnel_auth() -> str:
        if s.tonnel_auth_data:
            return s.tonnel_auth_data.get_secret_value()
        return await tg.init_data(s.tonnel_bot, s.tonnel_app)

    async def mrkt_auth() -> str:
        return await tg.init_data(s.mrkt_bot, s.mrkt_app)

    factories = {
        "portals": lambda: PortalsMarket(portals_auth),
        "tonnel": lambda: TonnelMarket(tonnel_auth, proxies=proxies),
        "mrkt": lambda: MrktMarket(mrkt_auth, proxy=proxy),
    }
    return {name: factories[name]() for name in s.enabled_markets}


async def main() -> None:
    s = Settings()
    setup_logging(s.secret_values())
    os.makedirs(os.path.dirname(s.db_path) or ".", exist_ok=True)

    db = Database(s.db_path)
    await db.connect()
    risk = RiskManager(db, s)
    await risk.load()
    if risk.mode == LIVE:  # боевой режим ещё не реализован — на всякий случай
        await risk.set_mode(OBSERVE)

    tg = TelegramAuth(s.tg_api_id, s.tg_api_hash.get_secret_value(), s.tg_session_string.get_secret_value())
    markets = build_markets(s, tg)
    pricer = Pricer(db, markets, s)
    strategy = Strategy(db, pricer, risk, s)
    bot = ControlBot(s, db, risk)
    trader = Trader(s, db, markets, pricer, strategy, risk, VirtualExecutor(db), bot.notify)

    scheduler = AsyncIOScheduler(timezone=s.timezone)

    async def weekly() -> None:
        await bot.notify(await build_report(db, risk, s.virtual_balance_ton))

    scheduler.add_job(weekly, "cron", day_of_week=s.report_weekday, hour=s.report_hour, minute=0)
    scheduler.start()

    log.info("Старт. Площадки: %s, режим: %s", ", ".join(markets), risk.mode)
    await bot.notify(f"🚀 Бот запущен. Режим: наблюдение. Площадки: {', '.join(markets)}"
                     + ("\n🛑 Действует /stop — сделки не совершаются." if risk.stopped else ""))
    try:
        await asyncio.gather(bot.run(), trader.run())
    finally:
        scheduler.shutdown(wait=False)
        await tg.stop()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
