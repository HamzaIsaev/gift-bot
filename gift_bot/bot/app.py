"""Telegram-бот управления: меню, команды, уведомления. Доступ только для ADMIN_IDS."""
from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, MenuButtonWebApp, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

from gift_bot.bot.texts import mk, ton
from gift_bot.config import Settings
from gift_bot.markets.base import MarketAdapter
from gift_bot.markets.manual_auth import CONSOLE_SNIPPET, MARKETS, ManualAuthStore
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
    def __init__(self, settings: Settings, db: Database, risk: RiskManager,
                 store: ManualAuthStore | None = None, markets: dict[str, MarketAdapter] | None = None):
        self.s = settings
        self.db = db
        self.risk = risk
        self.store = store or ManualAuthStore(db)
        self.markets = markets or {}
        self.refresher = None  # WebAuthRefresher — вход через Telegram Web (/login, /qr)
        self.login_state: dict | None = None  # идёт вход по телефону: {"login", "step", "chat"}
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

    async def send_photo(self, png: bytes, caption: str) -> None:
        for uid in self.s.admin_ids:
            try:
                await self.bot.send_photo(uid, BufferedInputFile(png, "qr.png"), caption=caption)
            except Exception as e:
                log.warning("Не удалось отправить фото %s: %s", uid, e)

    async def run(self) -> None:
        if self.s.webapp_url:
            try:  # кнопка «APP» слева от поля ввода открывает мини-приложение
                await self.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="APP", web_app=WebAppInfo(url=self.s.webapp_url)))
            except Exception as e:
                log.warning("Не удалось установить кнопку мини-приложения: %s", e)
        await self.dp.start_polling(self.bot, handle_signals=False)

    def app_keyboard(self) -> InlineKeyboardMarkup | None:
        if not self.s.webapp_url:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎛 Открыть панель", web_app=WebAppInfo(url=self.s.webapp_url))]])

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
        else:
            lines.append("<b>Балансы на площадках:</b>")
            for name, market in self.markets.items():
                try:
                    lines.append(f"  {mk(name)}: {ton(await market.balance())}")
                except Exception:
                    lines.append(f"  {mk(name)}: — (нет связи или входа)")
            lines.append(f"  В подарках: {ton(in_positions)}")
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

    async def auth_text(self) -> str:
        status = await self.store.status()
        lines = ["🔑 <b>Вход на площадки</b>"]
        for m in MARKETS:
            st = status[m]
            mark = f"✅ данные от {st['age_hours']:.0f} ч назад" if st["set"] else "❌ нет данных"
            lines.append(f"{mk(m)}: {mark}")
        if self.s.auto_auth:
            lines.append("\nСейчас включён автоматический вход через api_id — /auth не нужен.")
        lines += [
            "",
            "<b>Как получить данные (Chrome на Mac):</b>",
            "1. Откройте web.telegram.org/k и войдите во второй аккаунт",
            "2. Откройте мини-приложение: @portals, @Tonnel_Network_bot или @mrkt",
            "3. Нажмите Cmd+Option+J — откроется консоль",
            "4. Вверху консоли в списке <b>top</b> выберите фрейм площадки",
            f"5. Введите <code>{CONSOLE_SNIPPET}</code> и Enter — данные скопированы",
            "6. Отправьте сюда: <code>/auth portals </code> и вставьте (Cmd+V)",
            "",
            "Сообщение с данными бот сразу удалит. Данные устаревают — бот напомнит обновить.",
        ]
        return "\n".join(lines)

    # --- вход по номеру телефона ---
    PROMPTS = {
        "phone": "📱 Отправьте номер телефона <b>второго</b> аккаунта, например <code>+79991234567</code>.\n"
                 "Сообщение я сразу удалю. Отмена: /cancel",
        "code": "🔢 Telegram прислал код на второй аккаунт. Отправьте его <b>с пробелами</b>, например "
                "<code>1 2 3 4 5</code> — иначе Telegram аннулирует код. Отмена: /cancel",
        "password": "🔐 На аккаунте стоит облачный пароль (2FA). Отправьте его — сообщение я сразу удалю. "
                    "Отмена: /cancel",
    }

    def _awaiting_input(self, m: Message) -> bool:
        return (self.login_state is not None and m.chat.id == self.login_state["chat"]
                and bool(m.text) and not m.text.startswith("/"))

    async def _login_finish(self, chat_id: int) -> None:
        login = self.login_state["login"]
        self.login_state = None
        await self.bot.send_message(chat_id, "✅ Вход выполнен. Захожу на площадки…")
        result = await self.refresher.phone_login_end(login, grab=True)
        for market in self.markets.values():
            market.reset_auth()
        await self.bot.send_message(chat_id, result or "Готово.")

    async def _login_step(self, m: Message) -> None:
        st = self.login_state
        login, step, text = st["login"], st["step"], m.text.strip()
        try:
            await m.delete()  # номер, код и пароль не оставляем в чате
        except Exception:
            pass
        await self.bot.send_chat_action(m.chat.id, "typing")
        try:
            if step == "phone":
                phone = "+" + "".join(ch for ch in text if ch.isdigit())
                if len(phone) < 8:
                    await m.answer("Не похоже на номер. Пример: <code>+79991234567</code>")
                    return
                where = await login.submit_phone(phone)
            elif step == "code":
                where = await login.submit_code("".join(ch for ch in text if ch.isdigit()))
            else:
                where = await login.submit_password(text)
        except Exception as e:
            log.warning("Ошибка входа по телефону: %s", e)
            try:
                await self.send_photo(await login.screenshot(), "❌ Снимок экрана в момент ошибки")
            except Exception:
                pass
            await m.answer(f"❌ Ошибка: {escape(str(e)[:800])}\n\nПерешлите это сообщение разработчику и попробуйте /login заново.")
            await self.refresher.phone_login_end(login, grab=False)
            self.login_state = None
            return

        if where == "done":
            await self._login_finish(m.chat.id)
        elif where in ("code", "password") and where != step:
            st["step"] = where
            await m.answer(self.PROMPTS[where])
        elif where == "signup":
            await m.answer("❌ На этот номер нет аккаунта Telegram. Проверьте номер: /login")
            await self.refresher.phone_login_end(login, grab=False)
            self.login_state = None
        else:
            hint = {"phone": "номер не принят — проверьте и отправьте ещё раз",
                    "code": "код не подошёл или просрочен — отправьте ещё раз (с пробелами) или /cancel",
                    "password": "пароль не подошёл — отправьте ещё раз или /cancel"}.get(step, "что-то пошло не так")
            await self.send_photo(await login.screenshot(), f"⚠️ {hint}")

    async def _qr_login(self, chat_id: int) -> None:
        result = await self.refresher.login_via_bot(self.send_photo)
        for name, market in self.markets.items():
            market.reset_auth()
        await self.bot.send_message(chat_id, result)

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
                           "/status /mode /limits /report /stop /login /qr /app", reply_markup=MENU)
            if self.s.webapp_url:
                await m.answer("Все функции удобнее в панели:", reply_markup=self.app_keyboard())

        @r.message(Command("app"))
        async def app(m: Message):
            if not self.s.webapp_url:
                await m.answer("Мини-приложение работает на сервере: там его включает scripts/server_setup.sh.")
                return
            await m.answer("🎛 Панель управления:", reply_markup=self.app_keyboard())

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
                if self.risk.mode == LIVE:
                    await c.answer("Боевой режим уже включён")
                    return
                lim = self.risk.limits
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Да, торговать реальными TON", callback_data="mode:live_confirm"),
                    InlineKeyboardButton(text="Отмена", callback_data="mode:observe")]])
                await c.answer()
                await c.message.answer(
                    "💰 <b>Включить боевой режим?</b>\n"
                    "Бот будет покупать и продавать подарки за реальные TON с балансов площадок.\n\n"
                    f"Лимиты: сделка до {lim.max_trade_ton} TON, дневной убыток до {lim.max_daily_loss_ton} TON, "
                    f"не больше {lim.max_open_positions} подарков.\n"
                    "Для первых дней советую уменьшить: <code>/limits max_trade 5</code>\n"
                    "Остановить в любой момент: /stop", reply_markup=kb)
                return
            if target == "live_confirm":
                await self.risk.set_mode(LIVE)
                log.warning("Боевой режим включён пользователем %s", c.from_user.id)
                await c.answer("Боевой режим включён")
                await c.message.edit_text(f"Сейчас: {MODE_TITLES[LIVE]}\nОстановить: /stop",
                                          reply_markup=self.mode_keyboard())
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

        @r.message(Command("login"))
        async def login_cmd(m: Message):
            if self.refresher is None:
                await m.answer("Вход не нужен: включён автоматический вход через api_id.")
                return
            if self.login_state is not None:
                await m.answer(self.PROMPTS[self.login_state["step"]])
                return
            await m.answer("⏳ Открываю Telegram Web…")
            try:
                res = await self.refresher.phone_login_begin()
            except Exception as e:
                await m.answer(f"❌ Не удалось открыть Telegram Web: {escape(str(e)[:150])}")
                return
            if res is None:
                await m.answer("Сейчас уже идёт вход или обновление — попробуйте через минуту.")
                return
            login, where = res
            if where == "done":
                self.login_state = {"login": login, "step": "done", "chat": m.chat.id}
                await self._login_finish(m.chat.id)
                return
            self.login_state = {"login": login, "step": "phone", "chat": m.chat.id}
            await m.answer(self.PROMPTS["phone"])

        @r.message(Command("cancel"))
        async def cancel(m: Message):
            if self.login_state is None:
                await m.answer("Нечего отменять.")
                return
            login = self.login_state["login"]
            self.login_state = None
            await self.refresher.phone_login_end(login, grab=False)
            await m.answer("Вход отменён.")

        @r.message(self._awaiting_input)
        async def login_input(m: Message):
            await self._login_step(m)

        @r.message(Command("qr"))
        async def qr(m: Message):
            if self.refresher is None:
                await m.answer("Вход по QR не нужен: включён автоматический вход через api_id.")
                return
            await m.answer("⏳ Открываю Telegram Web… Через несколько секунд пришлю QR-код.\n"
                           "Если на втором аккаунте стоит облачный пароль (2FA), временно отключите его.")
            asyncio.create_task(self._qr_login(m.chat.id))

        @r.message(Command("auth"))
        async def auth(m: Message, command: CommandObject):
            args = (command.args or "").split(maxsplit=1)
            if len(args) < 2:
                await m.answer(await self.auth_text())
                return
            market, raw = args[0].lower(), args[1]
            try:
                await m.delete()  # в сообщении данные входа — не оставляем в чате
            except Exception:
                pass
            try:
                when = await self.store.set(market, raw)
            except ValueError as e:
                await m.answer(f"❌ {escape(str(e))}")
                return
            if market in self.markets:
                self.markets[market].reset_auth()
            await m.answer(f"✅ {mk(market)}: данные входа сохранены "
                           f"(выданы {when.astimezone(self.risk.tz):%d.%m %H:%M}). Подключаюсь…")

        @r.message(Command("report"))
        async def report(m: Message):
            await m.answer(await build_report(self.db, self.risk, self.s.virtual_balance_ton))

        return r
