"""
Клієнт для OKX DEX (Web3) Aggregator API.

Документація (перевірено наживо 2026-08-22, стара v5-адреса вже 404):
  https://web3.okx.com/onchainos/dev-docs/trade/dex-get-quote
  https://web3.okx.com/onchainos/dev-docs/trade/dex-swap

СКЕПТИЧНИЙ КОМЕНТАР: OKX вже ОДИН РАЗ повністю прибрав V5 API без довгого
запасного періоду (реальна помилка з продакшна: "V5 API is being
deprecated. Please refer to our documentation and upgrade to the latest
V6 API for continued access.") — код нижче використовує V6, але немає
жодної гарантії, що OKX не зробить те саме з V6 колись. Якщо `get_quote()`
раптом почне валитись з подібною помилкою — перше, що перевіряти, це чи
не змінилась версія API знову, а не шукати баг деінде.

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
from urllib.parse import urlencode

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

OKX_BASE_URL = "https://web3.okx.com"
SOLANA_CHAIN_ID = "501"  # ідентифікатор Solana в OKX DEX API

# Native SOL "адреса" за конвенцією OKX/Jupiter. З переходом на USDT як базову
# торгову валюту (див. USDT_MINT_SOLANA нижче) ця адреса вже НЕ використовується
# для buy/sell у main.py — лишається потрібною лише для запиту балансу SOL
# (резерв на газ, core/wallet.py).
SOL_NATIVE_ADDRESS = "11111111111111111111111111111111"

# USDT (Tether) на Solana — SPL-токен, mint-адреса. Базова торгова валюта бота.
#
# ДЖЕРЕЛО (навмисно НЕ з пам'яті — перевірено двома незалежними способами
# перед тим, як цю адресу вписали в код):
#   1. Пряме читання on-chain даних з mainnet-beta через RPC getAccountInfo:
#      owner = TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA (стандартна SPL
#      Token Program, підтверджує що це справжній SPL-токен, а не підробка),
#      decimals = 6 (співпадає з офіційним USDT).
#   2. Незалежне підтвердження на публічних explorer/wallet-сервісах, усі
#      показують ту саму адресу для "USDT" на Solana:
#      https://explorer.solana.com/address/Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB
#      https://solscan.io/token/Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB
#      https://phantom.com/tokens/solana/Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB
#
# ПЕРЕД LIVE-ЗАПУСКОМ (DRY_RUN=false): звір цю адресу ще раз сам, окремо від
# цього коментаря — помилка в mint-адресі тут означає, що бот купує/рахує
# баланс не того токена (може навіть підставного токена з такою ж назвою).
USDT_MINT_SOLANA = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
USDT_DECIMALS = 6


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

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        message = f"{timestamp}{method}{request_path}{body}"
        signature = hmac.new(
            settings.okx_secret_key.encode(), message.encode(), hashlib.sha256
        ).digest()
        return base64.b64encode(signature).decode()

    def _headers(self, method: str, request_path: str, body: str = "") -> dict:
        """
        request_path МАЄ включати query-string для GET-запитів (напр.
        "/api/v5/dex/aggregator/quote?chainId=501&amount=..."), інакше підпис
        не збігається з реальним запитом і OKX повертає 401 Unauthorized —
        сам факт наявності правильних ключів тут не рятує, підпис рахується
        не з того рядка. Джерело: офіційна документація OKX V5 API,
        https://www.okx.com/docs-v5/en/#overview-rest-authentication —
        приклад підпису явно включає query-string:
        timestamp + 'GET' + '/api/v5/account/balance?ccy=BTC' + secretKey.
        """
        timestamp = str(int(time.time() * 1000))
        return {
            "OK-ACCESS-KEY": settings.okx_api_key,
            "OK-ACCESS-SIGN": self._sign(timestamp, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": settings.okx_passphrase,
            "OK-ACCESS-PROJECT": settings.okx_project_id,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _build_url_and_signed_path(path: str, params: dict) -> tuple[str, str]:
        """
        Будує query-string ОДИН РАЗ і використовує той самий рядок і для
        реального запиту, і для підпису — щоб гарантовано збігались. Раніше
        query-string передавався в httpx окремо через params=, а підписувався
        лише голий path без параметрів — це і давало 401 Unauthorized
        (сигнатура рахувалась не з того, що реально йшло в запиті).
        """
        query_string = urlencode(params)
        request_path = f"{path}?{query_string}" if query_string else path
        return OKX_BASE_URL + request_path, request_path

    def get_quote(
        self, from_token: str, to_token: str, amount_raw: str, chain_id: str = SOLANA_CHAIN_ID
    ) -> QuoteResult:
        """
        Отримує котирування свопу (без виконання транзакції).

        price_impact_pct — це OKX V6 "priceImpactPercent", НЕ той знак, що
        інтуїтивно очікуєш: за офіційним визначенням OKX це
        (отримано - заплачено) / заплачено, тобто ДОДАТНЄ значення = вигідніше
        за очікуване (більше отримав), ВІД'ЄМНЕ = гірше за очікуване (менше
        отримав, тобто "класичний" price impact). Перевірка ліміту
        (core/risk_manager.py:check_price_impact) враховує цей інвертований
        знак — не переплутай при зміні цього коду.
        """
        path = "/api/v6/dex/aggregator/quote"
        params = {
            "chainIndex": chain_id,
            "fromTokenAddress": from_token,
            "toTokenAddress": to_token,
            "amount": amount_raw,
        }
        url, request_path = self._build_url_and_signed_path(path, params)
        try:
            resp = self.client.get(url, headers=self._headers("GET", request_path))
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "0":
                return QuoteResult(success=False, error=data.get("msg", "unknown error"))

            quote_data = data["data"][0]
            return QuoteResult(
                success=True,
                from_amount=quote_data.get("fromTokenAmount"),
                to_amount=quote_data.get("toTokenAmount"),
                price_impact_pct=float(quote_data.get("priceImpactPercent", 0)),
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
        force_dry_run: bool = False,
    ) -> SwapResult:
        """
        Виконує реальний своп. В DRY_RUN режимі (або коли force_dry_run=True)
        повертає симуляцію без реального виклику.

        force_dry_run — ОКРЕМИЙ від settings.dry_run прапорець. Використовується
        ВИКЛЮЧНО кнопкою "🧪 Тест" (core/self_test.py), яка МАЄ гарантовано
        ніколи не відправляти реальну транзакцію, НАВІТЬ якщо бот зараз працює
        в DRY_RUN=false (live). Якби перевірка спиралась лише на settings.dry_run,
        тест перевіряв би реальний live-шлях лише тоді, коли settings.dry_run
        сам по собі False — тобто саме тоді, коли ціна помилки найвища, а
        джерело "чи виконати реально" — це один загальний прапорець з .env,
        яким могла б випадково маніпулювати інша частина коду чи майбутня
        зміна конфігурації. force_dry_run — явний параметр, переданий
        буквально літералом True з місця виклику тесту (див.
        core/self_test.py, дивись коментар "ЛІТЕРАЛ" там) — його неможливо
        випадково "забути" виставити чи підмінити через .env.
        """
        if settings.dry_run or force_dry_run:
            tx_hash = "TEST_SIMULATION_NO_TX" if force_dry_run and not settings.dry_run else "DRY_RUN_NO_TX"
            logger.info(
                f"[{'TEST' if force_dry_run else 'DRY RUN'}] Своп: {amount_raw} {from_token} -> {to_token} "
                f"(slippage {slippage_pct}%) — БЕЗ реального виконання"
            )
            return SwapResult(success=True, tx_hash=tx_hash, dry_run=True)

        # --- Реальний виклик (тільки коли DRY_RUN=false І force_dry_run=False) ---
        path = "/api/v6/dex/aggregator/swap"
        params = {
            "chainIndex": chain_id,
            "fromTokenAddress": from_token,
            "toTokenAddress": to_token,
            "amount": amount_raw,
            "userWalletAddress": wallet_address,
            # V6 slippagePercent — це вже сам відсоток (0.5 = 0.5%), а НЕ
            # частка (на відміну від старого v5 "slippage", де 3% писалось
            # як 0.03). Ділити на 100 тут НЕ треба.
            "slippagePercent": str(slippage_pct),
        }
        url, request_path = self._build_url_and_signed_path(path, params)
        try:
            resp = self.client.get(url, headers=self._headers("GET", request_path))
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
