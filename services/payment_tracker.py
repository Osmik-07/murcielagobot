"""
Клиент TonConsole Payment Tracker (docs.tonconsole.com/tonconsole/invoices).

Схема работы:
  * создаём инвойс на точную сумму (POST, Authorization: Bearer);
  * статус инвойса всегда перепроверяем авторизованным GET (?api_key=...),
    даже если пришёл вебхук — тело вебхука это НЕДОВЕРЕННЫЙ ввод из интернета,
    оно только повод сходить в API, а не основание двигать деньги.

Важные особенности API, на которых держится вся логика:
  * суммы передаются строкой в МИНИМАЛЬНЫХ неделимых единицах
    (1e9 на GRAM/TON, 1e6 на USDT) — здесь только Decimal, никаких float;
  * своего поля под внешний id заказа нет, поэтому id заказа кладём
    в description, а сам id инвойса сохраняем в заказе;
  * pay_to_address один и тот же для всех инвойсов — параллельные инвойсы
    различаются ТОЛЬКО точной суммой (уникальный хвост даёт services.pricing);
  * статусы ровно: pending | paid | cancelled | expired. Статуса "overpaid" НЕТ,
    overpayment — отдельное поле, которое бывает ненулевым и при expired/cancelled
    (деньги пришли поздно), поэтому его смотрим независимо от статуса.

Токен никогда не попадает в логи и в тексты исключений (см. _redact).
"""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from typing import Any, Final, Mapping

import httpx
import structlog

logger = structlog.get_logger(__name__)

__all__ = [
    "BASE_URL",
    "Invoice",
    "InvoiceNotFoundError",
    "PaymentTrackerClient",
    "PaymentTrackerError",
    "PaymentTrackerTransportError",
    "build_description",
    "extract_order_id",
    "parse_webhook_payload",
]

BASE_URL: Final[str] = "https://tonconsole.com/api/v1"

_CREATE_PATH: Final[str] = "/services/invoices/invoice"
_INVOICE_PATH: Final[str] = "/services/invoices/{invoice_id}"

# Валюты трекера и их знаки после запятой (минимальные неделимые единицы).
CURRENCY_GRAM: Final[str] = "GRAM"  # Fragment/TonConsole называют нативный TON "GRAM"
CURRENCY_USDT: Final[str] = "USDT"

# Asset из db.models -> валюта трекера. Это allowlist: всё, чего здесь нет, отвергаем.
_ASSET_TO_CURRENCY: Final[dict[str, str]] = {
    "ton": CURRENCY_GRAM,
    "usdt_ton": CURRENCY_USDT,
}
_CURRENCY_DECIMALS: Final[dict[str, int]] = {
    CURRENCY_GRAM: 9,
    CURRENCY_USDT: 6,
}

# Статусы инвойса — ровно эти четыре, других в документации нет.
STATUS_PENDING: Final[str] = "pending"
STATUS_PAID: Final[str] = "paid"
STATUS_CANCELLED: Final[str] = "cancelled"
STATUS_EXPIRED: Final[str] = "expired"
KNOWN_STATUSES: Final[frozenset[str]] = frozenset(
    {STATUS_PENDING, STATUS_PAID, STATUS_CANCELLED, STATUS_EXPIRED}
)

# Отдельного поля под наш order_id нет — прячем его в description с префиксом.
DESCRIPTION_PREFIX: Final[str] = "order:"
_ORDER_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# id инвойса подставляется в путь URL, поэтому charset жёстко ограничен.
_INVOICE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

# Границы life_time: меньше минуты бессмысленно, больше недели — почти наверняка ошибка.
_MIN_LIFE_TIME: Final[int] = 60
_MAX_LIFE_TIME: Final[int] = 7 * 24 * 60 * 60

_RETRY_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
_BODY_SNIPPET_LIMIT: Final[int] = 300
_MAX_RETRY_AFTER_SECONDS: Final[float] = 10.0

# Точности хватает с запасом на любые реальные суммы; ROUND_DOWN делаем явно.
_MONEY_CONTEXT_PRECISION: Final[int] = 60


# --------------------------------------------------------------------------- #
# Исключения
# --------------------------------------------------------------------------- #


