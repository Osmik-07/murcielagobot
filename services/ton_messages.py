"""
Сборка сообщений для TON Connect — общая для бота и Mini App.

Вынесено в отдельный модуль намеренно: перевод денег собирается ровно в одном
месте. Раньше это жило приватными методами внутри TonConnectGateway, и когда
появился второй потребитель (Mini App, где кошелёк подключается в браузере,
а не через питоновский мост), копировать эту логику было бы худшим из
вариантов — расхождение между копиями означает перевод не туда или не столько.

Про суммы. У нативного TON и у джеттона поле amount значит РАЗНОЕ:
  * TON: amount — это и есть переводимая сумма (в наноTON);
  * USDT: amount — это TON на газ, а сама сумма джеттонов лежит в payload.
Перепутать их нельзя — отсюда два отдельных строителя, а не один с флагом.
"""

from __future__ import annotations

import base64
from typing import Any

from ton_core import Address, JettonTransferBody, NetworkGlobalID, TextCommentBody, to_nano
from tonutils.clients import TonapiClient, ToncenterClient
from tonutils.contracts.jetton.methods import get_wallet_address_get_method

__all__ = [
    "USDT_TON_JETTON_MASTER",
    "JETTON_TRANSFER_GAS",
    "make_ton_client",
    "build_ton_message",
    "build_jetton_message",
]

#: Мастер-контракт USDT в сети TON. То же значение используют FragmentAPI
#: (чтение баланса) и ton://-ссылка в боте. Расхождение здесь = перевод не туда.
USDT_TON_JETTON_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"

#: Газ на джеттон-перевод: комиссия сети плюс forward-уведомление получателю.
#: Списывается с TON-баланса отправителя СВЕРХ суммы USDT.
JETTON_TRANSFER_GAS = to_nano("0.05")


def make_ton_client(*, api_key: str, api_provider: str = "tonapi") -> TonapiClient | ToncenterClient:
    """Клиент к TON API. Используется только для чтения состояния сети."""
    client_cls = ToncenterClient if api_provider == "toncenter" else TonapiClient
    return client_cls(network=NetworkGlobalID.MAINNET, api_key=api_key)


def build_ton_message(pay_to_address: str, amount_units: int, comment: str) -> dict[str, Any]:
    """
    Перевод нативного TON: адресуется прямо получателю, amount — сумма в наноTON.
    Комментарий (id заказа) уходит текстовой ячейкой — по нему платёж видно в сети.
    """
    message: dict[str, Any] = {"address": pay_to_address, "amount": str(amount_units)}
    if comment:
        body = TextCommentBody(comment).serialize()
        message["payload"] = base64.b64encode(body.to_boc()).decode()
    return message


async def build_jetton_message(
    *,
    customer_address: str,
    pay_to_address: str,
    amount_units: int,
    comment: str,
    api_key: str,
    api_provider: str = "tonapi",
) -> dict[str, Any]:
    """
    Перевод USDT (TON).

    Сообщение адресуется НЕ получателю, а джеттон-кошельку САМОГО отправителя —
    это он по команде владельца пересылает джеттоны дальше. Адрес этого
    джеттон-кошелька вычисляется детерминированно view-запросом к мастер-
    контракту: приватный ключ клиента для этого не нужен, это просто чтение.
    """
    async with make_ton_client(api_key=api_key, api_provider=api_provider) as ton:
        sender_jetton_wallet = await get_wallet_address_get_method(
            client=ton,
            address=USDT_TON_JETTON_MASTER,
            owner_address=Address(customer_address),
        )

    forward_payload = TextCommentBody(comment).serialize() if comment else None
    body = JettonTransferBody(
        destination=Address(pay_to_address),
        jetton_amount=amount_units,
        # Сдача с газа возвращается отправителю, а не оседает у нас.
        response_address=Address(customer_address),
        # 1 наноTON, чтобы получателю ушло уведомление с комментарием.
        forward_amount=1,
        forward_payload=forward_payload,
    ).serialize()

    return {
        "address": str(sender_jetton_wallet),
        "amount": str(JETTON_TRANSFER_GAS),
        "payload": base64.b64encode(body.to_boc()).decode(),
    }
