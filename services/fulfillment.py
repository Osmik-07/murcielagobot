"""
Единственный путь, которым заказ превращается в выданный товар.

Через эту функцию обязаны проходить ВСЕ триггеры выдачи:
  * вебхук TonConsole (основной),
  * кнопка «я оплатил» у клиента (для нетерпеливых),
  * джоба сверки (страховка на случай потерянного вебхука).

Правила, ради которых модуль вообще существует:

1. Источником правды об оплате является только авторизованный GET статуса
   инвойса. Тело вебхука — это «сходи проверь», а не «деньги пришли»: его шлёт
   кто угодно из интернета.

2. Право сходить на Fragment разыгрывается атомарным claim-ом paid -> fulfilling.
   Проиграл — молча ушёл. Двух покупок по одному заказу быть не может.

3. Если неизвестно, ушла транзакция в сеть или нет, заказ остаётся в fulfilling
   и ждёт человека. Автоповтор в такой ситуации — прямой путь оплатить товар
   дважды, а деньги клиента у нас уже есть.
"""

from __future__ import annotations

import enum
import html
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from db.models import TERMINAL_STATUSES, Order, OrderStatus, ProductType
from db.repository import (
    claim_for_fulfillment,
    claim_order,
    get_order,
    mark_admin_notified,
    mark_failed,
    mark_fulfilled,
    record_payment_facts,
    record_transaction,
    release_fulfillment,
)
from services.fragment_gateway import (
    FragmentAuthError,
    FragmentConfigError,
    FragmentGateway,
    InsufficientBalanceError,
    RecipientNotEligibleError,
    RecipientNotFoundError,
    UncertainFulfillmentError,
)
from services.payment_tracker import Invoice, PaymentTrackerClient, PaymentTrackerError

logger = structlog.get_logger(__name__)

__all__ = [
    "Outcome",
    "FulfillmentResult",
    "process_order_payment",
    "notify_admin",
    "notify_user",
]


class Outcome(str, enum.Enum):
    """Чем закончилась попытка провести заказ через выдачу."""

    not_found = "not_found"          # заказа нет
    not_paid = "not_paid"            # денег по инвойсу ещё нет
    fulfilled = "fulfilled"          # товар выдан именно этим вызовом
    already_done = "already_done"    # заказ уже в терминальном статусе
    in_progress = "in_progress"      # покупку прямо сейчас делает кто-то другой
    failed = "failed"                # выдать нельзя, деньги не потрачены
    needs_manual = "needs_manual"    # нужен человек: неясный статус или переплата
    retry_later = "retry_later"      # временная проблема, джоба сверки повторит


@dataclass(frozen=True)
class FulfillmentResult:
    outcome: Outcome
    order: Order | None = None
    #: Текст для клиента (уже по-русски). None — клиенту говорить нечего.
    user_message: str | None = None

    @property
    def is_terminal_for_user(self) -> bool:
        """Стоит ли убирать у клиента кнопку «я оплатил»."""
        return self.outcome in {
            Outcome.fulfilled,
            Outcome.already_done,
            Outcome.failed,
            Outcome.needs_manual,
        }


async def notify_admin(bot: Bot, text: str) -> None:
    """
    Алерт админу. Никогда не роняет вызывающий код: провал доставки алерта
    не должен превращаться в провал обработки денег.
    """
    if not settings.alerts_enabled:
        logger.warning("admin_alert.skipped", reason="ADMIN_CHAT_ID не задан", text=text[:200])
        return
    try:
        await bot.send_message(settings.admin_chat_id, text)
    except Exception as exc:  # noqa: BLE001 - алерт не важнее заказа
        logger.error("admin_alert.failed", error=type(exc).__name__, reason=str(exc)[:200])


async def notify_user(bot: Bot, telegram_user_id: int, text: str) -> None:
    """Сообщение клиенту. Тоже не роняет обработку: он мог заблокировать бота."""
    try:
        await bot.send_message(telegram_user_id, text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "user_notify.failed",
            telegram_user_id=telegram_user_id,
            error=type(exc).__name__,
        )


