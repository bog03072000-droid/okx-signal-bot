"""
core/price_feed.py: batch-запит, ізоляція помилок (одна погана адреса в
батчі не ламає решту), 429-backoff, кешування ЦІН (модуль сам НЕ кешує —
кеш живе в core/position_monitor.py — тут перевіряємо лише сам batch-виклик).
"""
import httpx
import pytest

import core.price_feed as pf


@pytest.fixture(autouse=True)
def _reset_backoff():
    """_backoff_until/_backoff_seconds — module-level стан, не ізольований
    per-тест автоматично (на відміну від storage/runtime_state) — скидаємо
    вручну, інакше тест з 429 псує наступний тест довгим backoff-вікном."""
    pf._backoff_until = 0.0
    pf._backoff_seconds = 1.0
    yield
    pf._backoff_until = 0.0
    pf._backoff_seconds = 1.0


class _FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


def _pair(address, price_usd, liquidity_usd=50_000):
    return {
        "baseToken": {"address": address},
        "priceUsd": str(price_usd),
        "liquidity": {"usd": liquidity_usd},
    }


def test_batch_request_returns_prices_for_all_addresses(monkeypatch):
    addr1, addr2 = "AAA111", "BBB222"

    def fake_get(url):
        assert addr1 in url and addr2 in url, "обидві адреси мають піти в ОДНОМУ запиті (batch)"
        return _FakeResponse(200, [_pair(addr1, 0.001), _pair(addr2, 0.002)])

    monkeypatch.setattr(pf._client, "get", fake_get)

    prices = pf.fetch_prices_usd([addr1, addr2], "solana")
    assert prices == {addr1: 0.001, addr2: 0.002}


def test_missing_address_is_absent_not_zero(monkeypatch):
    """
    Адреса, для якої DexScreener не знайшов пари, ПРОСТО відсутня в
    результаті — НЕ 0.0 (0.0 виглядало б як миттєвий -100% і викликало б
    хибний stop-loss в core/position_monitor.py).
    """
    addr_found, addr_missing = "AAA111", "CCC333"

    def fake_get(url):
        return _FakeResponse(200, [_pair(addr_found, 0.005)])

    monkeypatch.setattr(pf._client, "get", fake_get)

    prices = pf.fetch_prices_usd([addr_found, addr_missing], "solana")
    assert addr_found in prices
    assert addr_missing not in prices, "відсутня пара має бути ВІДСУТНЬОЮ в словнику, не 0.0"


def test_multiple_pairs_same_address_picks_highest_liquidity(monkeypatch):
    addr = "AAA111"

    def fake_get(url):
        return _FakeResponse(200, [
            _pair(addr, 0.001, liquidity_usd=500),
            _pair(addr, 0.999, liquidity_usd=1_000_000),  # ця пара має "виграти"
        ])

    monkeypatch.setattr(pf._client, "get", fake_get)

    prices = pf.fetch_prices_usd([addr], "solana")
    assert prices[addr] == 0.999


def test_one_bad_address_does_not_break_batch(monkeypatch):
    """
    Один "поганий" елемент батчу (напр. невалідний priceUsd) не має зламати
    парсинг решти адрес у тій самій відповіді.
    """
    addr_good, addr_bad = "AAA111", "BADADDR"

    def fake_get(url):
        return _FakeResponse(200, [
            _pair(addr_good, 0.001),
            {"baseToken": {"address": addr_bad}, "priceUsd": "не число", "liquidity": {"usd": 100}},
        ])

    monkeypatch.setattr(pf._client, "get", fake_get)

    prices = pf.fetch_prices_usd([addr_good, addr_bad], "solana")
    assert prices.get(addr_good) == 0.001
    assert addr_bad not in prices


def test_429_triggers_backoff_and_skips_subsequent_calls(monkeypatch):
    calls = {"n": 0}

    def fake_get(url):
        calls["n"] += 1
        return _FakeResponse(429, {})

    monkeypatch.setattr(pf._client, "get", fake_get)

    result1 = pf.fetch_prices_usd(["AAA111"], "solana")
    assert result1 == {}
    assert calls["n"] == 1

    # Наступний виклик одразу після 429 — має бути ПРОПУЩЕНИЙ (backoff),
    # без реального HTTP-запиту.
    result2 = pf.fetch_prices_usd(["AAA111"], "solana")
    assert result2 == {}
    assert calls["n"] == 1, "виклик під час backoff-вікна не мав дійти до HTTP"


def test_network_error_returns_empty_dict_not_exception(monkeypatch):
    def raise_error(url):
        raise httpx.ConnectTimeout("мережа недоступна")

    monkeypatch.setattr(pf._client, "get", raise_error)

    result = pf.fetch_prices_usd(["AAA111"], "solana")
    assert result == {}


def test_fetch_price_usd_single_address_wraps_batch(monkeypatch):
    addr = "AAA111"

    def fake_get(url):
        return _FakeResponse(200, [_pair(addr, 0.0000001234)])

    monkeypatch.setattr(pf._client, "get", fake_get)

    price = pf.fetch_price_usd(addr, "solana")
    assert price == pytest.approx(0.0000001234)


def test_fetch_price_usd_missing_returns_none(monkeypatch):
    monkeypatch.setattr(pf._client, "get", lambda url: _FakeResponse(200, []))
    assert pf.fetch_price_usd("NOTHING_HERE", "solana") is None
