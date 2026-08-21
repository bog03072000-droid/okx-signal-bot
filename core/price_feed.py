"""
Отримання цін токенів (USD) через публічний DexScreener batch-ендпоінт.
Спільний модуль для:
  - main.py — одноразовий lookup ціни одразу після buy (entry_price)
  - core/position_monitor.py — циклічний batch-моніторинг відкритих позицій
    (усі відкриті позиції одним запитом, а не по запиту на позицію)

ФОРМАТ ЕНДПОІНТУ (перевірено наживо, НЕ з пам'яті):
  GET https://api.dexscreener.com/tokens/v1/{chainId}/{tokenAddresses}
  {tokenAddresses} — адреси токенів через кому, до MAX_ADDRESSES_PER_BATCH
  за один запит.

  Джерело: https://docs.dexscreener.com/api/reference — офіційна OpenAPI-
  специфікація DexScreener, ендпоінт "GET /tokens/v1/{chainId}/{tokenAddresses}"
  (позначений в документації як batch-ендпоінт для кількох адрес одразу).

  Живий тест 2026-08-21:
    GET https://api.dexscreener.com/tokens/v1/solana/Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB,So11111111111111111111111111111111111111112
    → HTTP 200, JSON-масив з 2 pair-об'єктів (USDT і SOL), кожен з полем
    priceUsd. Підтверджує: (а) шлях і формат параметрів коректні,
    (б) comma-separated декілька адрес в одному запиті працює.

  МАКСИМУМ 30 АДРЕС ЗА ЗАПИТ: узято з документації/технічного завдання —
  НЕ підтверджено емпірично окремим тестом (не хотілось навмисно спамити
  публічний сервіс запитом на межі ліміту заради перевірки числа). Якщо
  колись виявиться, що реальний ліміт інший — зменш MAX_ADDRESSES_PER_BATCH.

  RATE LIMIT: точне число req/хв для САМЕ цього ендпоінту на сторінці
  /api/reference явно не вказане (там є довідка про типовий патерн
  "60 запитів/хв" для частини ендпоінтів, але не підтверджено для цього
  конкретного шляху). Емпірично спостережено: відповідь має заголовок
  Cache-Control: public, max-age=30 — тобто сам DexScreener кешує на CDN
  ~30с, і часті запити на ту саму адресу все одно повертають ту саму
  "застарілу" з їхньої точки зору ціну. Це додатковий аргумент за власним
  TTL-кешем (core/position_monitor.py, PRICE_CACHE_TTL_SECONDS).
"""
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/tokens/v1/{chain_id}/{addresses}"
MAX_ADDRESSES_PER_BATCH = 30

_client = httpx.Client(timeout=10.0)

# Backoff НЕ через time.sleep(): це блокуючий виклик, а fetch_prices_usd()
# викликається з core/position_monitor.py — asyncio-задачі, що працює в
# ОДНОМУ event loop з Telethon listener'ом і control-ботом (main.py,
# asyncio.gather). Синхронний sleep() тут заморозив би геть усе на секунди
# (а при максимальному backoff — на цілу хвилину). Замість сну просто
# пропускаємо запити до _backoff_until — "очікування" природно відбувається
# між ітераціями циклу на 1с у position_monitor.py.
_backoff_until: float = 0.0
_backoff_seconds: float = 1.0
_MAX_BACKOFF_SECONDS = 60.0


def _chunk(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_prices_usd(contract_addresses: list[str], chain: str = "solana") -> dict[str, float]:
    """
    Повертає {адреса_контракту: priceUsd} для тих адрес, де DexScreener
    знайшов торгову пару. Адреси, яких нема в результаті, — просто відсутні
    в словнику (НЕ 0.0!) — викликач має явно перевіряти .get() і пропускати
    позицію цей цикл, а не трактувати відсутність як ціну 0 (це б виглядало
    як миттєвий -100% і викликало б хибний stop-loss).

    Якщо кілька пар (різні DEX/пули) знайдено для однієї адреси — береться
    пара з найбільшою ліквідністю (той самий підхід, що й у
    core/token_screener.py, для узгодженості).
    """
    global _backoff_until, _backoff_seconds

    if not contract_addresses:
        return {}

    if time.time() < _backoff_until:
        return {}

    prices: dict[str, float] = {}

    for batch in _chunk(contract_addresses, MAX_ADDRESSES_PER_BATCH):
        url = DEXSCREENER_TOKENS_URL.format(chain_id=chain, addresses=",".join(batch))
        try:
            resp = _client.get(url)
        except httpx.HTTPError as e:
            logger.warning(f"Мережева помилка запиту цін до DexScreener: {e}")
            continue

        if resp.status_code == 429:
            _backoff_until = time.time() + _backoff_seconds
            logger.warning(
                f"DexScreener 429 (rate limit) — пауза {_backoff_seconds:.0f}с перед наступною спробою"
            )
            _backoff_seconds = min(_backoff_seconds * 2, _MAX_BACKOFF_SECONDS)
            continue

        if resp.status_code != 200:
            logger.warning(f"DexScreener повернув {resp.status_code} для батчу з {len(batch)} адрес")
            continue

        _backoff_seconds = 1.0  # успішний запит — скидаємо backoff

        try:
            pairs = resp.json() or []
        except ValueError as e:
            logger.warning(f"Некоректний JSON від DexScreener: {e}")
            continue

        by_address: dict[str, list] = {}
        for p in pairs:
            addr = (p.get("baseToken") or {}).get("address")
            if addr:
                by_address.setdefault(addr, []).append(p)

        for addr, addr_pairs in by_address.items():
            best = max(addr_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
            price_str = best.get("priceUsd")
            if price_str is not None:
                try:
                    prices[addr] = float(price_str)
                except (TypeError, ValueError):
                    pass

    return prices


def fetch_price_usd(contract_address: str, chain: str = "solana") -> Optional[float]:
    """Разовий lookup ціни одного токена (напр. entry_price одразу після buy)."""
    return fetch_prices_usd([contract_address], chain).get(contract_address)
