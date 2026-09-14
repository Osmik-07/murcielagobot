"""
Протухание неоплаченных заказов.

Главное правило: заказ нельзя объявить просроченным, не убедившись у трекера,
что денег по нему нет. Просрочить оплаченный заказ — значит оставить у себя
деньги клиента и не выдать товар. Поэтому если на инвойсе обнаруживаются
средства, заказ уходит не в expired, а в обычную процедуру выдачи.
"""

from __future__ import annotations

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.repository import expire_order, list_expired_pending_orders
from services.fragment_gateway import FragmentGateway
from services.fulfillment import process_order_payment
from services.payment_tracker import PaymentTrackerClient, PaymentTrackerError

logger = structlog.get_logger(__name__)


async def expire_stale_orders(
    session_maker: async_sessionmaker,
    *,
    gateway: FragmentGateway,
    tracker: PaymentTrackerClient,
    bot: Bot,
) -> None:
    async with session_maker() as session:
        stale = await list_expired_pending_orders(session)
        if not stale:
            return

        logger.info("expire_orders.batch", count=len(stale))
        for order in stale:
            if not order.payment_tracker_invoice_id:
                # Счёт так и не выставился — платить было некуда, гасим спокойно.
                await expire_order(session, order.id)
                logger.info("order.expired", order_id=str(order.id), reason="no_invoice")
                continue

            try:
                invoice = await tracker.get_invoice(
                    order.payment_tracker_invoice_id,
                    currency=order.asset.tracker_currency,
                )
            except PaymentTrackerError as exc:
                # Не смогли проверить — НЕ гасим. Лучше подождать следующего круга,
                # чем закрыть заказ, по которому на самом деле пришли деньги.
                logger.warning(
                    "expire_orders.check_failed",
                    order_id=str(order.id),
                    error=type(exc).__name__,
                )
                continue

            if invoice.has_funds:
                logger.warning(
                    "expire_orders.paid_after_expiry",
                    order_id=str(order.id),
                    invoice_status=invoice.status,
                )
                # Деньги есть — это не просрочка, а заказ на выдачу.
                await process_order_payment(
                    session,
                    order.id,
                    gateway=gateway,
                    tracker=tracker,
                    bot=bot,
                    source="expire_check",
                )
                continue

            if await expire_order(session, order.id) is not None:
                logger.info("order.expired", order_id=str(order.id), reason="unpaid")
