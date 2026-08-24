"""
П.3 (повторний аудит): retry з exponential backoff на get_quote()/execute_swap()
— тільки на транзієнтні збої (мережеві таймаути, 5xx), НЕ на 4xx чи логічні
відмови OKX (JSON code != "0").
"""
import httpx
import pytest

from core.okx_dex_client import OKXDexClient
from core.config import settings


def _set_dry_run(value: bool):
    object.__setattr__(settings, "dry_run", value)


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://web3.okx.com/fake")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self):
        return self._json_data


_SUCCESS_QUOTE_JSON = {
    "code": "0",
    "data": [{"fromTokenAmount": "1000000", "toTokenAmount": "500000", "priceImpactPercent": "1.5"}],
}


def test_get_quote_retries_and_succeeds_on_third_attempt(monkeypatch):
    """Edge case 1: перші 2 виклики кидають httpx.ConnectTimeout, 3-й — успішний."""
    client = OKXDexClient()
    calls = {"n": 0}

    def fake_get(url, headers=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectTimeout("з'єднання не вдалось")
        return _FakeResponse(200, _SUCCESS_QUOTE_JSON)

    monkeypatch.setattr(client.client, "get", fake_get)

    result = client.get_quote("FROM", "TO", "1000000")

    assert result.success is True
    assert calls["n"] == 3, f"мало бути рівно 3 спроби, було {calls['n']}"


def test_get_quote_exhausts_retries_returns_clean_error(monkeypatch):
    """Edge case 2: усі 3 спроби невдалі -> штатна помилка (не неспійманий виняток)."""
    client = OKXDexClient()
    calls = {"n": 0}

    def always_fail(url, headers=None):
        calls["n"] += 1
        raise httpx.ConnectTimeout("з'єднання постійно не вдається")

    monkeypatch.setattr(client.client, "get", always_fail)

    result = client.get_quote("FROM", "TO", "1000000")  # НЕ має кинути виняток нагору

    assert result.success is False
    assert calls["n"] == 3, f"мало бути рівно 3 спроби перед відмовою, було {calls['n']}"
    assert result.error


def test_get_quote_4xx_does_not_retry(monkeypatch):
    """Edge case 3: 4xx (напр. невалідна адреса) -> одразу відмова, без повторних спроб."""
    client = OKXDexClient()
    calls = {"n": 0}

    def bad_request(url, headers=None):
        calls["n"] += 1
        return _FakeResponse(400, {})

    monkeypatch.setattr(client.client, "get", bad_request)

    result = client.get_quote("FROM", "TO", "1000000")

    assert result.success is False
    assert calls["n"] == 1, f"4xx НЕ має ретраїтись, було {calls['n']} спроб"


def test_get_quote_5xx_is_retried(monkeypatch):
    """5xx (проблема на боці OKX) — транзієнтна, МАЄ ретраїтись, на відміну від 4xx."""
    client = OKXDexClient()
    calls = {"n": 0}

    def server_error_then_ok(url, headers=None):
        calls["n"] += 1
        if calls["n"] < 2:
            return _FakeResponse(503, {})
        return _FakeResponse(200, _SUCCESS_QUOTE_JSON)

    monkeypatch.setattr(client.client, "get", server_error_then_ok)

    result = client.get_quote("FROM", "TO", "1000000")

    assert result.success is True
    assert calls["n"] == 2


def test_logical_failure_code_not_zero_does_not_retry(monkeypatch):
    """
    Логічна відмова OKX (JSON з code != "0", напр. недостатня ліквідність) —
    це НЕ виняток, обробляється окремо в get_quote() без жодного ретраю.
    """
    client = OKXDexClient()
    calls = {"n": 0}

    def logical_failure(url, headers=None):
        calls["n"] += 1
        return _FakeResponse(200, {"code": "51000", "msg": "недостатня ліквідність пулу"})

    monkeypatch.setattr(client.client, "get", logical_failure)

    result = client.get_quote("FROM", "TO", "1000000")

    assert result.success is False
    assert calls["n"] == 1, "логічна відмова OKX не має ретраїтись"
    assert "ліквідність" in result.error


def test_execute_swap_dry_run_never_hits_retry_path(monkeypatch):
    """
    dry_run=True — execute_swap() короткочасно повертає симуляцію ще ДО
    будь-якого HTTP-виклику, ретрай-логіка тут взагалі не бере участі.
    """
    _set_dry_run(True)
    client = OKXDexClient()

    def should_not_be_called(url, headers=None):
        raise AssertionError("HTTP-виклик не мав статись у dry_run режимі")

    monkeypatch.setattr(client.client, "get", should_not_be_called)

    result = client.execute_swap("FROM", "TO", "1000000", wallet_address="W", slippage_pct=1.0)
    assert result.success is True
    assert result.dry_run is True
