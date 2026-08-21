"""
Запит балансу гаманця. З переходом бота на USDT як торгову валюту тут
рахуються ДВА окремі баланси:
  - USDT (SPL-токен) — торговий капітал, саме ним бот купує/продає токени;
  - native SOL — НЕ торгова валюта, лишається потрібним виключно на комісії
    мережі (газ). Без SOL бот не зможе відправити жодну транзакцію, навіть
    маючи багато USDT.

SOL-баланс показується як є, в SOL, без конвертації в USD (він і не торговий
капітал — конвертувати резерв на газ у USD нема великого сенсу). USDT-баланс
теж НЕ множиться на жоден курс — USDT-сума й так є USD-еквівалентом за
визначенням стейблкоїна (див. застереження в main.py щодо можливого де-пегу).

СКЕПТИЧНИЙ КОМЕНТАР: якщо SOLANA_PRIVATE_KEY не задано (типово для DRY_RUN=true
без підключеного гаманця), реальний баланс отримати нема звідки — повертаємо
MOCK_WALLET_BALANCE_USD (той самий, що main.py використовує для розрахунку
розміру позиції в dry-run — імпортується звідси, щоб не розходились).
"""
import logging
from dataclasses import dataclass
from typing import Optional

from core.config import settings
from core.okx_dex_client import USDT_MINT_SOLANA

logger = logging.getLogger(__name__)

# TODO: використовується як fallback, коли реальний баланс USDT отримати нема
# звідки (немає ключа) або не вдалось (RPC-помилка в dry-run). Єдине джерело
# правди для main.py і control_bot.py — обидва імпортують звідси.
MOCK_WALLET_BALANCE_USD = 1000.0

# Нижче цього рівня SOL — попереджаємо, що скоро нічим буде платити за газ,
# навіть якщо USDT достатньо. 0.01 SOL — приблизно кілька десятків транзакцій
# з запасом при типовій комісії Solana (~0.000005-0.0001 SOL/tx), не точна
# наука — підбери під свою реальну активність.
MIN_SOL_FOR_GAS = 0.01


@dataclass
class WalletBalance:
    sol_balance: Optional[float]      # резерв на газ, native SOL
    usdt_balance: Optional[float]     # торговий капітал, ≈ USD (стейблкоїн)
    is_real: bool                     # False = mock/заглушка, не реальний гаманець
    low_gas_warning: bool = False
    note: str = ""


def _fetch_spl_token_balance(rpc_client, owner_pubkey, mint_address: str) -> float:
    """
    Баланс SPL-токена (у людських одиницях, з урахуванням decimals mint'а).

    Якщо у власника ще немає Associated Token Account для цього mint'а
    (типово для гаманця, який ще ЖОДНОГО разу не отримував цей токен) —
    RPC повертає не помилку-виняток, а відповідь без поля `value`
    (перевірено емпірично на mainnet-beta: InvalidParamsMessage
    "could not find account"). Це НЕ помилка мережі — просто баланс 0.
    Інші RPC-помилки (мережа, rate limit) проходять як виняток — їх ловить
    викликач у get_wallet_balance().
    """
    from spl.token.instructions import get_associated_token_address
    from solders.pubkey import Pubkey

    ata = get_associated_token_address(owner_pubkey, Pubkey.from_string(mint_address))
    resp = rpc_client.get_token_account_balance(ata)
    if not hasattr(resp, "value"):
        return 0.0
    ui_amount = resp.value.ui_amount
    return float(ui_amount) if ui_amount is not None else 0.0


def get_wallet_balance() -> WalletBalance:
    """
    Реальні баланси — тільки якщо задано SOLANA_PRIVATE_KEY (звідки береться
    публічний ключ гаманця). Інакше — mock USDT-баланс з приміткою, що це
    не реальні кошти.

    SOL і USDT запитуються НЕЗАЛЕЖНО один від одного: якщо один запит впаде
    (напр. rate limit на публічному RPC), це не повинно "стирати" вже успішно
    отримане значення другого — тому кожен обгорнутий в окремий try/except,
    а не один спільний блок на всю функцію.
    """
    if not settings.solana_private_key:
        return WalletBalance(
            sol_balance=None,
            usdt_balance=MOCK_WALLET_BALANCE_USD,
            is_real=False,
            low_gas_warning=False,
            note="SOLANA_PRIVATE_KEY не задано — показано mock USDT-баланс для dry-run, це НЕ реальні кошти.",
        )

    try:
        # BaseException, НЕ Exception: solders — обгортка над Rust-кодом (pyo3), і
        # Keypair.from_base58_string() на невалідному ключі кидає pyo3_runtime.PanicException,
        # який успадковується від BaseException, а не Exception.
        from solders.keypair import Keypair

        keypair = Keypair.from_base58_string(settings.solana_private_key)
        owner_pubkey = keypair.pubkey()
    except BaseException as e:
        logger.error(f"Некоректний SOLANA_PRIVATE_KEY: {type(e).__name__}: {e}")
        return WalletBalance(
            sol_balance=None,
            usdt_balance=MOCK_WALLET_BALANCE_USD,
            is_real=False,
            low_gas_warning=False,
            note=f"Некоректний SOLANA_PRIVATE_KEY ({type(e).__name__}) — показано mock-баланс.",
        )

    from solana.rpc.api import Client

    rpc_client = Client(settings.solana_rpc_url)
    notes = []

    sol_balance: Optional[float] = None
    try:
        resp = rpc_client.get_balance(owner_pubkey)
        sol_balance = resp.value / 1_000_000_000
    except Exception as e:
        logger.warning(f"Не вдалось отримати баланс SOL: {e}")
        notes.append("Не вдалось отримати баланс SOL (RPC недоступний/rate limit).")

    usdt_balance: Optional[float] = None
    try:
        usdt_balance = _fetch_spl_token_balance(rpc_client, owner_pubkey, USDT_MINT_SOLANA)
    except Exception as e:
        logger.warning(f"Не вдалось отримати баланс USDT: {e}")
        notes.append("Не вдалось отримати баланс USDT (RPC недоступний/rate limit).")

    low_gas_warning = sol_balance is not None and sol_balance < MIN_SOL_FOR_GAS
    if low_gas_warning:
        warning = (
            f"⛽ SOL на комісії закінчується ({sol_balance:.4f} SOL, поріг {MIN_SOL_FOR_GAS}) — "
            "навіть маючи багато USDT, бот не зможе відправити транзакцію без SOL на газ."
        )
        notes.append(warning)
        logger.warning(warning)

    return WalletBalance(
        sol_balance=sol_balance,
        usdt_balance=usdt_balance,
        is_real=True,
        low_gas_warning=low_gas_warning,
        note=" ".join(notes),
    )
