"""
JSON API для Telegram Mini App.

Это тонкий слой, а не второй бэкенд: цены, заказы и выдача — всё та же
services.fulfillment / fragment_gateway / payment_tracker / pricing, что и у
бота. Здесь только HTTP, проверка подписи и приведение к JSON.

Что важно про деньги:

* Сумму и цену считает ТОЛЬКО сервер. Клиент присылает «что и кому», но не
  «сколько платить» — иначе цену можно было бы продиктовать самому себе.
* telegram_user_id берётся исключительно из подписанного initData, никогда из
  тела запроса.
* Транзакцию для TON Connect тоже собирает сервер (services.ton_messages) —
  фронтенд только передаёт её кошельку на подпись. Так в браузере не нужен
  ни один TON-примитив, и джеттон-перевод собирается тем же кодом, что у бота.
* Статус заказа здесь только читается. Меняет его по-прежнему исключительно
  services.fulfillment после авторизованной проверки у TonConsole.
"""

from __future__ import annotations

import re
import time
from decimal import Decimal
from typing import Any

import structlog
from aiohttp import web
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.config import settings
from bot.miniapp_auth import InitDataError, MiniAppUser, parse_init_data
from db.models import Asset, Order, OrderStatus, ProductType
from db.repository import (
    attach_invoice,
    create_order,
    expire_order,
    get_active_order_for_user,
    get_order,
)
from services.fragment_gateway import FragmentGateway, FragmentGatewayError
from services.payment_tracker import PaymentTrackerClient, PaymentTrackerError
from services.pricing import (
    calculate_invoice_amount,
    coerce_asset,
    format_amount,
    select_base_price,
    to_minimal_units,
)
from services.ton_messages import build_jetton_message, build_ton_message

logger = structlog.get_logger(__name__)

__all__ = ["add_miniapp_routes"]

#: Ровно то же правило, что в services.fragment_gateway. Разойдутся — примем
#: заказ, который Fragment потом отвергнет уже после оплаты.
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,30}[A-Za-z0-9]$")

#: Сроки Premium, которые реально продаёт Fragment.
ALLOWED_MONTHS = frozenset({3, 6, 12})

#: Сколько живёт подписанная транзакция TON Connect, прежде чем кошелёк
#: сочтёт её протухшей.
TX_VALID_FOR_SECONDS = 300


class ApiError(Exception):
    """Ожидаемая ошибка запроса: наружу уходит как JSON с нужным кодом."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _require_user(request: web.Request) -> MiniAppUser:
    """
    Достать пользователя из подписанного initData.

    Заголовок в формате `Authorization: tma <initData>` — это соглашение
    экосистемы telegram-apps, фронтенд шлёт его на каждый запрос.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, raw = header.partition(" ")
    if scheme.lower() != "tma" or not raw:
        raise ApiError(401, "Нет авторизации Telegram")
    try:
        return parse_init_data(raw, bot_token=settings.bot_token.get_secret_value())
    except InitDataError as exc:
        # Наружу — без подробностей: почему именно не сошлось, знать незачем.
        logger.warning("miniapp.auth_failed", reason=str(exc))
        raise ApiError(401, "Авторизация Telegram не подтвердилась") from exc


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001 - мусор в теле это обычное дело
        raise ApiError(400, "Некорректное тело запроса") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "Некорректное тело запроса")
    return body


def _parse_product(body: dict[str, Any]) -> tuple[ProductType, int | None, int | None]:
    """Разобрать и проверить товар. Всё, что пришло от клиента, — недоверенное."""
    raw_product = str(body.get("product", "")).strip().lower()
    try:
        product = ProductType(raw_product)
    except ValueError:
        raise ApiError(400, "Неизвестный товар") from None

    if product is ProductType.premium:
        months = body.get("months")
        if not isinstance(months, int) or isinstance(months, bool) or months not in ALLOWED_MONTHS:
            raise ApiError(400, "Premium продаётся на 3, 6 или 12 месяцев")
        return product, months, None

    stars = body.get("stars")
    if not isinstance(stars, int) or isinstance(stars, bool):
        raise ApiError(400, "Количество Stars должно быть целым числом")
    if stars < settings.stars_min_amount:
        raise ApiError(400, f"Минимум {settings.stars_min_amount} Stars")
    if stars > settings.stars_max_amount:
        raise ApiError(400, f"Максимум {settings.stars_max_amount} Stars за заказ")
    return product, None, stars


def _parse_asset(body: dict[str, Any]) -> Asset:
    try:
        return coerce_asset(body.get("asset", ""))
    except ValueError:
        raise ApiError(400, "Неизвестный способ оплаты") from None


def _parse_recipient(body: dict[str, Any]) -> str:
    recipient = str(body.get("recipient", "")).strip().lstrip("@")
    if not USERNAME_RE.match(recipient):
        raise ApiError(400, "Некорректный username получателя")
    return recipient


