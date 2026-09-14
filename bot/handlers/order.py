"""
Сбор заказа: товар -> срок/количество -> получатель -> способ оплаты.

Здесь всё, что приходит от клиента, считается враждебным. Особенно callback_data:
Telegram НЕ привязывает её к той клавиатуре, которую мы отправили, и любой
клиент может прислать произвольную строку в callback_query. Поэтому каждое
разобранное значение сверяется с белым списком, а не просто парсится.
"""

from __future__ import annotations

import re

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.config import settings
from bot.keyboards import asset_choice_kb, premium_duration_kb

logger = structlog.get_logger(__name__)

router = Router(name="order")

#: Сроки Premium, которые реально продаёт Fragment (PREMIUM_MONTHS_VALID).
ALLOWED_MONTHS: frozenset[int] = frozenset({3, 6, 12})

#: Правило то же, что в services.fragment_gateway: 4..32 символа, начинается
#: с буквы, заканчивается буквой или цифрой. Держим одинаковым, чтобы не принять
#: заказ, который шлюз потом всё равно отвергнет — уже после оплаты.
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,30}[A-Za-z0-9]$")


async def _edit(callback: CallbackQuery, text: str, **kwargs) -> None:
    """
    Заменить текст сообщения под кнопкой.

    callback.message бывает None (сообщение старше суток — Telegram его уже
    не отдаёт), поэтому в таком случае просто отвечаем всплывашкой.
    """
    if callback.message is None:
        await callback.answer("Сообщение устарело, начни заново: /start", show_alert=True)
        return
    await callback.message.edit_text(text, **kwargs)


class OrderFSM(StatesGroup):
    choosing_duration = State()      # для Premium: 3/6/12 мес
    entering_amount = State()        # для Stars: количество
    entering_recipient = State()     # username получателя
    choosing_asset = State()         # TON или USDT (TON)
    awaiting_payment = State()       # счёт выставлен, ждём оплату


@router.callback_query(F.data == "product:premium")
async def choose_premium(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(product="premium", stars_amount=None)
    await state.set_state(OrderFSM.choosing_duration)
    await _edit(callback, "На какой срок?", reply_markup=premium_duration_kb())
    await callback.answer()


@router.callback_query(OrderFSM.choosing_duration, F.data.startswith("duration:"))
async def set_duration(callback: CallbackQuery, state: FSMContext) -> None:
    raw = (callback.data or "").split(":", 1)[-1]
    # Ни int(), ни срез без проверки: "duration:abc" уронил бы хендлер,
    # а "duration:999" уехал бы дальше как настоящий срок подписки.
    try:
        months = int(raw)
    except ValueError:
        await callback.answer("Некорректный срок", show_alert=True)
        return
    if months not in ALLOWED_MONTHS:
        await callback.answer("Такого срока нет", show_alert=True)
        return

    await state.update_data(duration_months=months)
    await state.set_state(OrderFSM.entering_recipient)
    await _edit(callback, "Кому дарим? Введи username получателя (без @):")
    await callback.answer()


@router.callback_query(F.data == "product:stars")
async def choose_stars(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(product="stars", duration_months=None)
    await state.set_state(OrderFSM.entering_amount)
    await _edit(
        callback,
        f"Сколько Stars нужно купить?\n"
        f"Введи число от {settings.stars_min_amount} до {settings.stars_max_amount}:",
    )
    await callback.answer()


@router.message(OrderFSM.entering_amount)
async def set_stars_amount(message: Message, state: FSMContext) -> None:
    # text может быть None: стикер, фото, голосовое.
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(
            f"Нужно целое число, например {max(settings.stars_min_amount, 100)}"
        )
        return

    amount = int(raw)
    if amount < settings.stars_min_amount:
        await message.answer(f"Минимум {settings.stars_min_amount} Stars")
        return
    if amount > settings.stars_max_amount:
        await message.answer(f"Максимум {settings.stars_max_amount} Stars за один заказ")
        return

    await state.update_data(stars_amount=amount)
    await state.set_state(OrderFSM.entering_recipient)
    await message.answer("Кому дарим? Введи username получателя (без @):")


@router.message(OrderFSM.entering_recipient)
async def set_recipient(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip().lstrip("@")
    if not USERNAME_RE.match(username):
        await message.answer(
            "Не похоже на username. Нужен ник получателя без @: "
            "латиница, цифры и подчёркивания, от 4 до 32 символов.\n"
            "Например: durov"
        )
        return

    await state.update_data(recipient=username)
    await state.set_state(OrderFSM.choosing_asset)
    await message.answer("В чём оплачиваешь?", reply_markup=asset_choice_kb())
