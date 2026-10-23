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
from gift_bot.execution.base import RealExecutor, VirtualExecutor
from gift_bot.logging_setup import setup_logging
from gift_bot.markets.base import MarketAdapter
from gift_bot.markets.manual_auth import ManualAuthStore
from gift_bot.markets.mrkt import MrktMarket
from gift_bot.markets.web_login import WebAuthRefresher
from gift_bot.markets.portals import PortalsMarket
from gift_bot.markets.telegram_auth import TelegramAuth
from gift_bot.markets.tonnel import TonnelMarket
from gift_bot.panel.server import Panel
from gift_bot.pricing.valuation import Pricer
from gift_bot.reports.weekly import build_report
from gift_bot.risk.limits import LIVE, RiskManager
from gift_bot.storage.db import Database

log = logging.getLogger("gift_bot")


def build_markets(s: Settings, tg: TelegramAuth | None, store: ManualAuthStore) -> dict[str, MarketAdapter]:
    proxy = s.http_proxy.get_secret_value() if s.http_proxy else None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    def provider(market: str, bot: str, app: str, platform: str = "android"):
        async def get() -> str:
            if market == "tonnel" and s.tonnel_auth_data:
                return s.tonnel_auth_data.get_secret_value()
            if tg is None:  # ручной вход: данные из /auth
                return await store.get(market)
            return await tg.init_data(bot, app, platform=platform)
        return get

    portals_raw = provider("portals", s.portals_bot, s.portals_app, platform="desktop")

    async def portals_auth() -> str:
        return "tma " + await portals_raw()

    factories = {
        "portals": lambda: PortalsMarket(portals_auth),
        "tonnel": lambda: TonnelMarket(provider("tonnel", s.tonnel_bot, s.tonnel_app), proxies=proxies),
        "mrkt": lambda: MrktMarket(provider("mrkt", s.mrkt_bot, s.mrkt_app), proxy=proxy),
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
    tg = None
    if s.auto_auth:
        tg = TelegramAuth(s.tg_api_id, s.tg_api_hash.get_secret_value(), s.tg_session_string.get_secret_value())
    store = ManualAuthStore(db)
    markets = build_markets(s, tg, store)
    pricer = Pricer(db, markets, s)
    strategy = Strategy(db, pricer, risk, s)
    bot = ControlBot(s, db, risk, store, markets)
    refresher = None
    if tg is None:  # вход через Telegram Web: на Mac — окно с QR, на сервере — QR приходит в бот (/qr)
        async def save_auth(market: str, data: str) -> None:
            await store.set(market, data)
            markets[market].reset_auth()

        refresher = WebAuthRefresher(s.web_profile_dir, s.web_login_targets(), save_auth, bot.notify)
        bot.refresher = refresher
    executors = {"virtual": VirtualExecutor(db), "real": RealExecutor(db, markets)}
    trader = Trader(s, db, markets, pricer, strategy, risk, executors, bot.notify,
                    on_auth_missing=refresher.refresh if refresher else None)

    scheduler = AsyncIOScheduler(timezone=s.timezone)

    async def weekly() -> None:
        await bot.notify(await build_report(db, risk, s.virtual_balance_ton))

    scheduler.add_job(weekly, "cron", day_of_week=s.report_weekday, hour=s.report_hour, minute=0)
    scheduler.start()

    panel = Panel(s, db, risk, bot.notify, store, markets) if s.panel_enabled else None
    if panel:
        await panel.start()

    log.info("Старт. Площадки: %s, режим: %s", ", ".join(markets), risk.mode)
    mode_title = "💰 БОЕВОЙ (реальные сделки)" if risk.mode == LIVE else "наблюдение"
    start_msg = f"🚀 Бот запущен. Режим: {mode_title}. Площадки: {', '.join(markets)}"
    if risk.stopped:
        start_msg += "\n🛑 Действует /stop — сделки не совершаются."
    if tg is None:
        missing = [m for m, st in (await store.status()).items() if m in markets and not st["set"]]
        if missing:
            start_msg += (f"\n🔑 Нужен вход на площадки ({', '.join(missing)}): отправьте /qr "
                          f"и отсканируйте QR-код вторым аккаунтом")
    await bot.notify(start_msg)
    try:
        jobs = [bot.run(), trader.run()]
        if refresher:
            jobs.append(refresher.run_periodic(s.web_refresh_hours))
        await asyncio.gather(*jobs)
    finally:
        scheduler.shutdown(wait=False)
        if panel:
            await panel.stop_server()
        if tg:
            await tg.stop()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
