"""
Точка входа.

В одном процессе и одном event loop живут три вещи:
  * long-polling бота (диалог с клиентом),
  * aiohttp-сервер вебхуков TonConsole (основной триггер выдачи товара),
  * периодические джобы: сверка платежей и протухание неоплаченных заказов.
"""

from __future__ import annotations

import asyncio
import logging

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.config import settings
from bot.handlers import order, payment, start
from bot.middlewares import DBSessionMiddleware
from db.models import Base
from jobs.expire_orders import expire_stale_orders
from jobs.reconcile_payments import reconcile_payments
from services.fragment_gateway import FragmentGateway, FragmentGatewayError
from services.payment_tracker import PaymentTrackerClient

logger = structlog.get_logger(__name__)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            # Без метки времени логи бесполезны при разборе платежей.
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def build_storage():
    """FSM: Redis, если задан, иначе память (с честным предупреждением)."""
    if settings.use_redis_fsm:
        from aiogram.fsm.storage.redis import RedisStorage

        logger.info("fsm.storage", kind="redis")
        return RedisStorage.from_url(settings.redis_url)

    logger.warning(
        "fsm.storage",
        kind="memory",
        warning="состояние диалогов будет теряться при рестарте; "
        "заказы и деньги при этом не теряются (лежат в БД), но клиенту "
        "придётся начать диалог заново. Для прода задай REDIS_URL",
    )
    return MemoryStorage()


async def preflight(gateway: FragmentGateway, bot: Bot) -> None:
    """
    Проверка на старте: авторизуемся на Fragment и смотрим баланс кошелька.
    Лучше узнать о проблеме сейчас, чем в момент первой оплаченной покупки.
    """
    await gateway.connect()
    try:
        ton_balance, usdt_balance = await gateway.get_wallet_balances()
    except FragmentGatewayError as exc:
        logger.error("preflight.balance_failed", reason=str(exc)[:200])
        return

    logger.info(
        "preflight.wallet",
        ton_balance=str(ton_balance),
        usdt_balance=str(usdt_balance),
        address=settings.fragment_wallet_address,
    )

    warnings = []
    if ton_balance < settings.low_balance_alert_ton:
        # TON нужен всегда: даже покупка за USDT платит комиссию сети в TON.
        warnings.append(f"TON: {ton_balance} (порог {settings.low_balance_alert_ton})")
    if usdt_balance < settings.low_balance_alert_usdt:
        warnings.append(f"USDT: {usdt_balance} (порог {settings.low_balance_alert_usdt})")

    if warnings and settings.alerts_enabled:
        try:
            await bot.send_message(
                settings.admin_chat_id,
                "⚠️ Низкий баланс операторского кошелька:\n" + "\n".join(warnings),
            )
        except Exception:  # noqa: BLE001
            logger.warning("preflight.alert_failed")


async def main() -> None:
    configure_logging()

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        # На старте — create_all. Когда появятся прод-данные, переходить на alembic:
        # добавление статуса fulfilling потребует ALTER TYPE для нативного enum.
        await conn.run_sync(Base.metadata.create_all)

    gateway = FragmentGateway(
        seed=settings.fragment_wallet_seed.get_secret_value(),
        api_key=settings.tonconsole_api_key.get_secret_value(),
        wallet_version=settings.fragment_wallet_version,
        cookie_ttl_seconds=int(settings.fragment_cookie_ttl_seconds),
        timeout=settings.fragment_timeout_seconds,
    )
    tracker = PaymentTrackerClient(
        token=settings.payment_tracker_token.get_secret_value(),
        wallet_address=settings.fragment_wallet_address,
        base_url=settings.payment_tracker_base_url,
        timeout=settings.payment_tracker_timeout_seconds,
    )

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=build_storage())

    dp.include_router(start.router)
    dp.include_router(order.router)
    dp.include_router(payment.router)

    dp["fragment_gateway"] = gateway
    dp["payment_tracker"] = tracker

    db_middleware = DBSessionMiddleware(session_maker)
    dp.message.middleware(db_middleware)
    dp.callback_query.middleware(db_middleware)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        """Никакое исключение в хендлере не должно оставлять клиента без ответа."""
        logger.exception("handler.unhandled_error", error=type(event.exception).__name__)
        callback = getattr(event.update, "callback_query", None)
        message = getattr(event.update, "message", None)
        try:
            if callback is not None:
                await callback.answer("Что-то пошло не так, попробуй ещё раз", show_alert=True)
            elif message is not None:
                await message.answer("Что-то пошло не так. Попробуй ещё раз или /start")
        except Exception:  # noqa: BLE001
            pass
        return True

    await preflight(gateway, bot)

    runner = None
    if settings.webhook_enabled:
        from services.webhook_server import run_webhook_server

        runner = await run_webhook_server(
            session_maker=session_maker,
            gateway=gateway,
            tracker=tracker,
            bot=bot,
        )
    else:
        logger.warning(
            "webhook.disabled",
            warning="выдача будет зависеть от кнопки клиента и джобы сверки; "
            "для прода задай WEBHOOK_BASE_URL и WEBHOOK_SECRET_PATH",
        )

    scheduler = AsyncIOScheduler()
    job_kwargs = {"gateway": gateway, "tracker": tracker, "bot": bot}
    scheduler.add_job(
        reconcile_payments,
        "interval",
        seconds=settings.reconcile_interval_seconds,
        args=[session_maker],
        kwargs=job_kwargs,
        # Не даём джобе наслаиваться саму на себя, если круг затянулся.
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        expire_stale_orders,
        "interval",
        seconds=settings.expire_interval_seconds,
        args=[session_maker],
        kwargs=job_kwargs,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    logger.info("bot_starting")
    try:
        await dp.start_polling(bot)
    finally:
        logger.info("bot_stopping")
        scheduler.shutdown(wait=False)
        if runner is not None:
            await runner.cleanup()
        await tracker.close()
        await gateway.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
