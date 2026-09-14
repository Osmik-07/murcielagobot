"""Приветствие и возврат в главное меню."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards import main_menu_kb

router = Router(name="start")

WELCOME = (
    "Привет! Здесь можно купить Telegram Premium или Telegram Stars.\n"
    "Оплата напрямую в TON или USDT (сеть TON), без посредников.\n\n"
    "Что выбираем?"
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    # Чистим состояние: незаконченный прошлый заказ не должен подмешиваться в новый.
    await state.clear()
    await message.answer(WELCOME, reply_markup=main_menu_kb())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменил. Начнём заново?", reply_markup=main_menu_kb())


@router.callback_query(F.data == "back:menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if callback.message is None:
        # Сообщение слишком старое и недоступно для редактирования.
        return
    await callback.message.edit_text("Что выбираем?", reply_markup=main_menu_kb())
