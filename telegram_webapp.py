"""Валидация initData от Telegram Mini App — см.
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import time
import urllib.parse


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 3600) -> dict | None:
    """Возвращает распарсенные данные (dict с ключом 'user' и т.п.), если
    подпись верна и данные не протухли. None — если невалидно."""
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = pairs.get("auth_date")
    if auth_date is None or time.time() - int(auth_date) > max_age_seconds:
        return None  # протухшие данные — не доверяем

    result = dict(pairs)
    if "user" in result:
        try:
            result["user"] = json.loads(result["user"])
        except (json.JSONDecodeError, TypeError):
            return None
    return result