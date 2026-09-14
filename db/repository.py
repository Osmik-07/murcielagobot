"""
Доступ к данным заказов.

Главная функция модуля — claim_order. Именно она делает невозможной двойную
выдачу товара. Всё остальное здесь обслуживает её.

Почему это так важно: выдачу заказа могут запустить одновременно три
независимых источника — вебхук от TonConsole, кнопка «я оплатил» у клиента и
джоба сверки. Если каждый из них просто прочитает статус, увидит «pending» и
пойдёт покупать на Fragment, клиент получит товар трижды, а заплатит один раз.
Читать-потом-писать здесь нельзя в принципе.

Поэтому переход статуса делается ОДНИМ условным UPDATE:

    UPDATE orders SET status = :new WHERE id = :id AND status = :expected

Postgres на READ COMMITTED сериализует такие апдейты сам: второй вызов ждёт
коммита первого, перечитывает строку, видит уже изменившийся статус и обновляет
НОЛЬ строк. Тот, кто обновил ноль строк, знает, что заказ забрал кто-то другой,
и молча уходит. Никаких блокировок в коде, никаких гонок.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Sequence

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    ACTIVE_STATUSES,
    Asset,
    Order,
    OrderStatus,
    ProductType,
    Transaction,
    utcnow,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "create_order",
    "get_order",
    "get_order_by_invoice_id",
    "get_active_order_for_user",
    "attach_invoice",
    "claim_order",
    "claim_for_fulfillment",
    "release_fulfillment",
    "record_payment_facts",
    "mark_fulfilled",
    "mark_failed",
    "expire_order",
    "list_orders_to_reconcile",
    "list_stuck_fulfilling_orders",
    "list_expired_pending_orders",
    "get_expired_pending_orders",
    "record_transaction",
    "mark_admin_notified",
]


def coerce_order_id(order_id: "uuid.UUID | str") -> uuid.UUID:
    """
    Привести id заказа к UUID.

    Наружу (в FSM, в description инвойса, в тело вебхука) id уезжает строкой и
    возвращается оттуда уже недоверенным: подсунуть можно что угодно.
    """
    if isinstance(order_id, uuid.UUID):
        return order_id
    try:
        return uuid.UUID(str(order_id))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"Некорректный id заказа: {order_id!r}") from None


async def create_order(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    product: ProductType,
    recipient_username: str,
    asset: Asset,
    base_price: Decimal,
    invoice_amount: Decimal,
    invoice_amount_units: int | None = None,
    duration_months: int | None = None,
    stars_amount: int | None = None,
    ttl_minutes: int = 25,
) -> Order:
    """Создать заказ в статусе pending. Инвойс привязывается отдельно (attach_invoice)."""
    order = Order(
        telegram_user_id=telegram_user_id,
        product=product,
        recipient_username=recipient_username,
        asset=asset,
        base_price_ton=base_price,
        invoice_amount=invoice_amount,
        invoice_amount_units=invoice_amount_units,
        duration_months=duration_months,
        stars_amount=stars_amount,
        status=OrderStatus.pending,
        expires_at=utcnow() + timedelta(minutes=ttl_minutes),
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    logger.info(
        "order.created",
        order_id=str(order.id),
        product=product.value,
        asset=asset.value,
        amount=str(invoice_amount),
    )
    return order


async def get_order(session: AsyncSession, order_id: "uuid.UUID | str") -> Order | None:
    """Заказ по id. Всегда читаем свежую версию: статус мог измениться в другой корутине."""
    try:
        resolved = coerce_order_id(order_id)
    except ValueError:
        return None
    return await session.get(Order, resolved, populate_existing=True)


async def get_order_by_invoice_id(session: AsyncSession, invoice_id: str) -> Order | None:
    """Заказ по id инвойса трекера — точка входа для вебхука."""
    if not invoice_id:
        return None
    result = await session.execute(
        select(Order)
        .where(Order.payment_tracker_invoice_id == str(invoice_id))
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def get_active_order_for_user(
    session: AsyncSession,
    telegram_user_id: int,
    *,
    statuses: Iterable[OrderStatus] = ACTIVE_STATUSES,
) -> Order | None:
    """
    Последний незавершённый заказ пользователя.

    Нужен, чтобы кнопка «я оплатил» работала независимо от FSM: состояние живёт
    в памяти процесса и теряется при рестарте, а заказ и деньги — нет.
    """
    result = await session.execute(
        select(Order)
        .where(
            Order.telegram_user_id == telegram_user_id,
            Order.status.in_(list(statuses)),
        )
        .order_by(Order.created_at.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def attach_invoice(
    session: AsyncSession,
    order_id: "uuid.UUID | str",
    *,
    invoice_id: str,
    pay_to_address: str | None = None,
) -> Order | None:
    """
    Привязать инвойс трекера к заказу.

    На колонке стоит unique: один инвойс не может обслуживать два заказа.
    Это второй барьер против двойной выдачи, уже на уровне схемы БД.
    """
    resolved = coerce_order_id(order_id)
    stmt = (
        update(Order)
        .where(Order.id == resolved)
        .values(payment_tracker_invoice_id=str(invoice_id), pay_to_address=pay_to_address)
        .returning(Order.id)
    )
    try:
        claimed = (await session.execute(stmt)).scalar_one_or_none()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        logger.error("order.invoice_conflict", order_id=str(resolved), invoice_id=str(invoice_id))
        raise
    if claimed is None:
        return None
    return await session.get(Order, resolved, populate_existing=True)


async def claim_order(
    session: AsyncSession,
    order_id: "uuid.UUID | str",
    *,
    expected: OrderStatus,
    new: OrderStatus,
    **values: Any,
) -> Order | None:
    """
    Атомарно перевести заказ из expected в new.

    Возвращает заказ, если переход выполнил ИМЕННО ЭТОТ вызов, и None, если
    заказ был уже не в статусе expected (значит, его забрал кто-то другой —
    вебхук, кнопка или джоба).

    None здесь — не ошибка, а штатный ответ «этот заказ уже не твой».
    Вызывающий код обязан на нём остановиться и НЕ трогать Fragment.
    """
    resolved = coerce_order_id(order_id)

    payload: dict[str, Any] = {"status": new, **values}
    now = utcnow()
    # Отметки времени ставим в том же UPDATE, чтобы они не разъехались со статусом.
    if new is OrderStatus.paid:
        payload.setdefault("paid_at", now)
    elif new is OrderStatus.fulfilling:
        payload.setdefault("fulfillment_started_at", now)
    elif new is OrderStatus.fulfilled:
        payload.setdefault("fulfilled_at", now)

    stmt = (
        update(Order)
        .where(Order.id == resolved, Order.status == expected)
        .values(**payload)
        .returning(Order.id)
    )
    claimed = (await session.execute(stmt)).scalar_one_or_none()
    await session.commit()

    if claimed is None:
        logger.info(
            "order.claim_skipped",
            order_id=str(resolved),
            expected=expected.value,
            new=new.value,
        )
        return None

    logger.info(
        "order.claimed",
        order_id=str(resolved),
        from_status=expected.value,
        to_status=new.value,
    )
    # populate_existing: в identity map может лежать версия со старым статусом.
    return await session.get(Order, resolved, populate_existing=True)


async def record_payment_facts(
    session: AsyncSession,
    order_id: "uuid.UUID | str",
    *,
    paid_amount: Decimal | None = None,
    overpayment: Decimal | None = None,
    paid_by_address: str | None = None,
) -> None:
    """
    Сохранить фактические данные платежа. Статус не трогаем: за переходы
    отвечает только claim_order.
    """
    resolved = coerce_order_id(order_id)
    values: dict[str, Any] = {}
    if paid_amount is not None:
        values["paid_amount"] = paid_amount
    if overpayment is not None:
        values["overpayment"] = overpayment
    if paid_by_address:
        values["paid_by_address"] = paid_by_address[:128]
    if not values:
        return
    await session.execute(update(Order).where(Order.id == resolved).values(**values))
    await session.commit()


async def claim_for_fulfillment(
    session: AsyncSession,
    order_id: "uuid.UUID | str",
) -> Order | None:
    """
    paid -> fulfilling. Разыгрывает право сходить на Fragment.

    Вернуть заказ может ровно один вызов: тот, кто получил None, обязан молча
    уйти и НЕ покупать — иначе клиент получит товар дважды за одну оплату.
    """
    return await claim_order(
        session,
        order_id,
        expected=OrderStatus.paid,
        new=OrderStatus.fulfilling,
    )


async def release_fulfillment(
    session: AsyncSession,
    order_id: "uuid.UUID | str",
    *,
    reason: str,
) -> Order | None:
    """
    fulfilling -> paid: вернуть заказ в очередь на выдачу.

    Звать МОЖНО ТОЛЬКО когда точно известно, что транзакция в сеть не уходила
    (например, не хватило баланса или отвалилась авторизация). Если есть хоть
    малейшее сомнение — заказ остаётся в fulfilling и ждёт человека.
    """
    return await claim_order(
        session,
        order_id,
        expected=OrderStatus.fulfilling,
        new=OrderStatus.paid,
        fulfillment_started_at=None,
        failure_reason=str(reason)[:512],
    )


async def mark_fulfilled(
    session: AsyncSession,
    order_id: "uuid.UUID | str",
    *,
    fragment_tx_id: str | None = None,
) -> Order | None:
    """fulfilling -> fulfilled. Товар выдан, в заказе остаётся хеш транзакции Fragment."""
    return await claim_order(
        session,
        order_id,
        expected=OrderStatus.fulfilling,
        new=OrderStatus.fulfilled,
        fragment_tx_id=(str(fragment_tx_id)[:128] if fragment_tx_id else None),
        failure_reason=None,
    )


async def mark_failed(
    session: AsyncSession,
    order_id: "uuid.UUID | str",
    *,
    reason: str,
) -> Order | None:
    """
    fulfilling -> failed. Деньги у нас, товара у клиента нет: нужен ручной разбор,
    поэтому причина сохраняется в заказе, а не только в логах.
    """
    return await claim_order(
        session,
        order_id,
        expected=OrderStatus.fulfilling,
        new=OrderStatus.failed,
        failure_reason=str(reason)[:512],
    )


async def expire_order(session: AsyncSession, order_id: "uuid.UUID | str") -> Order | None:
    """
    pending -> expired, тоже атомарно.

    Вызывать можно ТОЛЬКО после проверки в трекере, что денег по инвойсу нет:
    просрочить оплаченный заказ — значит забрать деньги и не выдать товар.
    """
    return await claim_order(
        session,
        order_id,
        expected=OrderStatus.pending,
        new=OrderStatus.expired,
    )


async def list_orders_to_reconcile(
    session: AsyncSession,
    *,
    limit: int = 100,
    include_paid: bool = True,
) -> Sequence[Order]:
    """
    Заказы, состояние которых стоит перепроверить в трекере:

    * pending с привязанным инвойсом — вдруг оплата пришла, а вебхук потерялся;
    * paid — деньги получены, но фулфилмент не довели до конца (упал процесс,
      отвалился Fragment). Их нужно дожать, иначе клиент останется без товара.
    """
    statuses = [OrderStatus.pending]
    if include_paid:
        statuses.append(OrderStatus.paid)
    result = await session.execute(
        select(Order)
        .where(
            Order.status.in_(statuses),
            Order.payment_tracker_invoice_id.is_not(None),
        )
        .order_by(Order.created_at.asc())
        .limit(limit)
        .execution_options(populate_existing=True)
    )
    return result.scalars().all()


async def list_stuck_fulfilling_orders(
    session: AsyncSession,
    *,
    older_than: timedelta,
    limit: int = 50,
) -> Sequence[Order]:
    """
    Заказы, зависшие в fulfilling дольше разумного: процесс упал во время покупки
    либо Fragment не ответил.

    Такие заказы НЕЛЬЗЯ перезапускать автоматически — неизвестно, ушла ли
    транзакция в сеть. Их дело — попасть к админу.
    """
    threshold = utcnow() - older_than
    result = await session.execute(
        select(Order)
        .where(
            Order.status == OrderStatus.fulfilling,
            Order.fulfillment_started_at.is_not(None),
            Order.fulfillment_started_at < threshold,
        )
        .order_by(Order.fulfillment_started_at.asc())
        .limit(limit)
        .execution_options(populate_existing=True)
    )
    return result.scalars().all()


async def list_expired_pending_orders(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> Sequence[Order]:
    """Неоплаченные заказы, у которых вышел срок. Гасить только после проверки в трекере."""
    moment = now or utcnow()
    result = await session.execute(
        select(Order)
        .where(Order.status == OrderStatus.pending, Order.expires_at < moment)
        .order_by(Order.expires_at.asc())
        .limit(limit)
        .execution_options(populate_existing=True)
    )
    return result.scalars().all()


async def get_expired_pending_orders(session: AsyncSession) -> list[Order]:
    """Совместимость со старым кодом джобы."""
    return list(await list_expired_pending_orders(session))


async def mark_admin_notified(session: AsyncSession, order_id: "uuid.UUID | str") -> bool:
    """
    Отметить, что админу уже писали по этому заказу.

    Возвращает True, только если отметку поставил этот вызов — так джоба сверки,
    которая крутится раз в минуту, не превращает один сломанный заказ
    в бесконечный поток одинаковых сообщений.
    """
    resolved = coerce_order_id(order_id)
    stmt = (
        update(Order)
        .where(Order.id == resolved, Order.admin_notified_at.is_(None))
        .values(admin_notified_at=utcnow())
        .returning(Order.id)
    )
    claimed = (await session.execute(stmt)).scalar_one_or_none()
    await session.commit()
    return claimed is not None


async def record_transaction(
    session: AsyncSession,
    *,
    tx_hash: str,
    amount: Decimal,
    asset: Asset,
    order_id: "uuid.UUID | str | None" = None,
    overpayment: Decimal = Decimal("0"),
    from_address: str | None = None,
    source: str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> Transaction | None:
    """
    Сохранить сырые данные платежа для ручной сверки.

    Идемпотентно по tx_hash: повторный вызов из вебхука и из джобы сверки
    не создаёт дубль. Сохранение платежа никогда не должно ронять фулфилмент,
    поэтому конфликт здесь — не ошибка, а ожидаемая ситуация.
    """
    if not tx_hash:
        return None

    resolved_order_id: uuid.UUID | None = None
    if order_id is not None:
        try:
            resolved_order_id = coerce_order_id(order_id)
        except ValueError:
            resolved_order_id = None

    transaction = Transaction(
        tx_hash=str(tx_hash)[:128],
        amount=amount,
        asset=asset,
        order_id=resolved_order_id,
        overpayment=overpayment or Decimal("0"),
        from_address=(from_address[:128] if from_address else None),
        matched=resolved_order_id is not None,
        source=(str(source)[:32] if source else None),
        raw_payload=raw_payload,
    )
    try:
        # Savepoint: если tx_hash уже есть, откатываем только вставку,
        # а не всю внешнюю транзакцию вызывающего кода.
        async with session.begin_nested():
            session.add(transaction)
        await session.commit()
        return transaction
    except IntegrityError:
        await session.rollback()
        existing = await session.execute(
            select(Transaction).where(Transaction.tx_hash == str(tx_hash)[:128])
        )
        logger.info("transaction.duplicate_ignored", tx_hash=str(tx_hash)[:64])
        return existing.scalars().first()
