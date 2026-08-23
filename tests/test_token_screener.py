"""
П.4 аудиту: resp.json() на валідній HTTP-відповіді (200) з поламаним тілом
кидає json.JSONDecodeError (підклас ValueError), який раніше НЕ ловився
поряд з httpx.HTTPError — screen() падав неспійманим винятком замість
чіткого ScreeningResult(passed=False, ...).
"""
import httpx
import pytest

from core.token_screener import TokenScreener


class _BrokenJSONResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        import json
        raise json.JSONDecodeError("Expecting value", "не валідний json {{{", 0)


def test_dexscreener_broken_json_returns_failed_result_not_exception(monkeypatch):
    screener = TokenScreener()
    monkeypatch.setattr(screener.client, "get", lambda url: _BrokenJSONResponse())

    result = screener.screen("SomeContractAddress", "solana")

    assert result.passed is False
    assert any("DexScreener" in r for r in result.reasons_failed)


def test_goplus_broken_json_does_not_fail_whole_screening(monkeypatch):
    """
    GoPlus — додаткова перевірка: її недоступність (мережева чи JSON) НЕ
    має провалювати весь скринінг, лише пропускати цю конкретну перевірку.
    Мокаємо DexScreener на успіх (проходить ліквідність/вік), а GoPlus — на
    поламаний JSON.
    """
    screener = TokenScreener()

    class _GoodDexScreenerResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "pairs": [{
                    "liquidity": {"usd": 100_000},
                    "pairCreatedAt": 0,  # дуже старий -> вік точно пройде MIN_TOKEN_AGE_HOURS
                }]
            }

    def fake_get(url):
        if "dexscreener" in url:
            return _GoodDexScreenerResponse()
        return _BrokenJSONResponse()

    monkeypatch.setattr(screener.client, "get", fake_get)
    result = screener.screen("SomeContractAddress", "solana")

    assert result.passed is True, f"GoPlus JSON-помилка не має провалювати скринінг: {result.reasons_failed}"


def test_network_error_still_handled_as_before(monkeypatch):
    """Переконуємось, що звичайні httpx-помилки і далі обробляються (не зламали існуючу поведінку)."""
    screener = TokenScreener()

    def raise_connect_error(url):
        raise httpx.ConnectTimeout("з'єднання не вдалось")

    monkeypatch.setattr(screener.client, "get", raise_connect_error)
    result = screener.screen("SomeContractAddress", "solana")

    assert result.passed is False
    assert any("DexScreener" in r for r in result.reasons_failed)
