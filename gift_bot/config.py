"""Конфигурация: секреты только из .env, стратегия и лимиты — со значениями по умолчанию.

Лимиты и режим можно менять на лету через Telegram — они сохраняются в БД
(таблица settings) и имеют приоритет над значениями отсюда.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MarketFees(BaseModel):
    """Комиссии площадки в долях (0.10 = 10%).

    buy  — надбавка к цене лота для покупателя;
    sell — сколько площадка удерживает с продавца.
    """

    buy: float = 0.0
    sell: float = 0.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Секреты (только .env) ---
    bot_token: SecretStr = Field(..., alias="BOT_TOKEN")
    admin_ids: list[int] = Field(..., alias="ADMIN_IDS")
    tg_api_id: int = Field(..., alias="TG_API_ID")
    tg_api_hash: SecretStr = Field(..., alias="TG_API_HASH")
    tg_session_string: SecretStr = Field(..., alias="TG_SESSION_STRING")
    # Запасной вариант для Tonnel: initData, скопированный из браузера
    tonnel_auth_data: SecretStr | None = Field(None, alias="TONNEL_AUTH_DATA")
    # Прокси для запросов к площадкам (необязательно), формат http://user:pass@host:port
    http_proxy: SecretStr | None = Field(None, alias="MARKETS_PROXY")

    # --- Общее ---
    db_path: str = Field("data/gift_bot.sqlite3", alias="DB_PATH")
    timezone: str = Field("Europe/Moscow", alias="TIMEZONE")
    enabled_markets: list[str] = Field(["portals", "tonnel", "mrkt"], alias="ENABLED_MARKETS")
    poll_interval_sec: float = Field(6.0, alias="POLL_INTERVAL_SEC")
    # Виртуальный баланс для режима наблюдения (для % прибыли)
    virtual_balance_ton: float = Field(100.0, alias="VIRTUAL_BALANCE_TON")

    # --- Web App площадок (если площадка сменит имя — правится здесь) ---
    portals_bot: str = "portals"
    portals_app: str = "market"
    tonnel_bot: str = Field("Tonnel_Network_bot", alias="TONNEL_BOT")
    tonnel_app: str = Field("gifts", alias="TONNEL_APP")
    mrkt_bot: str = "mrkt"
    mrkt_app: str = "app"

    # --- Комиссии (ПРОВЕРИТЬ в мини-приложениях перед боевым режимом) ---
    fees_portals: MarketFees = Field(MarketFees(buy=0.0, sell=0.05), alias="FEES_PORTALS")
    fees_tonnel: MarketFees = Field(MarketFees(buy=0.10, sell=0.0), alias="FEES_TONNEL")
    fees_mrkt: MarketFees = Field(MarketFees(buy=0.0, sell=0.0), alias="FEES_MRKT")
    network_fee_ton: float = Field(0.05, alias="NETWORK_FEE_TON")
    # Стоимость переноса подарка между площадками (вывод + перевод + ввод), TON
    transfer_cost_ton: float = Field(0.5, alias="TRANSFER_COST_TON")

    # --- Стратегия ---
    min_profit_pct: float = Field(12.0, alias="MIN_PROFIT_PCT")
    undercut_pct: float = Field(1.0, alias="UNDERCUT_PCT")
    sales_window_days: int = Field(7, alias="SALES_WINDOW_DAYS")
    min_sales_for_blend: int = Field(3, alias="MIN_SALES_FOR_BLEND")
    reprice_after_hours: float = Field(6.0, alias="REPRICE_AFTER_HOURS")
    reprice_step_pct: float = Field(3.0, alias="REPRICE_STEP_PCT")
    # Адаптация: надбавка к порогу для неликвидных/волатильных моделей
    adaptive: bool = Field(True, alias="ADAPTIVE")
    illiquid_extra_pct: float = Field(8.0, alias="ILLIQUID_EXTRA_PCT")
    liquid_bonus_pct: float = Field(2.0, alias="LIQUID_BONUS_PCT")
    volatility_k: float = Field(20.0, alias="VOLATILITY_K")
    loser_cooldown_days: int = Field(7, alias="LOSER_COOLDOWN_DAYS")
    # Сигналы со скидкой меньше этого не пишутся в БД (чтобы не раздувать её)
    log_signal_min_discount_pct: float = Field(5.0, alias="LOG_SIGNAL_MIN_DISCOUNT_PCT")

    # --- Лимиты по умолчанию ---
    max_trade_ton: float = Field(20.0, alias="MAX_TRADE_TON")
    max_daily_loss_ton: float = Field(30.0, alias="MAX_DAILY_LOSS_TON")
    max_open_positions: int = Field(5, alias="MAX_OPEN_POSITIONS")

    # --- Еженедельный отчёт ---
    report_weekday: str = Field("mon", alias="REPORT_WEEKDAY")
    report_hour: int = Field(9, alias="REPORT_HOUR")

    def fees(self, market: str) -> MarketFees:
        return getattr(self, f"fees_{market}")

    def secret_values(self) -> list[str]:
        """Все секреты — чтобы вырезать их из логов."""
        out = []
        for v in (self.bot_token, self.tg_api_hash, self.tg_session_string,
                  self.tonnel_auth_data, self.http_proxy):
            if v is not None and v.get_secret_value():
                out.append(v.get_secret_value())
        return out
