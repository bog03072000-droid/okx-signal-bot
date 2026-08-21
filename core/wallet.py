"""
Запит балансу гаманця. Реальний баланс SOL береться через Solana RPC (безкоштовно,
не потребує окремого API-ключа); курс SOL/USD — через публічний CoinGecko endpoint.

СКЕПТИЧНИЙ КОМЕНТАР: якщо SOLANA_PRIVATE_KEY не задано (типово для DRY_RUN=true
без підключеного гаманця), реальний баланс отримати нема звідки — повертаємо
MOCK_WALLET_BALANCE_USD (той самий, що main.py використовує для розрахунку
розміру позиції в dry-run — імпортується звідси, щоб не розходились). Це
означає, що /balance в dry-run без ключа показує вигадану цифру, а не реальний гаманець.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

COINGECKO_SOL_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"

# TODO: замінити на реальний баланс гаманця (запит через RPC) для dry-run без ключа.
# Єдине джерело правди для main.py і control_bot.py — обидва імпортують звідси.
MOCK_WALLET_BALANCE_USD = 1000.0


@dataclass
class WalletBalance:
    sol_balance: Optional[float]
    usd_estimate: float
    is_real: bool
    note: str = ""


def _fetch_sol_usd_price() -> Optional[float]:
    try:
        resp = httpx.get(COINGECKO_SOL_PRICE_URL, timeout=8.0)
        resp.raise_for_status()
        return float(resp.json()["solana"]["usd"])
    except Exception as e:
        logger.warning(f"Не вдалось отримати курс SOL/USD з CoinGecko: {e}")
        return None


def get_wallet_balance() -> WalletBalance:
    """
    Реальний баланс — тільки якщо задано SOLANA_PRIVATE_KEY (звідки береться публічний
    ключ гаманця). Інакше — mock-баланс з приміткою, що це не реальні кошти.
    """
    if not settings.solana_private_key:
        return WalletBalance(
            sol_balance=None,
            usd_estimate=MOCK_WALLET_BALANCE_USD,
            is_real=False,
            note="SOLANA_PRIVATE_KEY не задано — показано mock-баланс для dry-run, це НЕ реальні кошти.",
        )

    try:
        # BaseException, НЕ Exception: solders — обгортка над Rust-кодом (pyo3), і
        # Keypair.from_base58_string() на невалідному ключі кидає pyo3_runtime.PanicException,
        # який успадковується від BaseException, а не Exception. Звичайний
        # "except Exception" його НЕ ловить — і без цього кривий SOLANA_PRIVATE_KEY
        # впав би необробленою помилкою через увесь спільний asyncio event loop
        # (main.py) і поклав би весь процес, а не тільки цю команду.
        from solders.keypair import Keypair
        from solana.rpc.api import Client

        keypair = Keypair.from_base58_string(settings.solana_private_key)
        rpc_client = Client(settings.solana_rpc_url)
        resp = rpc_client.get_balance(keypair.pubkey())
        lamports = resp.value
        sol_balance = lamports / 1_000_000_000

        sol_price = _fetch_sol_usd_price()
        if sol_price is None:
            return WalletBalance(
                sol_balance=sol_balance,
                usd_estimate=0.0,
                is_real=True,
                note="Не вдалось отримати курс SOL/USD (CoinGecko недоступний) — USD-еквівалент невідомий.",
            )

        return WalletBalance(
            sol_balance=sol_balance,
            usd_estimate=sol_balance * sol_price,
            is_real=True,
            note="Баланс враховує лише нативний SOL, БЕЗ SPL-токенів відкритих позицій.",
        )
    except BaseException as e:
        # Навмисно НЕ логуємо/показуємо settings.solana_private_key тут і ніде вище —
        # лише текст помилки {e}, який (перевірено) не містить самого ключа навіть
        # при PanicException на невалідному base58.
        logger.error(f"Помилка запиту балансу гаманця через RPC: {type(e).__name__}: {e}")
        return WalletBalance(
            sol_balance=None,
            usd_estimate=MOCK_WALLET_BALANCE_USD,
            is_real=False,
            note=f"Помилка запиту реального балансу ({type(e).__name__}) — показано mock-баланс.",
        )
