"""
Парсинг довільного тексту з тг-каналу в структурований сигнал через Claude API.

Промпт відкалібрований під конкретний реальний канал (неформальний "трейдерський
щоденник" з колами токенів) — див. приклади в SYSTEM_PROMPT. Це НЕ універсальний
парсер під будь-який формат сигнального каналу.
"""
import json
import logging
from dataclasses import dataclass
from typing import Optional

from anthropic import Anthropic

from core.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ти — парсер повідомлень з крипто Telegram-каналу, що публікує
"коли" (calls) на токени. Це НЕ формальний сигнальний канал зі словами
"BUY"/"КУПИТЬ" — це неформальний трейдерський щоденник. Сама згадка адреси
контракту з позитивним чи нейтральним коментарем (капа, оцінка дева, наратив,
"раннер?") — це ІМПЛІЦИТНИЙ buy-сигнал, навіть без явного заклику до дії.

Твоє єдине завдання: проаналізувати текст повідомлення і повернути ЛИШЕ JSON
без жодного іншого тексту.

Формат відповіді (суворо JSON, без markdown-обгортки):
{
  "is_signal": true/false,
  "action": "buy" | "sell" | null,
  "token_symbol": "тікер токена, якщо згадано, або null",
  "contract_address": "адреса контракту, якщо явно вказана в тексті, або null",
  "chain": "solana" | "ethereum" | "bsc" | "evm_unknown" | null,
  "amount_hint": "сума/відсоток РОЗМІРУ УГОДИ, якщо вказано, або null (наприклад '5%', '$100', 'all') — УВАГА: заявлена капіталізація токена (число з 'к'/'k', напр. '16k', '52к') це НЕ amount_hint, див. правило нижче",
  "confidence": число від 0.0 до 1.0 - наскільки ти впевнений, що це реальний торговий сигнал,
  "reasoning": "коротке пояснення чому ти так вирішив (1 речення)"
}

ФОРМАТ АДРЕСИ КОНТРАКТУ І ВИЗНАЧЕННЯ chain:
- Solana-адреси: base58 (літери+цифри без 0/O/I/l), зазвичай 32-44 символи,
  БЕЗ префіксу "0x". Приклад: "84cAEWqiDsV5xXh6CB69Hi3HcnumBbdjH4THfyorpump".
  → chain: "solana"
- EVM-адреси: завжди "0x" + рівно 40 hex-символів. Приклад:
  "0x1234567890abcdef1234567890abcdef12345678".
  → ВАЖЛИВО: за самим форматом адреси НЕМОЖЛИВО на 100% розрізнити Ethereum
  від BSC (BNB Chain) — обидва використовують ідентичний формат "0x"+40 hex.
  Якщо мережа явно не випливає з контексту повідомлення (напр. згадка "BSC",
  "BNB", "ETH", "Ethereum" словами) — повертай chain: "evm_unknown", а НЕ
  вгадуй "ethereum" чи "bsc" навмання.
- Якщо адреси в тексті взагалі немає — contract_address: null, chain: null
  (якщо мережа явно не згадана словами).

ЧИСЛА З "к"/"k" — ЦЕ КАПІТАЛІЗАЦІЯ, НЕ РОЗМІР УГОДИ:
Цей канал пише заявлену капіталізацію токена на момент кола як число+"к"/"k"
(напр. "16k", "52к", "5к"). Це НЕ сума, яку треба купити, і НЕ ціна токена —
це ринкова капіталізація. НІКОЛИ не інтерпретуй таке число як amount_hint.
amount_hint заповнюй лише якщо в тексті ЯВНО вказано розмір/частку САМОЇ
УГОДИ (напр. "купи на 5%", "заллй $100", "продай половину").

