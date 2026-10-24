"""Бэктест на синтетической истории: v2 обходит неликвидную ловушку, куда лезет v1."""
from __future__ import annotations

from datetime import timedelta

from gift_bot.backtest import History, report, run
from gift_bot.models import utcnow
from gift_bot.storage.db import iso
from tests.conftest import listing, make_settings, sale

T0 = utcnow() - timedelta(days=12)


async def seed(db):
    # Ликвидная модель: продажи по 13 каждые 6 часов
    await db.add_sales([sale(sid=f"A{i}", price=13.0, when=T0 + timedelta(hours=6 * i)) for i in range(48)])
    # Ловушка: 2 старые дорогие продажи, длинный стакан, после покупки никто не покупает
    await db.add_sales([sale(sid=f"B{i}", price=20.0, model="Ghost", when=T0 - timedelta(days=20 + i))
                        for i in range(2)])
    for h in range(0, 12 * 24, 3):
        ts = iso(T0 + timedelta(hours=h))
        await db.conn.execute("INSERT INTO floor_snapshots VALUES (?,?,?,?,?,?,?)",
                              (ts, "portals", "toy bear|wizard", 13.0, 13.5, 14.0, 3))
        await db.conn.execute("INSERT INTO floor_snapshots VALUES (?,?,?,?,?,?,?)",
                              (ts, "portals", "toy bear|ghost", 20.0, 22.0, 25.0, 10))
    await db.conn.commit()
    good = listing(lid="GOOD", price=10.0)
    trap = listing(lid="TRAP", price=14.0, model="Ghost")
    for lst, day in ((good, 8), (trap, 8.1)):
        lst.seen_at = T0 + timedelta(days=day)
        lst.model_rarity, lst.backdrop_rarity, lst.symbol_rarity = 0.8, 1.0, 0.5
        await db.record_listing(lst)
    await db.upsert_collection("toy bear", "Toy Bear", supply=20_000, upgraded=8_000)
    # Коллекция известна с начала наблюдения — не «новая»
    await db.conn.execute("UPDATE collections SET first_seen=?", (iso(T0 - timedelta(days=30)),))
    await db.conn.execute("UPDATE settings SET value=? WHERE key='db_created_at'",
                          (f'"{iso(T0 - timedelta(days=30))}"',))
    await db.conn.commit()


async def test_backtest_v1_vs_v2(env):
    e = await env()
    await seed(e.db)
    cfg = make_settings(STRATEGY_V2=True)
    h = await History.load(e.db)
    assert len(h.events) == 2
    v1, v2 = run(h, cfg, "v1"), run(h, cfg, "v2")
    s1, s2 = v1.summary(), v2.summary()
    assert s1["сделок"] == 2 and s1["не продано к концу"] == 1      # v1 купил ловушку
    assert s2["сделок"] == 1 and s2["закрыто"] == 1                   # v2 взял только ликвидную
    assert s2["заморожено в непроданных, TON"] == 0
    assert any(k.startswith("неликвид") for k in v2.skips)
    text = report([v1, v2])
    assert "прибыль, TON" in text and "v2" in text
