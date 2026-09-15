"""
Клавиатуры бота.

Осталась одна: главное меню. Весь процесс покупки — выбор срока или
количества, получатель, способ оплаты и сама оплата — живёт в Mini App,
поэтому клавиатур под диалог больше нет.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from bot.config import settings


def main_menu_kb() -> InlineKeyboardMarkup:
    """
    Оба пункта открывают Mini App сразу на нужном товаре.

    Если MINIAPP_URL не задан (локальная разработка без собранного фронтенда),
    кнопок нет: диалогового запасного пути больше не существует, и рисовать
    кнопку, которая никуда не ведёт, хуже, чем честно её не показать.
    """
    if not settings.miniapp_enabled:
        return InlineKeyboardMarkup(inline_keyboard=[])

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Telegram Premium",
                    web_app=WebAppInfo(url=settings.miniapp_product_url("premium")),
                )
            ],
            [
                InlineKeyboardButton(
                    text="✨ Telegram Stars",
                    web_app=WebAppInfo(url=settings.miniapp_product_url("stars")),
                )
            ],
        ]
    )
