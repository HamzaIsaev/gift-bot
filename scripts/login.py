"""Однократный вход в Telegram-аккаунт бота и получение строки сессии.

Запуск:  python scripts/login.py
Скрипт спросит номер телефона и код из Telegram, затем напечатает
TG_SESSION_STRING — вставьте её в .env. Никому её не показывайте:
она даёт полный доступ к аккаунту.
"""
import asyncio

from pyrogram import Client


async def main() -> None:
    api_id = int(input("TG_API_ID (с my.telegram.org): ").strip())
    api_hash = input("TG_API_HASH: ").strip()
    async with Client("login", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
        me = await app.get_me()
        session = await app.export_session_string()
    print(f"\nВход выполнен: {me.first_name} (id {me.id})")
    print("\nДобавьте в .env строку:\n")
    print(f"TG_SESSION_STRING={session}\n")


if __name__ == "__main__":
    asyncio.run(main())
