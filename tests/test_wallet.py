"""
core/wallet.py: обробка кривого SOLANA_PRIVATE_KEY (BaseException/panic фікс
з ранньої сесії — solders кидає pyo3_runtime.PanicException, підклас
BaseException, НЕ Exception), і розрахунок USDT+SOL балансу.
"""
from types import SimpleNamespace

import pytest

import core.wallet as wallet
from core.config import settings


def _set_private_key(value: str):
    object.__setattr__(settings, "solana_private_key", value)


@pytest.fixture(autouse=True)
def _restore_key():
    original = settings.solana_private_key
    yield
    object.__setattr__(settings, "solana_private_key", original)


def test_no_private_key_returns_mock_balance():
    _set_private_key("")
    balance = wallet.get_wallet_balance()

    assert balance.is_real is False
    assert balance.usdt_balance == wallet.MOCK_WALLET_BALANCE_USD
    assert balance.sol_balance is None
    assert "не задано" in balance.note


def test_invalid_private_key_does_not_crash_and_returns_mock(monkeypatch):
    """
    Регресійний тест на фікс з ранньої сесії: Keypair.from_base58_string()
    на невалідному ключі кидає solders' pyo3_runtime.PanicException, який
    успадковується від BaseException, а НЕ Exception — звичайний
    'except Exception' його НЕ ловить. get_wallet_balance() має ловити
    BaseException явно навколо саме цього виклику.
    """
    _set_private_key("це вочевидь не валідний base58 приватний ключ!!!")

    # Не мокаємо сам solders — використовуємо РЕАЛЬНИЙ Keypair.from_base58_string(),
    # це і є те, що раніше падало неспійманим винятком.
    balance = wallet.get_wallet_balance()

    assert balance.is_real is False
    assert balance.usdt_balance == wallet.MOCK_WALLET_BALANCE_USD
    assert "Некоректний SOLANA_PRIVATE_KEY" in balance.note


def test_invalid_private_key_error_message_does_not_leak_key(monkeypatch):
    """Повідомлення про помилку не має містити сам приватний ключ (навіть невалідний)."""
    secret_looking_key = "МІЙ_СЕКРЕТНИЙ_НЕВАЛІДНИЙ_КЛЮЧ_XYZ"
    _set_private_key(secret_looking_key)

    balance = wallet.get_wallet_balance()
    assert secret_looking_key not in balance.note


def test_valid_key_fetches_sol_and_usdt_independently(monkeypatch):
    """
    SOL і USDT запитуються НЕЗАЛЕЖНО: якщо USDT-запит впаде, SOL-баланс
    (отриманий раніше) не має "стиратись".
    """
    import base58
    fake_key = base58.b58encode(bytes(64)).decode()
    _set_private_key(fake_key)

    class FakeKeypair:
        @staticmethod
        def from_base58_string(s):
            return SimpleNamespace(pubkey=lambda: "FAKE_PUBKEY")

    class FakeRpcClient:
        def __init__(self, url):
            pass

        def get_balance(self, pubkey):
            return SimpleNamespace(value=2_000_000_000)  # 2 SOL у lamports

        def get_token_account_balance(self, ata):
            raise RuntimeError("RPC rate limit — USDT-запит впав")

    monkeypatch.setitem(
        __import__("sys").modules, "solders.keypair",
        SimpleNamespace(Keypair=FakeKeypair),
    )
    monkeypatch.setitem(
        __import__("sys").modules, "solana.rpc.api",
        SimpleNamespace(Client=FakeRpcClient),
    )
    monkeypatch.setitem(
        __import__("sys").modules, "spl.token.instructions",
        SimpleNamespace(get_associated_token_address=lambda owner, mint: "FAKE_ATA"),
    )
    monkeypatch.setitem(
        __import__("sys").modules, "solders.pubkey",
        SimpleNamespace(Pubkey=SimpleNamespace(from_string=lambda s: s)),
    )

    balance = wallet.get_wallet_balance()

    assert balance.is_real is True
    assert balance.sol_balance == pytest.approx(2.0), "SOL мав отриматись, попри падіння USDT-запиту"
    assert balance.usdt_balance is None
    assert "USDT" in balance.note


def test_low_gas_warning_triggered_below_threshold(monkeypatch):
    import base58
    fake_key = base58.b58encode(bytes(64)).decode()
    _set_private_key(fake_key)

    class FakeKeypair:
        @staticmethod
        def from_base58_string(s):
            return SimpleNamespace(pubkey=lambda: "FAKE_PUBKEY")

    class FakeRpcClient:
        def __init__(self, url):
            pass

        def get_balance(self, pubkey):
            return SimpleNamespace(value=1_000_000)  # 0.001 SOL — нижче MIN_SOL_FOR_GAS

        def get_token_account_balance(self, ata):
            return SimpleNamespace(value=SimpleNamespace(ui_amount=50.0))

    monkeypatch.setitem(__import__("sys").modules, "solders.keypair", SimpleNamespace(Keypair=FakeKeypair))
    monkeypatch.setitem(__import__("sys").modules, "solana.rpc.api", SimpleNamespace(Client=FakeRpcClient))
    monkeypatch.setitem(
        __import__("sys").modules, "spl.token.instructions",
        SimpleNamespace(get_associated_token_address=lambda owner, mint: "FAKE_ATA"),
    )
    monkeypatch.setitem(
        __import__("sys").modules, "solders.pubkey",
        SimpleNamespace(Pubkey=SimpleNamespace(from_string=lambda s: s)),
    )

    balance = wallet.get_wallet_balance()

    assert balance.low_gas_warning is True
    assert balance.usdt_balance == 50.0
