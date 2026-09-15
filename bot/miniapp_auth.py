"""
Проверка initData из Telegram Mini App.

Единственное, что отличает «запрос пришёл из Mini App от конкретного
пользователя Telegram» от «кто угодно постучался в наш API» — подпись,
которой Telegram заверяет initData. Без этой проверки любой мог бы прислать
чужой telegram_user_id и оформлять заказы от чужого имени.

Алгоритм (документация Telegram, Validating data received via Mini App):
  1. Разобрать initData как query string.
  2. Вынуть из него hash — это и есть подпись, её в проверяемую строку не кладём.
  3. Оставшиеся пары отсортировать по ключу и склеить как "k=v\\n".
  4. Секрет = HMAC-SHA256(ключ="WebAppData", данные=bot_token).
  5. Ожидаемая подпись = HMAC-SHA256(ключ=секрет, данные=строка из п.3).
  6. Сравнить с hash в постоянном времени.

Плюс проверяем auth_date: подписанный, но старый initData — это перехваченный
токен доступа, и жить он должен недолго.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

__all__ = ["MiniAppUser", "InitDataError", "parse_init_data"]

#: Сколько initData считается свежим. Telegram не задаёт это жёстко, сутки —
#: разумный компромисс: дольше живёт открытая вкладка Mini App, но украденный
#: initData не работает вечно.
MAX_AUTH_AGE_SECONDS = 24 * 60 * 60


class InitDataError(Exception):
    """initData отсутствует, подделан или протух. Наружу — всегда 401, без подробностей."""


@dataclass(frozen=True)
class MiniAppUser:
    """Пользователь, чью личность подтвердила подпись Telegram."""

    id: int
    username: str | None = None
    first_name: str | None = None
    language_code: str | None = None


def parse_init_data(
    init_data: str,
    *,
    bot_token: str,
    max_age_seconds: int = MAX_AUTH_AGE_SECONDS,
) -> MiniAppUser:
    """
    Проверить подпись и вернуть пользователя. Любая проблема — InitDataError.

    Возвращённому id можно доверять ровно настолько, насколько мы доверяем
    секретности bot_token: подделать его, не зная токена, нельзя.
    """
    if not init_data:
        raise InitDataError("пустой initData")

    # strict_parsing: мусор вместо query string не должен молча стать пустым dict.
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError as exc:
        raise InitDataError("initData не разбирается как query string") from exc

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataError("в initData нет hash")

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise InitDataError("подпись initData не сходится")

    # Подпись верна — теперь свежесть.
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise InitDataError("некорректный auth_date") from exc
    if auth_date <= 0:
        raise InitDataError("в initData нет auth_date")

    age = time.time() - auth_date
    if age > max_age_seconds:
        raise InitDataError("initData протух")
    # Заметный сдвиг в будущее — тоже повод отказать: либо часы врут, либо
    # кто-то играет со значениями.
    if age < -300:
        raise InitDataError("auth_date из будущего")

    raw_user = pairs.get("user")
    if not raw_user:
        raise InitDataError("в initData нет пользователя")
    try:
        user = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise InitDataError("поле user не разбирается") from exc

    user_id = user.get("id")
    if not isinstance(user_id, int):
        raise InitDataError("некорректный id пользователя")

    return MiniAppUser(
        id=user_id,
        username=user.get("username"),
        first_name=user.get("first_name"),
        language_code=user.get("language_code"),
    )
