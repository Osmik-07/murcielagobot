"""
Денежная часть диалога: создание счёта и проверка оплаты.

Сама выдача товара живёт не здесь, а в services.fulfillment — туда же ходят
вебхук и джоба сверки. Хендлер лишь дёргает общую функцию и показывает клиенту
её результат, поэтому кнопка «я оплатил» физически не может выдать товар
повторно, сколько по ней ни кликай.
"""

from __future__ import annotations

import html
from decimal import Decimal

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.handlers.order import OrderFSM
from bot.keyboards import main_menu_kb, pay_link_kb
from db.models import Asset, Order, OrderStatus, ProductType
from db.repository import (
    attach_invoice,
    create_order,
    expire_order,
    get_active_order_for_user,
    get_order,
)
from services.fragment_gateway import FragmentGateway, FragmentGatewayError
from services.fulfillment import Outcome, process_order_payment
from services.payment_tracker import PaymentTrackerClient, PaymentTrackerError
from services.pricing import (
    calculate_invoice_amount,
    coerce_asset,
    format_amount,
    select_base_price,
    to_minimal_units,
)

logger = structlog.get_logger(__name__)

router = Router(name="payment")


def build_ton_deep_link(address: str, amount_nanoton: int, comment: str) -> str:
    """
    Запасная ссылка на оплату в нативном TON.

    Используется, только если трекер не отдал свой payment_link. Для USDT такая
    ссылка не годится: там перевод джеттона, а не нативной монеты, поэтому для
    USDT-заказов запасного варианта нет — просто показываем адрес и сумму.
    """
    return f"ton://transfer/{address}?amount={amount_nanoton}&text={comment}"


def _order_summary(order: Order) -> str:
    recipient = html.escape(order.recipient_username or "")
    if order.product is ProductType.premium:
        return f"Premium на {order.duration_months} мес. для @{recipient}"
    return f"{order.stars_amount} Stars для @{recipient}"


def _invoice_text(order: Order, *, pay_to_address: str | None) -> str:
    amount = format_amount(order.invoice_amount, order.asset)
    lines = [
        f"Заказ создан: {_order_summary(order)}",
        "",
        f"К оплате: <b>{html.escape(amount)}</b>",
        f"Ссылка действительна {settings.order_ttl_minutes} минут.",
        "",
        "⚠️ Переведи <b>ровно эту сумму</b> — по ней мы и находим твой платёж.",
    ]
    if pay_to_address:
        lines += ["", f"Адрес: <code>{html.escape(pay_to_address)}</code>"]
    return "\n".join(lines)


