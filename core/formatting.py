"""
Форматування цін токенів для показу користувачу в чаті.

Проблема, яку вирішує: наївне фіксоване форматування типу f"${price:.2f}"
для типової memecoin-ціни з 5-10 нулями після коми (напр. $0.0000001234)
покаже просто "$0.00" — усі значущі цифри губляться. format_price_usd()
нижче показує звичайні 2-4 знаки для "нормальних" цін (≥ $0.01), а для
менших — досить знаків після коми, щоб було видно перші ~4 значущі
(ненульові) цифри, незалежно від того, скільки нулів їм передує.

ВАЖЛИВО: це стосується ЛИШЕ показу користувачу. Внутрішні розрахунки
(entry_price, pct_change у core/position_monitor.py) працюють з "сирим"
Python float (SQLAlchemy Float = 8-байтний double) і НІКОЛИ не проходять
через цю функцію — форматування рядка тут не впливає на точність порівнянь.
"""
import math


def format_price_usd(price: float) -> str:
    """
    ≥ $0.01  → звичайні 2-4 знаки після коми (з відсіканням зайвих нулів,
               але не менше 2 знаків) — як і було раніше для "нормальних" цін.
    < $0.01  → рахує позицію першої значущої (ненульової) цифри після коми
               через floor(log10(|price|)) і показує ще 3 цифри після неї
               (разом 4 значущі цифри) — без переходу в наукову нотацію,
               щоб було зрозуміло для користувача без чату.
    """
    if price is None:
        return "н/д"
    if price == 0:
        return "$0.00"

    sign = "-" if price < 0 else ""
    abs_price = abs(price)

    if abs_price >= 0.01:
        s = f"{abs_price:,.4f}".rstrip("0").rstrip(".")
        if "." not in s:
            s += ".00"
        else:
            int_part, dec_part = s.split(".")
            if len(dec_part) < 2:
                dec_part = dec_part.ljust(2, "0")
            s = f"{int_part}.{dec_part}"
        return f"{sign}${s}"

    exponent = math.floor(math.log10(abs_price))
    decimals = -exponent + 3  # 4 значущі цифри, рахуючи від першої ненульової
    return f"{sign}${abs_price:.{decimals}f}"


def display_token_symbol(token_symbol: "str | None", contract_address: "str | None" = None) -> str:
    """
    token_symbol часто None: SignalParser (core/signal_parser.py) повертає
    його лише якщо тікер ЯВНО згаданий у тексті сигналу — канал зазвичай
    пише лише капу+адресу, без тікера, тому None тут ОЧІКУВАНА поведінка
    LLM, а не баг парсера. Замість "(None)" в /history, /статистика й
    сповіщеннях — показуємо скорочену адресу контракту як читабельний
    fallback (перші 4 + останні 4 символи).
    """
    if token_symbol:
        return token_symbol
    if contract_address and len(contract_address) > 10:
        return f"{contract_address[:4]}...{contract_address[-4:]}"
    return contract_address or "?"