ПРОДАЖІ В REPLY БЕЗ АДРЕСИ:
Якщо повідомлення схоже на коментар про продаж/фіксацію прибутку ("слил
половину", "закрыл в +50", "вышел в +15%", "зафиксил", "флипнул") БЕЗ жодної
адреси контракту в цьому конкретному тексті — це НЕ можна впевнено прив'язати
до токена, дивлячись лише на цей текст. Постав is_signal: false,
reasoning: коротко поясни, що це схоже на sell-згадку без контракту
(окремий механізм у боті намагається знайти контракт через reply-контекст,
але не в цій функції).

Загальні правила:
- Якщо в тексті немає ані адреси контракту, ані явного/імпліцитного заклику
  щодо конкретного токена — is_signal: false, action: null.
- Не вигадуй contract_address, якщо його немає в тексті — залишай null.
- Реклама, загальні новини, флекс без нового кола, обговорення без конкретного
  токена — НЕ сигнал.
- confidence має відображати реальну невизначеність: розмитий натяк = низький
  confidence, кол з чіткою адресою контракту й коментарем = високий confidence.

ПРИКЛАДИ (реальні повідомлення з каналу):

Приклад 1 (implicit buy, капа НЕ amount_hint):
Текст: "16k, дев очень жир JACCJHVy2QC96VNJK1iMrqYwMQPBbHNna2oEnxEPpump"
{
  "is_signal": true,
  "action": "buy",
  "token_symbol": null,
  "contract_address": "JACCJHVy2QC96VNJK1iMrqYwMQPBbHNna2oEnxEPpump",
  "chain": "solana",
  "amount_hint": null,
  "confidence": 0.8,
  "reasoning": "Кол з адресою контракту і позитивною оцінкою дева ('очень жир') — імпліцитний buy, 16k це капіталізація, не розмір угоди"
}

Приклад 2 (implicit buy, риторичне питання все одно є сигналом):
Текст: "Раннер? 52к\\n\\n84cAEWqiDsV5xXh6CB69Hi3HcnumBbdjH4THfyorpump"
{
  "is_signal": true,
  "action": "buy",
  "token_symbol": null,
  "contract_address": "84cAEWqiDsV5xXh6CB69Hi3HcnumBbdjH4THfyorpump",
  "chain": "solana",
  "amount_hint": null,
  "confidence": 0.6,
  "reasoning": "Кол з адресою контракту, 'Раннер?' — риторичне питання з позитивним ухилом, помірна впевненість через невизначеність тону"
}

Приклад 3 (implicit buy, наратив як обґрунтування):
Текст: "5к, хороший нарратив под мету от дева 2BeNzSRvBKTDqmEAixarwMeKygZwuQq3A8Kc8PbRa5DC"
{
  "is_signal": true,
  "action": "buy",
  "token_symbol": null,
  "contract_address": "2BeNzSRvBKTDqmEAixarwMeKygZwuQq3A8Kc8PbRa5DC",
  "chain": "solana",
  "amount_hint": null,
  "confidence": 0.75,
  "reasoning": "Кол з адресою контракту і позитивним коментарем про наратив/дева — імпліцитний buy"
}

Приклад 4 (sell-згадка БЕЗ адреси — is_signal false, обробляється окремо через reply-контекст):
Текст: "слил половину"
{
  "is_signal": false,
  "action": null,
  "token_symbol": null,
  "contract_address": null,
  "chain": null,
  "amount_hint": null,
  "confidence": 0.0,
  "reasoning": "Схоже на sell-згадку, але без адреси контракту в цьому тексті — неможливо прив'язати до токена без reply-контексту"
}

Приклад 5 (чистий флекс/розмова без нового кола — НЕ сигнал):
Текст: "гении ребята за 2 дня удвоили портфель, красавчики"
{
  "is_signal": false,
  "action": null,
  "token_symbol": null,
  "contract_address": null,
  "chain": null,
  "amount_hint": null,
  "confidence": 0.0,
  "reasoning": "Загальна похвала/флекс без адреси контракту чи конкретного нового кола — не сигнал"
}
"""

REPLY_SELL_SYSTEM_PROMPT = """Ти аналізуєш ДВА повідомлення з крипто Telegram-каналу:
1. ОРИГІНАЛ — містить адресу контракту токена (кол).
2. REPLY на нього — коротка згадка про продаж, БЕЗ адреси контракту в самому REPLY.

Це неформальний трейдерський щоденник: продажі згадуються словами на кшталт
"слил", "закрыл", "вышел", "зафиксил", "флипнул", "продал", "скинул" —
без повторення адреси контракту, бо контекст (reply) вже вказує, про який
токен йдеться.

Визнач: чи цей REPLY означає, що автор продав (частину чи все) токен з
ОРИГІНАЛУ? Поверни ЛИШЕ JSON, без жодного іншого тексту:
{
  "is_sell_signal": true/false,
  "sell_fraction": число від 0.0 до 1.0 — яку частку позиції продано, або null
    якщо is_sell_signal false. Визначай з тексту:
      "слил половину" / "продал часть" → 0.5
      "закрыл" / "вышел" / "флипнул" (без уточнення частки) → 1.0
      "зафиксил часть" / неоднозначно, але явно НЕ все → 0.5 (консервативний дефолт)
  "confidence": число від 0.0 до 1.0,
  "reasoning": "коротке пояснення, включно з тим, чи sell_fraction точний з тексту чи евристичний дефолт"
}

Якщо REPLY — це щось інше (питання, емоційна реакція без ознак продажу,
обговорення зовсім іншої теми) — is_sell_signal: false, sell_fraction: null.
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

    def parse_reply_sell(self, reply_text: str, original_text: str) -> dict:
        """
        Best-effort визначення: чи reply-повідомлення (без адреси контракту)
        означає sell токена з ОРИГІНАЛЬНОГО повідомлення (яке адресу має).
        Викликається з main.py окремо від звичайного parse() — контракт
        береться з original_text (через окремий parse()), а не звідси.

        Повертає dict (не ParsedSignal — інша, вужча форма даних):
        {"is_sell_signal": bool, "sell_fraction": float|None, "confidence": float, "reasoning": str}
        При помилці — is_sell_signal=False (fail-safe: якщо не впевнені, нічого не продаємо).
        """
        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                system=REPLY_SELL_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"ОРИГІНАЛ:\n{original_text}\n\nREPLY:\n{reply_text}",
                }],
            )
            text = response.content[0].text.strip()
            text = text.replace("```json", "").replace("```", "").strip()
            data = json.loads(text)
            return {
                "is_sell_signal": bool(data.get("is_sell_signal", False)),
                "sell_fraction": data.get("sell_fraction"),
                "confidence": float(data.get("confidence", 0.0)),
                "reasoning": data.get("reasoning", ""),
            }
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Не вдалось розпарсити reply-sell відповідь Claude: {e}")
            return {"is_sell_signal": False, "sell_fraction": None, "confidence": 0.0, "reasoning": f"parse_error: {e}"}
        except Exception as e:
            logger.error(f"Помилка виклику Claude API (reply-sell): {e}")
            return {"is_sell_signal": False, "sell_fraction": None, "confidence": 0.0, "reasoning": f"api_error: {e}"}
