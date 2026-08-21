"""
Централізована конфігурація. Все читається з .env — ніяких хардкод-секретів у коді.
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

from core import runtime_state

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


@dataclass(frozen=True)
class Settings:
    # Telegram (user-акаунт listener, через Telethon)
    tg_api_id: int = _int("TG_API_ID", 0)
    tg_api_hash: str = os.getenv("TG_API_HASH", "")
    tg_session_name: str = os.getenv("TG_SESSION_NAME", "signal_bot_session")
    tg_channel_username: str = os.getenv("TG_CHANNEL_USERNAME", "")
    tg_notify_chat_id: str = os.getenv("TG_NOTIFY_CHAT_ID", "")

    # Telegram control-бот (окремий бот-акаунт через Bot API/aiogram)
    tg_bot_token: str = os.getenv("TG_BOT_TOKEN", "")
    tg_owner_user_id: int = _int("TG_OWNER_USER_ID", 0)

    # Claude
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Solana
    solana_private_key: str = os.getenv("SOLANA_PRIVATE_KEY", "")
    solana_rpc_url: str = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

    # OKX DEX API
    okx_api_key: str = os.getenv("OKX_API_KEY", "")
    okx_secret_key: str = os.getenv("OKX_SECRET_KEY", "")
    okx_passphrase: str = os.getenv("OKX_PASSPHRASE", "")
    okx_project_id: str = os.getenv("OKX_PROJECT_ID", "")

    # Режим
    dry_run: bool = _bool("DRY_RUN", True)
    chain: str = os.getenv("CHAIN", "solana")

    # Ризик-менеджмент
    max_position_pct: float = _float("MAX_POSITION_PCT", 5.0)
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", 5)
    max_slippage_pct: float = _float("MAX_SLIPPAGE_PCT", 3.0)
    max_price_impact_pct: float = _float("MAX_PRICE_IMPACT_PCT", 5.0)
    daily_loss_limit_pct: float = _float("DAILY_LOSS_LIMIT_PCT", 10.0)
    trade_cooldown_seconds: int = _int("TRADE_COOLDOWN_SECONDS", 30)
    min_token_age_hours: float = _float("MIN_TOKEN_AGE_HOURS", 24)
    min_liquidity_usd: float = _float("MIN_LIQUIDITY_USD", 20000)
    min_signal_confidence: float = _float("MIN_SIGNAL_CONFIDENCE", 0.7)
    default_stop_loss_pct: float = _float("DEFAULT_STOP_LOSS_PCT", 25.0)
    default_take_profit_pct: float = _float("DEFAULT_TAKE_PROFIT_PCT", 100.0)


settings = Settings()


def validate_settings() -> list[str]:
    """Повертає список помилок конфігурації. Викликати перед стартом бота."""
    errors = []
    if not settings.tg_api_id or not settings.tg_api_hash:
        errors.append("TG_API_ID / TG_API_HASH не задані (потрібні з my.telegram.org)")
    if not settings.tg_channel_username:
        errors.append("TG_CHANNEL_USERNAME не задано")
    if not settings.anthropic_api_key:
        errors.append("ANTHROPIC_API_KEY не задано")
    if not settings.tg_bot_token:
        errors.append("TG_BOT_TOKEN не задано (потрібен для Telegram control-бота, @BotFather)")
    if not settings.tg_owner_user_id:
        errors.append("TG_OWNER_USER_ID не задано (інакше control-бот не пустить нікого, включно з тобою)")
    if not settings.dry_run:
        if not settings.solana_private_key:
            errors.append("SOLANA_PRIVATE_KEY не задано (обов'язково для реальних угод)")
        if not settings.okx_api_key:
            errors.append("OKX_API_KEY не задано (обов'язково для реальних угод)")
    return errors


# --- Ризик-ліміти, які можна перевизначити "на льоту" через /setlimit ---
# ключ — те саме ім'я, що і в .env / команді /setlimit; значення — (поле Settings, тип, одиниці для показу)
LIMIT_FIELDS: dict[str, tuple[str, type, str]] = {
    "MAX_POSITION_PCT": ("max_position_pct", float, "% від балансу"),
    "MAX_OPEN_POSITIONS": ("max_open_positions", int, "позицій"),
    "MAX_SLIPPAGE_PCT": ("max_slippage_pct", float, "%"),
    "MAX_PRICE_IMPACT_PCT": ("max_price_impact_pct", float, "%"),
    "DAILY_LOSS_LIMIT_PCT": ("daily_loss_limit_pct", float, "% від балансу/день"),
    "TRADE_COOLDOWN_SECONDS": ("trade_cooldown_seconds", int, "сек"),
    "MIN_TOKEN_AGE_HOURS": ("min_token_age_hours", float, "год"),
    "MIN_LIQUIDITY_USD": ("min_liquidity_usd", float, "$"),
    "MIN_SIGNAL_CONFIDENCE": ("min_signal_confidence", float, "(0-1)"),
    "DEFAULT_STOP_LOSS_PCT": ("default_stop_loss_pct", float, "%"),
    "DEFAULT_TAKE_PROFIT_PCT": ("default_take_profit_pct", float, "%"),
}


def get_limit(env_name: str):
    """
    Ефективне значення ліміту: перевизначення з runtime_state (/setlimit),
    якщо воно є, інакше — значення з .env. Читає файл щоразу (не кешує),
    тому /setlimit діє миттєво для наступної ж перевірки сигналу.
    """
    field_name, _, _ = LIMIT_FIELDS[env_name]
    overrides = runtime_state.get_limit_overrides()
    if env_name in overrides:
        return overrides[env_name]
    return getattr(settings, field_name)


def is_limit_overridden(env_name: str) -> bool:
    return env_name in runtime_state.get_limit_overrides()
