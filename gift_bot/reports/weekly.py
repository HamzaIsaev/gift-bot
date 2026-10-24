"""Еженедельный отчёт."""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape

from gift_bot.models import parse_dt
from gift_bot.risk.limits import RiskManager
from gift_bot.storage.db import Database


LIMIT_TITLES = {"limit:kelly": "размер позиции (Келли)", "limit:concentration": "концентрация в коллекции",
                "limit:max_trade": "лимит суммы сделки", "limit:max_positions": "лимит числа подарков",
                "limit:balance": "не хватило баланса", "limit:daily_loss": "дневной лимит убытка",
                "limit:stop": "остановлен /stop"}


def skip_category(reason: str) -> str:
    """Группа причины пропуска: «неликвид: продаж 2 < 5» -> «неликвид»."""
    r = reason.strip()
    if r.startswith("limit:"):
        key = r.split(" ", 1)[0]
        return LIMIT_TITLES.get(key, key)
    if r.startswith("EV") or r.startswith("прибыль"):
        return "прибыль ниже порога"
    if r.startswith("фундаментальность"):
        return "слабая фундаментальность"
    for sep in (":", " — "):
        if sep in r:
            return r.split(sep, 1)[0]
    return r[:40] or "другое"


def skip_breakdown(reasons: list[str]) -> list[tuple[str, int]]:
    counts: dict[str, int] = defaultdict(int)
    for r in reasons:
        counts[skip_category(r)] += 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)


async def week_stats(db: Database, mode: str, since: datetime, until: datetime) -> dict:
    rows = await db.closed_positions(mode, since, until)
    by_model: dict[str, float] = defaultdict(float)
    hold_hours = []
    for r in rows:
        by_model[f"{r['collection']} · {r['model']}"] += r["pnl"]
        hold_hours.append((parse_dt(r["closed_at"]) - parse_dt(r["opened_at"])).total_seconds() / 3600)
    pnl = sum(r["pnl"] for r in rows)
    return {
        "trades": len(rows),
        "wins": sum(1 for r in rows if r["pnl"] >= 0),
        "losses": sum(1 for r in rows if r["pnl"] < 0),
        "pnl": pnl,
        "by_model": sorted(by_model.items(), key=lambda kv: kv[1], reverse=True),
        "avg_hold_h": statistics.fmean(hold_hours) if hold_hours else None,
        "skipped_limits": await db.count_skipped(since, until),
        "skips": skip_breakdown(await db.skip_reasons(since, until)),
    }


def _delta(cur: float, prev: float) -> str:
    d = cur - prev
    return f"{'▲' if d > 0 else '▼' if d < 0 else '='} {d:+.2f}"


async def build_report(db: Database, risk: RiskManager, balance_ton: float) -> str:
    until = risk.today_start()
    since = until - timedelta(days=7)
    mode = risk.mode_db
    cur = await week_stats(db, mode, since, until)
    prev = await week_stats(db, mode, since - timedelta(days=7), since)
    pct = cur["pnl"] / balance_ton * 100 if balance_ton else 0
    virtual = mode == "virtual"

    lines = [
        f"📊 <b>Отчёт за неделю</b> {since:%d.%m}–{(until - timedelta(days=1)):%d.%m}"
        + (" · 🧪 виртуальные сделки" if virtual else ""),
        "",
        f"Сделок: <b>{cur['trades']}</b> (в плюс {cur['wins']}, в минус {cur['losses']})",
        f"Чистая прибыль: <b>{cur['pnl']:+.2f} TON</b> ({pct:+.1f}% от баланса {balance_ton:.0f} TON)",
        f"Среднее время покупка→продажа: "
        + (f"{cur['avg_hold_h']:.1f} ч" if cur["avg_hold_h"] is not None else "—"),
        f"Пропущено сигналов из-за лимитов: {cur['skipped_limits']}",
    ]
    if cur["skips"]:
        total = sum(n for _, n in cur["skips"])
        lines += ["", f"🔍 Отсеяно сделок: {total}"] + [f"  {escape(c)}: {n}" for c, n in cur["skips"][:8]]
    if cur["by_model"]:
        top = [m for m in cur["by_model"] if m[1] > 0][:3]
        worst = [m for m in reversed(cur["by_model"]) if m[1] < 0][:3]
        if top:
            lines += ["", "🏆 Самые прибыльные:"] + [f"  {escape(n)}: {p:+.2f}" for n, p in top]
        if worst:
            lines += ["", "💸 Самые убыточные:"] + [f"  {escape(n)}: {p:+.2f}" for n, p in worst]
    lines += [
        "",
        "📈 К прошлой неделе:",
        f"  сделок {cur['trades']} vs {prev['trades']}",
        f"  прибыль {cur['pnl']:+.2f} vs {prev['pnl']:+.2f} TON ({_delta(cur['pnl'], prev['pnl'])})",
    ]
    return "\n".join(lines)
