"""
SignalParser: тестуємо ЛИШЕ наш код навколо виклику Claude API (парсинг
JSON-відповіді, обробка markdown-обгортки, fallback при помилці) —
замоковано anthropic.Anthropic().messages.create(), реальний API-ключ не
потрібен. Не тестуємо "чи LLM правильно вирішить" (це не наш код), а що
парсер коректно перетворює РІЗНІ форми відповіді LLM у ParsedSignal.

Наприкінці — тест мережевого відсіювання (EVM) через main.process_signal():
чи дійсно тихо (без notify), точно як задокументовано в main.py.
"""
import json
from types import SimpleNamespace

import pytest

from core.signal_parser import SignalParser


def _mock_anthropic_response(payload: dict, wrap_markdown: bool = False):
    text = json.dumps(payload, ensure_ascii=False)
    if wrap_markdown:
        text = f"```json\n{text}\n```"
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def test_parse_implicit_buy_example_1_from_real_channel(monkeypatch):
    """Приклад 1 із SYSTEM_PROMPT: "16k, дев очень жир <адреса>" — капа НЕ amount_hint."""
    parser = SignalParser.__new__(SignalParser)  # обходимо __init__ (не створюємо реальний Anthropic client)
    payload = {
        "is_signal": True, "action": "buy", "token_symbol": None,
        "contract_address": "JACCJHVy2QC96VNJK1iMrqYwMQPBbHNna2oEnxEPpump",
        "chain": "solana", "amount_hint": None, "confidence": 0.8,
        "reasoning": "Кол з адресою контракту і позитивною оцінкою дева",
    }
    parser.client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: _mock_anthropic_response(payload)
    ))

    result = parser.parse("16k, дев очень жир JACCJHVy2QC96VNJK1iMrqYwMQPBbHNna2oEnxEPpump")

    assert result.is_signal is True
    assert result.action == "buy"
    assert result.contract_address == "JACCJHVy2QC96VNJK1iMrqYwMQPBbHNna2oEnxEPpump"
    assert result.chain == "solana"
    assert result.amount_hint is None, "капа '16k' НЕ має потрапити в amount_hint"
    assert result.confidence == 0.8


def test_parse_strips_markdown_json_wrapper(monkeypatch):
    parser = SignalParser.__new__(SignalParser)
    payload = {"is_signal": False, "action": None, "token_symbol": None, "contract_address": None,
               "chain": None, "amount_hint": None, "confidence": 0.0, "reasoning": "флекс без кола"}
    parser.client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: _mock_anthropic_response(payload, wrap_markdown=True)
    ))

    result = parser.parse("гении ребята за 2 дня удвоили портфель")
    assert result.is_signal is False


def test_parse_malformed_json_returns_safe_fallback():
    """Якщо Claude колись поверне не-JSON — is_signal=False з parse_error, а НЕ виняток нагору."""
    parser = SignalParser.__new__(SignalParser)
    parser.client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(content=[SimpleNamespace(text="це не json взагалі")])
    ))

    result = parser.parse("будь-який текст")
    assert result.is_signal is False
    assert "parse_error" in result.reasoning


def test_parse_evm_address_without_explicit_network_is_evm_unknown():
    parser = SignalParser.__new__(SignalParser)
    payload = {
        "is_signal": True, "action": "buy", "token_symbol": None,
        "contract_address": "0x1234567890abcdef1234567890abcdef12345678",
        "chain": "evm_unknown", "amount_hint": None, "confidence": 0.6,
        "reasoning": "EVM-адреса без явної згадки мережі словами",
    }
    parser.client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: _mock_anthropic_response(payload)
    ))
    result = parser.parse("0x1234567890abcdef1234567890abcdef12345678 хороший проект")
    assert result.chain == "evm_unknown"


def test_parse_reply_sell_basic(monkeypatch):
    parser = SignalParser.__new__(SignalParser)
    payload = {"is_sell_signal": True, "sell_fraction": 0.5, "confidence": 0.85,
               "reasoning": "'слил половину' -> явно вказана частка"}
    parser.client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: _mock_anthropic_response(payload)
    ))

    result = parser.parse_reply_sell("слил половину", "16k, дев очень жир JACC...pump")
    assert result["is_sell_signal"] is True
    assert result["sell_fraction"] == 0.5


@pytest.mark.asyncio
async def test_evm_signal_rejected_silently_by_main(monkeypatch):
    """
    main.process_signal(): chain != "solana" -> тихий запис у SignalLog,
    БЕЗ notify() (задокументовано в main.py — канал регулярно кидає EVM-адреси,
    сповіщення про кожну засмічувало б чат).
    """
    import main as main_module
    from core.storage import get_session, SignalLog

    fake_parsed = SimpleNamespace(
        is_signal=True, action="buy", token_symbol="PEPE", contract_address="0xabc",
        chain="ethereum", confidence=0.9, reasoning="EVM call", raw_text="0xabc на eth",
    )
    monkeypatch.setattr(main_module.parser, "parse", lambda text: fake_parsed)

    notify_calls = []
    async def fake_notify(client, text):
        notify_calls.append(text)
    monkeypatch.setattr(main_module, "notify", fake_notify)

    await main_module.process_signal(client=None, message_text="0xabc на eth")

    session = get_session()
    log = session.query(SignalLog).order_by(SignalLog.id.desc()).first()
    assert log is not None
    assert log.chain == "ethereum"
    assert "не підтримується" in log.rejection_reason
    assert len(notify_calls) == 0, "EVM-відсіювання має бути ТИХИМ, без сповіщення"