def _same_order(
    order: Order,
    product: ProductType,
    months: int | None,
    stars: int | None,
    asset: Asset,
    recipient: str,
) -> bool:
    """Совпадает ли уже выставленный заказ с тем, что клиент просит сейчас."""
    return (
        order.product is product
        and order.duration_months == months
        and order.stars_amount == stars
        and order.asset is asset
        and order.recipient_username.lower() == recipient.lower()
    )


def _order_public(order: Order) -> dict[str, Any]:
    """Представление заказа для фронтенда. Ничего лишнего наружу."""
    if order.product is ProductType.premium:
        title = f"Premium на {order.duration_months} мес."
    else:
        title = f"{order.stars_amount} Stars"

    return {
        "id": str(order.id),
        "status": order.status.value,
        "title": title,
        "product": order.product.value,
        "months": order.duration_months,
        "stars": order.stars_amount,
        "recipient": order.recipient_username,
        "asset": order.asset.value,
        "asset_label": order.asset.display_name,
        "amount": str(order.invoice_amount),
        "amount_units": order.invoice_amount_units,
        "amount_formatted": format_amount(order.invoice_amount, order.asset),
        "pay_to_address": order.pay_to_address,
        "expires_at": order.expires_at.isoformat() if order.expires_at else None,
        "fragment_tx_id": order.fragment_tx_id,
    }


