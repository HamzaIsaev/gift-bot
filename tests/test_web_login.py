"""Проверка входа через Telegram Web на имитации web.telegram.org в настоящем браузере."""
from __future__ import annotations

import os
from urllib.parse import quote

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from gift_bot.markets import web_login
from gift_bot.markets.web_login import grab_init_data, init_data_from_url

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
INIT = "query_id=AAE&user=%7B%22id%22%3A1%7D&auth_date=1760000000&hash=abc"

# Имитация Telegram Web: «вход» через localStorage, по ссылке tgaddr показывает
# кнопку «Открыть», после нажатия встраивает фрейм мини-приложения с tgWebAppData.
FAKE_TG = """<!doctype html><html><body><script>
const params = new URLSearchParams(location.hash.replace(/^#\\?/, ''));
if (location.search.includes('login')) localStorage.setItem('user_auth', '{"id":1}');
const addr = params.get('tgaddr');
if (addr && localStorage.getItem('user_auth')) {
  const bot = new URLSearchParams(addr.split('?')[1]).get('domain');
  const b = document.createElement('button'); b.textContent = 'Open';
  b.onclick = () => { const f = document.createElement('iframe');
    f.src = '/app/' + bot + '#tgWebAppData=' + encodeURIComponent('INIT_' + bot) + '&tgWebAppVersion=8.0';
    document.body.appendChild(f); };
  document.body.appendChild(b);
}
</script></body></html>"""


def test_init_data_from_url():
    url = f"https://portal-market.com/#tgWebAppData={quote(INIT, safe='')}&tgWebAppVersion=8.0&tgWebAppPlatform=web"
    assert init_data_from_url(url) == INIT
    assert init_data_from_url("https://example.com/") is None


@pytest.mark.skipif(not os.path.exists(CHROME), reason="нет Chromium")
async def test_grab_init_data_end_to_end(tmp_path, monkeypatch):
    app = web.Application()
    app.router.add_get("/k/", lambda r: web.Response(text=FAKE_TG, content_type="text/html"))
    app.router.add_get("/app/{bot}", lambda r: web.Response(text="<html>app</html>", content_type="text/html"))
    server = TestServer(app)
    await server.start_server()
    try:
        base = str(server.make_url("/k/"))
        monkeypatch.setattr(web_login, "WEB_URL", base)
        monkeypatch.setattr(web_login, "APP_WAIT", 10)
        profile = str(tmp_path / "profile")

        # Без входа в headless — понятная ошибка, а не зависание
        monkeypatch.setattr(web_login, "LOGIN_WAIT_HEADLESS", 2)
        with pytest.raises(web_login.WebLoginError):
            await grab_init_data(profile, {"portals": ("portals", "market")}, headless=True, executable_path=CHROME)

        # «Сканируем QR»: первая загрузка страницы с ?login сохраняет сессию в профиле
        monkeypatch.setattr(web_login, "WEB_URL", base + "?login=1")
        targets = {"portals": ("portals", "market"), "mrkt": ("mrkt", "app")}
        res = await grab_init_data(profile, targets, headless=True, executable_path=CHROME)
        assert res == {"portals": "INIT_portals", "mrkt": "INIT_mrkt"}

        # Повторно — уже без «входа»: сессия берётся из сохранённого профиля
        monkeypatch.setattr(web_login, "WEB_URL", base)
        res = await grab_init_data(profile, {"tonnel": ("Tonnel_Network_bot", "gifts")},
                                   headless=True, executable_path=CHROME)
        assert res == {"tonnel": "INIT_Tonnel_Network_bot"}
    finally:
        await server.close()
