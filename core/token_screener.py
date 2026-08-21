"""
Перевірка токена перед купівлею: вік, ліквідність, ознаки скаму/honeypot.
Використовує безкоштовні публічні API: DexScreener + GoPlus Security.
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from core.config import get_limit

logger = logging.getLogger(__name__)

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
GOPLUS_SOLANA_URL = "https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={address}"


@dataclass
class ScreeningResult:
    passed: bool
    liquidity_usd: Optional[float]
    age_hours: Optional[float]
    reasons_failed: list[str]
    is_honeypot_flag: bool


class TokenScreener:
    def __init__(self):
        self.client = httpx.Client(timeout=10.0)

    def screen(self, contract_address: str, chain: str) -> ScreeningResult:
        reasons = []
        liquidity_usd = None
        age_hours = None
        is_honeypot = False

        # --- 1. DexScreener: ліквідність і вік пари ---
        try:
            resp = self.client.get(DEXSCREENER_URL.format(address=contract_address))
            resp.raise_for_status()
            data = resp.json()
            pairs = data.get("pairs") or []
            if not pairs:
                reasons.append("Токен не знайдено в DexScreener (немає торгових пар)")
            else:
                # Беремо пару з найбільшою ліквідністю
                best_pair = max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)
                liquidity_usd = best_pair.get("liquidity", {}).get("usd", 0) or 0
                created_at_ms = best_pair.get("pairCreatedAt")
                if created_at_ms:
                    age_hours = (time.time() * 1000 - created_at_ms) / (1000 * 3600)

                min_liquidity = get_limit("MIN_LIQUIDITY_USD")
                min_age = get_limit("MIN_TOKEN_AGE_HOURS")
                if liquidity_usd < min_liquidity:
                    reasons.append(
                        f"Ліквідність ${liquidity_usd:,.0f} нижче мінімуму "
                        f"${min_liquidity:,.0f}"
                    )
                if age_hours is not None and age_hours < min_age:
                    reasons.append(
                        f"Токен занадто новий: {age_hours:.1f}год "
                        f"(мінімум {min_age}год)"
                    )
        except httpx.HTTPError as e:
            reasons.append(f"Помилка запиту до DexScreener: {e}")

        # --- 2. GoPlus Security: honeypot / mint authority / owner privileges ---
        if chain == "solana":
            try:
                resp = self.client.get(GOPLUS_SOLANA_URL.format(address=contract_address))
                resp.raise_for_status()
                data = resp.json()
                token_data = (data.get("result") or {}).get(contract_address, {})

                if token_data.get("mintable", {}).get("status") == "1":
                    is_honeypot = True
                    reasons.append("Токен має активний mint authority (девелопер може довільно мінтити)")
                if token_data.get("freezable", {}).get("status") == "1":
                    is_honeypot = True
                    reasons.append("Токен має freeze authority (можуть заморозити твої токени)")
            except httpx.HTTPError as e:
                logger.warning(f"GoPlus API недоступний: {e} — пропускаємо цю перевірку")
        # Для ETH/BSC аналогічно є GoPlus endpoint /api/v1/token_security/{chain_id}
        # додати за потреби при розширенні на ці мережі

        passed = len(reasons) == 0
        return ScreeningResult(
            passed=passed,
            liquidity_usd=liquidity_usd,
            age_hours=age_hours,
            reasons_failed=reasons,
            is_honeypot_flag=is_honeypot,
        )
