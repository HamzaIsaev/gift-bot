"""Однократный вход в Telegram-аккаунт бота и получение строки сессии.

Запуск:  python scripts/login.py [--write-env]
Берёт TG_API_ID / TG_API_HASH из окружения (или спрашивает), просит телефон
и код из Telegram. С --write-env сам записывает TG_SESSION_STRING в .env,
иначе печатает её. Никому её не показывайте: она даёт полный доступ к аккаунту.
"""
import asyncio
import os
import sys
from pathlib import Path

from pyrogram import Client

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def write_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main() -> None:
    api_id = os.environ.get("TG_API_ID") or input("TG_API_ID (с my.telegram.org): ").strip()
    api_hash = os.environ.get("TG_API_HASH") or input("TG_API_HASH: ").strip()
    async with Client("login", api_id=int(api_id), api_hash=api_hash, in_memory=True) as app:
        me = await app.get_me()
        session = await app.export_session_string()
    print(f"\nВход выполнен: {me.first_name} (id {me.id})")
    if "--write-env" in sys.argv:
        write_env("TG_SESSION_STRING", session)
        print("Строка сессии сохранена в .env")
    else:
        print("\nДобавьте в .env строку:\n")
        print(f"TG_SESSION_STRING={session}\n")


if __name__ == "__main__":
    asyncio.run(main())
