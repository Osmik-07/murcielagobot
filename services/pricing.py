"""
Ценообразование и работа с суммами.

Три правила, которые здесь зашиты:

1. Никаких float. Только Decimal — на всём пути от цены Fragment до строки,
   которую мы отдаём в Payment Tracker. float для денег даёт «12.299999999».

2. Цена берётся у Fragment на лету, ничего не хардкодим. Заказ в TON считаем
   от gram_price (Fragment называет нативный TON «GRAM»), заказ в USDT — от
   usd_price, потому что USDT это долларовый стейбл. Дальше сверху наценка.

3. У каждого инвойса УНИКАЛЬНЫЙ ХВОСТ в младших единицах. Это не украшение:
   Payment Tracker выдаёт один и тот же pay_to_address на все инвойсы сразу,
   и единственное, чем два одновременных инвойса отличаются друг от друга, —
   это точная сумма. Без хвоста два клиента, купившие одно и то же в одну
   минуту, схлопнутся в один платёж, и кто-то останется без товара.

Хвост добавляется ПОСЛЕ наценки и всегда в плюс: сумма к оплате может быть
на копейку больше расчётной, но никогда меньше.
"""

import re
import secrets
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal, InvalidOperation, localcontext

from db.models import Asset

# Точность для промежуточных вычислений: с запасом, чтобы quantize никогда
# не упёрся в предел контекста.
_CALC_PRECISION = 50


@dataclass(frozen=True)
class AssetSpec:
    """Всё, что отличает один способ оплаты от другого."""

    decimals: int                  # знаков после запятой у актива
    label: str                     # как показываем пользователю
    tracker_currency: str          # currency для Payment Tracker
    fragment_payment_method: str   # payment_method для FragmentClient
    tail_max_units: int            # максимальный уникальный хвост в младших единицах

    @property
    def quantum(self) -> Decimal:
        """Наименьшая единица как Decimal: 1E-9 для TON, 1E-6 для USDT."""
        return Decimal(1).scaleb(-self.decimals)


# TON: 1e9 наноТОН в монете, хвост до 0.000999999 TON — меньше цента.
# USDT: 1e6 микроединиц, хвост до 0.009999 USDT — около цента.
# Обе величины дают десятки тысяч различимых сумм: коллизия параллельных
# инвойсов с одинаковой базовой ценой практически исключена.
ASSET_SPECS: dict[Asset, AssetSpec] = {
    Asset.ton: AssetSpec(
        decimals=9,
        label="TON",
        tracker_currency="GRAM",
        fragment_payment_method="ton",
        tail_max_units=999_999,
    ),
    Asset.usdt_ton: AssetSpec(
        decimals=6,
        label="USDT",
        tracker_currency="USDT",
        fragment_payment_method="usdt_ton",
        tail_max_units=9_999,
    ),
}

# Совместимость со старым кодом.
TAIL_MAX_NANOTON = ASSET_SPECS[Asset.ton].tail_max_units
TAIL_MAX_MICRO_USDT = ASSET_SPECS[Asset.usdt_ton].tail_max_units


def coerce_asset(asset: "Asset | str") -> Asset:
    """
    Привести значение к Asset. Любой пришедший извне текст (callback_data,
    тело вебхука) враждебен: неизвестное значение — это ошибка, а не догадка.
    """
    if isinstance(asset, Asset):
        return asset
    try:
        return Asset(str(asset).strip().lower())
    except ValueError:
        raise ValueError(f"Неизвестный способ оплаты: {asset!r}") from None


def spec_for(asset: "Asset | str") -> AssetSpec:
    return ASSET_SPECS[coerce_asset(asset)]


def tracker_currency(asset: "Asset | str") -> str:
    """currency для тела инвойса Payment Tracker: GRAM или USDT."""
    return spec_for(asset).tracker_currency


def fragment_payment_method(asset: "Asset | str") -> str:
    """payment_method для purchase_stars/purchase_premium: 'ton' или 'usdt_ton'."""
    return spec_for(asset).fragment_payment_method


# --------------------------------------------------------------------------- цена


# «1,234.56» — запятая как разделитель тысяч. Отличаем от «12,34», где запятая
# десятичная: перепутать эти два случая значит ошибиться в цене в 100 раз.
_THOUSANDS_SEPARATED = re.compile(r"^\d{1,3}(,\d{3})+$")


def parse_price(raw: "str | Decimal | int") -> Decimal:
    """
    Fragment отдаёт gram_price/usd_price СТРОКАМИ. Разбираем их в Decimal,
    не превращая по дороге во float, и терпимо относимся к пробелам,
    неразрывным пробелам и разделителям тысяч.
    """
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, int):
        value = Decimal(raw)
    else:
        cleaned = str(raw).strip().replace(" ", "").replace(" ", "").replace(" ", "")
        if "," in cleaned:
            if "." in cleaned or _THOUSANDS_SEPARATED.match(cleaned):
                cleaned = cleaned.replace(",", "")   # запятая = разделитель тысяч
            else:
                cleaned = cleaned.replace(",", ".")  # запятая = десятичный разделитель
        try:
            value = Decimal(cleaned)
        except InvalidOperation:
            raise ValueError(f"Не удалось разобрать цену: {raw!r}") from None
    if not value.is_finite() or value <= 0:
        raise ValueError(f"Некорректная цена: {raw!r}")
    return value


def select_base_price(
    asset: "Asset | str",
    gram_price: "str | Decimal",
    usd_price: "str | Decimal",
) -> Decimal:
    """
    Выбрать базу для расчёта под способ оплаты:
    TON — по gram_price (цена Fragment в нативном TON),
    USDT — по usd_price (USDT привязан к доллару).
    """
    resolved = coerce_asset(asset)
    return parse_price(gram_price if resolved is Asset.ton else usd_price)


