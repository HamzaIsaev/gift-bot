"""Жёсткие лимиты, режим работы и аварийная остановка.

Состояние хранится в БД, поэтому переживает перезапуск: если был /stop,
бот после рестарта тоже останется остановленным.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from gift_bot.config import Settings
from gift_bot.storage.db import Database

OBSERVE = "observe"
LIVE = "live"
LIMIT_NAMES = {
    "max_trade": ("max_trade_ton", float, "Макс. сумма сделки, TON"),
    "max_daily_loss": ("max_daily_loss_ton", float, "Макс. дневной убыток, TON"),
    "max_positions": ("max_open_positions", int, "Макс. подарков одновременно"),
}


@dataclass
class Limits:
    max_trade_ton: float
    max_daily_loss_ton: float
    max_open_positions: int


class RiskManager:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.s = settings
        self.tz = ZoneInfo(settings.timezone)
        self.limits = Limits(settings.max_trade_ton, settings.max_daily_loss_ton, settings.max_open_positions)
        self.mode = OBSERVE
        self.stopped = False
        self.halted_day: str | None = None   # дата, на которую торговля остановлена из-за дневного убытка

    async def load(self) -> None:
        saved = await self.db.get_setting("limits")
        if saved:
            self.limits = Limits(**{**asdict(self.limits), **saved})
        self.mode = await self.db.get_setting("mode", OBSERVE)
        self.stopped = await self.db.get_setting("stopped", False)
        self.halted_day = await self.db.get_setting("halted_day")

    @property
    def mode_db(self) -> str:
        """Как сделки этого режима помечаются в БД."""
        return "real" if self.mode == LIVE else "virtual"

    async def set_mode(self, mode: str) -> None:
        self.mode = mode
        await self.db.set_setting("mode", mode)

    async def set_stopped(self, value: bool) -> None:
        self.stopped = value
        await self.db.set_setting("stopped", value)

    async def set_limit(self, short_name: str, raw_value: str) -> str:
        field, cast, title = LIMIT_NAMES[short_name]
        value = cast(raw_value.replace(",", "."))
        if value <= 0:
            raise ValueError("значение должно быть больше нуля")
        setattr(self.limits, field, value)
        await self.db.set_setting("limits", asdict(self.limits))
        return f"{title}: {value}"

    def today_start(self) -> datetime:
        now = datetime.now(self.tz)
        return datetime.combine(now.date(), time.min, tzinfo=self.tz)

    async def daily_pnl(self) -> float:
        return await self.db.realized_pnl(self.mode_db, since=self.today_start())

    async def check_daily_loss(self) -> bool:
        """True — если лимит дневного убытка только что достигнут (нужно уведомить)."""
        today = self.today_start().date().isoformat()
        if self.halted_day == today:
            return False
        if await self.daily_pnl() <= -self.limits.max_daily_loss_ton:
            self.halted_day = today
            await self.db.set_setting("halted_day", today)
            return True
        return False

    def is_halted_today(self) -> bool:
        return self.halted_day == self.today_start().date().isoformat()

    async def check_trade(self, cost: float) -> str | None:
        """None — сделку можно делать, иначе причина отказа (с префиксом limit:)."""
        if self.stopped:
            return "limit:stop — бот остановлен командой /stop"
        if self.is_halted_today():
            return "limit:daily_loss — достигнут дневной лимит убытка"
        if cost > self.limits.max_trade_ton:
            return f"limit:max_trade — {cost:.2f} > {self.limits.max_trade_ton} TON"
        if len(await self.db.open_positions(self.mode_db)) >= self.limits.max_open_positions:
            return f"limit:max_positions — уже {self.limits.max_open_positions} подарков"
        return None
