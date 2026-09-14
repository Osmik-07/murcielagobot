"""
HTTP-сервер для вебхуков TonConsole Payment Tracker.

Вебхук — основной триггер выдачи товара: без него клиенту пришлось бы жать
кнопку «я оплатил», а тот, кто её не нажал, остался бы без покупки.

При этом тело запроса приходит из открытого интернета, и подделать его может
кто угодно. Поэтому здесь оно используется РОВНО для одного: узнать, какой
инвойс стоит перепроверить. Все решения про деньги принимает
services.fulfillment, который сам ходит в трекер авторизованным запросом.
Даже если злоумышленник пришлёт «оплачено» на все инвойсы подряд, ничего
не произойдёт: трекер ответит, что денег нет.

Защита самого эндпоинта — неугадываемый сегмент пути плюс необязательный
общий секрет в заголовке. Оба сравниваем в постоянном времени.
"""

from __future__ import annotations

import asyncio
import hmac
from pathlib import Path
from typing import Any

import structlog
from aiogram import Bot
from aiohttp import web
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.config import settings
from db.repository import get_order_by_invoice_id
from services.fragment_gateway import FragmentGateway
from services.fulfillment import process_order_payment
from services.payment_tracker import PaymentTrackerClient, parse_webhook_payload

logger = structlog.get_logger(__name__)

__all__ = ["build_webhook_app", "run_webhook_server"]

#: Ссылки на фоновые задачи: без них сборщик мусора может убить задачу на середине.
_background_tasks: set[asyncio.Task[Any]] = set()

_MAX_BODY_BYTES = 64 * 1024


def _spawn(coro: Any, *, name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _header_secret_ok(request: web.Request) -> bool:
    """Необязательная вторая линия: общий секрет в заголовке."""
    expected = settings.webhook_secret_token
    if expected is None:
        return True
    provided = request.headers.get(settings.webhook_secret_header, "")
    return hmac.compare_digest(provided, expected.get_secret_value())


async def _handle_invoice_update(
    invoice_id: str,
    *,
    session_maker: async_sessionmaker,
    gateway: FragmentGateway,
    tracker: PaymentTrackerClient,
    bot: Bot,
) -> None:
    """
    Фоновая часть: найти заказ и прогнать его через общую функцию выдачи.

    Вынесено из обработчика запроса, потому что покупка на Fragment занимает
    десятки секунд, а TonConsole ждать столько не обязан.
    """
    try:
        async with session_maker() as session:
            order = await get_order_by_invoice_id(session, invoice_id)
            if order is None:
                # Инвойс не наш или заказ удалён — это не ошибка сервера.
                logger.warning("webhook.unknown_invoice", invoice_id=invoice_id[:64])
                return
            await process_order_payment(
                session,
                order.id,
                gateway=gateway,
                tracker=tracker,
                bot=bot,
                source="webhook",
            )
    except Exception:  # noqa: BLE001 - фоновая задача не должна умирать молча
        logger.exception("webhook.processing_failed", invoice_id=invoice_id[:64])


_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def build_webhook_app(
    *,
    session_maker: async_sessionmaker,
    gateway: FragmentGateway,
    tracker: PaymentTrackerClient,
    bot: Bot,
    public_base_url: str | None = None,
) -> web.Application:
    app = web.Application(client_max_size=_MAX_BODY_BYTES)

    async def handle_webhook(request: web.Request) -> web.Response:
        if not _header_secret_ok(request):
            logger.warning("webhook.bad_secret_header", remote=request.remote)
            return web.json_response({"ok": False}, status=403)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - мусор в теле это обычное дело
            logger.warning("webhook.bad_json", remote=request.remote)
            return web.json_response({"ok": False}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"ok": False}, status=400)

        try:
            invoice = parse_webhook_payload(payload)
        except Exception:  # noqa: BLE001
            logger.warning("webhook.unparsable_payload")
            return web.json_response({"ok": False}, status=400)

        if not invoice.id:
            return web.json_response({"ok": False}, status=400)

        logger.info(
            "webhook.received",
            invoice_id=invoice.id[:64],
            # Статус из тела — справочный. Решение принимается по ответу трекера.
            claimed_status=invoice.status,
        )

        # Отвечаем сразу: покупка идёт в фоне.
        _spawn(
            _handle_invoice_update(
                invoice.id,
                session_maker=session_maker,
                gateway=gateway,
                tracker=tracker,
                bot=bot,
            ),
            name=f"webhook-invoice-{invoice.id[:32]}",
        )
        return web.json_response({"ok": True})

    async def handle_health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def handle_manifest(_: web.Request) -> web.Response:
        # Нужен ради TON Connect (services.tonconnect_gateway): кошелёк
        # запрашивает этот файл при подключении, чтобы показать клиенту,
        # что за приложение просит доступ.
        base = (public_base_url or "").rstrip("/")
        return web.json_response(
            {
                "url": base,
                "name": "murcielagobot",
                "iconUrl": f"{base}/static/icon.png",
            }
        )

    app.router.add_post(settings.webhook_path, handle_webhook)
    app.router.add_get("/health", handle_health)
    if public_base_url:
        app.router.add_get("/tonconnect-manifest.json", handle_manifest)
        app.router.add_static("/static/", _STATIC_DIR, show_index=False)
    return app


async def run_webhook_server(
    *,
    session_maker: async_sessionmaker,
    gateway: FragmentGateway,
    tracker: PaymentTrackerClient,
    bot: Bot,
    public_base_url: str | None = None,
) -> web.AppRunner:
    """Поднять сервер и вернуть runner, чтобы его можно было аккуратно погасить."""
    app = build_webhook_app(
        session_maker=session_maker,
        gateway=gateway,
        tracker=tracker,
        bot=bot,
        public_base_url=public_base_url,
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.webhook_host, port=settings.webhook_port)
    await site.start()
    logger.info(
        "webhook.listening",
        host=settings.webhook_host,
        port=settings.webhook_port,
        # Путь содержит секрет — целиком в лог не пишем.
        path_prefix="/tonconsole/…",
    )
    return runner
