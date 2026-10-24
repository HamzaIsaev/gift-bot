"""Сравнение старой (v1) и новой (v2) торговой логики на сохранённой истории.

На сервере:
  cd ~/gift-bot && docker compose exec gift-bot python scripts/backtest.py --days 14
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gift_bot.backtest import History, report, run  # noqa: E402
from gift_bot.config import Settings  # noqa: E402
from gift_bot.storage.db import Database  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser(description="Бэктест v1 vs v2")
    ap.add_argument("--days", type=float, default=None, help="сколько последних дней прогнать (по умолчанию — всё)")
    ap.add_argument("--capital", type=float, default=None, help="стартовый капитал, TON")
    args = ap.parse_args()
    s = Settings()
    db = Database(s.db_path)
    await db.connect()
    try:
        h = await History.load(db, args.days)
        if not h.events:
            print("В базе пока нет истории лотов. Дайте боту поработать хотя бы несколько дней.")
            return
        capital = args.capital or s.virtual_balance_ton
        print(f"Лотов: {len(h.events)}, моделей с продажами: {len(h.sales)}\n")
        print(report([run(h, s, "v1", capital), run(h, s, "v2", capital)]))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
