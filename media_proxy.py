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

    async with httpx.AsyncClient(timeout=30) as client:
        upstream = await client.get(url, headers=headers)

    if upstream.status_code >= 400:
        raise HTTPException(status_code=502, detail="Не удалось получить медиафайл")

    passthrough_headers = {}
    for h in ("content-type", "content-length", "content-range", "accept-ranges"):
        if h in upstream.headers:
            passthrough_headers[h] = upstream.headers[h]
    passthrough_headers.setdefault("accept-ranges", "bytes")
    passthrough_headers.setdefault("accept-ranges", "bytes")
    passthrough_headers["cross-origin-resource-policy"] = "cross-origin"

    return StreamingResponse(
        iter([upstream.content]),
        status_code=upstream.status_code,
        headers=passthrough_headers,
    )