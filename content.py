import datetime as dt
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select

from auth_dependency import get_current_user
from db import async_session
from models import Attempt, ExamSession, Subject, Task, TaskType, Topic, User
from schemas import (
    AnswerIn,
    AnswerOut,
    HeatmapPoint,
    ProgressSummaryOut,
    SubjectOut,
    TaskOut,
    TopicOut,
    WeakSpotOut,
    WeeklyActivityPoint,
)

from xp import award_xp, XP_CORRECT, XP_INCORRECT
from rate_limit import rate_limit


router = APIRouter(prefix="/api", tags=["content"])

_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _normalize_answer(value) -> str:
    """Приводим краткий ответ к единому виду перед сравнением: убираем пробелы
    по краям, регистр, множественные пробелы внутри и разницу ё/е — в реальных
    ответах ЕГЭ эти мелочи не должны считаться ошибкой. Принимает не только
    строку, но и число/что угодно — приводим через str()."""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    normalized = " ".join(text.strip().lower().split())
    return normalized.replace("ё", "е")


def _try_parse_number(value) -> float | None:
    """Пытается прочитать значение как число, понимая запятую как десятичный
    разделитель (частый случай в русскоязычных коротких ответах вида "3,5").
    Возвращает None, если значение на число не похоже."""
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_correct_answer(raw: str | None):
    """Эталонный ответ хранится в БД как JSON (см. db_insert.py::add_task) —
    это может быть строка, число или список допустимых вариантов, например:
    '"Москва"', '24', '["24", "двадцать четыре"]'. Для заданий, добавленных
    ДО этого изменения, значение — обычный текст без JSON-обёртки; тогда
    json.loads упадёт, и мы просто используем текст как есть (обратная
    совместимость, миграция данных не требуется)."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw


def _answer_matches(user_answer: str | None, raw_correct: str | None) -> bool:
    """Сверяет ответ пользователя с эталоном, который может быть строкой,
    числом или списком допустимых вариантов (в любой комбинации строк/чисел).
    Совпадение засчитывается, если хотя бы один вариант совпадает с ответом
    пользователя текстово (после нормализации) ИЛИ оба значения читаются
    как одно и то же число (так "24", "24.0" и "24,0" считаются одним и тем
    же ответом)."""
    correct = _parse_correct_answer(raw_correct)
    if correct is None:
        return False

    candidates = correct if isinstance(correct, list) else [correct]

    user_norm = _normalize_answer(user_answer)
    user_num = _try_parse_number(user_answer)

    for candidate in candidates:
        if _normalize_answer(candidate) == user_norm:
            return True
        candidate_num = _try_parse_number(candidate)
        if user_num is not None and candidate_num is not None and user_num == candidate_num:
            return True

    return False


def _display_correct_answer(raw: str | None) -> str | None:
    """Готовит эталонный ответ для показа пользователю после ошибки: если
    в БД хранится список вариантов — показываем их через " / ", иначе просто
    само значение в виде строки."""
    correct = _parse_correct_answer(raw)
    if correct is None:
        return None
    if isinstance(correct, list):
        return " / ".join(str(c) for c in correct)
    return str(correct)


async def _load_all_subject_stats(
    session, user_telegram_id: int
) -> tuple[list[Subject], dict[int, SubjectOut]]:
    """Считает статистику по ВСЕМ предметам за 3 запроса вместо 4 запросов
    на каждый предмет по отдельности (было 44 запроса на 11 предметов —
    самая частая причина «долго грузит» при росте банка заданий)."""
    subjects = (await session.execute(select(Subject).order_by(Subject.id))).scalars().all()

    totals = dict(
        (await session.execute(
            select(Task.subject_id, func.count(Task.id)).group_by(Task.subject_id)
        )).all()
    )

    solved = dict(
        (await session.execute(
            select(Task.subject_id, func.count(func.distinct(Attempt.task_id)))
            .select_from(Attempt)
            .join(Task, Task.id == Attempt.task_id)
            .where(Attempt.user_telegram_id == user_telegram_id)
            .group_by(Task.subject_id)
        )).all()
    )

    attempts_raw = (
        await session.execute(
            select(
                Task.subject_id,
                func.count(Attempt.id),
                func.sum(case((Attempt.is_correct.is_(True), 1), else_=0)),
            )
            .select_from(Attempt)
            .join(Task, Task.id == Attempt.task_id)
            .where(Attempt.user_telegram_id == user_telegram_id)
            .group_by(Task.subject_id)
        )
    ).all()
    attempts = {row[0]: (row[1], row[2] or 0) for row in attempts_raw}

    total_points_by_subject = dict(
        (await session.execute(
            select(Task.subject_id, func.sum(Task.points)).group_by(Task.subject_id)
        )).all()
    )

    stats: dict[int, SubjectOut] = {}
    for s in subjects:
        total = totals.get(s.id, 0)
        solved_n = solved.get(s.id, 0)
        attempts_total, attempts_correct = attempts.get(s.id, (0, 0))
        percent = round(solved_n / total * 100) if total else 0
        accuracy = round(attempts_correct / attempts_total * 100) if attempts_total else 0
        stats[s.id] = SubjectOut(
            slug=s.slug, name=s.name, icon=s.icon, color=s.color,
            solved=solved_n, total=total, total_points=total_points_by_subject.get(s.id, 0) or 0,
            percent=percent, accuracy=accuracy,
        )

    return subjects, stats


@router.get("/subjects", response_model=list[SubjectOut])
async def list_subjects(user: User = Depends(get_current_user)) -> list[SubjectOut]:
    async with async_session() as session:
        subjects, stats = await _load_all_subject_stats(session, user.telegram_id)
        return [stats[s.id] for s in subjects]


@router.get("/subjects/{slug}/topics", response_model=list[TopicOut])
async def list_topics(slug: str, user: User = Depends(get_current_user)) -> list[TopicOut]:
    async with async_session() as session:
        subject = (
            await session.execute(select(Subject).where(Subject.slug == slug))
        ).scalar_one_or_none()
        if subject is None:
            raise HTTPException(status_code=404, detail="Предмет не найден")

        topics = (
            await session.execute(
                select(Topic)
                .where(Topic.subject_id == subject.id)
                .order_by(Topic.task_number.is_(None), Topic.task_number, Topic.name)
            )
        ).scalars().all()

        # totals/solved/attempts посчитаны сразу по всем темам предмета (3 запроса,
        # не по одному на тему) — ключ None соответствует заданиям без темы
        totals = dict(
            (await session.execute(
                select(Task.topic_id, func.count(Task.id))
                .where(Task.subject_id == subject.id)
                .group_by(Task.topic_id)
            )).all()
        )

        solved = dict(
            (await session.execute(
                select(Task.topic_id, func.count(func.distinct(Attempt.task_id)))
                .select_from(Attempt)
                .join(Task, Task.id == Attempt.task_id)
                .where(Task.subject_id == subject.id, Attempt.user_telegram_id == user.telegram_id)
                .group_by(Task.topic_id)
            )).all()
        )

        attempts_raw = (
            await session.execute(
                select(
                    Task.topic_id,
                    func.count(Attempt.id),
                    func.sum(case((Attempt.is_correct.is_(True), 1), else_=0)),
                )
                .select_from(Attempt)
                .join(Task, Task.id == Attempt.task_id)
                .where(Task.subject_id == subject.id, Attempt.user_telegram_id == user.telegram_id)
                .group_by(Task.topic_id)
            )
        ).all()
        attempts = {row[0]: (row[1], row[2] or 0) for row in attempts_raw}

        points_by_topic = dict(
            (await session.execute(
                select(Task.topic_id, func.sum(Task.points))
                .where(Task.subject_id == subject.id)
                .group_by(Task.topic_id)
            )).all()
        )

        def build_topic_out(
            topic_id: int | None, name: str, difficulty: str | None = None,
            task_number: int | None = None, task_number_to: int | None = None,
        ) -> TopicOut:
            total = totals.get(topic_id, 0)
            solved_n = solved.get(topic_id, 0)
            attempts_total, attempts_correct = attempts.get(topic_id, (0, 0))
            percent = round(solved_n / total * 100) if total else 0
            accuracy = round(attempts_correct / attempts_total * 100) if attempts_total else 0
            return TopicOut(
                name=name, topic_id=topic_id, difficulty=difficulty, task_number=task_number, task_number_to=task_number_to,
                solved=solved_n, total=total, total_points=points_by_topic.get(topic_id, 0) or 0,
                percent=percent, accuracy=accuracy,
            )

        result = [build_topic_out(t.id, t.name, t.difficulty, t.task_number, t.task_number_to) for t in topics]
        if totals.get(None, 0) > 0:
            result.append(build_topic_out(None, "Без темы"))
        return result


@router.get("/subjects/{slug}/next-task", response_model=TaskOut)
async def next_task(
    slug: str,
    topic_id: int | None = Query(default=None, description="Фильтр по теме; -1 = задания без темы"),
    user: User = Depends(get_current_user),
) -> TaskOut:
    async with async_session() as session:
        subject = (
            await session.execute(select(Subject).where(Subject.slug == slug))
        ).scalar_one_or_none()
        if subject is None:
            raise HTTPException(status_code=404, detail="Предмет не найден")

        filters = [Task.subject_id == subject.id]
        if topic_id is not None:
            filters.append(Task.topic_id.is_(None) if topic_id == -1 else Task.topic_id == topic_id)

        # Сначала пробуем найти задание, которое пользователь ещё не решал.
        answered_ids_subq = (
            select(Attempt.task_id)
            .where(Attempt.user_telegram_id == user.telegram_id)
            .scalar_subquery()
        )
        task = (
            await session.execute(
                select(Task)
                .where(*filters, Task.id.not_in(answered_ids_subq))
                .order_by(func.random())
                .limit(1)
            )
        ).scalar_one_or_none()

        # Если нерешённых не осталось — даём случайное задание повторно.
        if task is None:
            task = (
                await session.execute(
                    select(Task).where(*filters).order_by(func.random()).limit(1)
                )
            ).scalar_one_or_none()

        if task is None:
            raise HTTPException(status_code=404, detail="Для этой темы/предмета пока нет заданий")

        topic_name = None
        if task.topic_id is not None:
            topic = await session.get(Topic, task.topic_id)
            topic_name = topic.name if topic else None

        # Для части 2 (развёрнутый ответ) автоматическая проверка не имеет
        # смысла — сразу отдаём эталон/критерии вместе с заданием, фронтенд
        # покажет их по кнопке "Показать ответ" без отправки на /answer.
        # Для части 1 оба поля остаются None — ответ раскрывается только
        # после POST /tasks/{id}/answer, как и раньше.
        reveal_answer = task.part == 2
        return TaskOut(
            id=task.id,
            subject_slug=slug,
            topic=topic_name,
            task_type=task.task_type.value,
            part=task.part,
            question=task.question,
            options=task.options,
            points=task.points,
            image_urls=task.image_urls,
            file_urls=task.file_urls,
            correct_answer_text=_display_correct_answer(task.correct_answer_text) if reveal_answer else None,
            explanation=task.explanation if reveal_answer else None,
            task_number=task.task_number,  # ← добавлено
        )


DOUBLE_SUBMIT_WINDOW_SECONDS = 5


@router.post("/tasks/{task_id}/answer", response_model=AnswerOut, dependencies=[Depends(rate_limit(20, 60))])
async def submit_answer(
    task_id: int, payload: AnswerIn, user: User = Depends(get_current_user)
) -> AnswerOut:
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Задание не найдено")

        if task.part == 2:
            raise HTTPException(
                status_code=400,
                detail="Задания части 2 не проверяются автоматически — эталон уже показан вместе с заданием",
            )

        # Защита от двойного клика: вне пробника один и тот же task_id можно
        # решать повторно (next-task может выдать его снова, спустя время) —
        # поэтому нельзя просто запретить повтор навсегда по (user, task_id).
        # Вместо этого — короткое окно: если та же попытка уже была сохранена
        # буквально пару секунд назад, считаем это дублем клика и просто
        # отдаём тот же результат, не создавая вторую запись и не начисляя XP
        # повторно.
        recent_attempt = (
            await session.execute(
                select(Attempt)
                .where(
                    Attempt.user_telegram_id == user.telegram_id,
                    Attempt.task_id == task.id,
                    Attempt.exam_session_id.is_(None),
                )
                .order_by(Attempt.answered_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if recent_attempt is not None:
            age_seconds = (dt.datetime.now(dt.timezone.utc) - recent_attempt.answered_at).total_seconds()
            if age_seconds < DOUBLE_SUBMIT_WINDOW_SECONDS:
                return AnswerOut(
                    is_correct=recent_attempt.is_correct,
                    correct_index=task.correct_index,
                    correct_answer_text=_display_correct_answer(task.correct_answer_text),
                    explanation=task.explanation,
                )

        if task.task_type == TaskType.short_answer:
            is_correct = _answer_matches(payload.answer_text, task.correct_answer_text)
        else:
            is_correct = payload.selected_index == task.correct_index

        session.add(
            Attempt(user_telegram_id=user.telegram_id, task_id=task.id, is_correct=is_correct)
        )
        await award_xp(session, user.telegram_id, XP_CORRECT if is_correct else XP_INCORRECT)
        await session.commit()

        return AnswerOut(
            is_correct=is_correct,
            correct_index=task.correct_index,
            correct_answer_text=_display_correct_answer(task.correct_answer_text),
            explanation=task.explanation,
        )


@router.get("/progress/summary", response_model=ProgressSummaryOut)
async def progress_summary(user: User = Depends(get_current_user)) -> ProgressSummaryOut:
    async with async_session() as session:
        subjects, stats = await _load_all_subject_stats(session, user.telegram_id)
        by_subject = [stats[s.id] for s in subjects]

        total_solved = sum(s.solved for s in by_subject)
        total_attempts = await session.scalar(
            select(func.count()).select_from(Attempt).where(Attempt.user_telegram_id == user.telegram_id)
        ) or 0
        total_correct = await session.scalar(
            select(func.count())
            .select_from(Attempt)
            .where(Attempt.user_telegram_id == user.telegram_id, Attempt.is_correct.is_(True))
        ) or 0
        accuracy = round(total_correct / total_attempts * 100) if total_attempts else 0

        probniks_count = await session.scalar(
            select(func.count())
            .select_from(ExamSession)
            .where(ExamSession.user_telegram_id == user.telegram_id, ExamSession.finished_at.is_not(None))
        ) or 0

        # Активность за последние 7 дней (по дате попытки, в UTC)
        today = dt.datetime.now(dt.timezone.utc).date()
        week_start = today - dt.timedelta(days=6)
        rows = (
            await session.execute(
                select(func.date(Attempt.answered_at), func.count())
                .where(
                    Attempt.user_telegram_id == user.telegram_id,
                    func.date(Attempt.answered_at) >= week_start,
                )
                .group_by(func.date(Attempt.answered_at))
            )
        ).all()
        counts_by_date = {row[0]: row[1] for row in rows}
        weekly_activity = []
        for i in range(7):
            d = week_start + dt.timedelta(days=i)
            weekly_activity.append(
                WeeklyActivityPoint(day=_WEEKDAY_RU[d.weekday()], count=counts_by_date.get(d, 0))
            )

        # Слабые места — предметы с наименьшей точностью среди тех, где были попытки
        attempted = [s for s in by_subject if s.total and s.solved]
        weakest = sorted(attempted, key=lambda s: s.accuracy)[:4]
        weak_spots = [
            WeakSpotOut(subject_slug=s.slug, subject_name=s.name, percent=s.accuracy, color=s.color)
            for s in weakest
        ]

        return ProgressSummaryOut(
            total_solved=total_solved,
            accuracy=accuracy,
            probniks_count=probniks_count,
            weekly_activity=weekly_activity,
            weak_spots=weak_spots,
            by_subject=by_subject,
        )


@router.get("/progress/heatmap", response_model=list[HeatmapPoint])
async def progress_heatmap(
    days: int = Query(default=30, ge=1, le=90),
    user: User = Depends(get_current_user),
) -> list[HeatmapPoint]:
    async with async_session() as session:
        today = dt.datetime.now(dt.timezone.utc).date()
        start = today - dt.timedelta(days=days - 1)
        rows = (
            await session.execute(
                select(func.date(Attempt.answered_at), func.count())
                .where(
                    Attempt.user_telegram_id == user.telegram_id,
                    func.date(Attempt.answered_at) >= start,
                )
                .group_by(func.date(Attempt.answered_at))
            )
        ).all()
        counts_by_date = {row[0]: row[1] for row in rows}
        return [
            HeatmapPoint(date=(start + dt.timedelta(days=i)).isoformat(),
                         count=counts_by_date.get(start + dt.timedelta(days=i), 0))
            for i in range(days)
        ]