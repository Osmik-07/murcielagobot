"""Мидлвари. Пока одна: сессия БД на апдейт."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import async_sessionmaker


class DBSessionMiddleware(BaseMiddleware):
    """
    Одна сессия на апдейт. Коммитят сами функции репозитория (у них это часть
    контракта: claim обязан коммититься сразу, иначе атомарность перехода
    теряет смысл), поэтому здесь только выдача сессии и откат при исключении.
    """

    def __init__(self, session_maker: async_sessionmaker) -> None:
        self._session_maker = session_maker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._session_maker() as session:
            data["session"] = session
            try:
                return await handler(event, data)
            except Exception:
                await session.rollback()
                raise
