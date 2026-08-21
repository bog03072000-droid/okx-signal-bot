"""
Клієнт для OKX DEX (Web3) Aggregator API.
Документація: https://web3.okx.com/build/dev-docs/dex-api/dex-what-is-dex-api

ВАЖЛИВО: справжній підпис запитів OKX API вимагає HMAC-SHA256 підпису
(timestamp + method + path + body) — реалізовано нижче в _sign().
Перед реальним використанням звірся з актуальною документацією OKX,
вона періодично змінюється.
"""
import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

OKX_BASE_URL = "https://web3.okx.com"
SOLANA_CHAIN_ID = "501"  # ідентифікатор Solana в OKX DEX API

# Native SOL "адреса" за конвенцією OKX/Jupiter
SOL_NATIVE_ADDRESS = "11111111111111111111111111111111"


@dataclass
class QuoteResult:
    success: bool
    from_amount: Optional[str] = None
    to_amount: Optional[str] = None
    price_impact_pct: Optional[float] = None
    tx_data: Optional[dict] = None
    error: Optional[str] = None


@dataclass
class SwapResult:
    success: bool
    tx_hash: Optional[str] = None
    dry_run: bool = True
    error: Optional[str] = None


class OKXDexClient:
    def __init__(self):
        self.client = httpx.Client(timeout=15.0)

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        message = f"{timestamp}{method}{path}{body}"
        signature = hmac.new(
            settings.okx_secret_key.encode(), message.encode(), hashlib.sha256
        ).digest()
        return base64.b64encode(signature).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        timestamp = str(int(time.time() * 1000))
        return {
            "OK-ACCESS-KEY": settings.okx_api_key,
            "OK-ACCESS-SIGN": self._sign(timestamp, method, path, body),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": settings.okx_passphrase,
            "OK-ACCESS-PROJECT": settings.okx_project_id,
            "Content-Type": "application/json",
        }

    def get_quote(
        self, from_token: str, to_token: str, amount_raw: str, chain_id: str = SOLANA_CHAIN_ID
    ) -> QuoteResult:
        """Отримує котирування свопу (без виконання транзакції)."""
        path = "/api/v5/dex/aggregator/quote"
        params = {
            "chainId": chain_id,
            "fromTokenAddress": from_token,
            "toTokenAddress": to_token,
            "amount": amount_raw,
        }
        try:
            resp = self.client.get(
                OKX_BASE_URL + path,
                params=params,
                headers=self._headers("GET", path),
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "0":
                return QuoteResult(success=False, error=data.get("msg", "unknown error"))

            quote_data = data["data"][0]
            return QuoteResult(
                success=True,
                from_amount=quote_data.get("fromTokenAmount"),
                to_amount=quote_data.get("toTokenAmount"),
                price_impact_pct=float(quote_data.get("priceImpactPercentage", 0)),
                tx_data=quote_data,
            )
        except Exception as e:
            logger.error(f"Помилка отримання quote від OKX: {e}")
            return QuoteResult(success=False, error=str(e))

    def execute_swap(
        self,
        from_token: str,
        to_token: str,
        amount_raw: str,
        wallet_address: str,
        slippage_pct: float,
        chain_id: str = SOLANA_CHAIN_ID,
    ) -> SwapResult:
        """
        Виконує реальний своп. В DRY_RUN режимі повертає симуляцію без реального виклику.
        """
        if settings.dry_run:
            logger.info(
                f"[DRY RUN] Своп: {amount_raw} {from_token} -> {to_token} "
                f"(slippage {slippage_pct}%) — БЕЗ реального виконання"
            )
            return SwapResult(success=True, tx_hash="DRY_RUN_NO_TX", dry_run=True)

        # --- Реальний виклик (тільки коли DRY_RUN=false) ---
        path = "/api/v5/dex/aggregator/swap"
        params = {
            "chainId": chain_id,
            "fromTokenAddress": from_token,
            "toTokenAddress": to_token,
            "amount": amount_raw,
            "userWalletAddress": wallet_address,
            "slippage": str(slippage_pct / 100),
        }
        try:
            resp = self.client.get(
                OKX_BASE_URL + path,
                params=params,
                headers=self._headers("GET", path),
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "0":
                return SwapResult(success=False, dry_run=False, error=data.get("msg"))

            tx_data = data["data"][0]["tx"]
            # Тут потрібно підписати tx_data локальним приватним ключем через solders/solana-py
            # і відправити транзакцію в мережу. Це навмисно винесено в окрему функцію
            # _sign_and_broadcast() нижче — реалізується окремо з реальним ключем,
            # щоб приватний ключ ніколи не проходив через зайві шари абстракції.
            tx_hash = self._sign_and_broadcast(tx_data)
            return SwapResult(success=True, tx_hash=tx_hash, dry_run=False)
        except Exception as e:
            logger.error(f"Помилка виконання свопу: {e}")
            return SwapResult(success=False, dry_run=False, error=str(e))

    def _sign_and_broadcast(self, tx_data: dict) -> str:
        """
        Підписує сиру транзакцію Solana локальним ключем і відправляє в мережу.
        Реалізується через solders.keypair.Keypair + solana.rpc.api.Client.
        Навмисно залишено як TODO — під'єднання реального гаманця користувач
        робить свідомо на етапі 2 (див. README), після успішного dry-run.
        """
        raise NotImplementedError(
            "Підключення реального гаманця ще не налаштовано. "
            "Дивись README.md, розділ 'Перехід з dry-run на реальні угоди'."
        )
