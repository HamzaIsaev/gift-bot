"""Тексты уведомлений (HTML-разметка Telegram)."""
from __future__ import annotations

from html import escape

MARKET_NAMES = {"portals": "Portals", "tonnel": "Tonnel", "mrkt": "MRKT"}


def mk(market: str) -> str:
    return MARKET_NAMES.get(market, market)


def ton(x: float | None) -> str:
    return "—" if x is None else f"{x:.2f} TON"


def tag(virtual: bool) -> str:
    return "🧪 <b>[ВИРТУАЛЬНО]</b> " if virtual else ""


def bought(plan, pos_id: int, virtual: bool) -> str:
    lst = plan.listing
    v = plan.view
    return (
        f"{tag(virtual)}🛒 <b>Покупка #{pos_id}</b> · {mk(lst.market)}\n"
        f"{escape(lst.title)} · фон {escape(lst.backdrop or '—')}\n"
        f"Цена лота: {ton(lst.price)} (с комиссиями {ton(plan.buy_cost)})\n"
        f"Рыночная цена: {ton(plan.market_price)} "
        f"(floor {ton(v.floor)}, медиана продаж {ton(v.sales_median)}, продаж {v.sales_count})\n"
        f"Выставляю за: {ton(plan.ask_price)} · минимум {ton(plan.min_price)}\n"
        f"Ожидаемая прибыль: {ton(plan.profit_ton)} ({plan.profit_pct:+.1f}%)"
    )


def buy_failed(plan, err: Exception) -> str:
    lst = plan.listing
    return (f"⚠️ <b>Покупка не удалась</b> · {mk(lst.market)}\n{escape(lst.title)} за {ton(lst.price)}\n"
            f"{escape(str(err)[:200])}")


def arbitrage_signal(plan, virtual: bool) -> str:
    lst = plan.listing
    return (
        f"{tag(virtual)}🔀 <b>Арбитраж</b> (только сигнал)\n"
        f"{escape(lst.title)}\n"
        f"Купить на {mk(lst.market)}: {ton(lst.price)} (с комиссиями {ton(plan.buy_cost)})\n"
        f"Продать на {mk(plan.target_market)}: {ton(plan.ask_price)}\n"
        f"Чистыми после переноса: {ton(plan.profit_ton)} ({plan.profit_pct:+.1f}%)"
    )


def repriced(pos, new_ask: float, virtual: bool) -> str:
    return (
        f"{tag(virtual)}📉 Снижаю цену #{pos['id']}: {escape(pos['title'])}\n"
        f"{ton(pos['ask_price'])} → {ton(new_ask)} (минимум {ton(pos['min_price'])})"
    )


def sold(pos, price: float, net: float, pnl: float, virtual: bool) -> str:
    icon = "✅" if pnl >= 0 else "🔻"
    return (
        f"{tag(virtual)}{icon} <b>Продано #{pos['id']}</b> · {mk(pos['market'])}\n"
        f"{escape(pos['title'])}\n"
        f"Куплено: {ton(pos['buy_cost'])} → продано: {ton(price)} (на руки {ton(net)})\n"
        f"Чистая прибыль: <b>{pnl:+.2f} TON</b>"
    )


def daily_loss_hit(pnl: float, limit: float) -> str:
    return (
        f"🛑 <b>Дневной лимит убытка достигнут</b>: {pnl:+.2f} TON (лимит {limit} TON).\n"
        f"Новые покупки остановлены до завтра. Открытые позиции продолжают продаваться."
    )


def exit_signal(pos, fair: float, drop_pct: float, trend: float, virtual: bool) -> str:
    return (
        f"{tag(virtual)}🚪 <b>Сигнал на выход #{pos['id']}</b> · {mk(pos['market'])}\n"
        f"{escape(pos['title'])}\n"
        f"Справедливая цена упала до {ton(fair)}: на {drop_pct:.0f}% ниже покупки ({ton(pos['buy_cost'])}), "
        f"тренд {trend:+.0%}.\n"
        f"Сейчас выставлено за {ton(pos['ask_price'])}. Решите: держать или продать дешевле."
    )
