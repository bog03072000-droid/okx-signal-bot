"""
Парсинг довільного тексту з тг-каналу в структурований сигнал через Claude API.
"""
import json
import logging
from dataclasses import dataclass
from typing import Optional

from anthropic import Anthropic

from core.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ти — парсер торгових сигналів з крипто Telegram-каналів.
Твоє єдине завдання: проаналізувати текст повідомлення і визначити, чи є в ньому
торговий сигнал (buy/sell) на криптотокен, і повернути ЛИШЕ JSON без жодного іншого тексту.

Формат відповіді (суворо JSON, без markdown-обгортки):
{
  "is_signal": true/false,
  "action": "buy" | "sell" | null,
  "token_symbol": "тікер токена, якщо згадано, або null",
  "contract_address": "адреса контракту, якщо явно вказана в тексті, або null",
  "chain": "solana" | "ethereum" | "bsc" | null,
  "amount_hint": "сума/відсоток, якщо вказано, або null (наприклад '5%', '$100', 'all')",
  "confidence": число від 0.0 до 1.0 - наскільки ти впевнений, що це реальний торговий сигнал,
  "reasoning": "коротке пояснення чому ти так вирішив (1 речення)"
}

Правила:
- Якщо в тексті немає явного заклику купити/продати конкретний токен - is_signal: false, action: null.
- Не вигадуй contract_address, якщо його немає в тексті - залишай null.
- Якщо мережа явно не вказана і не випливає з контексту - chain: null.
- Реклама, загальні новини, аналітика без конкретної дії - НЕ сигнал.
- confidence має відображати реальну невизначеність: розмитий натяк = низький confidence,
  чіткий "купуй $TOKEN зараз" = високий confidence.
"""


@dataclass
class ParsedSignal:
    is_signal: bool
    action: Optional[str]
    token_symbol: Optional[str]
    contract_address: Optional[str]
    chain: Optional[str]
    amount_hint: Optional[str]
    confidence: float
    reasoning: str
    raw_text: str


class SignalParser:
    def __init__(self):
        self.client = Anthropic(api_key=settings.anthropic_api_key)

    def parse(self, message_text: str) -> ParsedSignal:
        """Синхронний виклик Claude API для парсингу одного повідомлення."""
        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": message_text}],
            )
            text = response.content[0].text.strip()
            # На випадок якщо модель обгорне в ```json ... ```
            text = text.replace("```json", "").replace("```", "").strip()
            data = json.loads(text)

            return ParsedSignal(
                is_signal=data.get("is_signal", False),
                action=data.get("action"),
                token_symbol=data.get("token_symbol"),
                contract_address=data.get("contract_address"),
                chain=data.get("chain"),
                amount_hint=data.get("amount_hint"),
                confidence=float(data.get("confidence", 0.0)),
                reasoning=data.get("reasoning", ""),
                raw_text=message_text,
            )
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Не вдалось розпарсити відповідь Claude: {e}")
            return ParsedSignal(
                is_signal=False, action=None, token_symbol=None,
                contract_address=None, chain=None, amount_hint=None,
                confidence=0.0, reasoning=f"parse_error: {e}", raw_text=message_text,
            )
        except Exception as e:
            logger.error(f"Помилка виклику Claude API: {e}")
            return ParsedSignal(
                is_signal=False, action=None, token_symbol=None,
                contract_address=None, chain=None, amount_hint=None,
                confidence=0.0, reasoning=f"api_error: {e}", raw_text=message_text,
            )
