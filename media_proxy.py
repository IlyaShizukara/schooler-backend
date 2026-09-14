"""
Прокси для медиа (аудио/картинки) с хотлинк-защитой (нужен корректный
Referer, которого WebView/браузер сами не присылают при загрузке ресурса
по прямой ссылке). Бэкенд скачивает файл с нужными заголовками и отдаёт
его клиенту как обычный http-ресурс — без раздувания сообщений Flet
base64-инлайном (см. историю: инлайн аудио в data:-URL ломал протокол
Flet/msgpack на больших файлах).

Поддерживает Range-запросы (важно для перемотки аудио) — проксирует их
1:1 в оригинальный запрос и пробрасывает статус 206/заголовки обратно.
"""
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/media-proxy", tags=["media-proxy"])

# На какие домены разрешаем проксировать — не открываем прокси "куда угодно"
ALLOWED_HOST_SUFFIXES = ("bank-zadach.ru", "selstorage.ru", "kompege.ru",)

UPSTREAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://bank-zadach.ru/",
}

# Имена файлов на upstream-хранилищах (формулы Wiris, аудио) захэшированы
# по содержимому — значит один и тот же url физически не может отдать
# другой контент в будущем. Можно кэшировать агрессивно и без revalidate:
#   - max-age на стороне браузера — повторные открытия того же задания не
#     ходят в сеть вообще;
#   - s-maxage — специально для CDN/edge-кэшей (в т.ч. Vercel Edge Network):
#     после первого запроса ОТ ЛЮБОГО пользователя ответ оседает на CDN, и
#     дальше все остальные получают формулу с эджа, даже не долетая до
#     этой функции и тем более до selstorage.ru.
# immutable — явная подсказка браузеру не перепроверять файл вообще, пока
# не истёк max-age (иначе Chrome иногда всё равно шлёт revalidate-запрос).
CACHE_CONTROL_IMMUTABLE = "public, max-age=31536000, s-maxage=31536000, immutable"

# Один общий клиент на весь "тёплый" инстанс функции вместо нового
# httpx.AsyncClient() на каждый запрос. `async with httpx.AsyncClient()`
# на каждый вызов означало новый TCP+TLS хендшейк до selstorage.ru/
# bank-zadach.ru/kompege.ru на КАЖДУЮ картинку и КАЖДЫЙ аудиофайл — на
# странице с десятком формул это десяток лишних хендшейков подряд.
# Общий клиент держит keep-alive соединения к upstream-хостам между
# запросами (как engine в db.py держит пул к базе), так что повторные
# запросы к тому же хосту переиспользуют уже открытое соединение.
_client = httpx.AsyncClient(
    timeout=30.0,
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
)


@router.get("")
async def proxy_media(url: str, request: Request):
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if not any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES):
        raise HTTPException(status_code=400, detail="Домен не разрешён для проксирования")

    headers = dict(UPSTREAM_HEADERS)
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    # stream=True + aiter_bytes ниже: раньше `client.get()` ждал, пока
    # скачается ВЕСЬ файл (например, весь mp3), и только потом начинал
    # отдавать его клиенту одним куском (`iter([upstream.content])`) — это
    # добавляло к времени ответа полное время скачивания с upstream ещё до
    # первого байта пользователю. Теперь первые байты уходят клиенту сразу
    # по мере поступления от upstream, а не после полной загрузки файла на
    # сервере.
    req = _client.build_request("GET", url, headers=headers)
    try:
        upstream = await _client.send(req, stream=True)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Не удалось получить медиафайл")

    if upstream.status_code >= 400:
        await upstream.aclose()
        raise HTTPException(status_code=502, detail="Не удалось получить медиафайл")

    passthrough_headers = {}
    for h in ("content-type", "content-length", "content-range", "accept-ranges"):
        if h in upstream.headers:
            passthrough_headers[h] = upstream.headers[h]
    passthrough_headers.setdefault("accept-ranges", "bytes")
    passthrough_headers["cross-origin-resource-policy"] = "cross-origin"
    passthrough_headers["cache-control"] = CACHE_CONTROL_IMMUTABLE

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=passthrough_headers,
    )
