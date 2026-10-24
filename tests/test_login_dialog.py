"""Диалог входа по телефону в боте: шаги, удаление сообщений с секретами, отмена."""
from __future__ import annotations

from types import SimpleNamespace

from gift_bot.bot.app import ControlBot


class FakeLogin:
    def __init__(self):
        self.got = []

    async def submit_phone(self, phone):
        self.got.append(("phone", phone))
        return "code"

    async def submit_code(self, code):
        self.got.append(("code", code))
        return "password"

    async def submit_password(self, pw):
        self.got.append(("password", pw))
        return "done"

    async def screenshot(self):
        return b"\x89PNG"


class FakeRefresher:
    def __init__(self):
        self.login = FakeLogin()
        self.ended = []

    async def phone_login_begin(self):
        return self.login, "qr"

    async def phone_login_end(self, login, grab):
        self.ended.append(grab)
        return "Вход выполнен на 3 из 3 площадок" if grab else None


class Msg:
    def __init__(self, text, sink):
        self.text, self.chat, self.sink, self.deleted = text, SimpleNamespace(id=1), sink, False

    async def delete(self):
        self.deleted = True

    async def answer(self, text, **kw):
        self.sink.append(text)


async def test_login_dialog(env):
    e = await env()
    bot = ControlBot(e.s, e.db, e.risk)
    sent = []

    async def send_message(chat_id, text, **kw):
        sent.append(text)

    async def noop(*a, **k):
        pass

    bot.bot = SimpleNamespace(send_message=send_message, send_chat_action=noop, session=None)
    bot.send_photo = noop
    bot.refresher = FakeRefresher()
    login = bot.refresher.login
    bot.login_state = {"login": login, "step": "phone", "chat": 1}

    msgs = [Msg("+7 999 123-45-67", sent), Msg("1 2 3 4 5", sent), Msg("my secret", sent)]
    for m in msgs:
        assert bot._awaiting_input(m)
        await bot._login_step(m)
    assert login.got == [("phone", "+79991234567"), ("code", "12345"), ("password", "my secret")]
    assert all(m.deleted for m in msgs)                    # номер, код и пароль удалены из чата
    assert bot.login_state is None and bot.refresher.ended == [True]
    assert any("3 из 3" in t for t in sent)
    assert not bot._awaiting_input(Msg("hello", sent))     # после входа обычные сообщения не перехватываются