def apply_margin(base_price: Decimal, margin_percent: Decimal) -> Decimal:
    """Цена с наценкой, без квантования и без хвоста."""
    if base_price <= 0:
        raise ValueError("Базовая цена должна быть положительной")
    if margin_percent < 0:
        raise ValueError("Наценка не может быть отрицательной")
    with localcontext() as ctx:
        ctx.prec = _CALC_PRECISION
        return base_price * (Decimal(1) + margin_percent / Decimal(100))


def quantize_amount(amount: Decimal, asset: "Asset | str") -> Decimal:
    """
    Округлить до наименьшей единицы актива: 9 знаков для TON, 6 для USDT.
    Округляем ВВЕРХ — лучше взять на одну нано больше, чем недобрать.
    """
    spec = spec_for(asset)
    if amount < 0:
        raise ValueError("Сумма не может быть отрицательной")
    with localcontext() as ctx:
        ctx.prec = _CALC_PRECISION
        return amount.quantize(spec.quantum, rounding=ROUND_UP)


def unique_tail_units(asset: "Asset | str") -> int:
    """Случайный хвост в младших единицах: 1..tail_max_units."""
    spec = spec_for(asset)
    return secrets.randbelow(spec.tail_max_units) + 1


def unique_tail(asset: "Asset | str") -> Decimal:
    """Тот же хвост, но уже как Decimal в единицах актива."""
    spec = spec_for(asset)
    return Decimal(unique_tail_units(asset)).scaleb(-spec.decimals)


def unique_tail_nanoton() -> int:
    """Совместимость со старым кодом: хвост для TON в наноТОН."""
    return unique_tail_units(Asset.ton)


def calculate_invoice_amount(
    base_price: Decimal,
    margin_percent: Decimal,
    asset: "Asset | str",
) -> Decimal:
    """
    Итоговая сумма к оплате.

    base_price — цена Fragment в валюте выбранного актива (см. select_base_price):
    для TON в TON, для USDT в долларах.

    Порядок важен: наценка -> квантование вверх -> плюс целое число младших
    единиц хвоста. Так хвост остаётся ровно тем, что мы задумали, и его не
    съедает округление.
    """
    priced = apply_margin(base_price, margin_percent)
    quantized = quantize_amount(priced, asset)
    with localcontext() as ctx:
        ctx.prec = _CALC_PRECISION
        return quantized + unique_tail(asset)


# ------------------------------------------------------------------ минимальные единицы


def to_minimal_units(amount: Decimal, asset: "Asset | str") -> int:
    """
    Перевести сумму в минимальные неделимые единицы (наноТОН / микро-USDT).
    Именно это ждёт поле amount у Payment Tracker и параметр amount у ton://.
    """
    spec = spec_for(asset)
    with localcontext() as ctx:
        ctx.prec = _CALC_PRECISION
        return int(quantize_amount(amount, asset).scaleb(spec.decimals).to_integral_exact())


def minimal_units_str(amount: Decimal, asset: "Asset | str") -> str:
    """То же самое строкой — Payment Tracker принимает amount строкой."""
    return str(to_minimal_units(amount, asset))


def from_minimal_units(units: "int | str", asset: "Asset | str") -> Decimal:
    """
    Обратное преобразование: строка минимальных единиц из ответа трекера
    (amount, overpayment) -> Decimal в единицах актива.
    """
    spec = spec_for(asset)
    try:
        raw = int(str(units).strip())
    except ValueError:
        raise ValueError(f"Не удалось разобрать сумму в минимальных единицах: {units!r}") from None
    with localcontext() as ctx:
        ctx.prec = _CALC_PRECISION
        return Decimal(raw).scaleb(-spec.decimals)


def ton_to_nanoton(amount_ton: Decimal) -> int:
    return to_minimal_units(amount_ton, Asset.ton)


def nanoton_to_ton(amount_nano: "int | str") -> Decimal:
    return from_minimal_units(amount_nano, Asset.ton)


def usdt_to_micro(amount_usdt: Decimal) -> int:
    return to_minimal_units(amount_usdt, Asset.usdt_ton)


def micro_to_usdt(amount_micro: "int | str") -> Decimal:
    return from_minimal_units(amount_micro, Asset.usdt_ton)


# ------------------------------------------------------------------------ сверка сумм


def is_paid_enough(paid: Decimal, expected: Decimal, asset: "Asset | str") -> bool:
    """
    Хватает ли пришедших денег. Сравниваем в целых минимальных единицах —
    Decimal-сравнение «на глаз» на разных масштабах ошибается.
    """
    return to_minimal_units(paid, asset) >= to_minimal_units(expected, asset)


def overpayment_amount(paid: Decimal, expected: Decimal, asset: "Asset | str") -> Decimal:
    """
    Сколько клиент переплатил (0, если не переплатил). Переплату нужно
    показать админу: у трекера это отдельное поле, которое бывает непустым
    даже при статусе expired/cancelled — деньги пришли поздно.
    """
    diff = to_minimal_units(paid, asset) - to_minimal_units(expected, asset)
    if diff <= 0:
        return Decimal(0)
    return from_minimal_units(diff, asset)


def format_amount(amount: Decimal, asset: "Asset | str") -> str:
    """Человекочитаемая сумма для сообщений бота: «12.345 TON», «5.00 USDT»."""
    spec = spec_for(asset)
    text = f"{quantize_amount(amount, asset):f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    integer, _, fraction = text.partition(".")
    # Меньше двух знаков после запятой выглядит как ошибка в цене.
    return f"{integer}.{fraction.ljust(2, '0')} {spec.label}"
