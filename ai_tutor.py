"""
ИИ-репетитор. Два режима в одном эндпоинте:
  - task_id передан → объяснение конкретного задания/ошибки. Модели в
    сообщение подмешивается материал задания (вопрос, правильный ответ,
    эталон/критерии) — модель обязана объяснять СТРОГО по нему, не
    придумывая другие числа или факты, которых там нет.
  - task_id не передан → свободный чат по темам подготовки к экзаменам.

Доступен только залогиненным (см. get_current_user, не optional-версия) —
это платный по токенам ресурс, а банк заданий и пробники и так уже открыты
гостю без входа (см. content.py/probnik.py).

Модель — DeepSeek (не Anthropic напрямую): для проекта из России это сняло
вопрос доступа/оплаты API — у DeepSeek нет таких ограничений, а бэкенд
всё равно работает на серверах Vercel, а не физически в РФ. Технически
используется Anthropic-совместимый эндпоинт DeepSeek
(https://api.deepseek.com/anthropic) — пакет `anthropic` и интерфейс
client.messages.stream() остаются те же, меняются только base_url, ключ
и имя модели.
"""
import os
from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth_dependency import get_current_user
from db import async_session
from models import Task, User
from rate_limit import rate_limit

router = APIRouter(prefix="/api/ai", tags=["ai-tutor"])

# Ключ — с platform.deepseek.com (НЕ console.anthropic.com), переменная
# окружения DEEPSEEK_API_KEY. Добавить в Vercel: Settings → Environment
# Variables. AsyncAnthropic по умолчанию сам читает ANTHROPIC_API_KEY —
# нам нужен другой ключ, поэтому передаём api_key и base_url явно.
_client = AsyncAnthropic(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com/anthropic",
)

# deepseek-v4-pro — сильнее в рассуждениях (важно для объяснения решений по
# математике/физике/химии), deepseek-v4-flash — дешевле и быстрее, но для
# сложных задач может объяснять более поверхностно. Если счёт по API
# станет ощутимым — можно попробовать переключиться на flash и сравнить
# качество объяснений на реальных заданиях из банка.
MODEL = "deepseek-v4-pro"
MAX_TOKENS = 1024

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
    messages = [m.model_dump() for m in payload.messages[-MAX_HISTORY_MESSAGES:]]

    if payload.task_id is not None:
        context = await _build_task_context(payload.task_id)
        if context is None:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        # Материал задания подмешиваем в ПОСЛЕДНЕЕ сообщение пользователя
        # этого запроса (а не только в первое) — модель видит его при каждом
        # ответе в диалоге, даже если фронтенд не хранит системный контекст
        # отдельно от истории сообщений.
        last_user_idx = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=None)
        if last_user_idx is not None:
            original = messages[last_user_idx]["content"]
            messages[last_user_idx] = {
                "role": "user",
                "content": f"Материал задания:\n{context}\n\nВопрос ученика: {original}",
            }

    async def stream() -> AsyncIterator[bytes]:
        try:
            async with _client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=TUTOR_SYSTEM_PROMPT,
                messages=messages,
            ) as response:
                async for text in response.text_stream:
                    yield text.encode("utf-8")
        except Exception as exc:
            # Стрим уже начат (заголовки ушли клиенту) — вернуть корректный
            # HTTP-код ошибки здесь нельзя, поэтому дописываем сообщение об
            # ошибке прямо в тело, а не роняем соединение молча.
            yield f"\n\n[Не удалось получить ответ: {exc}]".encode("utf-8")

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")