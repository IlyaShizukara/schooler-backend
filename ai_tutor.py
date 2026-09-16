"""
ИИ-репетитор. Модель — YandexGPT (Yandex Foundation Models), REST API
напрямую через httpx (тот же пакет, что уже используется в media_proxy.py) —
официального Python-SDK от Яндекса не используем, обычный HTTP-запрос
проще и прозрачнее для потокового ответа.

Два режима в одном эндпоинте:
  - task_id передан → объяснение конкретного задания/ошибки. Материал
    задания (вопрос, правильный ответ, эталон/критерии, а также последняя
    попытка ЭТОГО ученика — что он ответил и было ли это верно) подмешивается
    в сообщение пользователя — модель обязана объяснять СТРОГО по нему и
    фокусироваться на конкретной ошибке ученика, не придумывая другие числа
    или факты.
  - task_id не передан → свободный чат по темам подготовки к экзаменам.

Доступен только залогиненным (см. get_current_user, не optional-версия) —
это платный по токенам ресурс, а банк заданий и пробники и так уже открыты
гостю без входа (см. content.py/probnik.py).
"""
import datetime as dt
import json
import logging
import os
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from auth_dependency import get_current_user
from db import async_session
from models import Attempt, ChatMessage, ChatSession, Subject, Task, TaskType, User, UserProfile
from rate_limit import rate_limit

logger = logging.getLogger("ai_tutor")

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
# Было 1024 — для части 2 (развёрнутый ответ, критерии оценивания часто
# сами по себе длинные) объяснение по шагам легко упирается в этот лимит и
# обрывается на середине, особенно если модель сначала пересказывает
# критерии, а потом уже объясняет. 1536 даёт больше запаса; если счёт по
# токенам станет ощутимым — можно вернуть обратно или уменьшить выборочно
# для part=1.
MAX_TOKENS = "1536"
TEMPERATURE = 0.3  # ниже дефолтных 0.6 у Яндекса — объяснение решения должно быть точным, а не творческим

# Ограничение истории — без этого один длинный диалог может незаметно
# разогнаться до огромного количества токенов за один вызов API.
MAX_HISTORY_MESSAGES = 20

_BASE_SYSTEM_PROMPT = (
    "Ты — доброжелательный репетитор по подготовке к ЕГЭ и ОГЭ в приложении "
    "Schooler. Объясняешь темы и ошибки понятно, по шагам, на русском "
    "языке, в тоне поддерживающего учителя, а не сухого справочника. Если "
    "в сообщении есть блок «Материал задания» — используй ТОЛЬКО факты из "
    "него (условие, правильный ответ, эталон, критерии) и не придумывай "
    "других чисел или фактов, которых там нет; если материала не хватает "
    "для точного объяснения — честно скажи об этом, а не выдумывай. Если "
    "в материале указан «Ответ ученика на это задание» — это именно то, "
    "что ответил ученик, и именно на этой конкретной ошибке нужно "
    "сфокусироваться: покажи, на каком шаге и почему его ответ разошёлся "
    "с правильным, а не объясняй решение с нуля так, будто не знаешь, что "
    "он уже пробовал. "
    "Отвечай простым текстом с переносами строк, без markdown-таблиц и "
    "заголовков — это чат, а не документ.\n\n"
    "Метод объяснения — сократический, не выдавай сразу готовое решение. "
    "Когда ученик просит разобрать ошибку или задание: сначала задай один "
    "наводящий вопрос или дай маленькую подсказку — например, укажи, на "
    "каком шаге искать ошибку, или напомни нужную формулу/правило, не "
    "решая задачу целиком. Дай ученику попробовать самому в следующем "
    "сообщении. Полное пошаговое решение объясняй сразу только если: "
    "ученик явно просит решение целиком («реши полностью», «покажи весь "
    "ход решения» и т.п.), или это уже не первая подсказка в этом диалоге "
    "по этой ошибке и ученик всё ещё не разобрался, или вопрос ученика "
    "вообще не про конкретную ошибку, а общий («объясни тему X»,"
    " «как решать такие задачи») — тогда сократический подход неуместен, "
    "объясняй как обычно. Не изображай подсказку там, где ученик и так "
    "явно просит развёрнутый ответ — это должно ощущаться как помощь, а "
    "не как искусственная задержка."
)


def _build_system_prompt(profile: UserProfile | None) -> str:
    """Персонализирует системный промпт под профиль ученика — раньше он был
    одинаковым для всех, хотя объяснение производной для 9-го класса (ОГЭ)
    и для 11-го (ЕГЭ) должно отличаться и по глубине, и по нотации."""
    if profile is None or (not profile.exam_type and not profile.grade):
        return _BASE_SYSTEM_PROMPT

    parts = []
    if profile.exam_type:
        parts.append(f"готовится к {profile.exam_type}")
    if profile.grade:
        parts.append(f"{profile.grade} класс")
    student_context = (
        "Контекст ученика: " + ", ".join(parts) + ". Учитывай уровень сложности, "
        "терминологию и формат заданий, характерные именно для этого экзамена — "
        "не объясняй методы, которые не входят в его программу."
    )
    return _BASE_SYSTEM_PROMPT + "\n\n" + student_context


