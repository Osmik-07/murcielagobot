"""
Приветствие и вход в Mini App.

Диалогового оформления заказа в боте больше нет — кнопки ведут в Mini App,
где и происходит вся покупка. За ботом остаются две вещи, которые Mini App
делать не может: первое касание (/start) и уведомления по заказу — их шлёт
services.fulfillment после того, как деньги реально пришли.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.config import settings
from bot.keyboards import main_menu_kb

router = Router(name="start")

WELCOME = (
    "Привет! Здесь можно купить Telegram Premium или Telegram Stars.\n"
    "Оплата напрямую в TON или USDT (сеть TON), без посредников.\n\n"
    "Что выбираем?"
)

#: Если фронтенд не задеплоен, честнее сказать об этом, чем показать меню
#: без кнопок.
UNAVAILABLE = (
    "Привет! Магазин сейчас недоступен — приложение не настроено.\nЗагляни чуть позже."
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not settings.miniapp_enabled:
        await message.answer(UNAVAILABLE)
        return
    await message.answer(WELCOME, reply_markup=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Покупка идёт в приложении: нажми /start и выбери товар.\n\n"
        "Оплата — переводом в TON или USDT (сеть TON) на наш кошелёк. Как только "
        "платёж подтвердится в сети, подарок уйдёт получателю автоматически, "
        "и я пришлю сюда подтверждение.\n\n"
        "Если что-то пошло не так — напиши, разберёмся вручную."
    )
