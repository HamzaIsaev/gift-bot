"""Настройки, которые можно менять из админ-панели — с понятными объяснениями.

Значения по умолчанию берутся из .env/config.py; изменения из панели хранятся в БД
(ключ strategy_overrides) и применяются сразу, без перезапуска.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from gift_bot.config import Settings
from gift_bot.risk.limits import RiskManager
from gift_bot.storage.db import Database

OVERRIDES_KEY = "strategy_overrides"


@dataclass(frozen=True)
class Item:
    key: str
    group: str
    title: str
    help: str
    kind: str = "number"      # number | toggle
    unit: str = ""
    min: float = 0
    max: float = 100
    step: float = 1
    scale: float = 1          # показываем value*scale (0.4 -> 40 %)
    danger: bool = False      # включение требует подтверждения


GROUPS = [
    ("safety", "🛡 Безопасность", "Жёсткие ограничения: бот никогда их не нарушит."),
    ("buy", "🎯 Когда покупать", "Насколько выгодной должна быть сделка."),
    ("liquidity", "💧 Ликвидность", "Не покупать то, что потом долго не продать."),
    ("new", "🆕 Новые коллекции", "Осторожность с коллекциями, у которых ещё нет истории цен."),
    ("size", "⚖️ Размер позиции", "Сколько капитала можно вложить в одну сделку и одну коллекцию."),
    ("sell", "🏷 Продажа", "Как бот выставляет и снижает цену купленного подарка."),
    ("fees", "💸 Комиссии площадок", "Проверьте в самих площадках — от этого зависит расчёт прибыли."),
    ("strategy", "🧠 Логика", "Какая версия торговой математики работает."),
]

ITEMS: list[Item] = [
    # --- безопасность (лимиты RiskManager) ---
    Item("limit:max_trade", "safety", "Макс. сумма одной сделки",
         "Дороже этого бот не купит ни один подарок. Например, при 20 TON подарок за 25 TON будет пропущен.",
         unit="TON", min=1, max=200, step=1),
    Item("limit:max_daily_loss", "safety", "Макс. убыток за день",
         "Если за сегодня убыток дойдёт до этой суммы, бот перестанет покупать до завтра и напишет вам.",
         unit="TON", min=1, max=200, step=1),
    Item("limit:max_positions", "safety", "Макс. подарков одновременно",
         "Сколько купленных, ещё не проданных подарков может быть сразу. Больше — новые покупки ждут.",
         unit="шт", min=1, max=50, step=1),
    # --- когда покупать ---
    Item("min_profit_pct", "buy", "Минимальная ожидаемая прибыль",
         "Бот купит, только если ожидаемая прибыль после комиссий и с учётом риска не меньше этого. "
         "Например, при 12% подарок за 10 TON покупается, если ожидаемо принесёт хотя бы 1.2 TON.",
         unit="%", min=3, max=50, step=1),
    Item("min_score", "buy", "Минимальная оценка подарка",
         "Оценка 0–100: редкость модели, фона и символа, тираж, возраст коллекции, стабильность цены. "
         "Чем выше порог, тем только более «качественные» подарки.",
         unit="из 100", min=0, max=90, step=5),
    Item("adaptive", "buy", "Адаптивный порог",
         "Поднимает порог прибыли, если сегодня уже есть убыток, и пропускает модели, на которых бот недавно терял деньги.",
         kind="toggle"),
    # --- ликвидность ---
    Item("min_sales_7d", "liquidity", "Мин. продаж модели за неделю",
         "Сколько раз эту модель должны были купить за 7 дней (на всех площадках). Меньше — значит, её трудно продать.",
         unit="продаж", min=1, max=100, step=1),
    Item("min_volume_ton", "liquidity", "Мин. оборот модели за неделю",
         "Сумма всех продаж модели за 7 дней. Маленький оборот — мало покупателей.",
         unit="TON", min=0, max=5000, step=10),
    Item("max_days_to_sell", "liquidity", "Макс. ожидаемый срок продажи",
         "Сколько лотов сейчас на продаже ÷ сколько продаётся в день. Например, 10 лотов и 2 продажи в день = 5 дней.",
         unit="дней", min=1, max=30, step=1),
    Item("max_spread", "liquidity", "Макс. разрыв цены лотов и продаж",
         "Если подарки выставлены намного дороже, чем реально продаются, — рынок «пустой», цена ненадёжна.",
         unit="%", min=5, max=100, step=5, scale=100),
    # --- новые коллекции ---
    Item("new_collection_block_days", "new", "Не покупать первые",
         "Сколько дней после выхода коллекции бот её вообще не трогает — цены в это время скачут.",
         unit="дней", min=0, max=30, step=1),
    Item("new_collection_days", "new", "Коллекция считается новой",
         "Пока коллекции меньше стольких дней, действуют осторожные правила ниже.",
         unit="дней", min=1, max=60, step=1),
    Item("new_profit_mult", "new", "Порог прибыли для новых",
         "Во сколько раз выше требуемая прибыль. Например, ×1.5 при пороге 12% = 18%.",
         unit="×", min=1, max=3, step=0.1),
    Item("new_size_mult", "new", "Размер позиции для новых",
         "Какую долю обычного размера сделки можно вложить в новую коллекцию. 0.5 = вдвое меньше.",
         unit="×", min=0.1, max=1, step=0.1),
    # --- размер позиции ---
    Item("kelly_fraction", "size", "Агрессивность размера (Келли)",
         "Формула Келли считает, сколько капитала разумно ставить при данной выгоде и риске; "
         "мы берём долю от неё. 25% — осторожно, 50% — смелее.",
         unit="%", min=5, max=100, step=5, scale=100),
    Item("max_collection_share", "size", "Макс. доля капитала в одной коллекции",
         "Чтобы не держать все деньги в одной коллекции. 30% при капитале 100 TON = не больше 30 TON.",
         unit="%", min=5, max=100, step=5, scale=100),
    # --- продажа ---
    Item("undercut_pct", "sell", "Насколько ниже рынка выставлять",
         "Чтобы продаться первыми, бот ставит цену чуть ниже справедливой и 2-го лота в стакане.",
         unit="%", min=0, max=10, step=0.5),
    Item("reprice_after_hours", "sell", "Снижать цену каждые",
         "Если подарок не продался за это время, бот снизит цену на шаг (но не ниже безубыточности).",
         unit="ч", min=1, max=48, step=1),
    Item("reprice_step_pct", "sell", "Шаг снижения цены",
         "На сколько снижать за раз. Для ликвидных моделей бот снижает вдвое медленнее, для неликвидных — в 1.5 раза быстрее.",
         unit="%", min=0.5, max=20, step=0.5),
    Item("exit_drop_pct", "sell", "Сигнал на выход при падении",
         "Если справедливая цена упала ниже цены покупки на столько процентов и тренд вниз — бот предупредит.",
         unit="%", min=1, max=50, step=1),
    # --- комиссии ---
    *[Item(f"fee:{m}:{side}", "fees", f"{title}: {'покупатель' if side == 'buy' else 'продавец'}",
           f"Сколько площадка берёт {'сверху с покупателя' if side == 'buy' else 'с продавца при продаже'}.",
           unit="%", min=0, max=20, step=0.5, scale=100)
      for m, title in (("portals", "Portals"), ("tonnel", "Tonnel"), ("mrkt", "MRKT")) for side in ("buy", "sell")],
    # --- логика ---
    Item("strategy_v2", "strategy", "Новая математика (v2) в наблюдении",
         "Справедливая цена, ликвидность, оценка подарка, ожидаемая прибыль с риском. Выключите — будет старая логика.",
         kind="toggle"),
    Item("strategy_v2_live", "strategy", "Новая математика (v2) в боевом режиме",
         "Включайте после проверки бэктестом: тогда реальные сделки тоже пойдут по v2.",
         kind="toggle", danger=True),
]
BY_KEY = {i.key: i for i in ITEMS}

PRESETS = {
    "careful": ("🐢 Осторожно", "Меньше сделок, только очень выгодные и ликвидные.", {
        "min_profit_pct": 18, "min_score": 55, "min_sales_7d": 10, "min_volume_ton": 150,
        "max_days_to_sell": 4, "max_spread": 0.30, "kelly_fraction": 0.15, "max_collection_share": 0.20,
        "new_collection_block_days": 7, "new_profit_mult": 2.0, "new_size_mult": 0.3}),
    "balanced": ("⚖️ Сбалансированно", "Значения по умолчанию.", {}),
    "aggressive": ("🚀 Агрессивно", "Больше сделок, выше риск.", {
        "min_profit_pct": 8, "min_score": 35, "min_sales_7d": 3, "min_volume_ton": 30,
        "max_days_to_sell": 10, "max_spread": 0.50, "kelly_fraction": 0.35, "max_collection_share": 0.40,
        "new_collection_block_days": 2, "new_profit_mult": 1.3, "new_size_mult": 0.6}),
}
PRESET_KEYS = sorted({k for _, _, vals in PRESETS.values() for k in vals})


class SettingsService:
    def __init__(self, settings: Settings, db: Database, risk: RiskManager):
        self.s = settings
        self.db = db
        self.risk = risk
        # Значения по умолчанию — как при запуске, до изменений из панели
        self.defaults = {i.key: self._read(i.key) for i in ITEMS if not i.key.startswith("limit:")}

    # --- чтение/запись значения по ключу ---
    def _read(self, key: str):
        if key.startswith("limit:"):
            return getattr(self.risk.limits, {"max_trade": "max_trade_ton", "max_daily_loss": "max_daily_loss_ton",
                                              "max_positions": "max_open_positions"}[key.split(":")[1]])
        if key.startswith("fee:"):
            _, market, side = key.split(":")
            return getattr(self.s.fees(market), side)
        return getattr(self.s, key)

    def _write(self, key: str, value) -> None:
        if key.startswith("fee:"):
            # новый объект, а не правка общего: значение по умолчанию у Settings разделяемое
            _, market, side = key.split(":")
            fees = self.s.fees(market).model_copy(update={side: value})
            setattr(self.s, f"fees_{market}", fees)
        else:
            setattr(self.s, key, value)

    def _cast(self, item: Item, raw):
        if item.kind == "toggle":
            if isinstance(raw, str):
                return raw.lower() in ("1", "true", "yes", "on")
            return bool(raw)
        value = float(str(raw).replace(",", ".")) / item.scale
        lo, hi = item.min / item.scale, item.max / item.scale
        if not lo <= value <= hi:
            raise ValueError(f"{item.title}: допустимо от {item.min:g} до {item.max:g} {item.unit}".strip())
        current = self._read(item.key)
        if isinstance(current, int) and not isinstance(current, bool):
            return int(round(value))
        return round(value, 6)

    # --- жизненный цикл ---
    async def load(self) -> None:
        """Применить сохранённые из панели изменения (вызывается при старте)."""
        for key, value in (await self.db.get_setting(OVERRIDES_KEY, {}) or {}).items():
            if key in BY_KEY and not key.startswith("limit:"):
                try:
                    self._write(key, value)
                except Exception:
                    pass

    async def _save(self) -> None:
        overrides = {k: self._read(k) for k in self.defaults if self._read(k) != self.defaults[k]}
        await self.db.set_setting(OVERRIDES_KEY, overrides)

    async def set(self, key: str, raw) -> str:
        item = BY_KEY.get(key)
        if item is None:
            raise ValueError("неизвестная настройка")
        if key.startswith("limit:"):
            return await self.risk.set_limit(key.split(":")[1], str(raw))
        value = self._cast(item, raw)
        self._write(key, value)
        await self._save()
        return f"{item.title}: {self.display(item)}"

    async def reset(self, key: str | None = None) -> None:
        for k in ([key] if key else list(self.defaults)):
            if k in self.defaults:
                self._write(k, self.defaults[k])
        await self._save()

    async def apply_preset(self, name: str) -> str:
        title, _, values = PRESETS[name]
        for k in PRESET_KEYS:
            self._write(k, values.get(k, self.defaults[k]))
        await self._save()
        return title

    # --- для панели ---
    def display(self, item: Item) -> str:
        v = self._read(item.key)
        if item.kind == "toggle":
            return "вкл" if v else "выкл"
        return f"{round(v * item.scale, 4):g} {item.unit}".strip()

    def snapshot(self) -> dict:
        groups = []
        for gid, title, desc in GROUPS:
            items = []
            for i in ITEMS:
                if i.group != gid:
                    continue
                value = self._read(i.key)
                default = self.defaults.get(i.key)
                d = asdict(i)
                d["value"] = value if i.kind == "toggle" else round(value * i.scale, 4)
                d["default"] = None if default is None else (default if i.kind == "toggle"
                                                              else round(default * i.scale, 4))
                d["changed"] = default is not None and value != default
                items.append(d)
            groups.append({"id": gid, "title": title, "desc": desc, "items": items})
        active = next((n for n, (_, _, vals) in PRESETS.items()
                       if all(self._read(k) == vals.get(k, self.defaults[k]) for k in PRESET_KEYS)), None)
        return {"groups": groups, "presets": [{"id": n, "title": t, "desc": d} for n, (t, d, _) in PRESETS.items()],
                "active_preset": active}
