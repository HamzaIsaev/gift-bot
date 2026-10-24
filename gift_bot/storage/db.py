"""SQLite-хранилище: продажи рынка, сигналы, позиции, настройки, события."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable

import aiosqlite

from gift_bot.models import Sale, utcnow

sqlite_row = aiosqlite.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    market     TEXT NOT NULL,
    sale_id    TEXT NOT NULL,
    model_key  TEXT NOT NULL,
    collection TEXT NOT NULL,
    model      TEXT NOT NULL,
    backdrop   TEXT,
    price      REAL NOT NULL,
    sold_at    TEXT NOT NULL,
    PRIMARY KEY (market, sale_id)
);
CREATE INDEX IF NOT EXISTS ix_sales_key_time ON sales (model_key, sold_at);

CREATE TABLE IF NOT EXISTS signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    mode           TEXT NOT NULL,
    kind           TEXT NOT NULL,          -- flip | arbitrage
    market         TEXT NOT NULL,
    target_market  TEXT NOT NULL,
    listing_id     TEXT NOT NULL,
    model_key      TEXT NOT NULL,
    title          TEXT NOT NULL,
    price          REAL NOT NULL,
    buy_cost       REAL NOT NULL,
    market_price   REAL NOT NULL,
    floor_price    REAL,
    sales_median   REAL,
    sales_count    INTEGER NOT NULL,
    expected_net   REAL NOT NULL,
    profit_pct     REAL NOT NULL,
    required_pct   REAL NOT NULL,
    decision       TEXT NOT NULL,          -- buy | skip | signal
    reason         TEXT
);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals (ts);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT NOT NULL,         -- virtual | real
    status          TEXT NOT NULL,         -- open | sold
    market          TEXT NOT NULL,
    listing_id      TEXT NOT NULL,
    model_key       TEXT NOT NULL,
    title           TEXT NOT NULL,
    collection      TEXT NOT NULL,
    model           TEXT NOT NULL,
    buy_price       REAL NOT NULL,
    buy_cost        REAL NOT NULL,
    market_price    REAL NOT NULL,
    ask_price       REAL NOT NULL,
    min_price       REAL NOT NULL,
    opened_at       TEXT NOT NULL,
    last_reprice_at TEXT NOT NULL,
    reprice_count   INTEGER NOT NULL DEFAULT 0,
    sold_price      REAL,
    net_proceeds    REAL,
    pnl             REAL,
    closed_at       TEXT,
    signal_id       INTEGER REFERENCES signals(id)
);
CREATE INDEX IF NOT EXISTS ix_positions_status ON positions (status);

CREATE TABLE IF NOT EXISTS events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    TEXT NOT NULL,
    level TEXT NOT NULL,
    text  TEXT NOT NULL
);

-- История для анализа и бэктеста
CREATE TABLE IF NOT EXISTS floor_snapshots (
    ts         TEXT NOT NULL,
    market     TEXT NOT NULL,
    model_key  TEXT NOT NULL,
    f1         REAL,
    f2         REAL,
    f3         REAL,
    n_listings INTEGER NOT NULL           -- сколько лотов видно в стакане (до FLOOR_DEPTH_LIMIT)
);
CREATE INDEX IF NOT EXISTS ix_floor_key_time ON floor_snapshots (model_key, ts);

CREATE TABLE IF NOT EXISTS listing_events (
    market          TEXT NOT NULL,
    listing_id      TEXT NOT NULL,
    seen_at         TEXT NOT NULL,
    model_key       TEXT NOT NULL,
    collection      TEXT NOT NULL,
    model           TEXT NOT NULL,
    backdrop        TEXT,
    symbol          TEXT,
    number          INTEGER,
    price           REAL NOT NULL,
    model_rarity    REAL,
    backdrop_rarity REAL,
    symbol_rarity   REAL,
    PRIMARY KEY (market, listing_id)
);
CREATE INDEX IF NOT EXISTS ix_listing_events_time ON listing_events (seen_at);

CREATE TABLE IF NOT EXISTS collections (
    coll_key   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    first_seen TEXT NOT NULL,             -- когда бот впервые увидел коллекцию
    created_at TEXT,                      -- дата выхода (если площадка отдаёт)
    supply     INTEGER,
    upgraded   INTEGER,
    updated_at TEXT NOT NULL
);
"""

# Колонки сигналов v2: справедливая цена, фундаментальность, ликвидность, EV
SIGNAL_V2_COLUMNS = {
    "strategy": "TEXT", "fair": "REAL", "score": "REAL", "sales_7d": "INTEGER",
    "days_to_sell": "REAL", "ev_pct": "REAL", "details": "TEXT",
}