def add_miniapp_routes(
    app: web.Application,
    *,
    session_maker: async_sessionmaker,
    gateway: FragmentGateway,
    tracker: PaymentTrackerClient,
) -> None:
    """Повесить /api/* на уже существующее aiohttp-приложение."""

    @web.middleware
    async def error_middleware(request: web.Request, handler):
        if not request.path.startswith("/api/"):
            return await handler(request)
        try:
            return await handler(request)
        except ApiError as exc:
            return _json_error(exc.status, exc.message)
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001 - наружу никогда не уходит трейсбек
            logger.exception("miniapp.unhandled", path=request.path)
            return _json_error(500, "Внутренняя ошибка, попробуй ещё раз")

    app.middlewares.append(error_middleware)

    async def handle_price(request: web.Request) -> web.Response:
        """Актуальная цена Fragment с наценкой — в обоих активах сразу."""
        _require_user(request)
        body = await _json_body(request)
        product, months, stars = _parse_product(body)

        try:
            price = await gateway.get_price(
                product.value, duration_months=months, stars_amount=stars
            )
        except FragmentGatewayError as exc:
            logger.warning("miniapp.price_failed", error=type(exc).__name__)
            raise ApiError(503, "Fragment сейчас не отдаёт цену, попробуй чуть позже") from exc

        result: dict[str, Any] = {}
        for asset in (Asset.ton, Asset.usdt_ton):
            base = select_base_price(asset, price.ton_price, price.usd_price)
            # Хвост случайный, поэтому показанная тут сумма — ориентир;
            # платить клиент будет ровно то, что вернёт создание заказа.
            total = calculate_invoice_amount(base, Decimal(settings.margin_percent), asset)
            result[asset.value] = {
                "base": str(base),
                "approx": str(total),
                "approx_formatted": format_amount(total, asset),
                "label": asset.display_name,
            }
        return web.json_response({"prices": result})

    async def handle_create_order(request: web.Request) -> web.Response:
        """Создать заказ и счёт. Сумму считает сервер, клиент её не диктует."""
        user = _require_user(request)
        body = await _json_body(request)
        product, months, stars = _parse_product(body)
        asset = _parse_asset(body)
        recipient = _parse_recipient(body)

        async with session_maker() as session:
            existing = await get_active_order_for_user(session, user.id)
            if existing is not None and existing.status is OrderStatus.pending:
                if _same_order(existing, product, months, stars, asset, recipient):
                    # Тот же заказ (например, после отказа в кошельке) — не плодим
                    # второй счёт с другой суммой, отдаём уже выставленный.
                    return web.json_response(
                        {"order": _order_public(existing), "reused": True}
                    )

                # Клиент передумал (другой товар, актив или получатель). Старый
                # счёт гасим — но только убедившись у трекера, что денег по нему
                # нет: просрочить оплаченный заказ значит забрать деньги и не
                # выдать товар. То же правило, что в jobs/expire_orders.py.
                if existing.payment_tracker_invoice_id:
                    try:
                        old_invoice = await tracker.get_invoice(
                            existing.payment_tracker_invoice_id,
                            currency=existing.asset.tracker_currency,
                        )
                    except PaymentTrackerError as exc:
                        raise ApiError(
                            503, "Не удалось проверить прошлый заказ, попробуй чуть позже"
                        ) from exc
                    if old_invoice.has_funds:
                        # Деньги уже пришли — этот заказ выполнится; показываем его.
                        return web.json_response(
                            {
                                "error": "По прошлому заказу уже пришла оплата — он выполняется",
                                "order": _order_public(existing),
                            },
                            status=409,
                        )
                await expire_order(session, existing.id)
                logger.info(
                    "miniapp.order_replaced",
                    old_order_id=str(existing.id),
                    telegram_user_id=user.id,
                )

            try:
                price = await gateway.get_price(
                    product.value, duration_months=months, stars_amount=stars
                )
            except FragmentGatewayError as exc:
                logger.warning("miniapp.price_failed", error=type(exc).__name__)
                raise ApiError(503, "Fragment сейчас не отдаёт цену, попробуй чуть позже") from exc

            base_price = select_base_price(asset, price.ton_price, price.usd_price)
            invoice_amount = calculate_invoice_amount(
                base_price, Decimal(settings.margin_percent), asset
            )

            order = await create_order(
                session,
                telegram_user_id=user.id,
                product=product,
                recipient_username=recipient,
                asset=asset,
                base_price=base_price,
                invoice_amount=invoice_amount,
                invoice_amount_units=to_minimal_units(invoice_amount, asset),
                duration_months=months,
                stars_amount=stars,
                ttl_minutes=settings.order_ttl_minutes,
            )

            try:
                invoice = await tracker.create_invoice(
                    order_id=str(order.id),
                    asset=asset.value,
                    amount=invoice_amount,
                    life_time_seconds=settings.invoice_life_time_seconds,
                )
            except PaymentTrackerError as exc:
                logger.error("miniapp.invoice_failed", order_id=str(order.id))
                # Заказ без счёта бесполезен и мешает создать новый.
                await expire_order(session, order.id)
                raise ApiError(503, "Не удалось выставить счёт, попробуй чуть позже") from exc

            order = await attach_invoice(
                session,
                order.id,
                invoice_id=invoice.id,
                pay_to_address=invoice.pay_to_address,
            ) or order

            logger.info(
                "miniapp.order_created",
                order_id=str(order.id),
                telegram_user_id=user.id,
                asset=asset.value,
            )
            return web.json_response({"order": _order_public(order), "reused": False})

    async def handle_order_tx(request: web.Request) -> web.Response:
        """
        Собрать транзакцию под подключённый кошелёк клиента.

        Отдельным шагом от создания заказа, потому что адрес кошелька известен
        только после того, как клиент подключил его в TON Connect — а для USDT
        без адреса отправителя джеттон-перевод не собрать.
        """
        user = _require_user(request)
        body = await _json_body(request)
        wallet_address = str(body.get("wallet_address", "")).strip()
        if not wallet_address:
            raise ApiError(400, "Не передан адрес кошелька")

        async with session_maker() as session:
            order = await get_order(session, request.match_info["order_id"])
            if order is None or order.telegram_user_id != user.id:
                raise ApiError(404, "Заказ не найден")
            if order.status is not OrderStatus.pending:
                raise ApiError(409, "Этот заказ уже не ждёт оплату")
            if not order.pay_to_address or not order.invoice_amount_units:
                raise ApiError(409, "У заказа нет счёта на оплату")

            comment = str(order.id)
            if order.asset is Asset.ton:
                message = build_ton_message(
                    order.pay_to_address, order.invoice_amount_units, comment
                )
            else:
                try:
                    message = await build_jetton_message(
                        customer_address=wallet_address,
                        pay_to_address=order.pay_to_address,
                        amount_units=order.invoice_amount_units,
                        comment=comment,
                        api_key=settings.tonconsole_api_key.get_secret_value(),
                        api_provider=settings.fragment_api_provider,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "miniapp.jetton_build_failed",
                        order_id=str(order.id),
                        error=type(exc).__name__,
                    )
                    raise ApiError(
                        503, "Не удалось подготовить перевод USDT, попробуй ещё раз"
                    ) from exc

            return web.json_response(
                {
                    "valid_until": int(time.time()) + TX_VALID_FOR_SECONDS,
                    "messages": [message],
                }
            )

    async def handle_get_order(request: web.Request) -> web.Response:
        """Статус заказа для опроса из приложения."""
        user = _require_user(request)
        async with session_maker() as session:
            order = await get_order(session, request.match_info["order_id"])
            if order is None or order.telegram_user_id != user.id:
                raise ApiError(404, "Заказ не найден")
            return web.json_response({"order": _order_public(order)})

    async def handle_active_order(request: web.Request) -> web.Response:
        """Незавершённый заказ пользователя — чтобы приложение восстановило экран."""
        user = _require_user(request)
        async with session_maker() as session:
            order = await get_active_order_for_user(session, user.id)
            return web.json_response(
                {"order": _order_public(order) if order is not None else None}
            )

    app.router.add_post("/api/price", handle_price)
    app.router.add_post("/api/orders", handle_create_order)
    app.router.add_get("/api/orders/active", handle_active_order)
    app.router.add_get("/api/orders/{order_id}", handle_get_order)
    app.router.add_post("/api/orders/{order_id}/tx", handle_order_tx)