class PaymentTrackerError(RuntimeError):
    """Ошибка обращения к Payment Tracker или разбора его ответа."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body_snippet: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body_snippet = body_snippet

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status_code is not None:
            parts.append(f"HTTP {self.status_code}")
        if self.body_snippet:
            parts.append(f"body={self.body_snippet}")
        return " | ".join(parts)


class PaymentTrackerTransportError(PaymentTrackerError):
    """Сеть/таймаут: ответа не получили, статус платежа НЕИЗВЕСТЕН.

    Важно для вызывающего кода: это не "не оплачено". Заказ трогать нельзя,
    нужно просто повторить попытку позже (сверка это сделает сама).
    """


class InvoiceNotFoundError(PaymentTrackerError):
    """Трекер ответил 404 — такого инвойса у нас нет."""


# --------------------------------------------------------------------------- #
# Вспомогательные функции разбора (всё максимально терпимо к мусору)
# --------------------------------------------------------------------------- #


def _redact(text: str, token: str | None) -> str:
    """Вырезает токен из строки перед логированием/исключением."""
    if not text:
        return ""
    cleaned = text
    if token:
        cleaned = cleaned.replace(token, "***")
    # Дополнительно глушим api_key=... на случай, если токен попал в URL иначе.
    cleaned = re.sub(r"(api_key=)[^&\s]+", r"\1***", cleaned)
    return cleaned


def _snippet(text: str, token: str | None = None) -> str:
    """Короткий безопасный кусок тела ответа для логов и исключений."""
    flat = " ".join(_redact(text or "", token).split())
    if len(flat) > _BODY_SNIPPET_LIMIT:
        return flat[:_BODY_SNIPPET_LIMIT] + "…"
    return flat


def _opt_str(value: Any) -> str | None:
    """Строка или None. Числа приводим к строке, всё остальное отбрасываем."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _parse_units(value: Any) -> int | None:
    """Минимальные неделимые единицы из ответа: строка или число."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        text = repr(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text:
        return None
    try:
        # Через Decimal, чтобы пережить "0", "0.0" и экспоненциальную запись.
        return int(Decimal(text).to_integral_value(rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    """Дата из ответа: unix-секунды, unix-миллисекунды или ISO-8601."""
    if value is None or isinstance(value, bool):
        return None
    raw: float | None = None
    if isinstance(value, (int, float)):
        raw = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            raw = float(text)
        except ValueError:
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if raw is None:
        return None
    if raw > 1e11:  # похоже на миллисекунды
        raw /= 1000.0
    try:
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def build_description(order_id: str) -> str:
    """description инвойса, из которого потом достаём order_id при сверке."""
    return f"{DESCRIPTION_PREFIX}{order_id}"


def extract_order_id(description: str | None) -> str | None:
    """Обратная операция к build_description. Мусор -> None."""
    if not description:
        return None
    text = description.strip()
    if not text.startswith(DESCRIPTION_PREFIX):
        return None
    candidate = text[len(DESCRIPTION_PREFIX):].strip()
    return candidate if _ORDER_ID_RE.match(candidate) else None


def normalize_asset(asset: Any) -> str:
    """Asset (enum или строка) -> канонический ключ allowlist'а."""
    # У str-enum str() даёт "Asset.ton", поэтому берём именно .value.
    raw = getattr(asset, "value", asset)
    if not isinstance(raw, str):
        raise ValueError(f"неизвестный asset: {raw!r}")
    key = raw.strip().lower()
    if key not in _ASSET_TO_CURRENCY:
        raise ValueError(f"неизвестный asset: {raw!r}")
    return key


def asset_to_currency(asset: Any) -> str:
    """'ton' -> 'GRAM', 'usdt_ton' -> 'USDT'."""
    return _ASSET_TO_CURRENCY[normalize_asset(asset)]


def to_minimal_units(amount: Decimal | int, currency: str) -> int:
    """
    Decimal в целых единицах -> int в минимальных неделимых единицах.

    Округление ВНИЗ: лучше попросить на один нанотон меньше, чем показать
    клиенту сумму, которой не хватит трекеру для засчитывания платежа.
    float не принимаем принципиально — деньги только Decimal.
    """
    if isinstance(amount, (bool, float)):
        raise TypeError("сумма должна быть Decimal, float для денег запрещён")
    if isinstance(amount, int):
        amount = Decimal(amount)
    if not isinstance(amount, Decimal):
        raise TypeError(f"сумма должна быть Decimal, получено {type(amount).__name__}")
    if not amount.is_finite():
        raise ValueError("сумма должна быть конечным числом")

    decimals = _CURRENCY_DECIMALS.get(currency)
    if decimals is None:
        raise ValueError(f"неизвестная валюта: {currency!r}")

    with localcontext() as ctx:
        ctx.prec = _MONEY_CONTEXT_PRECISION
        scaled = amount.scaleb(decimals)
        units = int(scaled.to_integral_value(rounding=ROUND_DOWN))
        truncated = scaled != units

    if units <= 0:
        raise ValueError(f"сумма {amount} слишком мала для {currency}")
    if truncated:
        # Хвост уникальности живёт как раз в младших разрядах: если его срезало,
        # два параллельных инвойса могут стать неразличимыми по сумме.
        logger.warning(
            "payment_tracker.amount_truncated",
            currency=currency,
            requested=str(amount),
            units=units,
        )
    return units