def iso(dt: datetime) -> str:
    return dt.isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        # Подписчики на изменения (админ-панель рассылает их открытым вкладкам и мини-приложению)
        self.listeners: list[Callable[[str], None]] = []

    def _changed(self, kind: str) -> None:
        for listener in self.listeners:
            try:
                listener(kind)
            except Exception:
                pass

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        # Миграция: поля для реальных сделок (id подарка в инвентаре, выставлен ли он)
        cur = await self.conn.execute("PRAGMA table_info(positions)")
        cols = {r["name"] for r in await cur.fetchall()}
        if "item_id" not in cols:
            await self.conn.execute("ALTER TABLE positions ADD COLUMN item_id TEXT")
        if "listed" not in cols:
            await self.conn.execute("ALTER TABLE positions ADD COLUMN listed INTEGER NOT NULL DEFAULT 1")
        cur = await self.conn.execute("PRAGMA table_info(signals)")
        sig_cols = {r["name"] for r in await cur.fetchall()}
        for name, kind in SIGNAL_V2_COLUMNS.items():
            if name not in sig_cols:
                await self.conn.execute(f"ALTER TABLE signals ADD COLUMN {name} {kind}")
        # Момент, когда бот начал наблюдать рынок: коллекции, известные с этого момента, — не «новые»
        await self.conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('db_created_at', ?)", (json.dumps(iso(utcnow())),))
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # --- settings ---
    async def get_setting(self, key: str, default: Any = None) -> Any:
        cur = await self.conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return json.loads(row["value"]) if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        await self.conn.commit()
        self._changed("settings")

    # --- sales ---
    async def add_sales(self, sales: list[Sale]) -> list[Sale]:
        """Сохраняет продажи, возвращает только новые."""
        new = []
        for s in sales:
            cur = await self.conn.execute(
                "INSERT OR IGNORE INTO sales VALUES (?,?,?,?,?,?,?,?)",
                (s.market, s.sale_id, s.key, s.collection, s.model, s.backdrop, s.price, iso(s.sold_at)),
            )
            if cur.rowcount:
                new.append(s)
        await self.conn.commit()
        return new

    async def sales_rows(self, key: str, days: float, until: datetime | None = None) -> list[sqlite_row]:
        """Продажи модели на всех площадках за последние N дней (для fair value и ликвидности)."""
        until = until or utcnow()
        cur = await self.conn.execute(
            "SELECT market, price, sold_at, backdrop FROM sales "
            "WHERE model_key=? AND sold_at>=? AND sold_at<=? ORDER BY sold_at",
            (key, iso(until - timedelta(days=days)), iso(until)))
        return await cur.fetchall()

    # --- история рынка ---
    async def add_floor_snapshot(self, market: str, key: str, prices: list[float]) -> None:
        p = sorted(prices)[:3] + [None] * 3
        await self.conn.execute(
            "INSERT INTO floor_snapshots VALUES (?,?,?,?,?,?,?)",
            (iso(utcnow()), market, key, p[0], p[1], p[2], len(prices)))
        await self.conn.commit()

    async def record_listing(self, lst) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO listing_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lst.market, lst.listing_id, iso(lst.seen_at), lst.key, lst.collection, lst.model,
             lst.backdrop, lst.symbol, lst.number, lst.price,
             lst.model_rarity, lst.backdrop_rarity, lst.symbol_rarity))
        await self.conn.commit()

    async def upsert_collection(self, coll_key: str, name: str, *, supply: int | None = None,
                                upgraded: int | None = None, created_at: datetime | None = None) -> None:
        now = iso(utcnow())
        await self.conn.execute(
            "INSERT INTO collections(coll_key, name, first_seen, created_at, supply, upgraded, updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(coll_key) DO UPDATE SET "
            "created_at=COALESCE(excluded.created_at, created_at), supply=COALESCE(excluded.supply, supply), "
            "upgraded=COALESCE(excluded.upgraded, upgraded), updated_at=excluded.updated_at",
            (coll_key, name, now, iso(created_at) if created_at else None, supply, upgraded, now))
        await self.conn.commit()

    async def get_collection(self, coll_key: str) -> sqlite_row | None:
        cur = await self.conn.execute("SELECT * FROM collections WHERE coll_key=?", (coll_key,))
        return await cur.fetchone()

    async def recent_sale_prices(self, key: str, days: int, market: str | None = None) -> list[float]:
        since = iso(utcnow() - timedelta(days=days))
        sql = "SELECT price FROM sales WHERE model_key=? AND sold_at>=?"
        args: list[Any] = [key, since]
        if market:
            sql += " AND market=?"
            args.append(market)
        cur = await self.conn.execute(sql, args)
        return [r["price"] for r in await cur.fetchall()]

    async def sales_after(self, key: str, market: str, after: datetime) -> list[sqlite_row]:
        cur = await self.conn.execute(
            "SELECT * FROM sales WHERE model_key=? AND market=? AND sold_at>? ORDER BY sold_at",
            (key, market, iso(after)),
        )
        return await cur.fetchall()

    # --- signals ---
    async def add_signal(self, **f: Any) -> int:
        f.setdefault("ts", iso(utcnow()))
        cols = ",".join(f)
        cur = await self.conn.execute(
            f"INSERT INTO signals ({cols}) VALUES ({','.join('?' * len(f))})", tuple(f.values())
        )
        await self.conn.commit()
        self._changed("signals")
        return cur.lastrowid

    # --- positions ---
    async def open_position(self, **f: Any) -> int:
        now = iso(utcnow())
        f.setdefault("opened_at", now)
        f.setdefault("last_reprice_at", now)
        f.setdefault("status", "open")
        cols = ",".join(f)
        cur = await self.conn.execute(
            f"INSERT INTO positions ({cols}) VALUES ({','.join('?' * len(f))})", tuple(f.values())
        )
        await self.conn.commit()
        self._changed("positions")
        return cur.lastrowid

    async def open_positions(self, mode: str | None = None) -> list[sqlite_row]:
        sql = "SELECT * FROM positions WHERE status='open'"
        args: tuple = ()
        if mode:
            sql += " AND mode=?"
            args = (mode,)
        cur = await self.conn.execute(sql + " ORDER BY opened_at", args)
        return await cur.fetchall()

    async def has_open_listing(self, market: str, listing_id: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM positions WHERE market=? AND listing_id=? AND status='open'", (market, listing_id)
        )
        return await cur.fetchone() is not None

    async def reprice(self, pos_id: int, new_ask: float) -> None:
        await self.conn.execute(
            "UPDATE positions SET ask_price=?, last_reprice_at=?, reprice_count=reprice_count+1 WHERE id=?",
            (new_ask, iso(utcnow()), pos_id),
        )
        await self.conn.commit()
        self._changed("positions")

    async def close_position(self, pos_id: int, sold_price: float, net: float, pnl: float) -> None:
        await self.conn.execute(
            "UPDATE positions SET status='sold', sold_price=?, net_proceeds=?, pnl=?, closed_at=? WHERE id=?",
            (sold_price, net, pnl, iso(utcnow()), pos_id),
        )
        await self.conn.commit()
        self._changed("positions")

    async def is_our_item(self, market: str, listing_id: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM positions WHERE status='open' AND mode='real' AND market=? AND item_id=?",
            (market, listing_id))
        return await cur.fetchone() is not None

    async def link_signal(self, pos_id: int, signal_id: int | None) -> None:
        await self.conn.execute("UPDATE positions SET signal_id=? WHERE id=?", (signal_id, pos_id))
        await self.conn.commit()
        self._changed("positions")

    async def mark_listed(self, pos_id: int) -> None:
        await self.conn.execute("UPDATE positions SET listed=1 WHERE id=?", (pos_id,))
        await self.conn.commit()
        self._changed("positions")

    async def get_position(self, pos_id: int) -> sqlite_row | None:
        cur = await self.conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,))
        return await cur.fetchone()

    async def realized_pnl(self, mode: str, since: datetime | None = None,
                           until: datetime | None = None) -> float:
        sql = "SELECT COALESCE(SUM(pnl),0) AS s FROM positions WHERE status='sold' AND mode=?"
        args: list[Any] = [mode]
        if since:
            sql += " AND closed_at>=?"
            args.append(iso(since))
        if until:
            sql += " AND closed_at<?"
            args.append(iso(until))
        cur = await self.conn.execute(sql, args)
        return (await cur.fetchone())["s"]

    async def closed_positions(self, mode: str, since: datetime, until: datetime) -> list[sqlite_row]:
        cur = await self.conn.execute(
            "SELECT * FROM positions WHERE status='sold' AND mode=? AND closed_at>=? AND closed_at<?",
            (mode, iso(since), iso(until)),
        )
        return await cur.fetchall()

    async def model_pnl(self, key: str, mode: str, days: int) -> tuple[int, float]:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(pnl),0) AS s FROM positions "
            "WHERE status='sold' AND mode=? AND model_key=? AND closed_at>=?",
            (mode, key, iso(utcnow() - timedelta(days=days))),
        )
        row = await cur.fetchone()
        return row["n"], row["s"]

    async def skip_reasons(self, since: datetime, until: datetime) -> list[str]:
        cur = await self.conn.execute(
            "SELECT reason FROM signals WHERE decision='skip' AND ts>=? AND ts<?", (iso(since), iso(until)))
        return [r["reason"] or "" for r in await cur.fetchall()]

    async def count_skipped(self, since: datetime, until: datetime, reason_prefix: str = "limit:") -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM signals WHERE decision='skip' AND reason LIKE ? AND ts>=? AND ts<?",
            (reason_prefix + "%", iso(since), iso(until)),
        )
        return (await cur.fetchone())["n"]

    async def recent_closed(self, mode: str, limit: int = 50) -> list[sqlite_row]:
        cur = await self.conn.execute(
            "SELECT * FROM positions WHERE status='sold' AND mode=? ORDER BY closed_at DESC LIMIT ?",
            (mode, limit),
        )
        return await cur.fetchall()

    async def recent_signals(self, limit: int = 50) -> list[sqlite_row]:
        cur = await self.conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))
        return await cur.fetchall()

    # --- events ---
    async def log_event(self, level: str, text: str) -> None:
        await self.conn.execute(
            "INSERT INTO events(ts, level, text) VALUES (?,?,?)", (iso(utcnow()), level, text)
        )
        await self.conn.commit()

