"""Вход по номеру телефона на имитации страниц входа Telegram Web K в настоящем браузере."""
from __future__ import annotations

import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from gift_bot.markets import web_login
from gift_bot.markets.web_login import PhoneLogin

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

# Страницы как в Telegram Web K: QR → номер (contenteditable) → код (автоотправка) → облачный пароль.
FAKE = r"""<!doctype html><html><body><div id="app"></div><script>
const app = document.getElementById('app');
let phone = '';
function page(cls, html) { app.innerHTML = `<div class="${cls}">${html}</div>`; }
function qr() { page('page-signQR', '<img alt="qr"><button id="b">Войти по номеру телефона</button>');
  b.onclick = phonePage; }
function phonePage() { page('page-sign', '<div class="input-field-phone"><div contenteditable="true" id="ph"></div></div><button id="n">Далее</button><div id="err"></div>');
  n.onclick = () => { phone = ph.innerText.trim();
    if (phone.replace(/\D/g, '').length < 8) { err.textContent = 'Неверный номер'; return; }
    codePage(); }; }
function codePage() { page('page-authCode', '<input type="tel" id="c">');
  c.oninput = () => { if (c.value.length < 5) return;
    if (c.value !== '12345') { c.value = ''; return; }          // неверный код — остаёмся
    phone.endsWith('0') ? pwPage() : done(); }; }
function pwPage() { page('page-password', '<input type="password" id="p"><button id="go">Далее</button>');
  go.onclick = () => { if (p.value === 'secret') done(); else p.value = ''; }; }
function done() { localStorage.setItem('user_auth', '{"id":1}'); location.reload(); }

const params = new URLSearchParams(location.hash.replace(/^#\?/, ''));
const addr = params.get('tgaddr');
if (!localStorage.getItem('user_auth')) qr();
else if (addr) {
  const bot = new URLSearchParams(addr.split('?')[1]).get('domain');
  const f = document.createElement('iframe');
  f.src = '/app/' + bot + '#tgWebAppData=' + encodeURIComponent('INIT_' + bot);
  document.body.appendChild(f);
}
</script></body></html>"""


@pytest.fixture
async def fake_tg(monkeypatch):
    app = web.Application()
    app.router.add_get("/k/", lambda r: web.Response(text=FAKE, content_type="text/html"))
    app.router.add_get("/app/{bot}", lambda r: web.Response(text="app", content_type="text/html"))
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(web_login, "WEB_URL", str(server.make_url("/k/")))
    monkeypatch.setattr(web_login, "APP_WAIT", 10)
    monkeypatch.setattr(web_login, "STEP_WAIT", 3)
    yield
    await server.close()


pytestmark = pytest.mark.skipif(not os.path.exists(CHROME), reason="нет Chromium")


async def test_phone_code_password_flow(fake_tg, tmp_path):
    login = PhoneLogin(str(tmp_path / "p"), executable_path=CHROME)
    try:
        assert await login.start() == "qr"
        assert await login.submit_phone("+1 23") == "phone"            # слишком короткий — ошибка
        assert await login.submit_phone("+79991234560") == "code"
        assert await login.submit_code("99999") == "code"              # неверный код
        assert await login.submit_code("12345") == "password"          # у аккаунта облачный пароль
        assert await login.submit_password("wrong") == "password"
        assert await login.submit_password("secret") == "done"
        res = await login.grab({"mrkt": ("mrkt", "app")})
        assert res == {"mrkt": "INIT_mrkt"}
        assert (await login.screenshot())[:4] == b"\x89PNG"
    finally:
        await login.close()


async def test_without_password_and_session_kept(fake_tg, tmp_path):
    profile = str(tmp_path / "p")
    login = PhoneLogin(profile, executable_path=CHROME)
    try:
        await login.start()
        assert await login.submit_phone("+79991234567") == "code"
        assert await login.submit_code("12345") == "done"
    finally:
        await login.close()
    again = PhoneLogin(profile, executable_path=CHROME)                  # повторный вход не нужен
    try:
        assert await again.start() == "done"
    finally:
        await again.close()


# Новая вёрстка Web K: у страниц нет классов page-*, поле кода — обычный input[type=text],
# аккаунт хранится в account1 вместо user_auth.
FAKE_NEW = r"""<!doctype html><html><body><div id="app"></div><script>
const app = document.getElementById('app');
function phonePage() { app.innerHTML = '<div class="_auth_x1"><input type="tel" id="ph"><button id="n">Далее</button></div>';
  n.onclick = () => codePage(); }
function codePage() { app.innerHTML = '<div class="_auth_x2"><input type="text" class="_input_49ts9_11" id="c"></div>';
  c.onkeydown = e => { if (e.key === 'Enter' && c.value === '12345') {
    localStorage.setItem('account1', '{"userId":1}'); location.reload(); } }; }
if (!localStorage.getItem('account1')) phonePage();
else app.innerHTML = '<input type="text" placeholder="Поиск">';
</script></body></html>"""


async def test_new_layout(monkeypatch, tmp_path):
    app = web.Application()
    app.router.add_get("/k/", lambda r: web.Response(text=FAKE_NEW, content_type="text/html"))
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(web_login, "WEB_URL", str(server.make_url("/k/")))
    monkeypatch.setattr(web_login, "STEP_WAIT", 3)
    login = PhoneLogin(str(tmp_path / "p"), executable_path=CHROME)
    try:
        await login.start()
        assert await login.submit_phone("+79991234567") == "code"
        assert await login.submit_code("12345") == "done"
    finally:
        await login.close()
        await server.close()
