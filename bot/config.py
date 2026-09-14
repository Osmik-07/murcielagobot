"""
Все настройки читаются из .env. Ничего не хардкодим в коде.

ВАЖНО ДЛЯ ВСЕХ, КТО ЧИТАЕТ settings:
секретные поля объявлены как pydantic SecretStr — их значение НЕ попадает
в repr/str/логи/трейсбеки. Чтобы получить реальную строку, нужен явный вызов
.get_secret_value():

    Bot(token=settings.bot_token.get_secret_value())
    FragmentClient(seed=settings.fragment_wallet_seed.get_secret_value(), ...)

Секретные поля: bot_token, fragment_wallet_seed, tonconsole_api_key,
payment_tracker_token, webhook_secret_path, webhook_secret_token.
Никогда не логируй результат .get_secret_value().
"""

from decimal import Decimal
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Границы количества Stars у Fragment (FragmentAPI.types.constants:
# STARS_PURCHASE_MIN / STARS_PURCHASE_MAX). Держим здесь копию, чтобы модуль
# настроек не тянул за собой тяжёлый импорт библиотеки; жёсткую проверку перед
# покупкой всё равно делает fragment_gateway по константам самой библиотеки.
FRAGMENT_STARS_MIN = 50
FRAGMENT_STARS_MAX = 10_000_000

# Допустимые версии кошелька в FragmentAPI (SUPPORTED_WALLET_VERSIONS).
WalletVersion = Literal["V4R2", "V5R1"]
# Провайдеры TON API, которые понимает FragmentClient (SUPPORTED_API_PROVIDERS).
ApiProvider = Literal["tonapi", "toncenter"]


class Settings(BaseSettings):
    """Настройки приложения. Обязательны только секреты и адреса, у остального есть дефолты."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Пустое значение в .env (KEY=) считаем «не задано» и берём дефолт,
        # иначе скопированный .env.example ломает парсинг int/Decimal.
        env_ignore_empty=True,
    )

    # ------------------------------------------------------------------ Telegram
    bot_token: SecretStr

    # Чат (или личка) для алертов: провал фулфилмента, переплата, низкий баланс.
    # Отрицательный id — супергруппа/канал. None = алерты некуда слать (не рекомендуется).
    admin_chat_id: int | None = None

    # ------------------------------------------------------- TON / Fragment кошелёк
    # Seed-фраза кошелька: из неё берутся и cookies Fragment (auth_ton_proof),
    # и подпись исходящих транзакций. Самый ценный секрет в проекте.
    fragment_wallet_seed: SecretStr
    # Адрес этого же кошелька — используем для сверки с get_wallet() и в логах.
    fragment_wallet_address: str

    fragment_wallet_version: WalletVersion = "V5R1"
    fragment_api_provider: ApiProvider = "tonapi"
    fragment_timeout_seconds: float = 30.0

    # Cookies Fragment живут в памяти и обновляются лениво: если они старше TTL —
    # переавторизуемся перед вызовом (плюс принудительно по CookieError/VerificationError).
    fragment_cookie_ttl_minutes: int = 30

    # Порог алерта о низком балансе операторского кошелька. Ниже — заказы начнут
    # падать на фулфилменте. Держи ощутимо выше газового минимума (0.01 TON).
    low_balance_alert_ton: Decimal = Decimal("2")
    low_balance_alert_usdt: Decimal = Decimal("10")

    # ------------------------------------------------------------------ TonConsole
    # Ключ TonAPI (Unlimited) — уходит в FragmentClient(api_key=...).
    tonconsole_api_key: SecretStr
    # Токен Payment Tracker: Bearer при создании инвойса и ?api_key= при проверке статуса.
    payment_tracker_token: SecretStr
    payment_tracker_base_url: str = "https://tonconsole.com/api/v1"
    payment_tracker_timeout_seconds: float = 10.0

    # ------------------------------------------------------------- Вебхук трекера
    # Публичный https-адрес сервиса без хвостового слэша, например https://pay.example.com.
    # Пусто — вебхук выключен, остаются кнопка «проверить оплату» и джоб-сверка.
    webhook_base_url: str | None = None
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    # Неугадываемый сегмент пути (см. webhook_path). Обязателен, если задан webhook_base_url.
    webhook_secret_path: SecretStr | None = None
    # Необязательная вторая линия защиты: общий секрет в заголовке.
    webhook_secret_header: str = "X-Webhook-Secret"
    webhook_secret_token: SecretStr | None = None

    # ------------------------------------------------------------------------- БД
    database_url: str

    # FSM aiogram. Пусто — MemoryStorage (состояние теряется при рестарте,
    # ок только для одного процесса). Задан — RedisStorage.
    redis_url: str | None = None

    # ------------------------------------------------------------ Бизнес-параметры
    order_ttl_minutes: int = 25
    margin_percent: Decimal = Decimal("5")

    # Бизнес-лимиты на количество Stars (внутри лимитов Fragment).
    stars_min_amount: int = FRAGMENT_STARS_MIN
    stars_max_amount: int = 100_000

    # Периодические джобы: сверка оплат (страховка на случай потерянного вебхука)
    # и протухание неоплаченных заказов.
    reconcile_interval_seconds: int = 60
    expire_interval_seconds: int = 300

    # ----------------------------------------------------------------- Валидация
    @field_validator("margin_percent")
    @classmethod
    def _check_margin(cls, value: Decimal) -> Decimal:
        # Наценка не может быть отрицательной — это продажа в убыток.
        # Верхняя граница — защита от опечатки вида 500 вместо 5.0.
        if value < 0:
            raise ValueError("MARGIN_PERCENT не может быть отрицательным")
        if value > 1000:
            raise ValueError("MARGIN_PERCENT > 1000% — похоже на опечатку")
        return value

    @field_validator(
        "order_ttl_minutes",
        "fragment_cookie_ttl_minutes",
        "reconcile_interval_seconds",
        "expire_interval_seconds",
    )
    @classmethod
    def _check_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("значение должно быть больше нуля")
        return value

    @field_validator("fragment_timeout_seconds", "payment_tracker_timeout_seconds")
    @classmethod
    def _check_positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("таймаут должен быть больше нуля")
        return value

    @field_validator("low_balance_alert_ton", "low_balance_alert_usdt")
    @classmethod
    def _check_balance_threshold(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("порог баланса не может быть отрицательным")
        return value

    @field_validator("webhook_port")
    @classmethod
    def _check_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("WEBHOOK_PORT должен быть в диапазоне 1..65535")
        return value

    @field_validator("fragment_wallet_address", "database_url")
    @classmethod
    def _check_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("значение не может быть пустым")
        return value

    @field_validator("admin_chat_id")
    @classmethod
    def _check_admin_chat_id(cls, value: int | None) -> int | None:
        if value == 0:
            raise ValueError("ADMIN_CHAT_ID=0 — некорректный чат, оставь пустым или укажи реальный id")
        return value

    @field_validator("webhook_base_url")
    @classmethod
    def _check_webhook_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        if not value:
            return None
        if not value.startswith("https://"):
            # Вебхук несёт данные о деньгах — только https.
            raise ValueError("WEBHOOK_BASE_URL должен начинаться с https://")
        return value

    @field_validator("webhook_secret_path")
    @classmethod
    def _check_webhook_secret_path(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value().strip().strip("/")
        if not raw:
            return None
        # Сегмент пути: только url-safe символы и достаточная длина, чтобы его нельзя было перебрать.
        if len(raw) < 16:
            raise ValueError("WEBHOOK_SECRET_PATH короче 16 символов — слишком легко подобрать")
        if not all(ch.isalnum() or ch in "-_" for ch in raw):
            raise ValueError("WEBHOOK_SECRET_PATH: только латиница, цифры, '-' и '_'")
        return SecretStr(raw)

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if self.stars_min_amount < FRAGMENT_STARS_MIN:
            raise ValueError(f"STARS_MIN_AMOUNT не может быть меньше {FRAGMENT_STARS_MIN} (лимит Fragment)")
        if self.stars_max_amount > FRAGMENT_STARS_MAX:
            raise ValueError(f"STARS_MAX_AMOUNT не может быть больше {FRAGMENT_STARS_MAX} (лимит Fragment)")
        if self.stars_min_amount > self.stars_max_amount:
            raise ValueError("STARS_MIN_AMOUNT больше STARS_MAX_AMOUNT")
        if self.webhook_base_url and self.webhook_secret_path is None:
            # Открытый эндпоинт без секрета — приглашение слать нам фейковые «оплачено».
            raise ValueError("Задан WEBHOOK_BASE_URL, но не задан WEBHOOK_SECRET_PATH")
        return self

    # -------------------------------------------------------- Производные значения
    @property
    def alerts_enabled(self) -> bool:
        """Есть ли куда слать алерты о деньгах."""
        return self.admin_chat_id is not None

    @property
    def webhook_enabled(self) -> bool:
        return self.webhook_base_url is not None and self.webhook_secret_path is not None

    @property
    def webhook_path(self) -> str:
        """Путь, который слушает наш сервер. Содержит секрет — не логировать целиком."""
        if self.webhook_secret_path is None:
            raise ValueError("WEBHOOK_SECRET_PATH не задан")
        return f"/tonconsole/{self.webhook_secret_path.get_secret_value()}"

    @property
    def webhook_url(self) -> str:
        """Полный URL, который нужно прописать в TonConsole. Содержит секрет — не логировать."""
        if self.webhook_base_url is None:
            raise ValueError("WEBHOOK_BASE_URL не задан")
        return f"{self.webhook_base_url}{self.webhook_path}"

    @property
    def fragment_cookie_ttl_seconds(self) -> float:
        return self.fragment_cookie_ttl_minutes * 60.0

    @property
    def invoice_life_time_seconds(self) -> int:
        """life_time для инвойса Payment Tracker — совпадает с TTL заказа."""
        return self.order_ttl_minutes * 60

    @property
    def use_redis_fsm(self) -> bool:
        return bool(self.redis_url)


settings = Settings()  # type: ignore[call-arg]
