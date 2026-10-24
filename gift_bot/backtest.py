"""Бэктест: прогон сохранённой истории рынка через старую (v1) и новую (v2) логику.

Источник — та же SQLite, что пишет бот: listing_events (поток лотов), sales (продажи),
floor_snapshots (стаканы), collections. Решения принимаются только по данным,
известным на момент лота. Продажа засчитывается, когда на той же площадке проходит
продажа той же модели не дешевле нашей цены (как в режиме наблюдения).
"""
from __future__ import annotations

import bisect
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from gift_bot.engine.decision_v2 import MarketSnapshot, assess, decide, reprice_step_mult
from gift_bot.models import Listing, parse_dt
from gift_bot.pricing.valuation import Pricer
from gift_bot.storage.db import Database

SNAPSHOT_MAX_AGE = timedelta(hours=6)   # стакан старше — считаем неизвестным
LIQUID_SALES = 20
MAX_VOLATILITY_EXTRA = 15.0


@dataclass
class Trade:
    key: str
    market: str
    opened: datetime
    cost: float
    ask: float
    min_price: float
    last_reprice: datetime
    closed: datetime | None = None
    pnl: float | None = None


@dataclass
class Result:
    name: str
    trades: list[Trade] = field(default_factory=list)
    skips: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def summary(self) -> dict:
        closed = [t for t in self.trades if t.closed]
        pnl = sum(t.pnl for t in closed)
        equity, peak, dd = 0.0, 0.0, 0.0
        for t in sorted(closed, key=lambda t: t.closed):
            equity += t.pnl
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
        days = [(t.closed - t.opened).total_seconds() / 86400 for t in closed]
        return {
            "сделок": len(self.trades),
            "закрыто": len(closed),
            "в плюс": sum(1 for t in closed if t.pnl >= 0),
            "в минус": sum(1 for t in closed if t.pnl < 0),
            "прибыль, TON": round(pnl, 2),
            "макс. просадка, TON": round(dd, 2),
            "ср. срок продажи, дн": round(statistics.fmean(days), 2) if days else None,
            "не продано к концу": len(self.trades) - len(closed),
            "заморожено в непроданных, TON": round(sum(t.cost for t in self.trades if not t.closed), 2),
        }


class History:
    """История из БД с быстрым доступом «на момент t»."""

    def __init__(self):
        self.sales: dict[str, list[tuple[datetime, str, float]]] = defaultdict(list)
        self.snaps: dict[tuple[str, str], list[tuple[datetime, list[float]]]] = defaultdict(list)
        self.events: list[Listing] = []
        self.collections: dict[str, dict] = {}
        self.watch_started: datetime | None = None

    @classmethod
    async def load(cls, db: Database, days: float | None = None) -> "History":
        h = cls()
        since = "" if days is None else (datetime.now().astimezone() - timedelta(days=days + 30)).isoformat()
        for r in await (await db.conn.execute(
                "SELECT model_key, market, price, sold_at FROM sales WHERE sold_at>=? ORDER BY sold_at",
                (since,))).fetchall():
            h.sales[r["model_key"]].append((parse_dt(r["sold_at"]), r["market"], r["price"]))
        for r in await (await db.conn.execute(
                "SELECT * FROM floor_snapshots WHERE ts>=? ORDER BY ts", (since,))).fetchall():
            n = r["n_listings"]
            top = [x for x in (r["f1"], r["f2"], r["f3"]) if x is not None]
            book = top + [top[-1]] * max(0, n - len(top)) if top else []
            h.snaps[(r["model_key"], r["market"])].append((parse_dt(r["ts"]), book))
        ev_since = "" if days is None else (datetime.now().astimezone() - timedelta(days=days)).isoformat()
        for r in await (await db.conn.execute(
                "SELECT * FROM listing_events WHERE seen_at>=? ORDER BY seen_at", (ev_since,))).fetchall():
            h.events.append(Listing(
                market=r["market"], listing_id=r["listing_id"], collection=r["collection"], model=r["model"],
                backdrop=r["backdrop"] or "", symbol=r["symbol"] or "", number=r["number"], price=r["price"],
                seen_at=parse_dt(r["seen_at"]), model_rarity=r["model_rarity"],
                backdrop_rarity=r["backdrop_rarity"], symbol_rarity=r["symbol_rarity"]))
        for r in await (await db.conn.execute("SELECT * FROM collections")).fetchall():
            h.collections[r["coll_key"]] = dict(r)
        watch = await db.get_setting("db_created_at")
        h.watch_started = parse_dt(watch) if watch else None
        return h

    def sales_before(self, key: str, t: datetime, days: float = 30) -> list[tuple[str, float, datetime]]:
        rows = self.sales.get(key, [])
        lo = bisect.bisect_left(rows, (t - timedelta(days=days),))
        hi = bisect.bisect_right(rows, (t, "~"))
        return [(m, p, ts) for ts, m, p in rows[lo:hi]]

    def sales_after(self, key: str, market: str, t: datetime) -> list[tuple[datetime, float]]:
        rows = self.sales.get(key, [])
        lo = bisect.bisect_right(rows, (t, "~"))
        return [(ts, p) for ts, m, p in rows[lo:] if m == market]

    def floors_at(self, key: str, t: datetime, exclude_price: float | None) -> dict[str, list[float]]:
        out = {}
        for (k, m), snaps in self.snaps.items():
            if k != key:
                continue
            i = bisect.bisect_right(snaps, (t, [float("inf")])) - 1
            if i < 0 or t - snaps[i][0] > SNAPSHOT_MAX_AGE:
                continue
            book = list(snaps[i][1])
            if exclude_price is not None and exclude_price in book:
                book.remove(exclude_price)
            out[m] = book
        return out