def from_minimal_units(units: int, currency: str) -> Decimal:
    """Минимальные единицы -> Decimal в целых единицах валюты."""
    decimals = _CURRENCY_DECIMALS.get(currency)
    if decimals is None:
        raise ValueError(f"неизвестная валюта: {currency!r}")
    with localcontext() as ctx:
        ctx.prec = _MONEY_CONTEXT_PRECISION
        return Decimal(units).scaleb(-decimals)


# --------------------------------------------------------------------------- #
# Модель инвойса
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Invoice:
    """
    Инвойс Payment Tracker. Все опциональные поля терпят отсутствие:
    ответы create/status различаются набором полей, а тело вебхука вообще
    приходит из интернета и может быть каким угодно.

    Суммы в API — строки в минимальных единицах, поэтому храним и «сырое»
    значение, и разобранное. raw оставляем целиком: он нужен для ручного
    разбора платежей (недоплата/переплата/несовпадение).
    """

    id: str
    status: str
    amount_raw: str | None = None
    description: str | None = None
    date_create: datetime | None = None
    date_expire: datetime | None = None
    date_change: datetime | None = None
    payment_link: str | None = None
    pay_to_address: str | None = None
    paid_by_address: str | None = None
    overpayment_raw: str | None = None
    # Валюта в ответе может отсутствовать: тогда её подставляет вызывающий код
    # (мы знаем asset заказа). Гадать нельзя — GRAM и USDT отличаются в 1000 раз.
    currency: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    # --- статусы -------------------------------------------------------- #

    @property
    def is_paid(self) -> bool:
        return self.status == STATUS_PAID

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    @property
    def is_expired(self) -> bool:
        return self.status == STATUS_EXPIRED

    @property
    def is_cancelled(self) -> bool:
        return self.status == STATUS_CANCELLED

    @property
    def is_known_status(self) -> bool:
        return self.status in KNOWN_STATUSES

    # --- деньги ---------------------------------------------------------- #

    @property
    def amount_units(self) -> int | None:
        """Запрошенная сумма в минимальных единицах."""
        return _parse_units(self.amount_raw)

    @property
    def amount(self) -> Decimal | None:
        """Запрошенная сумма в целых единицах валюты. None, если валюта неизвестна."""
        return self._to_whole(self.amount_units)

    @property
    def overpayment_units(self) -> int:
        """
        Переплата в минимальных единицах. Не зависит от валюты, поэтому
        has_overpayment работает даже когда валюта не пришла.
        """
        units = _parse_units(self.overpayment_raw)
        if units is None:
            return 0
        return units if units > 0 else 0

    @property
    def has_overpayment(self) -> bool:
        """
        Переплата бывает ненулевой при ЛЮБОМ статусе, включая expired/cancelled
        (деньги пришли позже) — проверять её нужно всегда, а не только при paid.
        """
        return self.overpayment_units > 0

    @property
    def overpayment(self) -> Decimal | None:
        """Переплата в целых единицах валюты. None, если валюта неизвестна."""
        return self._to_whole(self.overpayment_units)

    @property
    def has_funds(self) -> bool:
        """
        Пришли ли по инвойсу деньги. Заказ нельзя гасить как просроченный,
        если это True (правило «оплаченный заказ никогда не expired»).
        """
        return self.is_paid or self.has_overpayment

    @property
    def order_id(self) -> str | None:
        """Наш id заказа, спрятанный в description."""
        return extract_order_id(self.description)

    def _to_whole(self, units: int | None) -> Decimal | None:
        if units is None or self.currency is None:
            return None
        try:
            return from_minimal_units(units, self.currency)
        except ValueError:
            return None

    def with_currency(self, currency: str | None) -> "Invoice":
        """Копия с проставленной валютой (когда её знает вызывающий код)."""
        if currency is None or self.currency == currency:
            return self
        return Invoice(
            id=self.id,
            status=self.status,
            amount_raw=self.amount_raw,
            description=self.description,
            date_create=self.date_create,
            date_expire=self.date_expire,
            date_change=self.date_change,
            payment_link=self.payment_link,
            pay_to_address=self.pay_to_address,
            paid_by_address=self.paid_by_address,
            overpayment_raw=self.overpayment_raw,
            currency=currency,
            raw=self.raw,
        )

    @classmethod
    def from_payload(cls, payload: Any, *, currency: str | None = None) -> "Invoice":
        """
        Разбор ответа API или тела вебхука. Обязательны только id и status —
        без них объект бессмысленен; остальное опционально.
        """
        if not isinstance(payload, Mapping):
            raise PaymentTrackerError(
                f"ожидался JSON-объект инвойса, получено {type(payload).__name__}"
            )

        invoice_id = _opt_str(payload.get("id"))
        if invoice_id is None:
            raise PaymentTrackerError("в ответе трекера нет поля id")

        status_raw = _opt_str(payload.get("status"))
        status = status_raw.lower() if status_raw else ""
        if status not in KNOWN_STATUSES:
            # Не падаем: неизвестный статус трактуется как "не оплачено"
            # (is_paid остаётся False), но в логе он обязан быть виден.
            logger.warning(
                "payment_tracker.unknown_status", invoice_id=invoice_id, status=status
            )

        overpayment_raw = _opt_str(payload.get("overpayment"))
        if overpayment_raw is not None and _parse_units(overpayment_raw) is None:
            # Переплата — это деньги, молча терять её нельзя.
            logger.warning(
                "payment_tracker.overpayment_unparsable",
                invoice_id=invoice_id,
                value=_snippet(overpayment_raw),
            )

        payload_currency = _opt_str(payload.get("currency"))
        resolved_currency = (
            payload_currency.upper() if payload_currency else None
        ) or currency

        return cls(
            id=invoice_id,
            status=status,
            amount_raw=_opt_str(payload.get("amount")),
            description=_opt_str(payload.get("description")),
            date_create=_parse_timestamp(payload.get("date_create")),
            date_expire=_parse_timestamp(payload.get("date_expire")),
            date_change=_parse_timestamp(payload.get("date_change")),
            payment_link=_opt_str(payload.get("payment_link")),
            pay_to_address=_opt_str(payload.get("pay_to_address")),
            paid_by_address=_opt_str(payload.get("paid_by_address")),
            overpayment_raw=overpayment_raw,
            currency=resolved_currency,
            raw=dict(payload),
        )


