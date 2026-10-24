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
QR_RESEND_SEC = 25
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


# Признаки, что вход в Telegram Web уже выполнен. Новые версии Web K хранят аккаунты
# в ключах account1, account2… вместо user_auth, а вместо страниц входа показывают
# список чатов с поиском — проверяем и хранилище, и экран.
LOGGED_IN_JS = """() => {
  if (localStorage.getItem('user_auth')) return true;
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (/^account\\d+$/.test(k) && /userId|user_auth|"id"/.test(localStorage.getItem(k) || '')) return true;
  }
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  if ([...document.querySelectorAll('[class*="page-sign"],[class*="page-auth"],[class*="page-password"]')].some(vis))
    return false;
  if ([...document.querySelectorAll('.chatlist,[data-peer-id]')].some(vis)) return true;
  return [...document.querySelectorAll('input')].some(e => vis(e) && /search|поиск/i.test(e.placeholder || ''));
}"""


async def _logged_in(page) -> bool:
    try:
        return bool(await page.evaluate(LOGGED_IN_JS))
    except Exception:
        return False


async def _wait_login(page, timeout: float, status: Status,
                      on_qr: Callable[[bytes], Awaitable[None]] | None = None) -> None:
    if await _logged_in(page):
        return
    if on_qr is None:
        status("Отсканируйте QR-код в окне браузера: Telegram на телефоне → Настройки → Устройства → "
               "Подключить устройство. Используйте ВТОРОЙ аккаунт.")
    deadline = time.monotonic() + timeout
    next_shot = 0.0
    while time.monotonic() < deadline:
        if await _logged_in(page):
            await asyncio.sleep(3)  # дать Telegram Web догрузиться
            return
        # Сервер без экрана: присылаем снимок страницы с QR (код обновляется примерно раз в 30 с)
        if on_qr and time.monotonic() >= next_shot:
            await asyncio.sleep(4 if next_shot == 0 else 0)  # дождаться отрисовки QR
            await on_qr(await page.screenshot())
            next_shot = time.monotonic() + QR_RESEND_SEC
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
                         executable_path: str | None = None,
                         on_qr: Callable[[bytes], Awaitable[None]] | None = None) -> dict[str, str | Exception]:
    """targets: {market: (bot_username, app_short_name)} -> {market: initData или ошибка}.
    on_qr — если задан, снимки QR-кода отправляются туда (вход на сервере без экрана)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        ctx = await _launch(pw, profile_dir, headless, executable_path)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(WEB_URL)
            wait = LOGIN_WAIT_INTERACTIVE if (on_qr or not headless) else LOGIN_WAIT_HEADLESS
            await _wait_login(page, wait, status, on_qr)
            return await _grab_all(page, targets, status)
        finally:
            await ctx.close()


async def _launch(pw, profile_dir: str, headless: bool, executable_path: str | None = None):
    opts = dict(headless=headless, viewport={"width": 1200, "height": 850}, locale="ru-RU")
    if executable_path:
        return await pw.chromium.launch_persistent_context(profile_dir, executable_path=executable_path, **opts)
    try:  # установленный Google Chrome (отдельный профиль — ваш обычный Chrome не затрагивается)
        return await pw.chromium.launch_persistent_context(profile_dir, channel="chrome", **opts)
    except Exception:
        return await pw.chromium.launch_persistent_context(profile_dir, **opts)


async def _grab_all(page, targets: dict[str, tuple[str, str]], status: Status) -> dict[str, str | Exception]:
    results: dict[str, str | Exception] = {}
    for market, (bot, app) in targets.items():
        status(f"Открываю {market}…")
        try:
            results[market] = await _grab_one(page, bot, app)
            status(f"✅ {market}: данные входа получены")
        except Exception as e:
            results[market] = e
            status(f"❌ {market}: {e}")
    return results


# ---------- вход по номеру телефона (пошагово, через бота) ----------

# Страницы входа Telegram Web K
PAGE_SELECTORS = {
    "qr": ".page-signQR", "phone": ".page-sign", "code": ".page-authCode",
    "password": ".page-password", "signup": ".page-signUp",
}
PHONE_BTN_RE = re.compile(r"(phone|телефон)", re.I)
NEXT_BTN_RE = re.compile(r"^\s*(next|далее|продолжить|continue|войти|log ?in)\b", re.I)
STEP_WAIT = 25


async def _visible(page, selector: str) -> bool:
    try:
        loc = page.locator(selector)
        return await loc.count() > 0 and await loc.first.is_visible()
    except Exception:
        return False


async def current_page(page) -> str:
    """Где сейчас Telegram Web: qr | phone | code | password | signup | done | unknown."""
    if await _logged_in(page):
        return "done"
    for name in ("password", "code", "signup", "phone", "qr"):
        if await _visible(page, PAGE_SELECTORS[name]):
            return name
    return "unknown"


# Поля ввода ищем по всей странице, от самых точных селекторов к общим:
# вёрстка Telegram Web меняется, а держаться за один контейнер ненадёжно.
FIELD_SELECTORS = {
    "phone": [".input-field-phone [contenteditable]", ".input-field-phone input", "input[type=tel]",
              "[inputmode=tel]", "input[name=phone]", ".page-sign .input-field-input",
              ".page-sign [contenteditable]", ".page-sign input"],
    "code": [".page-authCode input", "input[autocomplete=one-time-code]", "input[inputmode=numeric]",
             "input[type=tel]", "input[type=number]", ".page-authCode [contenteditable]",
             # новая вёрстка: обычное текстовое поле без особых признаков
             "input[type=text]:not([placeholder*=earch i]):not([placeholder*=оиск i])", "input:not([type])"],
    "password": ["input[type=password]", ".page-password input"],
}
FIELD_WAIT = 12


async def _find_field(page, kind: str, timeout: float = FIELD_WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for sel in FIELD_SELECTORS[kind]:
            loc = page.locator(sel)
            try:
                for i in range(await loc.count()):
                    if await loc.nth(i).is_visible():
                        return loc.nth(i)
            except Exception:
                continue
        await asyncio.sleep(0.4)
    return None


async def debug_info(page) -> str:
    """Что видно на странице — чтобы по сообщению об ошибке понять, где застряли."""
    try:
        info = await page.evaluate("""() => {
          const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
          const pages = [...document.querySelectorAll('[class*="page-"]')].filter(vis)
            .map(e => [...e.classList].filter(c => c.startsWith('page-')).join('.')).filter(Boolean);
          const btns = [...document.querySelectorAll('button,[role=button],.btn-primary')].filter(vis)
            .map(e => e.innerText.trim().slice(0, 40)).filter(Boolean);
          const inputs = [...document.querySelectorAll('input,[contenteditable]')].filter(vis)
            .map(e => e.tagName.toLowerCase() + (e.type ? '[' + e.type + ']' : '') + (e.className ? '.' + String(e.className).split(' ')[0] : '')
              + (e.placeholder ? ' «' + e.placeholder.slice(0, 30) + '»' : ''));
          const keys = Object.keys(localStorage).filter(k => /auth|account|user/i.test(k)).slice(0, 8);
          return {pages: [...new Set(pages)].slice(0, 5), buttons: btns.slice(0, 6), inputs: inputs.slice(0, 6),
                  url: location.pathname + location.hash.slice(0, 20), keys};
        }""")
        return (f"адрес: {info['url']}; страницы: {info['pages']}; кнопки: {info['buttons']}; "
                f"поля: {info['inputs']}; хранилище: {info['keys']}")
    except Exception as e:
        return f"нет данных ({e})"


async def _fill(page, kind: str, text: str) -> None:
    """Ввести текст в поле (поле может быть <input> или contenteditable)."""
    field = await _find_field(page, kind)
    if field is None:
        raise WebLoginError(f"не нашёл поле для ввода ({kind}). {await debug_info(page)}")
    await field.click()
    await page.keyboard.press("ControlOrMeta+A")
    await page.keyboard.press("Backspace")
    await page.keyboard.type(text, delay=40)


async def _click_text(page, container: str, pattern: re.Pattern) -> bool:
    """Нажать видимую кнопку с подходящим текстом (сначала в контейнере, потом на всей странице)."""
    for scope in (container, ""):
        prefix = f"{scope} " if scope else ""
        buttons = page.locator(f"{prefix}button, {prefix}[role=button], {prefix}.btn-primary, {prefix}a")
        try:
            n = await buttons.count()
        except Exception:
            continue
        for i in range(n):
            b = buttons.nth(i)
            try:
                if await b.is_visible() and pattern.search((await b.inner_text()) or ""):
                    await b.click()
                    return True
            except Exception:
                continue
    return False


async def _where_after_phone(page) -> str:
    """В новой вёрстке у страниц нет классов page-*: если номер отправлен, а поля номера
    больше нет — значит, Telegram уже ждёт код (или облачный пароль)."""
    now = await current_page(page)
    if now != "unknown":
        return now
    if await _find_field(page, "password", timeout=0.1):
        return "password"
    if not await _find_field(page, "phone", timeout=0.1):
        return "code"
    return "unknown"


async def _wait_change(page, before: str, timeout: float = STEP_WAIT, where=current_page) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        now = await where(page)
        if now not in (before, "unknown"):
            return now
        await asyncio.sleep(0.5)
    return await where(page)


class PhoneLogin:
    """Пошаговый вход в Telegram Web: номер → код → (облачный пароль). Браузер живёт между шагами."""

    def __init__(self, profile_dir: str, executable_path: str | None = None):
        self.profile_dir = profile_dir
        self.executable_path = executable_path
        self._pw = None
        self._ctx = None
        self.page = None

    async def start(self) -> str:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._ctx = await _launch(self._pw, self.profile_dir, True, self.executable_path)
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self.page.goto(WEB_URL)
        deadline = time.monotonic() + STEP_WAIT
        while time.monotonic() < deadline:
            where = await current_page(self.page)
            if where != "unknown":
                return where
            await asyncio.sleep(0.5)
        return "unknown"

    async def screenshot(self) -> bytes:
        return await self.page.screenshot()

    async def submit_phone(self, phone: str) -> str:
        """→ code | password | done | phone (ошибка: номер не принят)."""
        page = self.page
        # С экрана QR переходим на вход по номеру; пробуем несколько раз — кнопка появляется не сразу
        for _ in range(3):
            if await _find_field(page, "phone", timeout=2):
                break
            await _click_text(page, PAGE_SELECTORS["qr"], PHONE_BTN_RE)
            await asyncio.sleep(1.5)
        await _fill(page, "phone", phone)
        await asyncio.sleep(0.5)
        if not await _click_text(page, PAGE_SELECTORS["phone"], NEXT_BTN_RE):
            await page.keyboard.press("Enter")
        return await _wait_change(page, "phone", where=_where_after_phone)

    async def submit_code(self, code: str) -> str:
        """→ done | password | code (неверный/просроченный код) | signup."""
        await _fill(self.page, "code", code)
        await asyncio.sleep(0.5)
        if await _find_field(self.page, "code", timeout=0.1):   # код не отправился сам — жмём Enter
            await self.page.keyboard.press("Enter")
        return await _wait_change(self.page, "code", where=_where_after_phone)

    async def submit_password(self, password: str) -> str:
        """→ done | password (неверный пароль)."""
        await _fill(self.page, "password", password)
        await asyncio.sleep(0.3)
        if not await _click_text(self.page, PAGE_SELECTORS["password"], NEXT_BTN_RE):
            await self.page.keyboard.press("Enter")
        return await _wait_change(self.page, "password")

    async def grab(self, targets: dict[str, tuple[str, str]]) -> dict[str, str | Exception]:
        await asyncio.sleep(3)  # дать Telegram Web догрузиться после входа
        return await _grab_all(self.page, targets, lambda s: log.info(s))

    async def close(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()
            self._ctx = self._pw = self.page = None


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
                    await self.notify("🔑 Нужен вход в Telegram Web: отправьте боту /login (по номеру телефона) "
                                      "или /qr (по QR-коду).")
                return
            await self._save_all(results)
            self._failed_notified = False

    async def _save_all(self, results: dict[str, str | Exception]) -> int:
        ok = 0
        for market, data in results.items():
            if isinstance(data, str):
                await self.save(market, data)
                ok += 1
            else:
                log.warning("web login %s: %s", market, data)
        return ok

    async def login_via_bot(self, send_photo: Callable[[bytes, str], Awaitable[None]]) -> str:
        """Вход по QR через Telegram-бота: снимки QR приходят в чат, вы сканируете вторым аккаунтом."""
        if self._lock.locked():
            return "Вход уже выполняется — дождитесь QR-кода."
        async with self._lock:
            self._last = time.monotonic()

            async def on_qr(png: bytes) -> None:
                await send_photo(png, "📷 Отсканируйте вторым аккаунтом: Telegram → Настройки → Устройства → "
                                      "Подключить устройство. Код обновляется — используйте последний снимок.")
            try:
                results = await grab_init_data(self.profile_dir, self.targets, headless=True, on_qr=on_qr)
            except Exception as e:
                return f"❌ Вход не выполнен: {e}"
            ok = await self._save_all(results)
            self._failed_notified = False
            lines = [f"{'✅' if isinstance(v, str) else '❌'} {m}" for m, v in results.items()]
            return f"Вход выполнен на {ok} из {len(results)} площадок:\n" + "\n".join(lines)

    # --- вход по номеру телефона: сессия живёт между сообщениями бота ---
    PHONE_LOGIN_TIMEOUT = 10 * 60

    async def phone_login_begin(self) -> tuple[PhoneLogin, str] | None:
        """Открыть Telegram Web для входа по номеру. None — уже идёт другой вход."""
        if self._lock.locked():
            return None
        await self._lock.acquire()
        login = PhoneLogin(self.profile_dir)
        try:
            where = await login.start()
        except Exception:
            await login.close()
            self._lock.release()
            raise
        self._phone_login = login
        # страховка: если вход бросили на полпути — закрыть браузер и освободить профиль
        self._phone_watchdog = asyncio.create_task(self._phone_timeout(login))
        return login, where

    async def _phone_timeout(self, login: PhoneLogin) -> None:
        await asyncio.sleep(self.PHONE_LOGIN_TIMEOUT)
        if getattr(self, "_phone_login", None) is login:
            await self.phone_login_end(login, grab=False)

    async def phone_login_end(self, login: PhoneLogin, grab: bool) -> str | None:
        """Завершить вход: при grab=True собрать данные площадок и сохранить."""
        if getattr(self, "_phone_login", None) is not login:
            return None
        self._phone_login = None
        watchdog = getattr(self, "_phone_watchdog", None)
        if watchdog and watchdog is not asyncio.current_task():
            watchdog.cancel()
        try:
            if not grab:
                return None
            results = await login.grab(self.targets)
            ok = await self._save_all(results)
            self._failed_notified = False
            self._last = time.monotonic()
            lines = [f"{'✅' if isinstance(v, str) else '❌'} {m}" for m, v in results.items()]
            return f"Вход выполнен на {ok} из {len(results)} площадок:\n" + "\n".join(lines)
        finally:
            await login.close()
            self._lock.release()

    async def run_periodic(self, every_hours: float = 6.0) -> None:
        while True:
            await self.refresh(force=True)
            await asyncio.sleep(every_hours * 3600)
