"""Telegram-бот управления: меню, команды, уведомления. Доступ только для ADMIN_IDS."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

from gift_bot.bot.texts import mk, ton
from gift_bot.config import Settings
from gift_bot.reports.weekly import build_report
from gift_bot.risk.limits import LIMIT_NAMES, LIVE, OBSERVE, RiskManager
from gift_bot.storage.db import Database

log = logging.getLogger(__name__)

BTN_STATUS, BTN_MODE, BTN_LIMITS, BTN_STOP = "📊 Статус", "🔁 Режим", "⚙️ Лимиты", "🛑 Стоп"
MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_MODE)],
              [KeyboardButton(text=BTN_LIMITS), KeyboardButton(text=BTN_STOP)]],
    resize_keyboard=True,
)
MODE_TITLES = {OBSERVE: "🧪 наблюдение (без реальных сделок)", LIVE: "💰 боевой"}


class ControlBot:
    def __init__(self, settings: Settings, db: Database, risk: RiskManager):
        self.s = settings
        self.db = db
        self.risk = risk
        self.bot = Bot(settings.bot_token.get_secret_value(),
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self.dp.include_router(self._router())

    async def notify(self, text: str) -> None:
        for uid in self.s.admin_ids:
            try:
                await self.bot.send_message(uid, text)
            except Exception as e:
                log.warning("Не удалось отправить уведомление %s: %s", uid, e)

    async def run(self) -> None:
        await self.dp.start_polling(self.bot, handle_signals=False)

    # --- тексты ---
    async def status_text(self) -> str:
        mode = self.risk.mode_db
        positions = await self.db.open_positions(mode)
        day = await self.risk.daily_pnl()
        total = await self.db.realized_pnl(mode)
        in_positions = sum(p["buy_cost"] for p in positions)
        state = "🛑 ОСТАНОВЛЕН (/stop)" if self.risk.stopped else (
            "⏸ пауза до завтра — дневной лимит убытка" if self.risk.is_halted_today() else "▶️ работает")
        lines = [f"<b>Режим:</b> {MODE_TITLES[self.risk.mode]}", f"<b>Состояние:</b> {state}", ""]
        if mode == "virtual":
            balance = self.s.virtual_balance_ton + total
            lines.append(f"<b>Виртуальный баланс:</b> {ton(balance)} (свободно {ton(balance - in_positions)})")
        lines += [
            f"<b>Прибыль сегодня:</b> {day:+.2f} TON",
            f"<b>Прибыль за всё время:</b> {total:+.2f} TON",
            "",
            f"<b>Открытые позиции:</b> {len(positions)} / {self.risk.limits.max_open_positions}",
        ]
        for p in positions:
            lines.append(f"  #{p['id']} {escape(p['title'])} · {mk(p['market'])}: "
                         f"куплено {ton(p['buy_cost'])}, выставлено {ton(p['ask_price'])}")
        return "\n".join(lines)

    def limits_text(self) -> str:
        lim = self.risk.limits
        return (
            "<b>Лимиты</b>\n"
            f"max_trade — макс. сумма сделки: {lim.max_trade_ton} TON\n"
            f"max_daily_loss — макс. дневной убыток: {lim.max_daily_loss_ton} TON\n"
            f"max_positions — макс. подарков одновременно: {lim.max_open_positions}\n\n"
            "Изменить: <code>/limits max_trade 25</code>"
        )

    def mode_keyboard(self) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(text="🧪 Наблюдение", callback_data="mode:observe"),
                 InlineKeyboardButton(text="💰 Боевой", callback_data="mode:live")]]
        if self.risk.stopped:
            rows.append([InlineKeyboardButton(text="▶️ Снять /stop и продолжить", callback_data="resume")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    # --- обработчики ---
    def _router(self) -> Router:
        r = Router()
        admins = F.from_user.id.in_(set(self.s.admin_ids))
        r.message.filter(admins)
        r.callback_query.filter(admins)

        @r.message(CommandStart())
        async def start(m: Message):
            await m.answer("Бот торговли Telegram-подарками. Меню ниже.\n"
                           "/status /mode /limits /report /stop", reply_markup=MENU)

        @r.message(Command("stop"))
        @r.message(F.text == BTN_STOP)
        async def stop(m: Message):
            await self.risk.set_stopped(True)
            await m.answer("🛑 <b>Остановлено.</b> Новые сделки не совершаются.\n"
                           "Снять остановку: /mode → «Снять /stop».")
            log.warning("Аварийная остановка по команде %s", m.from_user.id)

        @r.message(Command("status"))
        @r.message(F.text == BTN_STATUS)
        async def status(m: Message):
            await m.answer(await self.status_text())

        @r.message(Command("mode"))
        @r.message(F.text == BTN_MODE)
        async def mode(m: Message):
            await m.answer(f"Сейчас: {MODE_TITLES[self.risk.mode]}", reply_markup=self.mode_keyboard())

        @r.callback_query(F.data.startswith("mode:"))
        async def set_mode(c: CallbackQuery):
            target = c.data.split(":", 1)[1]
            if target == LIVE:
                await c.answer("Боевой режим ещё не реализован", show_alert=True)
                await c.message.answer("💰 Боевой режим будет добавлен после вашего подтверждения "
                                       "по итогам режима наблюдения.")
                return
            await self.risk.set_mode(OBSERVE)
            await c.answer("Режим наблюдения")
            await c.message.edit_text(f"Сейчас: {MODE_TITLES[OBSERVE]}", reply_markup=self.mode_keyboard())

        @r.callback_query(F.data == "resume")
        async def resume(c: CallbackQuery):
            await self.risk.set_stopped(False)
            await c.answer("Работа возобновлена")
            await c.message.edit_text(f"▶️ Возобновлено. Режим: {MODE_TITLES[self.risk.mode]}",
                                      reply_markup=self.mode_keyboard())

        @r.message(Command("limits"))
        @r.message(F.text == BTN_LIMITS)
        async def limits(m: Message, command: CommandObject | None = None):
            args = (command.args or "").split() if command else []
            if len(args) == 2 and args[0] in LIMIT_NAMES:
                try:
                    done = await self.risk.set_limit(args[0], args[1])
                except ValueError as e:
                    await m.answer(f"Ошибка: {escape(str(e))}")
                    return
                await m.answer(f"✅ {done}")
            elif args:
                await m.answer("Не понял. Пример: <code>/limits max_trade 25</code>")
            await m.answer(self.limits_text())

        @r.message(Command("report"))
        async def report(m: Message):
            await m.answer(await build_report(self.db, self.risk, self.s.virtual_balance_ton))

        return r
