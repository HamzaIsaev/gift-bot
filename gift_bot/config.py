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
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      env_ignore_empty=True)

    # --- Секреты (только .env) ---
    bot_token: SecretStr = Field(..., alias="BOT_TOKEN")
    admin_ids: list[int] = Field(..., alias="ADMIN_IDS")
    # Автоматический вход на площадки (если my.telegram.org недоступен — оставить пустыми,
    # тогда вход ручной: /auth <площадка> <данные из Telegram Web>)
    tg_api_id: int | None = Field(None, alias="TG_API_ID")
    tg_api_hash: SecretStr | None = Field(None, alias="TG_API_HASH")
    tg_session_string: SecretStr | None = Field(None, alias="TG_SESSION_STRING")
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

    # --- Торговая математика v2 (fair value, ликвидность, EV) ---
    strategy_v2: bool = Field(True, alias="STRATEGY_V2")                # v2 в режиме наблюдения
    strategy_v2_live: bool = Field(False, alias="STRATEGY_V2_LIVE")     # v2 и в боевом (после проверки)
    sales_half_life_days: float = Field(3.0, alias="SALES_HALF_LIFE_DAYS")  # вес продажи вдвое меньше через N дней
    mad_k: float = Field(3.0, alias="MAD_K")                             # выброс — дальше 3·MAD от медианы
    fair_sales_k: float = Field(5.0, alias="FAIR_SALES_K")               # вес продаж = n/(n+K)
    fair_max_sales_weight: float = Field(0.8, alias="FAIR_MAX_SALES_WEIGHT")
    fair_thin_discount: float = Field(0.05, alias="FAIR_THIN_DISCOUNT")  # дисконт, если продаж мало
    fair_trend_k: float = Field(1.0, alias="FAIR_TREND_K")               # дисконт = падение тренда × K
    fair_trend_max_discount: float = Field(0.10, alias="FAIR_TREND_MAX_DISCOUNT")
    floor_depth_limit: int = Field(10, alias="FLOOR_DEPTH_LIMIT")        # сколько лотов смотреть в стакане
    # Ликвидность (по всем площадкам вместе)
    min_sales_7d: int = Field(5, alias="MIN_SALES_7D")
    min_volume_ton: float = Field(50.0, alias="MIN_VOLUME_TON")          # оборот за 7 дней
    max_days_to_sell: float = Field(7.0, alias="MAX_DAYS_TO_SELL")       # активные лоты / продаж в день
    max_spread: float = Field(0.40, alias="MAX_SPREAD")                  # |медиана − floor| / медиана
    # Фундаментальность
    min_score: float = Field(45.0, alias="MIN_SCORE")                    # 0–100
    # Новые коллекции (появились, когда бот уже работал)
    new_collection_days: float = Field(14, alias="NEW_COLLECTION_DAYS")
    new_collection_block_days: float = Field(3, alias="NEW_COLLECTION_BLOCK_DAYS")  # первые дни не покупать
    new_profit_mult: float = Field(1.5, alias="NEW_PROFIT_MULT")         # порог прибыли ×1.5
    new_size_mult: float = Field(0.5, alias="NEW_SIZE_MULT")             # размер позиции ×0.5
    new_min_sales: int = Field(10, alias="NEW_MIN_SALES")
    new_window_days: float = Field(3, alias="NEW_WINDOW_DAYS")           # окно цены короче
    new_vol_discount_k: float = Field(1.0, alias="NEW_VOL_DISCOUNT_K")   # доп. дисконт = σ·K
    # Размер позиции
    kelly_fraction: float = Field(0.25, alias="KELLY_FRACTION")
    max_collection_share: float = Field(0.30, alias="MAX_COLLECTION_SHARE")  # доля капитала в одной коллекции
    # Продажа
    exit_drop_pct: float = Field(10.0, alias="EXIT_DROP_PCT")            # fair ниже покупки на N% + тренд вниз → выход

    # --- Лимиты по умолчанию ---
    max_trade_ton: float = Field(20.0, alias="MAX_TRADE_TON")
    max_daily_loss_ton: float = Field(30.0, alias="MAX_DAILY_LOSS_TON")
    max_open_positions: int = Field(5, alias="MAX_OPEN_POSITIONS")

    # --- Админ-панель ---
    panel_enabled: bool = Field(True, alias="PANEL_ENABLED")
    # 127.0.0.1 — панель видна только на этом компьютере; на сервере 0.0.0.0 + пароль
    panel_host: str = Field("127.0.0.1", alias="PANEL_HOST")
    panel_port: int = Field(8080, alias="PANEL_PORT")
    panel_password: SecretStr | None = Field(None, alias="PANEL_PASSWORD")
    # Домен с https для мини-приложения (на сервере выставляет scripts/server_setup.sh)
    panel_domain: str | None = Field(None, alias="PANEL_DOMAIN")

    @property
    def webapp_url(self) -> str | None:
        return f"https://{self.panel_domain}/app" if self.panel_domain else None

    # --- Еженедельный отчёт ---
    report_weekday: str = Field("mon", alias="REPORT_WEEKDAY")
    report_hour: int = Field(9, alias="REPORT_HOUR")

    # Сессия Telegram Web для входа по QR (план Б)
    web_profile_dir: str = Field("data/tg_web_profile", alias="WEB_PROFILE_DIR")
    web_refresh_hours: float = Field(6.0, alias="WEB_REFRESH_HOURS")

    def web_login_targets(self) -> dict[str, tuple[str, str]]:
        all_targets = {
            "portals": (self.portals_bot, self.portals_app),
            "tonnel": (self.tonnel_bot, self.tonnel_app),
            "mrkt": (self.mrkt_bot, self.mrkt_app),
        }
        return {m: t for m, t in all_targets.items() if m in self.enabled_markets}

    @property
    def auto_auth(self) -> bool:
        """True — входить на площадки автоматически через Telegram-сессию."""
        return bool(self.tg_api_id and self.tg_api_hash and self.tg_session_string
                    and self.tg_session_string.get_secret_value())

    def fees(self, market: str) -> MarketFees:
        return getattr(self, f"fees_{market}")

    def secret_values(self) -> list[str]:
        """Все секреты — чтобы вырезать их из логов."""
        out = []
        for v in (self.bot_token, self.tg_api_hash, self.tg_session_string,
                  self.tonnel_auth_data, self.http_proxy, self.panel_password):
            if v is not None and v.get_secret_value():
                out.append(v.get_secret_value())
        return out
