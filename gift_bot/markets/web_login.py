"""Автоматическое получение данных входа через Telegram Web (план Б без api_id).

Один раз: открывается окно браузера с web.telegram.org — вы сканируете QR-код
телефоном (Telegram → Настройки → Устройства → Подключить устройство).
Дальше программа сама открывает мини-приложения площадок и забирает initData
из адреса их фрейма. Сессия браузера сохраняется в data/tg_web_profile,
поэтому бот потом сам обновляет данные в фоне, без окна.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

log = logging.getLogger(__name__)

WEB_URL = "https://web.telegram.org/k/"
LOGIN_WAIT_INTERACTIVE = 300   # секунд на сканирование QR
LOGIN_WAIT_HEADLESS = 40
APP_WAIT = 40
CONFIRM_RE = re.compile(r"^\s*(open|launch|start|confirm|открыть|запустить|подтвердить|начать)\b", re.I)

Status = Callable[[str], None]


class WebLoginError(Exception):
    pass


def init_data_from_url(url: str) -> str | None:
    """initData лежит во фрагменте адреса фрейма мини-приложения: #tgWebAppData=..."""
    if "tgWebAppData" not in url:
        return None
    frag = urlparse(url).fragment or url.split("#", 1)[-1]
    params = parse_qs(frag)
    if "tgWebAppData" in params:
        return params["tgWebAppData"][0]
    m = re.search(r"tgWebAppData=([^&]+)", url)
    return unquote(m.group(1)) if m else None


async def _logged_in(page) -> bool:
    try:
        return bool(await page.evaluate("() => !!localStorage.getItem('user_auth')"))
    except Exception:
        return False


async def _wait_login(page, timeout: float, status: Status) -> None:
    if await _logged_in(page):
        return
    status("Отсканируйте QR-код в окне браузера: Telegram на телефоне → Настройки → Устройства → "
           "Подключить устройство. Используйте ВТОРОЙ аккаунт.")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _logged_in(page):
            await asyncio.sleep(3)  # дать Telegram Web догрузиться
            return
        await asyncio.sleep(1)
    raise WebLoginError("вход в Telegram Web не выполнен (QR-код не отсканирован)")


async def _click_confirm(page) -> None:
    """Подтверждения вида «Открыть приложение?» — нажимаем, если появились."""
    try:
        buttons = page.locator("button, .btn, .popup-button")
        for i in range(await buttons.count()):
            b = buttons.nth(i)
            if await b.is_visible() and CONFIRM_RE.search((await b.inner_text()) or ""):
                await b.click()
                return
    except Exception:
        pass


async def _grab_one(page, bot: str, app: str) -> str:
    link = f"tg://resolve?domain={bot}&appname={app}"
    await page.goto(f"{WEB_URL}#?tgaddr={quote(link, safe='')}")
    await page.reload()  # смена только #-части не всегда перезапускает обработку ссылки
    deadline = time.monotonic() + APP_WAIT
    while time.monotonic() < deadline:
        for frame in page.frames:
            data = init_data_from_url(frame.url)
            if data:
                return data
        for src in await page.eval_on_selector_all("iframe", "els => els.map(e => e.src)"):
            data = init_data_from_url(src or "")
            if data:
                return data
        await _click_confirm(page)
        await asyncio.sleep(1)
    raise WebLoginError(f"мини-приложение @{bot} не открылось")


async def grab_init_data(profile_dir: str, targets: dict[str, tuple[str, str]], headless: bool,
                         status: Status = lambda s: log.info(s),
                         executable_path: str | None = None) -> dict[str, str | Exception]:
    """targets: {market: (bot_username, app_short_name)} -> {market: initData или ошибка}."""
    from playwright.async_api import async_playwright

    results: dict[str, str | Exception] = {}
    async with async_playwright() as pw:
        opts = dict(headless=headless, viewport={"width": 1200, "height": 850}, locale="ru-RU")
        if executable_path:
            ctx = await pw.chromium.launch_persistent_context(profile_dir, executable_path=executable_path, **opts)
        else:
            try:  # установленный Google Chrome (отдельный профиль — ваш обычный Chrome не затрагивается)
                ctx = await pw.chromium.launch_persistent_context(profile_dir, channel="chrome", **opts)
            except Exception:
                ctx = await pw.chromium.launch_persistent_context(profile_dir, **opts)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(WEB_URL)
            await _wait_login(page, LOGIN_WAIT_HEADLESS if headless else LOGIN_WAIT_INTERACTIVE, status)
            for market, (bot, app) in targets.items():
                status(f"Открываю {market}…")
                try:
                    results[market] = await _grab_one(page, bot, app)
                    status(f"✅ {market}: данные входа получены")
                except Exception as e:
                    results[market] = e
                    status(f"❌ {market}: {e}")
        finally:
            await ctx.close()
    return results


class WebAuthRefresher:
    """Фоновое обновление данных входа через сохранённую сессию Telegram Web."""

    MIN_INTERVAL = 10 * 60

    def __init__(self, profile_dir: str, targets: dict[str, tuple[str, str]],
                 save: Callable[[str, str], Awaitable[None]], notify: Callable[[str], Awaitable[None]]):
        self.profile_dir = profile_dir
        self.targets = targets
        self.save = save
        self.notify = notify
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._failed_notified = False

    async def refresh(self, force: bool = False) -> None:
        if self._lock.locked() or (not force and time.monotonic() - self._last < self.MIN_INTERVAL):
            return
        async with self._lock:
            self._last = time.monotonic()
            try:
                results = await grab_init_data(self.profile_dir, self.targets, headless=True)
            except Exception as e:
                log.warning("Обновление входа через Telegram Web не удалось: %s", e)
                if not self._failed_notified:
                    self._failed_notified = True
                    await self.notify("🔑 Не удалось обновить вход через Telegram Web. "
                                      "Перезапустите start.command — он попросит снова отсканировать QR.")
                return
            for market, data in results.items():
                if isinstance(data, str):
                    await self.save(market, data)
                else:
                    log.warning("web login %s: %s", market, data)
            self._failed_notified = False

    async def run_periodic(self, every_hours: float = 6.0) -> None:
        while True:
            await self.refresh(force=True)
            await asyncio.sleep(every_hours * 3600)
