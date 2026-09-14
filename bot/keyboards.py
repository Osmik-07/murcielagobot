"""Клавиатуры бота."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Telegram Premium", callback_data="product:premium")],
            [InlineKeyboardButton(text="✨ Telegram Stars", callback_data="product:stars")],
        ]
    )


def premium_duration_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="3 месяца", callback_data="duration:3"),
                InlineKeyboardButton(text="6 месяцев", callback_data="duration:6"),
                InlineKeyboardButton(text="12 месяцев", callback_data="duration:12"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:menu")],
        ]
    )


def asset_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="TON", callback_data="asset:ton"),
                InlineKeyboardButton(text="USDT (TON)", callback_data="asset:usdt_ton"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:menu")],
        ]
    )


def pay_link_kb(deep_link: str | None) -> InlineKeyboardMarkup:
    """
    Клавиатура под счётом.

    Ссылка на оплату опциональна: для USDT трекер может не отдать deep-link,
    и тогда клиент платит по адресу и сумме из текста сообщения. Кнопка
    «я оплатил» остаётся в любом случае — она безопасна, повторные нажатия
    не приводят к повторной выдаче.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if deep_link:
        rows.append([InlineKeyboardButton(text="💳 Оплатить", url=deep_link)])
    rows.append([InlineKeyboardButton(text="✅ Я оплатил", callback_data="check_payment")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
