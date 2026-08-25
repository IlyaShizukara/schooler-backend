"""Общее хранилище state для OAuth-флоу VK ID / Яндекс ID.
In-memory, как rate_limit.py — тот же компромисс: ок для одного процесса,
при масштабировании на несколько инстансов потребует Redis."""

import secrets
import time

STATE_TTL_SECONDS = 600
_states: dict[str, dict] = {}


def create_state(mode: str, telegram_id: int | None = None, **extra) -> str:
    state = secrets.token_urlsafe(24)
    _states[state] = {"mode": mode, "telegram_id": telegram_id, "created": time.monotonic(), **extra}
    return state


def pop_state(state: str) -> dict | None:
    data = _states.pop(state, None)
    if data is None:
        return None
    if time.monotonic() - data["created"] > STATE_TTL_SECONDS:
        return None
    return data