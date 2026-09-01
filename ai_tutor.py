"""
ИИ-репетитор. Модель — YandexGPT (Yandex Foundation Models), REST API
напрямую через httpx (тот же пакет, что уже используется в media_proxy.py) —
официального Python-SDK от Яндекса не используем, обычный HTTP-запрос
проще и прозрачнее для потокового ответа.

Два режима в одном эндпоинте:
  - task_id передан → объяснение конкретного задания/ошибки. Материал
    задания (вопрос, правильный ответ, эталон/критерии) подмешивается в
    последнее сообщение пользователя — модель обязана объяснять СТРОГО по
    нему, не придумывая другие числа или факты.
  - task_id не передан → свободный чат по темам подготовки к экзаменам.

Доступен только залогиненным (см. get_current_user, не optional-версия) —
это платный по токенам ресурс, а банк заданий и пробники и так уже открыты
гостю без входа (см. content.py/probnik.py).
"""
import json
import os
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth_dependency import get_current_user
from db import async_session
from models import Task, User
from rate_limit import rate_limit

router = APIRouter(prefix="/api/ai", tags=["ai-tutor"])

# Ключ и ID каталога — из консоли Yandex Cloud (сервисный аккаунт с ролью
# ai.languageModels.user или выше, либо обычный API-ключ). Переменные
# окружения YANDEX_API_KEY и YANDEX_FOLDER_ID — добавить в Vercel
# (Settings → Environment Variables).
YANDEX_API_KEY = os.environ["YANDEX_API_KEY"]
YANDEX_FOLDER_ID = os.environ["YANDEX_FOLDER_ID"]
YANDEX_COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

# yandexgpt/latest — версия Pro, увереннее в математике/физике, чем Lite.
# Если счёт станет ощутимым — можно попробовать "yandexgpt-lite/latest",
# сравнив качество объяснений на реальных заданиях из банка.
MODEL_URI = f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest"
MAX_TOKENS = "1024"
TEMPERATURE = 0.3  # ниже дефолтных 0.6 у Яндекса — объяснение решения должно быть точным, а не творческим

# Ограничение истории — без этого один длинный диалог может незаметно
# разогнаться до огромного количества токенов за один вызов API.
MAX_HISTORY_MESSAGES = 20

TUTOR_SYSTEM_PROMPT = (
    "Ты — доброжелательный репетитор по подготовке к ЕГЭ и ОГЭ в приложении "
    "Schooler. Объясняешь темы и ошибки понятно, по шагам, на русском "
    "языке, в тоне поддерживающего учителя, а не сухого справочника. Если "
    "в сообщении есть блок «Материал задания» — используй ТОЛЬКО факты из "
    "него (условие, правильный ответ, эталон, критерии) и не придумывай "
    "других чисел или фактов, которых там нет; если материала не хватает "
    "для точного объяснения — честно скажи об этом, а не выдумывай. "
    "Отвечай простым текстом с переносами строк, без markdown-таблиц и "
    "заголовков — это чат, а не документ."
)


class ChatMessageIn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)


class ChatIn(BaseModel):
    messages: list[ChatMessageIn] = Field(min_length=1, max_length=MAX_HISTORY_MESSAGES)
    task_id: int | None = None


async def _build_task_context(task_id: int) -> str | None:
    """Собирает текстовое описание задания для подмешивания в промпт.
    Возвращает None, если задания с таким id не существует."""
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return None

        lines = [f"Вопрос: {task.question}"]
        if task.options:
            lines.append("Варианты ответа: " + "; ".join(task.options))

        if task.part == 1:
            if task.correct_index is not None and task.options:
                lines.append(f"Правильный вариант: {task.options[task.correct_index]}")
            if task.correct_answer_text:
                lines.append(f"Правильный ответ: {task.correct_answer_text}")
        else:
            if task.correct_answer_text:
                lines.append(f"Эталонный ответ/решение: {task.correct_answer_text}")
            if task.criteria:
                lines.append(f"Критерии оценивания: {task.criteria}")

        if task.explanation:
            lines.append(f"Пояснение: {task.explanation}")

        return "\n".join(lines)


@router.post("/chat", dependencies=[Depends(rate_limit(15, 60))])
async def chat(payload: ChatIn, user: User = Depends(get_current_user)) -> StreamingResponse:
    history = [m.model_dump() for m in payload.messages[-MAX_HISTORY_MESSAGES:]]

    if payload.task_id is not None:
        context = await _build_task_context(payload.task_id)
        if context is None:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        # Материал задания подмешиваем в ПОСЛЕДНЕЕ сообщение пользователя
        # этого запроса — модель видит его при каждом ответе в диалоге, даже
        # если фронтенд не хранит системный контекст отдельно от истории.
        last_user_idx = max((i for i, m in enumerate(history) if m["role"] == "user"), default=None)
        if last_user_idx is not None:
            original = history[last_user_idx]["content"]
            history[last_user_idx] = {
                "role": "user",
                "content": f"Материал задания:\n{context}\n\nВопрос ученика: {original}",
            }

    # YandexGPT принимает сообщения как {"role", "text"} (не "content", как
    # у Anthropic/OpenAI), а системный промпт — обычным сообщением с
    # role="system" внутри того же списка, а не отдельным полем.
    yandex_messages = [{"role": "system", "text": TUTOR_SYSTEM_PROMPT}] + [
        {"role": m["role"], "text": m["content"]} for m in history
    ]

    request_body = {
        "modelUri": MODEL_URI,
        "completionOptions": {
            "stream": True,
            "temperature": TEMPERATURE,
            "maxTokens": MAX_TOKENS,
        },
        "messages": yandex_messages,
    }

    async def stream() -> AsyncIterator[bytes]:
        # ⚠️ YandexGPT в потоковом режиме присылает построчный JSON, где
        # КАЖДАЯ строка — это весь сгенерированный текст С НАЧАЛА (кумулятивно),
        # а не только новый кусок, как у Anthropic/OpenAI. Поэтому здесь
        # считаем разницу с предыдущей длиной и отдаём клиенту только её —
        # фронтенд (ai-chat-context.tsx) просто конкатенирует то, что пришло,
        # ничего менять на фронте не нужно. Это задокументированное поведение
        # Foundation Models API, но если на практике Яндекс пришлёт реальные
        # дельты вместо кумулятивного текста — эта разница уйдёт в минус и
        # здесь появится дублирующийся/оборванный текст; тогда нужно будет
        # убрать вычитание previous_text и слать full_text как есть.
        previous_text = ""
        try:
            headers = {
                "Authorization": f"Api-Key {YANDEX_API_KEY}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream(
                    "POST", YANDEX_COMPLETION_URL, json=request_body, headers=headers
                ) as response:
                    if response.status_code >= 400:
                        error_body = await response.aread()
                        raise RuntimeError(
                            f"YandexGPT ответил {response.status_code}: {error_body.decode(errors='replace')}"
                        )

                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        chunk = json.loads(line)
                        alternatives = chunk.get("result", {}).get("alternatives", [])
                        if not alternatives:
                            continue
                        full_text = alternatives[0].get("message", {}).get("text", "")
                        if len(full_text) > len(previous_text):
                            yield full_text[len(previous_text):].encode("utf-8")
                            previous_text = full_text
        except Exception as exc:
            # Стрим уже начат (заголовки ушли клиенту) — вернуть корректный
            # HTTP-код ошибки здесь нельзя, поэтому дописываем сообщение об
            # ошибке прямо в тело, а не роняем соединение молча.
            yield f"\n\n[Не удалось получить ответ: {exc}]".encode("utf-8")

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")