class ChatMessageIn(BaseModel):
    """Оставлен для истории/совместимости — сам эндпоинт больше не читает
    список messages целиком (см. ChatIn ниже), но формат одного сообщения
    такой же, каким его теперь возвращает GET /chat/history."""
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)


class ChatIn(BaseModel):
    # Раньше сюда уходил весь массив messages с фронта на каждый запрос —
    # источником истины была история, которую держал React-стейт на фронте
    # (и которая терялась при перезагрузке страницы/закрытии чата). Теперь
    # источник истины — БД: фронт шлёт только новое сообщение пользователя,
    # а предыдущие MAX_HISTORY_MESSAGES бэкенд сам подгружает из ChatMessage
    # по сессии (см. _get_or_create_chat_session). Меньше трафика на каждый
    # запрос и нет риска, что фронт и БД разойдутся в том, что "реально"
    # сохранено.
    message: str = Field(min_length=1, max_length=4000)
    task_id: int | None = None


class ChatHistoryMessageOut(BaseModel):
    role: str
    content: str


async def _build_task_context(session, task_id: int, user_telegram_id: int) -> str | None:
    """Собирает текстовое описание задания для подмешивания в промпт.
    Возвращает None, если задания с таким id не существует.

    Принимает session снаружи (не открывает свой) — вызывается вместе с
    загрузкой профиля пользователя в одном async_session-блоке в chat(),
    вместо двух отдельных open/close сессий на один HTTP-запрос."""
    task = await session.get(Task, task_id)
    if task is None:
        return None

    subject = await session.get(Subject, task.subject_id) if task.subject_id else None

    lines = []
    if subject is not None:
        # Название предмета помогает модели выбрать правильный регистр и
        # нотацию (например, "докажите" в геометрии vs формальное решение
        # в алгебре) — раньше в контекст уходил только текст вопроса без
        # указания, из какого он предмета.
        lines.append(f"Предмет: {subject.name}")
    lines.append(f"Вопрос: {task.question}")
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

    # ⚠️ Раньше здесь заканчивалось — модель знала правильный ответ, но НЕ
    # знала, что конкретно ответил ученик. В итоге "Объясни мою ошибку"
    # объяснялось с нуля, а не указанием на саму ошибку. Теперь подмешиваем
    # последнюю попытку ЭТОГО ученика по ЭТОМУ заданию (Attempt уже
    # отфильтрован по user_telegram_id — чужие попытки увидеть невозможно).
    last_attempt = (
        await session.execute(
            select(Attempt)
            .where(Attempt.task_id == task_id, Attempt.user_telegram_id == user_telegram_id)
            .order_by(Attempt.answered_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if last_attempt is not None:
        if (
            task.task_type == TaskType.mcq
            and last_attempt.selected_index is not None
            and task.options
            and 0 <= last_attempt.selected_index < len(task.options)
        ):
            given_answer = task.options[last_attempt.selected_index]
        else:
            given_answer = last_attempt.answer_text or "—"
        verdict = "верно" if last_attempt.is_correct else "неверно"
        lines.append(f"Ответ ученика на это задание: {given_answer} ({verdict})")

    return "\n".join(lines)


async def _get_chat_session(session, user_telegram_id: int, task_id: int | None) -> ChatSession | None:
    """Находит существующую сессию чата для (пользователь, task_id).
    task_id=None здесь корректно транслируется SQLAlchemy в "IS NULL", а не
    в сравнение с NULL (это тот редкий случай, когда `== None` в SQLAlchemy
    делает именно то, что нужно)."""
    return (
        await session.execute(
            select(ChatSession)
            .where(ChatSession.user_telegram_id == user_telegram_id, ChatSession.task_id == task_id)
            .order_by(ChatSession.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _get_or_create_chat_session(session, user_telegram_id: int, task_id: int | None) -> ChatSession:
    existing = await _get_chat_session(session, user_telegram_id, task_id)
    if existing is not None:
        return existing
    new_session = ChatSession(user_telegram_id=user_telegram_id, task_id=task_id)
    session.add(new_session)
    await session.flush()  # получить new_session.id до commit, он нужен сразу для сообщений
    return new_session


async def _load_chat_history(session, chat_session_id: int, limit: int) -> list[dict]:
    """Последние `limit` сообщений сессии в хронологическом порядке (старые
    → новые) — то, что уходит в контекст модели."""
    rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == chat_session_id)
            .order_by(ChatMessage.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


async def _save_chat_message(chat_session_id: int, role: str, content: str) -> None:
    """Отдельный short-lived session — вызывается и до старта стрима (для
    сообщения пользователя), и после его завершения (для ответа модели),
    в разное время относительно основной session в chat()."""
    if not content:
        return
    async with async_session() as session:
        session.add(ChatMessage(session_id=chat_session_id, role=role, content=content))
        chat_session = await session.get(ChatSession, chat_session_id)
        if chat_session is not None:
            chat_session.last_message_at = dt.datetime.now(dt.timezone.utc)
        await session.commit()


@router.get("/chat/history", response_model=list[ChatHistoryMessageOut])
async def chat_history(task_id: int | None = None, user: User = Depends(get_current_user)) -> list[ChatHistoryMessageOut]:
    """Отдаёт сохранённую переписку по (пользователь, task_id) — фронт
    вызывает это при открытии чата вместо того, чтобы всегда начинать с
    чистого листа."""
    async with async_session() as session:
        chat_session = await _get_chat_session(session, user.telegram_id, task_id)
        if chat_session is None:
            return []
        rows = (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == chat_session.id)
                .order_by(ChatMessage.id)
            )
        ).scalars().all()
        return [ChatHistoryMessageOut(role=m.role, content=m.content) for m in rows]


@router.post("/chat", dependencies=[Depends(rate_limit(15, 60))])
async def chat(payload: ChatIn, user: User = Depends(get_current_user)) -> StreamingResponse:
    # Профиль (для персонализации промпта), сессия чата и контекст задания —
    # в одном async_session-блоке, раз всё это нужно на старте запроса.
    async with async_session() as session:
        profile = await session.get(UserProfile, user.telegram_id)

        context = None
        if payload.task_id is not None:
            context = await _build_task_context(session, payload.task_id, user.telegram_id)
            if context is None:
                raise HTTPException(status_code=404, detail="Задание не найдено")

        chat_session = await _get_or_create_chat_session(session, user.telegram_id, payload.task_id)
        # История ДО нового сообщения — само новое сообщение добавляем в
        # yandex_messages отдельно ниже, чтобы не гонять его туда-обратно
        # через БД лишний раз в рамках одного запроса.
        history = await _load_chat_history(session, chat_session.id, MAX_HISTORY_MESSAGES)

        await session.commit()

    # Сохраняем сообщение пользователя сразу, а не после ответа модели —
    # если стрим ниже упадёт до получения хоть какого-то текста, сам вопрос
    # ученика всё равно не потеряется.
    await _save_chat_message(chat_session.id, "user", payload.message)

    user_content = payload.message
    if context is not None:
        # Материал задания подмешиваем в ЭТО сообщение — модель видит его
        # при каждом ответе в диалоге по данному заданию. В БД при этом
        # сохраняется чистый payload.message (см. выше), а не эта склейка —
        # иначе при следующем открытии чата ученик увидел бы служебный
        # текст вместо своего реального вопроса.
        user_content = f"Материал задания:\n{context}\n\nВопрос ученика: {payload.message}"

    # YandexGPT принимает сообщения как {"role", "text"} (не "content", как
    # у Anthropic/OpenAI), а системный промпт — обычным сообщением с
    # role="system" внутри того же списка, а не отдельным полем.
    yandex_messages = (
        [{"role": "system", "text": _build_system_prompt(profile)}]
        + [{"role": m["role"], "text": m["content"]} for m in history]
        + [{"role": "user", "text": user_content}]
    )

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
        except Exception:
            # Раньше сюда попадал str(exc) целиком — то есть текст реальной
            # ошибки httpx (может включать детали запроса/ответа) прямо в
            # чат пользователю. Технические детали теперь только в логах
            # сервера (logger.exception печатает полный traceback), а
            # пользователь видит человеческое сообщение без утечки
            # внутренностей.
            logger.exception(
                "Сбой стриминга ответа YandexGPT (user_telegram_id=%s, task_id=%s)",
                user.telegram_id, payload.task_id,
            )
            error_note = "\n\n[Не получилось получить ответ — попробуйте отправить сообщение ещё раз через пару секунд.]"
            previous_text += error_note
            yield error_note.encode("utf-8")
        finally:
            # Сохраняем то, что успело сгенерироваться, даже если стрим
            # прервался на середине (сеть, таймаут, клиент закрыл чат) —
            # частичный ответ в истории лучше, чем полностью потерянный.
            # Если ничего не пришло вообще (previous_text пуст) — сообщение
            # не сохраняется, _save_chat_message сама это игнорирует.
            await _save_chat_message(chat_session.id, "assistant", previous_text)

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")