"""
ИИ-репетитор. Использует OpenRouter для доступа к YandexGPT.
Без folder_id, без проблем с подключением.
"""
import os
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from openai import AsyncOpenAI

from auth_dependency import get_current_user
from db import async_session
from models import Task, User
from rate_limit import rate_limit

router = APIRouter(prefix="/api/ai", tags=["ai-tutor"])

# OpenRouter клиент
_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
    default_headers={
        "HTTP-Referer": "https://myschooler.ru",  # замени на свой сайт
        "X-OpenRouter-Title": "Schooler AI Tutor",
    }
)

# Модель YandexGPT через OpenRouter
MODEL = "deepseek/deepseek-chat:free"
MAX_TOKENS = 1024
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
    messages = [m.model_dump() for m in payload.messages[-MAX_HISTORY_MESSAGES:]]

    if payload.task_id is not None:
        context = await _build_task_context(payload.task_id)
        if context is None:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        last_user_idx = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=None)
        if last_user_idx is not None:
            original = messages[last_user_idx]["content"]
            messages[last_user_idx] = {
                "role": "user",
                "content": f"Материал задания:\n{context}\n\nВопрос ученика: {original}",
            }

    openai_messages = [
        {"role": "system", "content": TUTOR_SYSTEM_PROMPT},
        *[{"role": m["role"], "content": m["content"]} for m in messages],
    ]

    async def stream() -> AsyncIterator[bytes]:
        try:
            stream = await _client.chat.completions.create(
                model=MODEL,
                messages=openai_messages,
                temperature=0.7,
                max_tokens=MAX_TOKENS,
                stream=True,
            )
            async for chunk in stream:
                content = chunk.choices[0].delta.content
                if content:
                    yield content.encode("utf-8")
        except Exception as exc:
            yield f"\n\n[Не удалось получить ответ: {exc}]".encode("utf-8")

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")