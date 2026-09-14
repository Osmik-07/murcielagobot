"""
Шлюз к Fragment.com поверх библиотеки fragment-api-py (импорт: FragmentAPI).

Режим только FULL MODE: cookies + seed + api_key.
Cookies получаем чистой подписью кошелька через auth_ton_proof(seed) — Telegram
OAuth (QR / телефон) не требуется, поэтому НЕЛЬЗЯ звать client.refresh_cookies()
или FragmentClient.authenticate(): они умеют уходить в интерактивный QR-флоу и
повесят бота. Обновление сессии всегда идёт через auth_ton_proof + пересборку
клиента.

Cookies на диск не пишем: переавторизация бесплатная и мгновенная.
Живут они по TTL (по умолчанию 30 минут) плюс принудительно обновляются,
если Fragment ответил, что сессия протухла.

ВАЖНО ПРО ГАЗ: покупка за usdt_ton — это jetton-перевод, комиссию сети всё
равно платим в TON. Поэтому даже для USDT-заказов на кошельке обязан быть
запас TON (см. GAS_RESERVE_TON), иначе транзакция не уйдёт.

ВАЖНО ПРО ДЕНЬГИ: этот модуль никогда не превращает ошибку в «успех».
Если библиотека вернула что-то кроме подтверждённого PurchaseResult —
поднимаем исключение. Идемпотентность (защита от повторной выдачи товара)
живёт уровнем выше, в фулфилменте заказа.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_UP
from typing import Any, Awaitable, Callable, TypeVar

import structlog
from FragmentAPI import FragmentClient
from FragmentAPI.exceptions import (
    AlreadySubscribedError,
    ConfigurationError,
    CookieError,
    FragmentAPIError,
    FragmentError,
    FragmentPageError,
    PaidMessageLimitError,
    TransactionError,
    UserNotFoundError,
    VerificationError,
    WalletError,
)
from FragmentAPI.types.constants import (
    MIN_GRAM_BALANCE,
    PREMIUM_MONTHS_VALID,
    REQUIRED_COOKIE_KEYS_WALLET,
    STARS_PURCHASE_MAX,
    STARS_PURCHASE_MIN,
)
from FragmentAPI.types.models import PurchaseResult

logger = structlog.get_logger(__name__)

_T = TypeVar("_T")

# api_key у нас от tonapi.io (TonConsole), провайдер фиксируем явно.
_API_PROVIDER = "tonapi"

# Товары и валюты оплаты, которые шлюз готов обслуживать.
_PRODUCTS: frozenset[str] = frozenset({"premium", "stars"})
_PAYMENT_METHODS: frozenset[str] = frozenset({"ton", "usdt_ton"})

# Запас TON на газ, который Fragment требует держать сверх суммы покупки.
# Нужен и для usdt_ton-заказов: комиссия сети платится в TON.
GAS_RESERVE_TON: Decimal = Decimal(str(MIN_GRAM_BALANCE))

# Точность денег: TON — 9 знаков, USD/USDT — 6.
_TON_QUANT = Decimal("0.000000001")
_USD_QUANT = Decimal("0.000001")

# Telegram-юзернейм: 4..32 символа, начинается с буквы, заканчивается буквой/цифрой.
# Четырёхсимвольные логины существуют (продаются на Fragment), поэтому минимум 4.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,30}[A-Za-z0-9]$")

# Из строк цены Fragment выкидываем всё, кроме цифр и точки ("$12.34", "12,345.6" и т.п.).
_PRICE_JUNK_RE = re.compile(r"[^0-9.]")

# Ошибки, после которых имеет смысл переавторизоваться и повторить вызов ОДИН раз.
# Все они возникают строго ДО отправки транзакции в сеть (протухшая сессия ловится
# на загрузке страницы Fragment либо на шаге getBuyStarsLink/getGiftPremiumLink),
# поэтому повтор покупки целиком безопасен: деньги ещё не ушли.
_REAUTH_TRIGGERS: tuple[type[BaseException], ...] = (CookieError, VerificationError)


class FragmentGatewayError(Exception):
    """Базовая ошибка шлюза. Хендлеры могут ловить только её."""


class FragmentConfigError(FragmentGatewayError):
    """Некорректные входные данные или конфигурация. Деньги не тратились."""


class FragmentAuthError(FragmentGatewayError):
    """Не удалось авторизоваться на Fragment по seed-фразе. Деньги не тратились."""


class PriceUnavailableError(FragmentGatewayError):
    """Fragment не отдал внятную цену — заказ создавать нельзя."""


class RecipientNotFoundError(FragmentGatewayError):
    """Получатель не найден на Fragment. Деньги не тратились."""


class RecipientNotEligibleError(FragmentGatewayError):
    """
    Получатель есть, но заказ ему не выполнить: уже активен Premium либо
    у него включены платные сообщения со своим минимумом Stars.
    Деньги не тратились.
    """


class InsufficientBalanceError(FragmentGatewayError):
    """На операторском кошельке не хватает средств. Деньги не тратились."""


class FulfillmentError(FragmentGatewayError):
    """
    Общая ошибка выполнения заказа. Транзакция в сеть НЕ уходила,
    заказ можно безопасно перезапустить.
    """


class UncertainFulfillmentError(FulfillmentError):
    """
    Транзакция могла уйти в сеть, но подтверждения мы не получили.
    Автоматически повторять НЕЛЬЗЯ: сначала ручная проверка кошелька/блокчейна,
    иначе есть риск оплатить товар дважды.
    """


@dataclass(frozen=True)
class FragmentPrice:
    """Цена товара на Fragment сразу в двух валютах, до наценки."""

    product: str
    ton_price: Decimal
    usd_price: Decimal
    duration_months: int | None = None
    stars_amount: int | None = None


def _normalize_enum_value(value: Any) -> str:
    """ProductType.premium / Asset.ton приходят как str-enum — берём .value."""
    return str(getattr(value, "value", value)).strip().lower()


def _validate_product(product: Any) -> str:
    kind = _normalize_enum_value(product)
    if kind not in _PRODUCTS:
        raise FragmentConfigError(f"Неизвестный товар: {kind!r}")
    return kind


def _validate_payment_method(payment_method: Any) -> str:
    """Валюта оплаты на Fragment: только ton или usdt_ton (белый список)."""
    method = _normalize_enum_value(payment_method)
    if method not in _PAYMENT_METHODS:
        raise FragmentConfigError(f"Неподдерживаемый способ оплаты: {method!r}")
    return method


def _validate_username(username: Any) -> str:
    """Проверяем формат Telegram-юзернейма до любых трат."""
    if not isinstance(username, str):
        raise FragmentConfigError("Юзернейм получателя должен быть строкой")
    candidate = username.strip().lstrip("@")
    if not _USERNAME_RE.match(candidate):
        raise FragmentConfigError(f"Некорректный юзернейм получателя: {username!r}")
    return candidate


def _validate_months(months: Any) -> int:
    """Срок Premium — только то, что реально продаёт Fragment (3/6/12)."""
    if isinstance(months, bool) or not isinstance(months, int):
        raise FragmentConfigError(f"Некорректный срок Premium: {months!r}")
    if months not in PREMIUM_MONTHS_VALID:
        allowed = ", ".join(str(m) for m in sorted(PREMIUM_MONTHS_VALID))
        raise FragmentConfigError(f"Premium продаётся только на {allowed} мес., получено {months}")
    return months


def _validate_stars_amount(amount: Any) -> int:
    """Количество Stars — в границах, которые принимает Fragment."""
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise FragmentConfigError(f"Некорректное количество Stars: {amount!r}")
    if not (STARS_PURCHASE_MIN <= amount <= STARS_PURCHASE_MAX):
        raise FragmentConfigError(
            f"Количество Stars должно быть от {STARS_PURCHASE_MIN} до {STARS_PURCHASE_MAX}, "
            f"получено {amount}"
        )
    return amount


def _price_to_decimal(raw: Any, *, quant: Decimal, field: str) -> Decimal:
    """
    Библиотека отдаёт цены строками ("12.34", иногда с валютным символом).
    Переводим в Decimal (никаких float для денег) и округляем ВВЕРХ,
    чтобы никогда не выставить клиенту счёт ниже реальной цены Fragment.
    """
    text = str(raw or "")
    # Минус в цене — всегда мусор; молча сделать из "-5" пятёрку нельзя.
    cleaned = "" if "-" in text else _PRICE_JUNK_RE.sub("", text)
    if not cleaned or cleaned.count(".") > 1:
        raise PriceUnavailableError(f"Fragment вернул нечитаемую цену ({field}): {raw!r}")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise PriceUnavailableError(f"Fragment вернул нечитаемую цену ({field}): {raw!r}") from exc
    if value <= 0:
        raise PriceUnavailableError(f"Fragment вернул нулевую цену ({field}): {raw!r}")
    return value.quantize(quant, rounding=ROUND_UP)


def _validate_cookies(cookies: Any) -> dict[str, str]:
    """Сессия годится, только если пришли все 4 обязательных ключа кошелькового режима."""
    if not isinstance(cookies, dict):
        raise FragmentAuthError("Fragment вернул cookies неожиданного формата")
    missing = [key for key in REQUIRED_COOKIE_KEYS_WALLET if not str(cookies.get(key, "")).strip()]
    if missing:
        # Логируем и показываем только ИМЕНА ключей, значения — секрет.
        raise FragmentAuthError(
            "В сессии Fragment не хватает ключей: " + ", ".join(missing)
        )
    return {str(k): str(v) for k, v in cookies.items()}


def _map_error(exc: BaseException) -> FragmentGatewayError:
    """
    Переводим исключения библиотеки в небольшой набор ошибок шлюза.
    Ключевой принцип: если непонятно, ушли деньги или нет, считаем, что могли
    уйти (UncertainFulfillmentError) — ложный «точно не потратили» страшнее,
    потому что провоцирует автоповтор и двойную оплату.
    """
    if isinstance(exc, FragmentGatewayError):
        return exc

    if isinstance(exc, UserNotFoundError):
        return RecipientNotFoundError("Получатель не найден на Fragment")

    if isinstance(exc, (AlreadySubscribedError, PaidMessageLimitError)):
        return RecipientNotEligibleError(str(exc))

    if isinstance(exc, WalletError):
        # Единственный способ отличить «не хватило денег» от «не смогли прочитать
        # баланс» — текст: библиотека кидает один и тот же класс на оба случая.
        if "insufficient" in str(exc).lower():
            return InsufficientBalanceError(str(exc))
        return FulfillmentError(f"Проблема с кошельком: {exc}")

    if isinstance(exc, TransactionError):
        # INVALID_PAYLOAD — единственная ошибка этого семейства, которая гарантированно
        # возникает ДО броадкаста. Всё остальное (broadcast/seqno/таймаут подтверждения)
        # может означать уже отправленную транзакцию.
        if str(exc) == TransactionError.INVALID_PAYLOAD:
            return FulfillmentError("Fragment вернул пустую транзакцию")
        return UncertainFulfillmentError(
            f"Статус транзакции неизвестен, нужна ручная проверка кошелька: {exc}"
        )

    if isinstance(exc, (CookieError, VerificationError, FragmentPageError)):
        return FragmentAuthError(f"Сессия Fragment недействительна: {exc}")

    if isinstance(exc, ConfigurationError):
        return FragmentConfigError(str(exc))

    if isinstance(exc, FragmentAPIError):
        return FulfillmentError(f"Fragment отклонил запрос: {exc}")

    if isinstance(exc, FragmentError):
        return FulfillmentError(f"Ошибка Fragment: {exc}")

    return FulfillmentError(f"Непредвиденная ошибка при работе с Fragment: {exc}")


def _purchase_to_dict(
    result: Any,
    *,
    product: str,
    username: str,
    amount: int,
    payment_method: str,
) -> dict[str, Any]:
    """
    Приводим ответ библиотеки к плоскому словарю и заодно проверяем, что покупка
    действительно исполнена. Успех — только подтверждённый PurchaseResult
    с непустым transaction_id.
    """
    if not isinstance(result, PurchaseResult):
        # EvmPaymentResult / PreparedTransaction означают, что транзакция НЕ отправлена:
        # клиент свалился в чужой платёжный сценарий. Деньги на месте.
        raise FulfillmentError(
            f"Fragment не выполнил покупку, вернув {type(result).__name__} вместо PurchaseResult"
        )

    transaction_id = str(result.transaction_id or "").strip()
    if not transaction_id:
        raise UncertainFulfillmentError(
            "Fragment не вернул хеш транзакции — покупка могла пройти, нужна ручная проверка"
        )

    return {
        "transaction_id": transaction_id,
        "type": product,
        "username": username,
        "amount": amount,
        "payment_method": payment_method,
    }


class FragmentGateway:
    """
    Единственная точка входа к Fragment. Держит cookies-сессию и клиента,
    сам их обновляет и переводит ошибки библиотеки в ошибки шлюза.
    """

    def __init__(
        self,
        seed: str,
        api_key: str,
        wallet_version: str = "V5R1",
        cookie_ttl_seconds: int = 1800,
        *,
        timeout: float = 30.0,
    ) -> None:
        if not seed or not str(seed).strip():
            raise FragmentConfigError("Не задана seed-фраза кошелька")
        if not api_key or not str(api_key).strip():
            raise FragmentConfigError("Не задан api_key для TON API")
        if cookie_ttl_seconds <= 0:
            raise FragmentConfigError("cookie_ttl_seconds должен быть положительным")

        self._seed = str(seed).strip()
        self._api_key = str(api_key).strip()
        self._wallet_version = str(wallet_version).strip().upper()
        self._cookie_ttl_seconds = int(cookie_ttl_seconds)
        self._timeout = float(timeout)

        self._client: FragmentClient | None = None
        # Монотонные часы: системное время могут перевести, TTL от этого не должен ехать.
        self._obtained_at: float | None = None
        # Поколение сессии: по нему параллельные корутины понимают, что кто-то
        # уже переавторизовался, и не устраивают шторм запросов к Fragment.
        self._generation: int = 0
        self._auth_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Получить cookies по seed-фразе и собрать клиента. Вызывается на старте бота."""
        await self._acquire_client()
        logger.info(
            "fragment_gateway.connected",
            wallet_version=self._wallet_version,
            cookie_ttl_seconds=self._cookie_ttl_seconds,
        )

    async def close(self) -> None:
        """Отпускаем клиента. Своих открытых соединений библиотека не держит."""
        async with self._auth_lock:
            self._client = None
            self._obtained_at = None
        logger.info("fragment_gateway.closed")

    async def get_price(
        self,
        product: Any,
        *,
        duration_months: int | None = None,
        stars_amount: int | None = None,
    ) -> FragmentPrice:
        """
        Актуальная цена товара на Fragment ДО наценки, сразу в TON и в USD.
        TON-заказы считаются от ton_price, USDT-заказы — от usd_price
        (USDT — долларовый стейблкоин).
        """
        kind = _validate_product(product)

        if kind == "premium":
            months = _validate_months(duration_months)
            prices = await self._call(
                "get_premium_prices",
                lambda client: client.get_premium_prices(),
                retry_on_page_error=True,
            )
            option = next((opt for opt in prices.options if opt.months == months), None)
            if option is None:
                raise PriceUnavailableError(f"Fragment не отдал цену Premium на {months} мес.")
            price = FragmentPrice(
                product=kind,
                duration_months=months,
                ton_price=_price_to_decimal(option.gram_price, quant=_TON_QUANT, field="ton"),
                usd_price=_price_to_decimal(option.usd_price, quant=_USD_QUANT, field="usd"),
            )
        else:
            amount = _validate_stars_amount(stars_amount)
            stars_price = await self._call(
                "get_stars_price",
                lambda client: client.get_stars_price(amount),
                retry_on_page_error=True,
            )
            price = FragmentPrice(
                product=kind,
                stars_amount=amount,
                ton_price=_price_to_decimal(stars_price.gram_price, quant=_TON_QUANT, field="ton"),
                usd_price=_price_to_decimal(stars_price.usd_price, quant=_USD_QUANT, field="usd"),
            )

        logger.info(
            "fragment_gateway.price",
            product=price.product,
            duration_months=price.duration_months,
            stars_amount=price.stars_amount,
            ton_price=str(price.ton_price),
            usd_price=str(price.usd_price),
        )
        return price

    async def gift_premium(
        self,
        username: str,
        months: int,
        payment_method: str,
        *,
        show_sender: bool = True,
    ) -> dict[str, Any]:
        """
        Подарить Telegram Premium. payment_method: 'ton' | 'usdt_ton'.
        Транзакция подписывается и уходит в сеть локально из seed-фразы.
        """
        recipient = _validate_username(username)
        period = _validate_months(months)
        method = _validate_payment_method(payment_method)

        logger.info(
            "fragment_gateway.gift_premium.start",
            username=recipient,
            months=period,
            payment_method=method,
        )
        result = await self._call(
            "gift_premium",
            lambda client: client.purchase_premium(
                username=recipient,
                months=period,
                show_sender=show_sender,
                payment_method=method,
            ),
        )
        payload = _purchase_to_dict(
            result,
            product="premium",
            username=recipient,
            amount=period,
            payment_method=method,
        )
        logger.info("fragment_gateway.gift_premium.ok", **payload)
        return payload

    async def buy_stars(
        self,
        username: str,
        amount: int,
        payment_method: str,
        *,
        show_sender: bool = True,
    ) -> dict[str, Any]:
        """
        Купить Telegram Stars получателю. payment_method: 'ton' | 'usdt_ton'.
        """
        recipient = _validate_username(username)
        quantity = _validate_stars_amount(amount)
        method = _validate_payment_method(payment_method)

        logger.info(
            "fragment_gateway.buy_stars.start",
            username=recipient,
            amount=quantity,
            payment_method=method,
        )
        result = await self._call(
            "buy_stars",
            lambda client: client.purchase_stars(
                username=recipient,
                amount=quantity,
                show_sender=show_sender,
                payment_method=method,
            ),
        )
        payload = _purchase_to_dict(
            result,
            product="stars",
            username=recipient,
            amount=quantity,
            payment_method=method,
        )
        logger.info("fragment_gateway.buy_stars.ok", **payload)
        return payload

    async def get_wallet_balances(self) -> tuple[Decimal, Decimal]:
        """
        Балансы операторского кошелька: (TON, USDT).
        Нужны для предварительной проверки перед покупкой и для алертов админу.
        Помни: даже покупка за USDT съедает TON на газ (GAS_RESERVE_TON).
        """
        wallet = await self._call(
            "get_wallet",
            lambda client: client.get_wallet(),
            retry_on_page_error=True,
        )
        # Библиотека отдаёт float — переводим через str, чтобы не тащить
        # двоичную погрешность в денежные расчёты.
        ton_balance = Decimal(str(wallet.gram_balance))
        usdt_balance = Decimal(str(wallet.usdt_balance))
        logger.info(
            "fragment_gateway.wallet",
            ton_balance=str(ton_balance),
            usdt_balance=str(usdt_balance),
            state=wallet.state,
        )
        return ton_balance, usdt_balance

    async def _call(
        self,
        operation: str,
        func: Callable[[FragmentClient], Awaitable[_T]],
        *,
        retry_on_page_error: bool = False,
    ) -> _T:
        """
        Выполнить вызов Fragment с ленивым обновлением сессии.

        При CookieError/VerificationError переавторизуемся один раз и повторяем
        вызов ровно один раз. Это безопасно даже для покупок: обе ошибки
        библиотека кидает до отправки транзакции в сеть.

        retry_on_page_error разрешает тот же повтор ещё и на FragmentPageError
        (Fragment отдал 302/не-200 на протухшую сессию). Включаем его только для
        читающих вызовов (цены, баланс), где деньги не двигаются вообще.
        """
        triggers = _REAUTH_TRIGGERS + ((FragmentPageError,) if retry_on_page_error else ())

        client, generation = await self._acquire_client()
        try:
            return await func(client)
        except triggers as exc:
            logger.warning(
                "fragment_gateway.session_expired",
                operation=operation,
                error=type(exc).__name__,
            )
        except Exception as exc:
            raise self._fail(operation, exc) from exc

        client, _ = await self._reauthenticate(seen_generation=generation)
        try:
            return await func(client)
        except Exception as exc:
            raise self._fail(operation, exc) from exc

    def _fail(self, operation: str, exc: BaseException) -> FragmentGatewayError:
        mapped = _map_error(exc)
        logger.warning(
            "fragment_gateway.call_failed",
            operation=operation,
            error=type(exc).__name__,
            mapped=type(mapped).__name__,
            # В сообщениях библиотеки нет ни seed, ни токенов, ни api_key.
            reason=str(exc)[:500],
        )
        return mapped

    def _is_stale(self) -> bool:
        if self._obtained_at is None:
            return True
        return (time.monotonic() - self._obtained_at) >= self._cookie_ttl_seconds

    async def _acquire_client(self) -> tuple[FragmentClient, int]:
        """Готовый клиент со свежей сессией плюс её поколение."""
        client, generation = self._client, self._generation
        if client is not None and not self._is_stale():
            return client, generation
        return await self._reauthenticate(seen_generation=generation)

    async def _reauthenticate(self, *, seen_generation: int) -> tuple[FragmentClient, int]:
        """
        Переавторизация под локом, чтобы пачка одновременных заказов
        не устроила шторм запросов к Fragment.
        """
        async with self._auth_lock:
            # Пока ждали лок, соседняя корутина могла уже обновить сессию.
            if (
                self._client is not None
                and self._generation != seen_generation
                and not self._is_stale()
            ):
                return self._client, self._generation

            # Импорт внутри метода: держим зависимость от приватного пути библиотеки
            # в одном месте и не ломаем импорт модуля, если пакет не установлен.
            from FragmentAPI.utils.auth import auth_ton_proof

            try:
                raw_cookies = await auth_ton_proof(
                    seed=self._seed,
                    wallet_version=self._wallet_version,
                    timeout=self._timeout,
                )
            except Exception as exc:
                logger.error(
                    "fragment_gateway.auth_failed",
                    error=type(exc).__name__,
                    reason=str(exc)[:500],
                )
                raise FragmentAuthError(
                    f"Не удалось авторизоваться на Fragment по seed-фразе: {exc}"
                ) from exc

            cookies = _validate_cookies(raw_cookies)

            try:
                client = FragmentClient(
                    cookies=cookies,
                    seed=self._seed,
                    api_key=self._api_key,
                    api_provider=_API_PROVIDER,
                    wallet_version=self._wallet_version,
                    timeout=self._timeout,
                )
            except FragmentError as exc:
                raise FragmentAuthError(f"Не удалось собрать клиент Fragment: {exc}") from exc

            self._client = client
            self._obtained_at = time.monotonic()
            self._generation += 1
            logger.info(
                "fragment_gateway.session_refreshed",
                generation=self._generation,
                # Только ИМЕНА ключей: значения cookies — секрет и в логи не попадают.
                cookie_keys=sorted(cookies),
            )
            return client, self._generation


__all__ = [
    "FragmentGateway",
    "FragmentPrice",
    "FragmentGatewayError",
    "FragmentConfigError",
    "FragmentAuthError",
    "PriceUnavailableError",
    "RecipientNotFoundError",
    "RecipientNotEligibleError",
    "InsufficientBalanceError",
    "FulfillmentError",
    "UncertainFulfillmentError",
    "GAS_RESERVE_TON",
]
