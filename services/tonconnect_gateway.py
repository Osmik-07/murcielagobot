"""
Оплата через TON Connect — ради встроенного в Telegram кошелька (Wallet).

Почему это отдельный механизм, а не просто ещё одна ссылка: в официальном
реестре TON Connect (github.com/ton-blockchain/wallets-list) у Telegram
Wallet ЕСТЬ bridge, но НЕТ deepLink — в отличие от Tonkeeper. Это значит,
что статическая ссылка ton://transfer его не откроет с готовым переводом:
единственный официальный путь — сначала «подключить» кошелёк по протоколу
TON Connect (handshake через bridge), и только потом просить его подписать
конкретную транзакцию через установленную сессию.

Это ДОПОЛНИТЕЛЬНЫЙ способ инициировать перевод, не замена ссылки ton:// —
она остаётся для Tonkeeper и любого другого кошелька с deepLink.

Важно: этот модуль ни на что в деньгах не влияет. Он только помогает клиенту
отправить перевод удобнее. Источник правды об оплате как был, так и остаётся
один — авторизованный GET статуса у TonConsole (services.fulfillment).
Даже если TonConnect соврёт, что транзакция ушла, — это никак не отразится
на статусе заказа: выдача случится только когда TonConsole реально увидит
деньги.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog
from aiogram import Bot
from pytonconnect import TonConnect
from pytonconnect.exceptions import TonConnectError, UserRejectsError
from pytonconnect.storage import DefaultStorage
from ton_core import Address
from tonutils.clients import TonapiClient, ToncenterClient
from tonutils.contracts.jetton.methods import (
    get_wallet_address_get_method,
    get_wallet_data_get_method,
)

from db.models import Asset
from services.pricing import format_amount
from services.ton_messages import (
    JETTON_TRANSFER_GAS,
    USDT_TON_JETTON_MASTER,
    build_jetton_message,
    build_ton_message,
    make_ton_client,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "TonConnectGateway",
    "TonConnectUnavailableError",
    "PaymentOutcome",
]

# Мастер-контракт USDT и газ на джеттон-перевод живут в services.ton_messages —
# там же, где собираются сами сообщения. Здесь только переэкспорт для обратной
# совместимости с тем, что уже импортировало эти имена отсюда.
_JETTON_TRANSFER_GAS = JETTON_TRANSFER_GAS

# Официальный app_name кошелька Telegram в реестре TON Connect.
_TELEGRAM_WALLET_APP_NAME = "telegram-wallet"

# Сколько ждём, пока клиент подключит кошелёк и подтвердит перевод.
CONNECT_TIMEOUT_SECONDS = 300


class TonConnectUnavailableError(Exception):
    """Метод недоступен прямо сейчас (нет манифеста, кошелёк не найден в реестре и т.п.)."""


@dataclass(frozen=True)
class PaymentOutcome:
    """Итог одной попытки оплаты через TON Connect. Не про статус заказа — только про сам перевод."""

    ok: bool
    #: Текст для клиента, уже готовый к отправке.
    message: str
    tx_boc: str | None = None


@dataclass
class _Session:
    tc: TonConnect
    order_id: str
    created_at: float = field(default_factory=time.monotonic)


class TonConnectGateway:
    """
    Держит активные TON Connect сессии (по одной на заказ) и знает, как
    собрать и отправить транзакцию под конкретный актив заказа.

    Сессии живут только в памяти процесса — рестарт бота их роняет. Это
    осознанно: TON Connect здесь лишь способ ИНИЦИИРОВАТЬ перевод, а не
    хранилище состояния платежа (оно всё в БД и не зависит от этого модуля).
    """

    def __init__(
        self,
        *,
        manifest_url: str,
        tonapi_key: str,
        api_provider: str = "tonapi",
    ) -> None:
        self._manifest_url = manifest_url
        self._tonapi_key = tonapi_key
        self._api_provider = api_provider
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()
        # Ссылки на фоновые задачи: без них сборщик мусора может убить задачу
        # на середине (тот же приём, что в services.webhook_server._spawn).
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def _telegram_wallet_entry(self) -> dict[str, Any]:
        """
        Найти запись Telegram Wallet в официальном реестре TON Connect.

        get_wallets() сама тянет wallets-v2.json по HTTP синхронно — уводим
        в поток, чтобы не подвесить event loop. Результат кэшируется библиотекой
        на весь процесс (без TTL), так что реальный сетевой запрос — один раз.
        """
        wallets = await asyncio.to_thread(TonConnect.get_wallets)
        for wallet in wallets:
            if wallet.get("app_name") == _TELEGRAM_WALLET_APP_NAME:
                return wallet
        raise TonConnectUnavailableError(
            "Telegram Wallet не найден в реестре TON Connect — попробуй другой способ оплаты"
        )

    async def create_connect_link(self, order_id: str) -> str:
        """
        Начать (или переиспользовать) сессию для заказа и вернуть ссылку
        подключения — https://t.me/wallet?attach=wallet&... . Открывается
        прямо внутри Telegram, без перехода в другое приложение.
        """
        async with self._lock:
            existing = self._sessions.get(order_id)
            if existing is not None and existing.tc.connected:
                # Уже подключен с прошлой попытки — новую ссылку просить не нужно.
                return existing.tc.url or ""

            tc = TonConnect(manifest_url=self._manifest_url, storage=DefaultStorage())
            wallet = await self._telegram_wallet_entry()
            link = await tc.connect(wallet)
            self._sessions[order_id] = _Session(tc=tc, order_id=order_id)
            logger.info("tonconnect.link_created", order_id=order_id)
            return link

    async def wait_for_connection(self, order_id: str, *, timeout: float = CONNECT_TIMEOUT_SECONDS) -> bool:
        """Дождаться, пока клиент подтвердит подключение кошелька в своём приложении."""
        session = self._sessions.get(order_id)
        if session is None:
            return False
        if session.tc.connected:
            return True
        try:
            result = await asyncio.wait_for(session.tc.wait_for_connection(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.info("tonconnect.connect_timeout", order_id=order_id)
            return False
        connected = session.tc.connected and not isinstance(result, Exception)
        logger.info("tonconnect.connect_result", order_id=order_id, connected=connected)
        return connected

    async def send_payment(
        self,
        order_id: str,
        *,
        asset: Asset,
        pay_to_address: str,
        amount: Decimal,
        amount_units: int,
        comment: str,
    ) -> PaymentOutcome:
        """
        Собрать сообщение под актив заказа и попросить подключённый кошелёк
        его подписать. Ничего не пишет в БД и не меняет статус заказа —
        это по-прежнему решает только TonConsole.
        """
        session = self._sessions.get(order_id)
        if session is None or not session.tc.connected:
            return PaymentOutcome(ok=False, message="Кошелёк не подключён.")

        customer_address = session.tc.account.address

        # Проверяем баланс ДО запроса подписи — иначе клиент вместо понятного
        # «на кошельке нет денег» видит невнятную ошибку SDK (see: реальный
        # тест показал ровно это на BadRequestError при нулевом USDT).
        shortage = await self._check_customer_balance(
            customer_address=customer_address, asset=asset, amount=amount, amount_units=amount_units,
        )
        if shortage is not None:
            logger.info("tonconnect.insufficient_balance", order_id=order_id, asset=asset.value)
            return PaymentOutcome(ok=False, message=shortage)

        try:
            if asset is Asset.ton:
                message = self._build_ton_message(pay_to_address, amount_units, comment)
            else:
                message = await self._build_jetton_message(
                    customer_address=customer_address,
                    pay_to_address=pay_to_address,
                    amount_units=amount_units,
                    comment=comment,
                )

            result = await session.tc.send_transaction(
                {
                    "valid_until": int(time.time()) + 300,
                    "messages": [message],
                }
            )
            logger.info("tonconnect.tx_sent", order_id=order_id, asset=asset.value)
            return PaymentOutcome(
                ok=True,
                message=(
                    "Перевод отправлен и подтверждён в кошельке ✅\n"
                    "Ждём, пока сеть его проведёт — обычно это секунды. "
                    "Заказ выполнится автоматически, ничего нажимать не нужно."
                ),
                tx_boc=result.get("boc"),
            )
        except UserRejectsError:
            logger.info("tonconnect.rejected", order_id=order_id)
            return PaymentOutcome(
                ok=False,
                message="Перевод отклонён в кошельке. Можно попробовать ещё раз или оплатить другим способом.",
            )
        except TonConnectError as exc:
            logger.warning("tonconnect.send_failed", order_id=order_id, error=type(exc).__name__)
            return PaymentOutcome(
                ok=False,
                message=f"Кошелёк отклонил перевод: {exc}. Попробуй другой способ оплаты.",
            )
        except Exception as exc:  # noqa: BLE001 - клиенту нужен понятный ответ в любом случае
            logger.exception("tonconnect.send_unexpected", order_id=order_id)
            return PaymentOutcome(
                ok=False,
                message=f"Не удалось отправить перевод через кошелёк: {exc}. Попробуй другой способ оплаты.",
            )

    def _make_client(self) -> TonapiClient | ToncenterClient:
        return make_ton_client(api_key=self._tonapi_key, api_provider=self._api_provider)

    async def _check_customer_balance(
        self,
        *,
        customer_address: str,
        asset: Asset,
        amount: Decimal,
        amount_units: int,
    ) -> str | None:
        """
        Прочитать баланс клиента ДО того, как просить кошелёк подписать
        заведомо обречённую транзакцию.

        Возвращает None, если денег хватает (или баланс не удалось прочитать —
        в этом случае не блокируем, пусть решает сам кошелёк), иначе — готовый
        текст для клиента.
        """
        try:
            async with self._make_client() as ton:
                ton_info = await ton.get_info(Address(customer_address))

                if ton_info.balance <= 0:
                    return (
                        "На этом кошельке нет TON — даже перевод USDT не пройдёт без газа "
                        "на комиссию сети. Пополни кошелёк или выбери другой способ оплаты."
                    )

                if asset is Asset.ton:
                    if ton_info.balance < amount_units:
                        return (
                            f"На этом кошельке недостаточно TON: нужно {format_amount(amount, Asset.ton)}. "
                            "Пополни кошелёк или выбери другой способ оплаты."
                        )
                    return None

                # USDT: TON здесь только на газ, сама сумма — в джеттон-кошельке.
                if ton_info.balance < _JETTON_TRANSFER_GAS:
                    return (
                        "На газ для перевода USDT не хватает TON на этом кошельке. "
                        "Пополни его (немного TON, обычно достаточно 0.05) или выбери другой способ."
                    )

                try:
                    jetton_wallet = await get_wallet_address_get_method(
                        client=ton,
                        address=USDT_TON_JETTON_MASTER,
                        owner_address=Address(customer_address),
                    )
                    wallet_data = await get_wallet_data_get_method(client=ton, address=jetton_wallet)
                    usdt_balance_units = int(wallet_data[0])
                except Exception:
                    # Контракт джеттон-кошелька ещё не задеплоен — значит USDT там никогда не было.
                    usdt_balance_units = 0

                if usdt_balance_units < amount_units:
                    return (
                        "На этом кошельке нет USDT (TON) — либо переведи их туда заранее, "
                        "либо оплати заказ в TON, либо другим способом (например, Tonkeeper)."
                    )
                return None
        except Exception as exc:  # noqa: BLE001 - не блокируем оплату из-за сбоя самой проверки
            logger.warning("tonconnect.balance_check_failed", error=type(exc).__name__)
            return None

    def _build_ton_message(self, pay_to_address: str, amount_units: int, comment: str) -> dict[str, Any]:
        return build_ton_message(pay_to_address, amount_units, comment)

    async def _build_jetton_message(
        self,
        *,
        customer_address: str,
        pay_to_address: str,
        amount_units: int,
        comment: str,
    ) -> dict[str, Any]:
        return await build_jetton_message(
            customer_address=customer_address,
            pay_to_address=pay_to_address,
            amount_units=amount_units,
            comment=comment,
            api_key=self._tonapi_key,
            api_provider=self._api_provider,
        )

    def start_payment_flow(
        self,
        order_id: str,
        *,
        bot: Bot,
        telegram_user_id: int,
        asset: Asset,
        pay_to_address: str,
        amount: Decimal,
        amount_units: int,
        comment: str,
    ) -> None:
        """Запустить run_payment_flow в фоне, с защитой от сборщика мусора."""
        task = asyncio.create_task(
            self.run_payment_flow(
                order_id,
                bot=bot,
                telegram_user_id=telegram_user_id,
                asset=asset,
                pay_to_address=pay_to_address,
                amount=amount,
                amount_units=amount_units,
                comment=comment,
            ),
            name=f"tonconnect-{order_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def run_payment_flow(
        self,
        order_id: str,
        *,
        bot: Bot,
        telegram_user_id: int,
        asset: Asset,
        pay_to_address: str,
        amount: Decimal,
        amount_units: int,
        comment: str,
    ) -> None:
        """
        Фоновая часть: дождаться подключения кошелька и сразу запросить перевод.
        Результат — только сообщение клиенту, статус заказа не трогаем.
        """
        try:
            connected = await self.wait_for_connection(order_id)
            if not connected:
                await bot.send_message(
                    telegram_user_id,
                    "Не дождались подключения кошелька Telegram. Можно попробовать ещё раз "
                    "или оплатить другим способом — счёт всё ещё действителен.",
                )
                return

            outcome = await self.send_payment(
                order_id,
                asset=asset,
                pay_to_address=pay_to_address,
                amount=amount,
                amount_units=amount_units,
                comment=comment,
            )
            await bot.send_message(telegram_user_id, outcome.message)
        except Exception:  # noqa: BLE001 - фоновая задача не должна падать молча
            logger.exception("tonconnect.flow_failed", order_id=order_id)
            try:
                await bot.send_message(
                    telegram_user_id,
                    "Что-то пошло не так с оплатой через Wallet Telegram. "
                    "Попробуй другой способ оплаты — счёт всё ещё действителен.",
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._sessions.pop(order_id, None)