def _order_line(order: Order) -> str:
    """Короткое описание заказа для сообщений. Юзернейм экранируем: parse_mode=HTML."""
    recipient = html.escape(order.recipient_username or "")
    if order.product is ProductType.premium:
        what = f"Premium на {order.duration_months} мес."
    else:
        what = f"{order.stars_amount} Stars"
    return f"{what} для @{recipient}"


async def _save_payment_facts(
    session: AsyncSession,
    order: Order,
    invoice: Invoice,
    *,
    source: str,
) -> Decimal:
    """
    Записать фактические данные платежа и вернуть фактически полученную сумму.

    Пишем ДО любых попыток выдачи: если дальше что-то упадёт, у оператора всё
    равно останется, кто, сколько и по какому инвойсу заплатил.
    """
    expected = order.invoice_amount
    overpayment = invoice.overpayment or Decimal("0")
    paid_amount = (invoice.amount if invoice.amount is not None else expected) + overpayment

    await record_payment_facts(
        session,
        order.id,
        paid_amount=paid_amount,
        overpayment=overpayment,
        paid_by_address=invoice.paid_by_address,
    )
    # Ключ идемпотентности — id инвойса: блокчейн-хеша трекер нам не отдаёт.
    await record_transaction(
        session,
        tx_hash=invoice.id,
        amount=paid_amount,
        asset=order.asset,
        order_id=order.id,
        overpayment=overpayment,
        from_address=invoice.paid_by_address,
        source=source,
        raw_payload=dict(invoice.raw) if invoice.raw else None,
    )
    return paid_amount


async def _alert_once(session: AsyncSession, bot: Bot, order: Order, text: str) -> None:
    """
    Позвать админа ровно один раз на заказ. Джоба сверки крутится каждую минуту,
    и без этого один сломанный заказ превратился бы в поток одинаковых сообщений.
    """
    if await mark_admin_notified(session, order.id):
        await notify_admin(bot, text)


