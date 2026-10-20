"""Общие структуры данных, одинаковые для всех площадок."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

_RARITY_RE = re.compile(r"\s*\([\d.,]+%\)\s*$")


def clean_attr(value: str | None) -> str:
    """'Zebra (1.5%)' -> 'Zebra'."""
    if not value:
        return ""
    return _RARITY_RE.sub("", value).strip()


def model_key(collection: str, model: str) -> str:
    """Ключ модели подарка, единый для всех площадок."""
    return f"{clean_attr(collection).lower()}|{clean_attr(model).lower()}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Listing:
    """Лот, выставленный на продажу."""

    market: str
    listing_id: str          # id лота на площадке (то, что передаётся в buy)
    collection: str
    model: str
    backdrop: str
    symbol: str
    number: int | None
    price: float             # цена на витрине, TON
    seen_at: datetime = field(default_factory=utcnow)
    raw_seller_net: float | None = None  # если площадка сама отдаёт «цену без комиссии»

    @property
    def key(self) -> str:
        return model_key(self.collection, self.model)

    @property
    def title(self) -> str:
        num = f" #{self.number}" if self.number else ""
        return f"{self.collection}{num} · {self.model}"


@dataclass
class Sale:
    """Состоявшаяся продажа."""

    market: str
    sale_id: str
    collection: str
    model: str
    backdrop: str
    price: float
    sold_at: datetime

    @property
    def key(self) -> str:
        return model_key(self.collection, self.model)


def parse_dt(value: str | int | float | None) -> datetime:
    """ISO-строка или unix-время -> aware datetime (UTC). Нет значения — текущее время."""
    if value in (None, ""):
        return utcnow()
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e12 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
