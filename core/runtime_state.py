"""
Рантайм-стан бота, який має змінюватись "на льоту" без перезапуску процесу:
пауза торгівлі (/stop, /start), перевизначені ризик-ліміти (/setlimit) і
список користувачів/адмінів control-бота, доданих через кнопку "👥 Користувачі"
(окремо від TG_OWNER_USER_ID з .env — той завжди адмін, тут не зберігається).

Навмисно простий JSON-файл, а не SQLite: стан тут — це буквально пара прапорців
і невеликий словник, окрема таблиця в БД була б зайвою абстракцією. Файл
перечитується при КОЖНОМУ зверненні (див. risk_manager.py) — тому зміна через
/setlimit чи додавання користувача діє одразу, а не тільки при старті процесу.

Пишеться атомарно (tmp-файл + os.replace), щоб конкурентний запис з control-бота
і читання з risk_manager не могли зустріти напівзаписаний JSON.
"""
import json
import os
import threading
from typing import Any, Optional

STATE_FILE = "data/runtime_state.json"
_lock = threading.Lock()

_DEFAULT_STATE = {"paused": False, "limit_overrides": {}, "users": {}}


def _load() -> dict:
    if not os.path.exists(STATE_FILE):
        return dict(_DEFAULT_STATE)
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_STATE)
    data.setdefault("paused", False)
    data.setdefault("limit_overrides", {})
    data.setdefault("users", {})
    return data


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATE_FILE)


def is_paused() -> bool:
    with _lock:
        return _load().get("paused", False)


def set_paused(value: bool) -> None:
    with _lock:
        data = _load()
        data["paused"] = value
        _save(data)


def get_limit_overrides() -> dict:
    with _lock:
        return _load().get("limit_overrides", {})


def set_limit_override(name: str, value: Any) -> None:
    with _lock:
        data = _load()
        data["limit_overrides"][name] = value
        _save(data)


def clear_limit_override(name: str) -> bool:
    """Прибирає перевизначення, повертаючи ліміт до значення з .env. True якщо щось прибрали."""
    with _lock:
        data = _load()
        if name in data["limit_overrides"]:
            del data["limit_overrides"][name]
            _save(data)
            return True
        return False


def get_users() -> dict:
    """
    {user_id_str: "admin"|"user"} — користувачі/адміни, додані через control-бота.
    НЕ включає TG_OWNER_USER_ID з .env — той завжди адмін окремо, поза цим
    списком (щоб власника неможливо було випадково "видалити" через бота).
    """
    with _lock:
        return dict(_load().get("users", {}))


def add_user(user_id: int, role: str) -> None:
    if role not in ("admin", "user"):
        raise ValueError(f"Невідома роль: {role!r}, очікується 'admin' або 'user'")
    with _lock:
        data = _load()
        data["users"][str(user_id)] = role
        _save(data)


def remove_user(user_id: int) -> bool:
    with _lock:
        data = _load()
        if str(user_id) in data["users"]:
            del data["users"][str(user_id)]
            _save(data)
            return True
        return False


def get_user_role(user_id: int) -> Optional[str]:
    """'admin'/'user' для доданих через бота користувачів, або None якщо не в списку."""
    with _lock:
        return _load().get("users", {}).get(str(user_id))
