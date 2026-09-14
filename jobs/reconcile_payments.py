"""
Сверка платежей — страховка на случай, если вебхук не дошёл.

Делает две вещи:

1. Прогоняет через общую функцию выдачи все заказы, которые ещё в работе:
   * pending — вдруг оплата пришла, а вебхук потерялся по дороге;
   * paid — деньги получены, но покупку не довели до конца (упал процесс,
     отвалился Fragment, кончился баланс). Такие нужно дожать.

2. Ищет заказы, зависшие в fulfilling. Их НЕ перезапускает: неизвестно, ушла ли
   транзакция в сеть, а повтор означал бы риск купить товар второй раз за те же
   деньги. Про такие просто зовёт админа.

Сама идемпотентность здесь не реализуется: она целиком живёт в claim-ах внутри
process_order_payment. Джоба может спокойно работать одновременно с вебхуком.
"""

from __future__ import annotations

from datetime import timedelta

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.repository import (
    list_orders_to_reconcile,
    list_stuck_fulfilling_orders,
    mark_admin_notified,
)
from services.fragment_gateway import FragmentGateway
from services.fulfillment import Outcome, notify_admin, process_order_payment
from services.payment_tracker import PaymentTrackerClient

logger = structlog.get_logger(__name__)

#: Сколько заказ может честно провисеть в fulfilling, прежде чем это станет
#: поводом звать человека. Покупка на Fragment укладывается в пару минут.
STUCK_FULFILLING_AFTER = timedelta(minutes=10)


async def reconcile_payments(
    session_maker: async_sessionmaker,
    *,
    gateway: FragmentGateway,
    tracker: PaymentTrackerClient,
    bot: Bot,
) -> None:
    async with session_maker() as session:
        orders = await list_orders_to_reconcile(session)
        for order in orders:
            result = await process_order_payment(
                session,
                order.id,
                gateway=gateway,
                tracker=tracker,
                bot=bot,
                source="reconcile",
            )
            # not_paid — самый частый и совершенно нормальный исход, не шумим.
            if result.outcome not in {Outcome.not_paid, Outcome.in_progress}:
                logger.info(
                    "reconcile.order_processed",
                    order_id=str(order.id),
                    outcome=result.outcome.value,
                )

        await _alert_stuck_orders(session, bot)


async def _alert_stuck_orders(session, bot: Bot) -> None:
    """Заказы, застрявшие в fulfilling: только сигнал человеку, без автоповтора."""
    stuck = await list_stuck_fulfilling_orders(session, older_than=STUCK_FULFILLING_AFTER)
    for order in stuck:
        # Отметка ставится один раз, поэтому админ не получит одно и то же
        # сообщение каждую минуту.
        if await mark_admin_notified(session, order.id):
            logger.error("reconcile.stuck_fulfilling", order_id=str(order.id))
            await notify_admin(
                bot,
                "🚨 Заказ завис в статусе fulfilling — проверь ВРУЧНУЮ, прошла ли покупка "
                "на Fragment и ушли ли деньги с кошелька. Автоматически он повторяться "
                "не будет (иначе есть риск оплатить дважды).\n"
                f"Заказ: {order.id}\n"
                f"Начали выдачу: {order.fulfillment_started_at}",
            )