@router.callback_query(OrderFSM.choosing_asset, F.data.startswith("asset:"))
async def create_order_and_invoice(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    fragment_gateway: FragmentGateway,
    payment_tracker: PaymentTrackerClient,
) -> None:
    """Выбран способ оплаты: считаем живую цену, создаём заказ и счёт."""
    # Сообщение недоступно (слишком старое) — редактировать нечего.
    if callback.message is None:
        await callback.answer("Сообщение устарело, начни заново: /start", show_alert=True)
        return

    # callback_data приходит от клиента и может быть каким угодно.
    raw_asset = (callback.data or "").split(":", 1)[-1]
    try:
        asset = coerce_asset(raw_asset)
    except ValueError:
        await callback.answer("Неизвестный способ оплаты", show_alert=True)
        return

    data = await state.get_data()
    product_raw = data.get("product")
    recipient = data.get("recipient")
    if not product_raw or not recipient:
        await state.clear()
        await callback.answer("Заказ потерялся, начни заново: /start", show_alert=True)
        return

    product = ProductType(product_raw)
    duration_months = data.get("duration_months")
    stars_amount = data.get("stars_amount")

    # Не плодим параллельные заказы одного пользователя: у каждого свой инвойс
    # и своя уникальная сумма, и в них легко запутаться самому клиенту.
    existing = await get_active_order_for_user(session, callback.from_user.id)
    if existing is not None and existing.status is OrderStatus.pending:
        await callback.answer()
        await callback.message.answer(
            "У тебя уже есть неоплаченный заказ:\n\n"
            + _invoice_text(existing, pay_to_address=existing.pay_to_address)
            + "\n\nОплати его или дождись истечения срока.",
            reply_markup=pay_link_kb(None),
        )
        return

    await callback.answer()
    await callback.message.edit_text("Считаю цену на Fragment…")

    # --- живая цена, никаких захардкоженных чисел ---------------------------
    try:
        price = await fragment_gateway.get_price(
            product.value,
            duration_months=duration_months,
            stars_amount=stars_amount,
        )
    except FragmentGatewayError as exc:
        logger.warning("order.price_failed", error=type(exc).__name__, reason=str(exc)[:200])
        await callback.message.edit_text(
            "Не удалось узнать актуальную цену на Fragment. Попробуй ещё раз чуть позже.",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        return

    base_price = select_base_price(asset, price.ton_price, price.usd_price)
    invoice_amount = calculate_invoice_amount(base_price, Decimal(settings.margin_percent), asset)

    order = await create_order(
        session,
        telegram_user_id=callback.from_user.id,
        product=product,
        recipient_username=recipient,
        asset=asset,
        base_price=base_price,
        invoice_amount=invoice_amount,
        invoice_amount_units=to_minimal_units(invoice_amount, asset),
        duration_months=duration_months,
        stars_amount=stars_amount,
        ttl_minutes=settings.order_ttl_minutes,
    )

    # --- счёт в трекере ------------------------------------------------------
    try:
        invoice = await payment_tracker.create_invoice(
            order_id=str(order.id),
            asset=asset.value,
            amount=invoice_amount,
            life_time_seconds=settings.invoice_life_time_seconds,
        )
    except PaymentTrackerError as exc:
        logger.error("order.invoice_failed", order_id=str(order.id), reason=str(exc)[:200])
        # Заказ без счёта бесполезен и мешает создать новый — сразу гасим.
        await expire_order(session, order.id)
        await callback.message.edit_text(
            "Не удалось выставить счёт на оплату. Попробуй ещё раз чуть позже.",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        return

    await attach_invoice(
        session,
        order.id,
        invoice_id=invoice.id,
        pay_to_address=invoice.pay_to_address,
    )

    pay_link = invoice.payment_link
    if not pay_link and asset is Asset.ton and invoice.pay_to_address:
        # Запасной вариант только для нативного TON.
        pay_link = build_ton_deep_link(
            address=invoice.pay_to_address,
            amount_nanoton=to_minimal_units(invoice_amount, asset),
            comment=str(order.id),
        )

    await state.update_data(order_id=str(order.id))
    await state.set_state(OrderFSM.awaiting_payment)

    await callback.message.edit_text(
        _invoice_text(order, pay_to_address=invoice.pay_to_address),
        reply_markup=pay_link_kb(pay_link),
    )


@router.callback_query(F.data == "check_payment")
async def check_payment(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    fragment_gateway: FragmentGateway,
    payment_tracker: PaymentTrackerClient,
) -> None:
    """
    Кнопка «я оплатил».

    Ничего не выдаёт сама: вся логика в общей функции, которая одинаково
    защищена от повторов независимо от того, сколько раз нажали кнопку.
    """
    data = await state.get_data()
    order_id = data.get("order_id")

    order = None
    if order_id:
        order = await get_order(session, order_id)
    if order is None or order.telegram_user_id != callback.from_user.id:
        # FSM живёт в памяти процесса и теряется при рестарте — ищем заказ в БД.
        order = await get_active_order_for_user(session, callback.from_user.id)

    if order is None:
        await callback.answer("Активный заказ не найден. Начни заново: /start", show_alert=True)
        return

    result = await process_order_payment(
        session,
        order.id,
        gateway=fragment_gateway,
        tracker=payment_tracker,
        bot=callback.bot,
        source="button",
    )

    # Короткий ответ во всплывашке; подробности клиенту уже отправила общая функция.
    short = {
        Outcome.not_paid: "Платёж пока не найден",
        Outcome.in_progress: "Заказ уже выполняется ⏳",
        Outcome.retry_later: "Проверяем платёж, попробуй чуть позже",
        Outcome.fulfilled: "Готово ✅",
        Outcome.already_done: "Этот заказ уже закрыт",
        Outcome.failed: "Заказ не выполнен, разбираемся",
        Outcome.needs_manual: "Разбираемся вручную",
        Outcome.not_found: "Заказ не найден",
    }.get(result.outcome, "Проверяем…")
    await callback.answer(short, show_alert=result.outcome is not Outcome.not_paid)

    if result.is_terminal_for_user:
        # Убираем кнопку, чтобы по закрытому заказу нельзя было кликать дальше.
        if callback.message is not None:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001 - сообщение могли удалить или оно не менялось
                pass
        await state.clear()
