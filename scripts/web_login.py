"""Вход на площадки через Telegram Web по QR-коду (без api_id).

Запуск: python scripts/web_login.py
Откроется окно браузера — отсканируйте QR-код вторым аккаунтом. Скрипт сам
откроет Portals, Tonnel и MRKT и сохранит данные входа в базу бота.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gift_bot.config import Settings  # noqa: E402
from gift_bot.markets.manual_auth import ManualAuthStore  # noqa: E402
from gift_bot.markets.web_login import grab_init_data  # noqa: E402
from gift_bot.storage.db import Database  # noqa: E402


async def main() -> int:
    s = Settings()
    os.makedirs(os.path.dirname(s.db_path) or ".", exist_ok=True)
    db = Database(s.db_path)
    await db.connect()
    try:
        store = ManualAuthStore(db)
        results = await grab_init_data(s.web_profile_dir, s.web_login_targets(), headless=False, status=print)
        ok = 0
        for market, data in results.items():
            if isinstance(data, str):
                await store.set(market, data)
                ok += 1
        print(f"\nГотово: вход выполнен на {ok} из {len(results)} площадок.")
        return 0 if ok else 1
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