def decide_v1(lst: Listing, floors: dict[str, list[float]], sales, t: datetime, cfg, fees) -> tuple[float | None, float | None, str | None]:
    """Старая логика (v1) — для сравнения: (ask, cost, причина пропуска)."""
    own = sorted(floors.get(lst.market, []))
    week = [p for m, p, ts in sales if m == lst.market and t - ts <= timedelta(days=cfg.sales_window_days)]
    median = statistics.median(week) if len(week) >= cfg.min_sales_for_blend else None
    floor = own[0] if own else None
    mp = (floor + median) / 2 if (floor and median) else (floor or median)
    if mp is None:
        return None, None, "нет данных о рыночной цене"
    ask = round((min(mp, floor) if floor else mp) * (1 - cfg.undercut_pct / 100), 2)
    cost = fees.buy_cost(lst.market, lst.price)
    profit = (fees.sell_net(lst.market, ask) - cost) / cost * 100
    req = cfg.min_profit_pct
    if cfg.adaptive:
        if len(week) < cfg.min_sales_for_blend:
            req += cfg.illiquid_extra_pct
        elif len(week) >= LIQUID_SALES:
            req -= cfg.liquid_bonus_pct
        if len(week) >= 2:
            cv = statistics.pstdev(week) / statistics.fmean(week)
            req += min(cfg.volatility_k * cv, MAX_VOLATILITY_EXTRA)
    if profit < req:
        return ask, cost, f"прибыль {profit:.1f}% < порога {req:.1f}%"
    return ask, cost, None


def _settle(h: History, tr: Trade, until: datetime, cfg, fees, liquid_mult) -> None:
    """Довести позицию до продажи или конца данных: снижение цены по расписанию, продажа по факту рынка."""
    step_h = timedelta(hours=cfg.reprice_after_hours)
    for ts, price in h.sales_after(tr.key, tr.market, tr.opened):
        if ts > until:
            break
        while ts - tr.last_reprice >= step_h and tr.ask > tr.min_price:
            tr.last_reprice += step_h
            step = cfg.reprice_step_pct * liquid_mult(tr.last_reprice)
            tr.ask = round(max(tr.ask * (1 - step / 100), tr.min_price), 2)
        if price >= tr.ask:
            tr.closed = ts
            tr.pnl = round(fees.sell_net(tr.market, tr.ask) - tr.cost, 4)
            return


def run(h: History, cfg, logic: str, capital: float = 100.0) -> Result:
    fees = Pricer(None, {}, cfg)
    res = Result(logic)
    open_trades: list[Trade] = []
    realized = 0.0
    # Продажи учитываем до конца всей истории, а не до последнего лота
    end = max([e.seen_at for e in h.events] + [rows[-1][0] for rows in h.sales.values() if rows])
    for lst in h.events:
        t = lst.seen_at
        for tr in [x for x in open_trades if x.closed and x.closed <= t]:
            open_trades.remove(tr)
            realized += tr.pnl
        if len(open_trades) >= cfg.max_open_positions:
            res.skips["limit:max_positions"] += 1
            continue
        floors = h.floors_at(lst.key, t, lst.price)
        sales = h.sales_before(lst.key, t)
        if logic == "v1":
            ask, cost, reason = decide_v1(lst, floors, sales, t, cfg, fees)
        else:
            coll = h.collections.get(lst.collection_key, {})
            exposure = sum(x.cost for x in open_trades if x.key.split("|")[0] == lst.collection_key)
            snap = MarketSnapshot(
                now=t, listing=lst, sales=sales, floors=floors,
                supply=coll.get("supply"), upgraded=coll.get("upgraded"),
                created_at=parse_dt(coll["created_at"]) if coll.get("created_at") else None,
                first_seen=parse_dt(coll["first_seen"]) if coll.get("first_seen") else None,
                watch_started=h.watch_started, capital=capital + realized, collection_exposure=exposure)
            d = decide(snap, cfg, fees, cfg.min_profit_pct)
            ask, cost, reason = (d.flip.ask if d.flip else None), d.cost, (None if d.buy else d.reason)
        if reason:
            res.skips[reason.split(" ")[0].rstrip(":")] += 1
            continue
        if cost > cfg.max_trade_ton:
            res.skips["limit:max_trade"] += 1
            continue
        tr = Trade(lst.key, lst.market, t, round(cost, 4), ask, fees.min_ask(lst.market, cost), t)
        res.trades.append(tr)
        open_trades.append(tr)
        if logic == "v2":
            def mult(when, _lst=lst):
                snap = MarketSnapshot(now=when, listing=_lst, sales=h.sales_before(_lst.key, when),
                                      floors=h.floors_at(_lst.key, when, None))
                return reprice_step_mult(assess(snap, cfg).liq.days_to_sell)
        else:
            def mult(_when):
                return 1.0
        _settle(h, tr, end, cfg, fees, mult)
    return res


def report(results: list[Result]) -> str:
    keys = list(results[0].summary())
    width = max(len(k) for k in keys) + 2
    lines = ["".ljust(width) + "".join(r.name.rjust(12) for r in results)]
    for k in keys:
        lines.append(k.ljust(width) + "".join(str(r.summary()[k]).rjust(12) for r in results))
    for r in results:
        top = sorted(r.skips.items(), key=lambda kv: kv[1], reverse=True)[:6]
        if top:
            lines.append(f"\n{r.name}: причины пропуска — " + ", ".join(f"{k} {v}" for k, v in top))
    return "\n".join(lines)
