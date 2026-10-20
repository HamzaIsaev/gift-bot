"""Веб-админка: обзор, сделки, сигналы, лимиты, режим и /stop.

По умолчанию слушает только 127.0.0.1 (открывается на этом же компьютере).
Если панель открыта наружу (PANEL_HOST не локальный), обязателен PANEL_PASSWORD.
"""
from __future__ import annotations

import base64
import hmac
import logging
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

from gift_bot.config import Settings
from gift_bot.models import parse_dt
from gift_bot.reports.weekly import week_stats
from gift_bot.risk.limits import LIMIT_NAMES, LIVE, OBSERVE, RiskManager
from gift_bot.storage.db import Database

log = logging.getLogger(__name__)

INDEX = Path(__file__).with_name("index.html")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
Notify = Callable[[str], Awaitable[None]]


class Panel:
    def __init__(self, settings: Settings, db: Database, risk: RiskManager, notify: Notify):
        self.s = settings
        self.db = db
        self.risk = risk
        self.notify = notify
        self.app = web.Application(middlewares=[self._auth, self._csrf])
        self.app.add_routes([
            web.get("/", self.index),
            web.get("/api/overview", self.overview),
            web.get("/api/trades", self.trades),
            web.get("/api/signals", self.signals),
            web.post("/api/stop", self.stop),
            web.post("/api/resume", self.resume),
            web.post("/api/mode", self.mode),
            web.post("/api/limits", self.limits),
        ])
        self._runner: web.AppRunner | None = None

    # --- запуск ---
    async def start(self) -> None:
        public = self.s.panel_host not in LOCAL_HOSTS
        if public and not self.s.panel_password:
            log.error("Панель не запущена: PANEL_HOST=%s открыт наружу, задайте PANEL_PASSWORD", self.s.panel_host)
            return
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.s.panel_host, self.s.panel_port).start()
        log.info("Админ-панель: http://%s:%d", self.s.panel_host, self.s.panel_port)

    async def stop_server(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # --- защита ---
    @web.middleware
    async def _auth(self, request: web.Request, handler):
        if self.s.panel_password:
            expected = self.s.panel_password.get_secret_value()
            ok = False
            header = request.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    _, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                    ok = hmac.compare_digest(pwd, expected)
                except Exception:
                    ok = False
            if not ok:
                return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="Gift Bot"'})
        return await handler(request)

    @web.middleware
    async def _csrf(self, request: web.Request, handler):
        # Кастомный заголовок нельзя отправить с чужого сайта без CORS — защита от подделки запросов
        if request.method == "POST" and request.headers.get("X-Panel") != "1":
            return web.json_response({"error": "forbidden"}, status=403)
        return await handler(request)

    # --- страницы ---
    async def index(self, _: web.Request) -> web.Response:
        return web.Response(text=INDEX.read_text(encoding="utf-8"), content_type="text/html")

    async def overview(self, _: web.Request) -> web.Response:
        mode = self.risk.mode_db
        today = self.risk.today_start()
        week_ago = today - timedelta(days=6)
        positions = await self.db.open_positions(mode)
        total = await self.db.realized_pnl(mode)
        cur = await week_stats(self.db, mode, week_ago, today + timedelta(days=1))
        prev = await week_stats(self.db, mode, week_ago - timedelta(days=7), week_ago)

        daily: dict[str, float] = defaultdict(float)
        for r in await self.db.closed_positions(mode, week_ago, today + timedelta(days=1)):
            daily[parse_dt(r["closed_at"]).astimezone(self.risk.tz).date().isoformat()] += r["pnl"]
        days = [(week_ago + timedelta(days=i)).date() for i in range(7)]

        in_positions = sum(p["buy_cost"] for p in positions)
        balance = self.s.virtual_balance_ton + total if mode == "virtual" else None
        return web.json_response({
            "mode": self.risk.mode,
            "stopped": self.risk.stopped,
            "halted": self.risk.is_halted_today(),
            "balance": balance,
            "free": balance - in_positions if balance is not None else None,
            "pnl_today": await self.risk.daily_pnl(),
            "pnl_total": total,
            "week": {k: cur[k] for k in ("trades", "wins", "losses", "pnl", "avg_hold_h", "skipped_limits")},
            "prev_week_pnl": prev["pnl"],
            "top_models": cur["by_model"][:5],
            "daily": [{"day": d.isoformat(), "pnl": round(daily.get(d.isoformat(), 0.0), 4)} for d in days],
            "limits": {
                "max_trade": self.risk.limits.max_trade_ton,
                "max_daily_loss": self.risk.limits.max_daily_loss_ton,
                "max_positions": self.risk.limits.max_open_positions,
            },
            "min_profit_pct": self.s.min_profit_pct,
            "adaptive": self.s.adaptive,
            "positions": [dict(p) for p in positions],
        })

    async def trades(self, _: web.Request) -> web.Response:
        rows = await self.db.recent_closed(self.risk.mode_db, 100)
        return web.json_response([dict(r) for r in rows])

    async def signals(self, _: web.Request) -> web.Response:
        return web.json_response([dict(r) for r in await self.db.recent_signals(100)])

    # --- действия ---
    async def stop(self, _: web.Request) -> web.Response:
        await self.risk.set_stopped(True)
        log.warning("Аварийная остановка из админ-панели")
        await self.notify("🛑 <b>Остановлено из админ-панели.</b> Новые сделки не совершаются.")
        return web.json_response({"ok": True})

    async def resume(self, _: web.Request) -> web.Response:
        await self.risk.set_stopped(False)
        await self.notify("▶️ Работа возобновлена из админ-панели.")
        return web.json_response({"ok": True})

    async def mode(self, request: web.Request) -> web.Response:
        target = (await request.json()).get("mode")
        if target == LIVE:
            return web.json_response(
                {"error": "Боевой режим будет добавлен после подтверждения по итогам наблюдения"}, status=400)
        if target != OBSERVE:
            return web.json_response({"error": "неизвестный режим"}, status=400)
        await self.risk.set_mode(OBSERVE)
        return web.json_response({"ok": True})

    async def limits(self, request: web.Request) -> web.Response:
        body = await request.json()
        name, value = body.get("name"), str(body.get("value", ""))
        if name not in LIMIT_NAMES:
            return web.json_response({"error": "неизвестный лимит"}, status=400)
        try:
            done = await self.risk.set_limit(name, value)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        await self.notify(f"⚙️ Лимит изменён из админ-панели: {done}")
        return web.json_response({"ok": True, "message": done})