async def process_order_payment(
    session: AsyncSession,
    order_id: Any,
    *,
    gateway: FragmentGateway,
    tracker: PaymentTrackerClient,
    bot: Bot,
    source: str = "unknown",
) -> FulfillmentResult:
    """
    Довести заказ от «клиент говорит, что заплатил» до выданного товара.

    source — откуда пришёл триггер: "webhook" | "button" | "reconcile".
    Функция идемпотентна: сколько бы раз и откуда её ни звали одновременно,
    покупка на Fragment произойдёт максимум один раз.
    """
    order = await get_order(session, order_id)
    if order is None:
        logger.warning("fulfillment.order_not_found", order_id=str(order_id), source=source)
        return FulfillmentResult(Outcome.not_found)

    log = logger.bind(order_id=str(order.id), source=source, status=order.status.value)

    if order.status in TERMINAL_STATUSES:
        log.info("fulfillment.already_terminal")
        return FulfillmentResult(
            Outcome.already_done,
            order,
            user_message=_terminal_message(order),
        )

    if order.status is OrderStatus.fulfilling:
        # Покупку уже кто-то делает (или она зависла — этим займётся джоба сверки).
        log.info("fulfillment.busy")
        return FulfillmentResult(
            Outcome.in_progress,
            order,
            user_message="Заказ уже выполняется, подожди немного ⏳",
        )

    if not order.payment_tracker_invoice_id:
        log.warning("fulfillment.no_invoice")
        return FulfillmentResult(
            Outcome.not_paid,
            order,
            user_message="Счёт на оплату не создан. Начни заново через /start",
        )

    # --- источник правды: авторизованный запрос статуса, а не вебхук ---------
    try:
        invoice = await tracker.get_invoice(
            order.payment_tracker_invoice_id,
            currency=order.asset.tracker_currency,
        )
    except PaymentTrackerError as exc:
        log.warning("fulfillment.tracker_unavailable", error=type(exc).__name__, reason=str(exc)[:200])
        return FulfillmentResult(
            Outcome.retry_later,
            order,
            user_message="Не удалось проверить платёж прямо сейчас, попробуй чуть позже",
        )

    if invoice.has_funds:
        await _save_payment_facts(session, order, invoice, source=source)

    if not invoice.is_paid:
        if invoice.has_overpayment:
            # Деньги пришли, но инвойс не «paid»: заплатили позже срока либо не ту сумму.
            # Автоматически такое трогать нельзя — только руками.
            log.warning("fulfillment.funds_without_paid_status", invoice_status=invoice.status)
            await _alert_once(
                session,
                bot,
                order,
                "⚠️ Деньги по инвойсу пришли, но статус не paid — нужен ручной разбор.\n"
                f"Заказ: {order.id}\nИнвойс: {invoice.id}\nСтатус: {invoice.status}\n"
                f"Переплата: {invoice.overpayment}",
            )
            return FulfillmentResult(
                Outcome.needs_manual,
                order,
                user_message=(
                    "Мы видим твой платёж, но он не сошёлся со счётом автоматически. "
                    "Уже разбираемся вручную — деньги не потеряются."
                ),
            )
        log.info("fulfillment.not_paid_yet", invoice_status=invoice.status)
        return FulfillmentResult(
            Outcome.not_paid,
            order,
            user_message="Платёж пока не найден. Если только что оплатил — подожди минуту и нажми ещё раз",
        )

    # --- оплата подтверждена: фиксируем это ровно один раз -------------------
    if order.status is OrderStatus.pending:
        confirmed = await claim_order(
            session,
            order.id,
            expected=OrderStatus.pending,
            new=OrderStatus.paid,
        )
        if confirmed is None:
            # Кто-то подтвердил оплату параллельно — не страшно, идём дальше:
            # право на саму покупку всё равно разыгрывается ниже.
            order = await get_order(session, order.id)
            if order is None or order.status in TERMINAL_STATUSES:
                return FulfillmentResult(Outcome.already_done, order)
        else:
            order = confirmed
        if invoice.has_overpayment:
            await _alert_once(
                session,
                bot,
                order,
                "💰 Переплата по заказу — вероятно, нужен возврат.\n"
                f"Заказ: {order.id}\nИнвойс: {invoice.id}\n"
                f"Переплата: {invoice.overpayment} {order.asset.display_name}",
            )

    # --- право сходить на Fragment: ровно одному вызову ----------------------
    claimed = await claim_for_fulfillment(session, order.id)
    if claimed is None:
        log.info("fulfillment.claim_lost")
        return FulfillmentResult(
            Outcome.in_progress,
            order,
            user_message="Заказ уже выполняется, подожди немного ⏳",
        )
    order = claimed

    log.info("fulfillment.purchasing", product=order.product.value, asset=order.asset.value)
    try:
        if order.product is ProductType.premium:
            result = await gateway.gift_premium(
                username=order.recipient_username,
                months=order.duration_months,
                payment_method=order.asset.fragment_payment_method,
            )
        else:
            result = await gateway.buy_stars(
                username=order.recipient_username,
                amount=order.stars_amount,
                payment_method=order.asset.fragment_payment_method,
            )
    except UncertainFulfillmentError as exc:
        # Самый опасный случай: транзакция могла уйти. Заказ НАМЕРЕННО остаётся
        # в fulfilling — никакой автоповтор его больше не подхватит.
        log.error("fulfillment.uncertain", reason=str(exc)[:300])
        await _alert_once(
            session,
            bot,
            order,
            "🚨 НЕЯСНЫЙ СТАТУС ПОКУПКИ — проверь кошелёк и Fragment ВРУЧНУЮ, "
            "заказ намеренно оставлен в статусе fulfilling и повторяться не будет.\n"
            f"Заказ: {order.id}\n{_order_line(order)}\nПричина: {exc}",
        )
        await notify_user(
            bot,
            order.telegram_user_id,
            "Оплата получена. Заказ дорабатывается вручную — скоро всё будет ✅",
        )
        return FulfillmentResult(Outcome.needs_manual, order)

    except (RecipientNotFoundError, RecipientNotEligibleError, FragmentConfigError) as exc:
        # Детерминированный отказ: повтор не поможет, деньги не потрачены.
        log.warning("fulfillment.rejected", error=type(exc).__name__, reason=str(exc)[:300])
        await mark_failed(session, order.id, reason=f"{type(exc).__name__}: {exc}")
        await _alert_once(
            session,
            bot,
            order,
            "❌ Заказ не выполнен, деньги у нас — нужен возврат или ручная выдача.\n"
            f"Заказ: {order.id}\n{_order_line(order)}\nПричина: {exc}",
        )
        return FulfillmentResult(
            Outcome.failed,
            order,
            user_message=(
                "Не получилось выполнить заказ: "
                f"{html.escape(str(exc))}.\n"
                "Оплата получена — мы вернём деньги или выполним заказ вручную."
            ),
        )

    except (InsufficientBalanceError, FragmentAuthError) as exc:
        # Транзакция точно не уходила: не хватило баланса либо отвалилась сессия.
        # Возвращаем заказ в очередь — джоба сверки повторит, когда оператор починит.
        log.error("fulfillment.retryable", error=type(exc).__name__, reason=str(exc)[:300])
        await release_fulfillment(session, order.id, reason=f"{type(exc).__name__}: {exc}")
        await _alert_once(
            session,
            bot,
            order,
            "⏳ Заказ оплачен, но выдать не смогли — требуется вмешательство "
            "(баланс кошелька или сессия Fragment). Повторим автоматически после починки.\n"
            f"Заказ: {order.id}\n{_order_line(order)}\nПричина: {exc}",
        )
        await notify_user(
            bot,
            order.telegram_user_id,
            "Оплата получена ✅ Выдача немного задерживается — сделаем в ближайшее время.",
        )
        return FulfillmentResult(Outcome.retry_later, order)

    except Exception as exc:  # noqa: BLE001 - неизвестная ошибка = считаем неопределённой
        # Не знаем, что произошло — значит, не знаем и ушли ли деньги.
        # Безопасное поведение здесь только одно: остановиться и позвать человека.
        log.exception("fulfillment.unexpected_error")
        await _alert_once(
            session,
            bot,
            order,
            "🚨 Непредвиденная ошибка при выдаче, статус покупки неизвестен — "
            "проверь кошелёк ВРУЧНУЮ. Заказ оставлен в fulfilling.\n"
            f"Заказ: {order.id}\n{_order_line(order)}\n"
            f"Ошибка: {type(exc).__name__}: {exc}",
        )
        await notify_user(
            bot,
            order.telegram_user_id,
            "Оплата получена. Заказ дорабатывается вручную — скоро всё будет ✅",
        )
        return FulfillmentResult(Outcome.needs_manual, order)

    # --- успех ---------------------------------------------------------------
    tx_id = str(result.get("transaction_id") or "")
    done = await mark_fulfilled(session, order.id, fragment_tx_id=tx_id)
    log.info("fulfillment.done", fragment_tx_id=tx_id[:32])

    await notify_user(
        bot,
        order.telegram_user_id,
        f"Готово! {_order_line(order)} — выдано ✅",
    )
    return FulfillmentResult(
        Outcome.fulfilled,
        done or order,
        user_message=f"Готово! {_order_line(order)} — выдано ✅",
    )


def _terminal_message(order: Order) -> str:
    """Что сказать клиенту про заказ, который уже закрыт."""
    if order.status is OrderStatus.fulfilled:
        return f"Этот заказ уже выполнен: {_order_line(order)} ✅"
    if order.status is OrderStatus.failed:
        return (
            "По этому заказу произошла ошибка, мы разбираемся вручную. "
            "Деньги не потеряются."
        )
    return (
        "Срок оплаты этого заказа истёк. Если ты всё же оплатил — напиши нам, "
        "мы найдём платёж. Новый заказ: /start"
    )
