"""
Слой персистентности: модели заказов и сырых платежей.

Главное правило этого модуля — деньги считаем только в Decimal (Numeric),
время храним только в timezone-aware UTC. Никаких float для сумм.
"""

import enum
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Единая точка получения времени: всегда aware UTC."""
    return datetime.now(timezone.utc)


# Денежный тип: 20 знаков, 9 после запятой.
# Хватает и на TON (9 знаков), и на USDT (6 знаков) без потери точности.
MONEY = Numeric(20, 9)

# JSONB на постгресе, обычный JSON на всём остальном (например, в тестах).
JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class ProductType(str, enum.Enum):
    premium = "premium"
    stars = "stars"


class Asset(str, enum.Enum):
    """
    Валюта оплаты. Одно значение — три представления во внешних системах,
    поэтому маппинг живёт здесь, а не расползается по сервисам.
    """

    ton = "ton"
    usdt_ton = "usdt_ton"

    @property
    def fragment_payment_method(self) -> str:
        """Значение для FragmentClient.purchase_* (payment_method=...)."""
        return self.value  # "ton" | "usdt_ton" — совпадает один в один

    @property
    def tracker_currency(self) -> str:
        """Значение поля currency для инвойса TonConsole."""
        return "GRAM" if self is Asset.ton else "USDT"

    @property
    def decimals(self) -> int:
        """Сколько знаков после запятой у минимальной неделимой единицы."""
        return 9 if self is Asset.ton else 6

    @property
    def minor_unit_scale(self) -> int:
        """Множитель до минимальных единиц: 1e9 для TON, 1e6 для USDT."""
        return 10**self.decimals

    @property
    def display_name(self) -> str:
        return "TON" if self is Asset.ton else "USDT"

    def quantize(self, amount: Decimal) -> Decimal:
        """Округляет сумму до точности актива (9 знаков TON / 6 знаков USDT)."""
        return Decimal(amount).quantize(
            Decimal(1).scaleb(-self.decimals), rounding=ROUND_HALF_UP
        )

    def to_minor_units(self, amount: Decimal) -> int:
        """
        Сумма -> строка минимальных единиц для TonConsole (поле amount).
        Возвращаем int, вызывающий код делает str(...).
        """
        return int(self.quantize(amount).scaleb(self.decimals))


class OrderStatus(str, enum.Enum):
    pending = "pending"         # ждём оплату
    paid = "paid"               # платёж подтверждён трекером, покупку ещё не начинали
    fulfilling = "fulfilling"   # прямо сейчас покупаем на Fragment (см. ниже)
    fulfilled = "fulfilled"     # успешно куплено на Fragment
    failed = "failed"           # ошибка фулфилмента после оплаты — нужен ручной разбор
    expired = "expired"         # не оплачено вовремя


# Зачем отдельный fulfilling, а не сразу paid -> fulfilled:
#
# claim «pending -> paid» защищает от повторного ПРИЁМА оплаты, но не от
# повторной ПОКУПКИ. Вебхук и джоба сверки могут одновременно увидеть заказ,
# который уже в paid (например, процесс упал сразу после подтверждения оплаты),
# и оба честно пойдут покупать — товар уедет дважды за одни деньги.
#
# Поэтому право сходить на Fragment тоже разыгрывается атомарным claim-ом
# «paid -> fulfilling»: выигравший покупает, остальные видят None и уходят.
# Заказ, застрявший в fulfilling, автоматически НЕ перезапускается никогда —
# неизвестно, ушла ли транзакция в сеть, поэтому это случай для ручного разбора.
TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.fulfilled, OrderStatus.failed, OrderStatus.expired}
)
#: Статусы «заказ ещё в работе».
ACTIVE_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.pending, OrderStatus.paid, OrderStatus.fulfilling}
)


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """
    Нативный enum постгреса с явными значениями (values_callable), чтобы в БД
    лежали именно строки из enum-а, а не имена членов. Имя типа фиксируем —
    оно совпадает с историческим (lowercase имя класса), миграция не нужна.
    """
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


class Order(Base):
    __tablename__ = "orders"

    __table_args__ = (
        # Быстрый поиск активного заказа пользователя.
        Index("ix_orders_user_status_created", "telegram_user_id", "status", "created_at"),
        # Сверка/протухание: выборки по статусу и сроку.
        Index("ix_orders_status_expires", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)

    product: Mapped[ProductType] = mapped_column(_pg_enum(ProductType, "producttype"))
    recipient_username: Mapped[str] = mapped_column(String(64))

    duration_months: Mapped[int | None] = mapped_column(Integer, nullable=True)  # для premium
    stars_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)     # для stars

    asset: Mapped[Asset] = mapped_column(_pg_enum(Asset, "asset"))

    # ВНИМАНИЕ: историческое имя колонки. Здесь лежит базовая цена Fragment
    # в валюте оплаты: TON для asset=ton, USD для asset=usdt_ton (USDT — долларовый
    # стейбл). Читать удобнее через свойство Order.base_price.
    base_price_ton: Mapped[Decimal] = mapped_column(MONEY)
    # Сумма к оплате: база + наценка + уникальный случайный хвост.
    invoice_amount: Mapped[Decimal] = mapped_column(MONEY)
    # Ровно то число минимальных единиц, которое ушло в TonConsole.
    # Храним, чтобы сверять инвойс с заказом без пересчёта float-ами.
    invoice_amount_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Уникальность обязательна: один инвойс трекера = один заказ.
    # Это второй (после claim_order) барьер против двойного фулфилмента.
    payment_tracker_invoice_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )
    # Адрес, на который трекер просит платить (у всех инвойсов он один и тот же,
    # но пусть лежит рядом с заказом — пригодится для повторной выдачи ссылки).
    pay_to_address: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[OrderStatus] = mapped_column(
        _pg_enum(OrderStatus, "orderstatus"), default=OrderStatus.pending
    )

    # --- факт оплаты (заполняется по данным авторизованного GET статуса) ---
    paid_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    overpayment: Mapped[Decimal] = mapped_column(
        MONEY, default=Decimal("0"), server_default=text("0"), nullable=False
    )
    paid_by_address: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- факт фулфилмента ---
    # transaction_id из PurchaseResult Fragment: доказательство, что покупка прошла.
    fragment_tx_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Ставится один раз — чтобы админ не получал один и тот же алерт по кругу.
    admin_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Момент, когда заказ ушёл в fulfilling. По нему джоба сверки понимает,
    # что покупка зависла, и зовёт админа (сама она такое не перезапускает).
    fulfillment_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ------------------------------------------------------------------
    # Удобные производные свойства (без обращений к БД)
    # ------------------------------------------------------------------

    @property
    def base_price(self) -> Decimal:
        """Читаемый алиас base_price_ton: база в валюте оплаты."""
        return self.base_price_ton

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_expired(self) -> bool:
        """Истёк ли срок по часам (не путать со статусом expired)."""
        return _as_utc(self.expires_at) <= utcnow()

    @property
    def fragment_payment_method(self) -> str:
        return self.asset.fragment_payment_method

    @property
    def quantity(self) -> int | None:
        """Единый доступ к «объёму» заказа: месяцы для premium, штуки для stars."""
        return self.duration_months if self.product is ProductType.premium else self.stars_amount

    def __repr__(self) -> str:  # pragma: no cover - для логов и отладки
        return (
            f"<Order {self.id} {self.product.value} {self.status.value} "
            f"{self.invoice_amount} {self.asset.display_name}>"
        )


class Transaction(Base):
    """
    Сырые данные о входящем платеже — на случай overpaid/mismatch и ручного разбора.
    Пишется идемпотентно по tx_hash (см. repository.record_transaction).
    """

    __tablename__ = "transactions"

    __table_args__ = (Index("ix_transactions_order_created", "order_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )  # может быть не сопоставлена
    # Хэш транзакции, а если его нет — id инвойса трекера. Ключ идемпотентности.
    tx_hash: Mapped[str] = mapped_column(String(128), unique=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    asset: Mapped[Asset] = mapped_column(_pg_enum(Asset, "asset"))
    overpayment: Mapped[Decimal] = mapped_column(
        MONEY, default=Decimal("0"), server_default=text("0"), nullable=False
    )
    from_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    matched: Mapped[bool] = mapped_column(default=False)
    # Откуда узнали о платеже: "webhook" | "button" | "reconcile".
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Полный ответ трекера как есть — для ручной сверки.
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - для логов и отладки
        return f"<Transaction {self.tx_hash} {self.amount} {self.asset.display_name}>"


def _as_utc(value: datetime) -> datetime:
    """
    Страховка от naive datetime: если драйвер/БД вернули время без tz,
    считаем его UTC. Все сравнения времени в проекте идут через это.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
