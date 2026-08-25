"""Простой in-memory rate limiter на один процесс — без Redis/slowapi.

Считает запросы по (путь эндпоинта + IP клиента) в скользящем окне.
ВАЖНО: работает корректно только пока бэкенд — один процесс/под. Если
когда-нибудь появится несколько инстансов за балансировщиком, счётчики
будут раздельными по инстансам — тогда нужен будет Redis.

Также имейте в виду: словарь _buckets не чистится сам по себе для давно
неактивных ключей (лёгкая утечка памяти на очень долгом аптайме с большим
числом уникальных IP) — для масштаба этого приложения не критично, но если
когда-то станет критично, добавьте периодическую очистку по времени.
"""

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_buckets: dict[str, deque] = defaultdict(deque)


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(max_requests: int, window_seconds: float):
    """FastAPI-зависимость: не больше max_requests за window_seconds на
    клиента (по IP) для конкретного эндпоинта.

    Использование: dependencies=[Depends(rate_limit(5, 60))]
    """
    async def dependency(request: Request):
        key = f"{request.url.path}:{_client_key(request)}"
        now = time.monotonic()
        bucket = _buckets[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= max_requests:
            raise HTTPException(status_code=429, detail="Слишком много запросов, попробуйте чуть позже")
        bucket.append(now)
    return dependency