def parse_webhook_payload(raw: dict, *, currency: str | None = None) -> Invoice:
    """
    Разбор тела вебхука TonConsole (тот же JSON инвойса).

    Тело вебхука — НЕДОВЕРЕННЫЙ ввод: считать его подтверждением оплаты нельзя.
    Это только повод сходить в get_invoice() и уже там принять решение.
    Валюту в вебхуке может не прийти — передай её из заказа параметром currency.
    """
    try:
        invoice = Invoice.from_payload(raw, currency=currency)
    except PaymentTrackerError:
        raise
    except Exception as exc:  # тело пришло из интернета, ловим вообще всё
        raise PaymentTrackerError(f"не удалось разобрать вебхук: {type(exc).__name__}") from exc

    logger.info(
        "payment_tracker.webhook_parsed",
        invoice_id=invoice.id,
        status=invoice.status,
        order_id=invoice.order_id,
        has_overpayment=invoice.has_overpayment,
    )
    return invoice


# --------------------------------------------------------------------------- #
# Клиент
# --------------------------------------------------------------------------- #


class PaymentTrackerClient:
    """
    HTTP-клиент Payment Tracker.

    Авторизация различается по методам, как в документации:
      * POST  — заголовок Authorization: Bearer <token>;
      * GET   — query-параметр api_key=<token>.
    Поэтому Authorization НЕ ставится на уровне клиента, только на конкретный POST.
    """

    def __init__(
        self,
        token: str,
        wallet_address: str,
        base_url: str = BASE_URL,
        *,
        timeout: float = 10.0,
        max_attempts: int = 3,
        backoff_base: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise ValueError("payment tracker token пустой")
        self._token = token
        self._wallet_address = (wallet_address or "").strip()
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = max(0.0, backoff_base)
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            headers={"Accept": "application/json"},
        )

    @property
    def wallet_address(self) -> str:
        return self._wallet_address

    async def __aenter__(self) -> "PaymentTrackerClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Идемпотентно: повторный вызов безопасен."""
        if not self._client.is_closed:
            await self._client.aclose()

    # --- публичный API ---------------------------------------------------- #

    async def create_invoice(
        self,
        order_id: str,
        asset: str,
        amount: Decimal,
        life_time_seconds: int,
    ) -> Invoice:
        """
        Создать инвойс на точную сумму.

        amount — уже с наценкой и уникальным хвостом (см. services.pricing),
        в целых единицах валюты (TON или USDT). Здесь она переводится
        в минимальные неделимые единицы и уходит строкой.
        """
        order_id = self._validate_order_id(order_id)
        currency = asset_to_currency(asset)
        units = to_minimal_units(amount, currency)
        life_time = self._validate_life_time(life_time_seconds)

        payload = {
            "amount": str(units),
            "life_time": life_time,
            "currency": currency,
            # Отдельного поля под внешний id в API нет — кладём заказ сюда.
            "description": build_description(order_id),
        }

        logger.info(
            "payment_tracker.create_invoice",
            order_id=order_id,
            currency=currency,
            amount=str(amount),
            amount_units=units,
            life_time=life_time,
        )

        data = await self._request(
            "POST",
            _CREATE_PATH,
            json=payload,
            headers={"Authorization": f"Bearer {self._token}"},
            idempotent=False,
        )
        invoice = Invoice.from_payload(data, currency=currency)

        self._check_pay_to_address(invoice)
        if invoice.amount_units is not None and invoice.amount_units != units:
            # Клиенту показываем сумму, которую ждёт трекер, иначе платёж не сойдётся.
            logger.error(
                "payment_tracker.amount_mismatch",
                order_id=order_id,
                invoice_id=invoice.id,
                requested_units=units,
                returned_units=invoice.amount_units,
            )

        logger.info(
            "payment_tracker.invoice_created",
            order_id=order_id,
            invoice_id=invoice.id,
            status=invoice.status,
        )
        return invoice

    async def get_invoice(self, invoice_id: str, *, currency: str | None = None) -> Invoice:
        """
        Авторитетное состояние инвойса. Именно этот вызов (а не вебхук и не
        кнопка пользователя) решает, оплачен ли заказ.

        currency опционален: если трекер не вернёт поле currency, передай сюда
        валюту заказа, иначе суммы в целых единицах посчитать будет не из чего.
        """
        invoice_id = self._validate_invoice_id(invoice_id)
        data = await self._request(
            "GET",
            _INVOICE_PATH.format(invoice_id=invoice_id),
            params={"api_key": self._token},
            idempotent=True,
        )
        invoice = Invoice.from_payload(data, currency=currency)

        logger.info(
            "payment_tracker.invoice_fetched",
            invoice_id=invoice.id,
            status=invoice.status,
            order_id=invoice.order_id,
            has_overpayment=invoice.has_overpayment,
        )
        if invoice.has_overpayment:
            # Переплата — деньги клиента сверх счёта, нужен ручной разбор.
            logger.warning(
                "payment_tracker.overpayment_detected",
                invoice_id=invoice.id,
                order_id=invoice.order_id,
                status=invoice.status,
                overpayment_units=invoice.overpayment_units,
                overpayment=str(invoice.overpayment) if invoice.overpayment is not None else None,
            )
        return invoice

    # --- внутреннее ------------------------------------------------------- #

    @staticmethod
    def _validate_order_id(order_id: Any) -> str:
        # str() покрывает и uuid.UUID: даёт обычную запись с дефисами.
        candidate = str(order_id).strip()
        if not _ORDER_ID_RE.match(candidate):
            raise ValueError(f"недопустимый order_id: {candidate[:64]!r}")
        return candidate

    @staticmethod
    def _validate_invoice_id(invoice_id: Any) -> str:
        if not isinstance(invoice_id, str):
            raise ValueError(f"invoice_id должен быть строкой, получено {type(invoice_id).__name__}")
        candidate = invoice_id.strip()
        # id уходит в путь URL — никаких слешей, пробелов и прочей экзотики.
        if not _INVOICE_ID_RE.match(candidate):
            raise ValueError(f"недопустимый invoice_id: {candidate[:64]!r}")
        return candidate

    @staticmethod
    def _validate_life_time(life_time_seconds: Any) -> int:
        if isinstance(life_time_seconds, bool) or not isinstance(life_time_seconds, int):
            raise ValueError("life_time_seconds должен быть целым числом секунд")
        if not (_MIN_LIFE_TIME <= life_time_seconds <= _MAX_LIFE_TIME):
            raise ValueError(
                f"life_time_seconds вне допустимого диапазона "
                f"[{_MIN_LIFE_TIME}, {_MAX_LIFE_TIME}]: {life_time_seconds}"
            )
        return life_time_seconds

    def _check_pay_to_address(self, invoice: Invoice) -> None:
        """
        Сверка адреса получателя с нашим кошельком.

        Не роняем заказ: один и тот же аккаунт TON записывается по-разному
        (raw 0:..., bounceable EQ..., non-bounceable UQ...), поэтому строгое
        сравнение давало бы ложные срабатывания. Но расхождение обязано быть
        видно в логах — деньги уходят на адрес, который вернул трекер.
        """
        if not self._wallet_address or not invoice.pay_to_address:
            return
        if invoice.pay_to_address.strip() != self._wallet_address:
            logger.warning(
                "payment_tracker.pay_to_address_mismatch",
                invoice_id=invoice.id,
                pay_to_address=invoice.pay_to_address,
                configured=self._wallet_address,
                hint="разные формы записи одного адреса — норма, чужой адрес — нет",
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        idempotent: bool,
    ) -> Any:
        """
        Запрос с короткими повторами.

        Повторяем только 429/5xx и сетевые ошибки. Для POST (создание инвойса)
        повтор при обрыве соединения разрешён лишь на фазе подключения: запрос,
        который мог дойти до сервера, переигрывать нельзя — получим лишний инвойс.
        """
        last_error: Exception | None = None

        for attempt in range(self._max_attempts):
            is_last = attempt == self._max_attempts - 1
            try:
                response = await self._client.request(
                    method, path, params=params, json=json, headers=headers
                )
            except httpx.HTTPError as exc:
                if is_last or not self._retryable_transport(exc, idempotent=idempotent):
                    raise PaymentTrackerTransportError(
                        f"сеть недоступна при {method} {path}: {type(exc).__name__}"
                    ) from exc
                last_error = exc
                logger.warning(
                    "payment_tracker.transport_retry",
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    error=type(exc).__name__,
                )
                await self._sleep_backoff(attempt)
                continue

            if response.status_code in _RETRY_STATUS_CODES and not is_last:
                logger.warning(
                    "payment_tracker.http_retry",
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    status_code=response.status_code,
                )
                await self._sleep_backoff(attempt, response=response)
                continue

            return self._parse_response(response, method=method, path=path)

        # Сюда попадаем только если цикл исчерпан на повторах.
        raise PaymentTrackerTransportError(
            f"не удалось выполнить {method} {path} за {self._max_attempts} попыток"
        ) from last_error

    def _parse_response(self, response: httpx.Response, *, method: str, path: str) -> Any:
        if response.status_code == 404:
            raise InvoiceNotFoundError(
                "инвойс не найден",
                status_code=404,
                body_snippet=_snippet(response.text, self._token),
            )
        if not response.is_success:
            snippet = _snippet(response.text, self._token)
            logger.error(
                "payment_tracker.http_error",
                method=method,
                path=path,
                status_code=response.status_code,
                body=snippet,
            )
            raise PaymentTrackerError(
                f"{method} {path} завершился ошибкой",
                status_code=response.status_code,
                body_snippet=snippet,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise PaymentTrackerError(
                f"{method} {path} вернул не JSON",
                status_code=response.status_code,
                body_snippet=_snippet(response.text, self._token),
            ) from exc

    @staticmethod
    def _retryable_transport(exc: httpx.HTTPError, *, idempotent: bool) -> bool:
        # Ошибки фазы подключения: запрос точно не был обработан — повтор безопасен.
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return True
        # Запрос мог дойти до сервера: повторяем только идемпотентный GET.
        if isinstance(exc, (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError)):
            return idempotent
        return False

    async def _sleep_backoff(self, attempt: int, response: httpx.Response | None = None) -> None:
        delay = self._backoff_base * (2**attempt)
        if response is not None and response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    # Ограничиваем сверху: хендлер бота ждёт ответа, не висим долго.
                    delay = min(float(retry_after), _MAX_RETRY_AFTER_SECONDS)
                except ValueError:
                    pass
        # Джиттер, чтобы пачка заказов не долбила API синхронно.
        await asyncio.sleep(delay + random.uniform(0, self._backoff_base))
