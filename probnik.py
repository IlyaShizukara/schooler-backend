import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from auth_dependency import get_current_user
from content import _answer_matches, _display_correct_answer
from db import async_session
from models import (
    Attempt, ExamSession, ExamSessionTask, ProbnikPart2Grade,
    Subject, Task, TaskType, User,
)
from schemas import (
    ProbnikAnswerIn,
    ProbnikFinishOut,
    ProbnikHistoryItem,
    ProbnikPart2GradeIn,
    ProbnikPart2GradeOut,
    ProbnikReviewOut,
    ProbnikReviewTaskOut,
    ProbnikStartIn,
    ProbnikStartOut,
    TaskOut,
)

from xp import award_xp, XP_PER_PROBNIK_TASK
from secondary_scale import get_secondary_score, get_math_basic_grade
from rate_limit import rate_limit

EXAM_STRUCTURE = {
    "math_profile":   {"part1": 12, "part2": 7},
    "math_basic":     {"part1": 21, "part2": 0},
    "russian":        {"part1": 26, "part2": 1},
    "physics":        {"part1": 20, "part2": 6},
    "chemistry":      {"part1": 28, "part2": 6},
    "biology":        {"part1": 21, "part2": 7},
    "history":        {"part1": 12, "part2": 9},
    "social":         {"part1": 16, "part2": 9},
    "informatics":    {"part1": 27, "part2": 0},
    "english":        {"part1": 36, "part2": 6},
    "geography":      {"part1": 21, "part2": 8},
    "literature":     {"part1": 6,  "part2": 5},
}


router = APIRouter(prefix="/api/probnik", tags=["probnik"])


async def _select_probnik_tasks(session, subject: Subject, payload: ProbnikStartIn) -> list[Task]:
    """Выбирает набор заданий пробника по предмету/теме/частям — вынесено из
    start_probnik в отдельную функцию, чтобы использовать тот же алгоритм
    подбора и для гостевого /guest/start (без создания ExamSession).
    Поведение НЕ изменилось, просто перестало быть телом одного роута."""
    tasks: list[Task] = []

    if payload.topic_id is not None:
        limit = payload.task_count or 10
        tasks = (
            await session.execute(
                select(Task)
                .where(Task.subject_id == subject.id, Task.topic_id == payload.topic_id)
                .order_by(func.random())
                .limit(limit)
            )
        ).scalars().all()
    else:
        structure = EXAM_STRUCTURE.get(payload.subject_slug)
        selected_parts = set(payload.parts or [])
        want_part1 = not selected_parts or "Часть 1" in selected_parts
        want_part2 = not selected_parts or "Часть 2" in selected_parts

        if want_part1:
            q1 = (
                select(Task)
                .where(Task.subject_id == subject.id, Task.part == 1)
                .distinct(Task.task_number)
                .order_by(Task.task_number, func.random())
            )
            if structure:
                q1 = q1.limit(structure["part1"])
            tasks.extend((await session.execute(q1)).scalars().all())

        if want_part2:
            q2 = (
                select(Task)
                .where(Task.subject_id == subject.id, Task.part == 2)
                .distinct(Task.task_number)
                .order_by(Task.task_number, func.random())
            )
            if structure:
                q2 = q2.limit(structure["part2"])
            tasks.extend((await session.execute(q2)).scalars().all())

    return tasks


@router.post("/start", response_model=ProbnikStartOut, dependencies=[Depends(rate_limit(10, 60))])
async def start_probnik(payload: ProbnikStartIn, user: User = Depends(get_current_user)) -> ProbnikStartOut:
    async with async_session() as session:
        subject = (
            await session.execute(select(Subject).where(Subject.slug == payload.subject_slug))
        ).scalar_one_or_none()
        if subject is None:
            raise HTTPException(status_code=404, detail="Предмет не найден")

        tasks = await _select_probnik_tasks(session, subject, payload)

        if not tasks:
            raise HTTPException(status_code=404, detail="Для этого предмета пока нет заданий")

        gradable_tasks = [t for t in tasks if t.part == 1]

        exam = ExamSession(
            user_telegram_id=user.telegram_id,
            subject_id=subject.id,
            total_tasks=len(gradable_tasks),
        )
        session.add(exam)
        await session.flush()  # нужен exam.id перед вставкой связанных строк

        # НОВОЕ: сохраняем полный список заданий (часть 1 и часть 2) этого
        # пробника — без этого разбор после сдачи неоткуда собрать.
        session.add_all([
            ExamSessionTask(exam_session_id=exam.id, task_id=t.id, order_index=i)
            for i, t in enumerate(tasks)
        ])
        await session.commit()
        await session.refresh(exam)

        return ProbnikStartOut(
            session_id=exam.id,
            subject_slug=subject.slug,
            subject_name=subject.name,
            tasks=[
                TaskOut(
                    id=t.id, subject_slug=subject.slug, topic=None,
                    task_type=t.task_type.value, part=t.part,
                    task_number=t.task_number,
                    question=t.question, options=t.options, points=t.points,
                    image_urls=t.image_urls,
                    file_urls=t.file_urls,   # ← БЫЛО ПРОПУЩЕНО: аудио/файлы-вложения не доходили до фронта
                    correct_answer_text=None,
                    explanation=None,
                )
                for t in tasks
            ],
            total_points=sum(t.points for t in gradable_tasks),
        )


# ──────────────────────────────────────────────────────────────────────────
# Гостевой пробник — полностью без БД (ни ExamSession, ни Attempt, ни XP).
# Два запроса вместо пяти: /guest/start отдаёт задания (как обычный /start,
# но без session_id — писать некуда, ExamSession.user_telegram_id обязателен
# везде в проекте), /guest/grade принимает ВСЕ ответы одним пакетом и сразу
# считает итог в памяти. Часть 2 не проверяется автоматически даже у
# залогиненных (self-grade), а без сохранённой сессии самооценку тоже
# сохранять некуда — поэтому для гостя часть 2 только показывается для
# самопроверки (эталон/критерии), но не учитывается в баллах. Официальный
# вторичный балл в таком виде был бы вводящим в заблуждение (считается по
# полному экзамену), поэтому для гостя мы его не считаем вовсе — честно,
# как и договаривались по правилам проекта.
# ──────────────────────────────────────────────────────────────────────────

class ProbnikGuestStartOut(BaseModel):
    subject_slug: str
    subject_name: str
    tasks: list[TaskOut]
    total_points: int


class ProbnikGuestAnswerIn(BaseModel):
    task_id: int
    selected_index: int | None = None
    answer_text: str | None = None


class ProbnikGuestGradeIn(BaseModel):
    subject_slug: str
    answers: list[ProbnikGuestAnswerIn]


class ProbnikGuestGradeOut(BaseModel):
    subject_name: str
    tasks: list[ProbnikReviewTaskOut]
    total_points: int
    earned_points: int
    percent: int
    note: str


@router.post("/guest/start", response_model=ProbnikGuestStartOut, dependencies=[Depends(rate_limit(10, 60))])
async def start_probnik_guest(payload: ProbnikStartIn) -> ProbnikGuestStartOut:
    async with async_session() as session:
        subject = (
            await session.execute(select(Subject).where(Subject.slug == payload.subject_slug))
        ).scalar_one_or_none()
        if subject is None:
            raise HTTPException(status_code=404, detail="Предмет не найден")

        tasks = await _select_probnik_tasks(session, subject, payload)
        if not tasks:
            raise HTTPException(status_code=404, detail="Для этого предмета пока нет заданий")

        gradable_tasks = [t for t in tasks if t.part == 1]

        # Ничего не пишем в БД — просто отдаём набор заданий. Фронтенд сам
        # хранит ответы гостя (в памяти/state), пока тот проходит пробник,
        # и в конце отправляет их все разом на /guest/grade.
        return ProbnikGuestStartOut(
            subject_slug=subject.slug,
            subject_name=subject.name,
            tasks=[
                TaskOut(
                    id=t.id, subject_slug=subject.slug, topic=None,
                    task_type=t.task_type.value, part=t.part,
                    task_number=t.task_number,
                    question=t.question, options=t.options, points=t.points,
                    image_urls=t.image_urls,
                    file_urls=t.file_urls,
                    correct_answer_text=None,
                    explanation=None,
                )
                for t in tasks
            ],
            total_points=sum(t.points for t in gradable_tasks),
        )


@router.post("/guest/grade", response_model=ProbnikGuestGradeOut, dependencies=[Depends(rate_limit(10, 60))])
async def grade_probnik_guest(payload: ProbnikGuestGradeIn) -> ProbnikGuestGradeOut:
    async with async_session() as session:
        subject = (
            await session.execute(select(Subject).where(Subject.slug == payload.subject_slug))
        ).scalar_one_or_none()
        if subject is None:
            raise HTTPException(status_code=404, detail="Предмет не найден")

        task_ids = [a.task_id for a in payload.answers]
        if not task_ids:
            raise HTTPException(status_code=400, detail="Пустой список ответов")

        tasks_by_id = {
            t.id: t
            for t in (
                await session.execute(
                    select(Task).where(Task.id.in_(task_ids), Task.subject_id == subject.id)
                )
            ).scalars().all()
        }

        review_tasks: list[ProbnikReviewTaskOut] = []
        total_points = 0
        earned_points = 0

        for answer in payload.answers:
            task = tasks_by_id.get(answer.task_id)
            if task is None:
                # Задание не найдено или не относится к предмету — пропускаем,
                # не роняя весь пробник из-за одного плохого id.
                continue

            if task.part == 1:
                # total_points считаем ТОЛЬКО по части 1 — она единственная,
                # что реально оценивается. Раньше сюда попадали и баллы части
                # 2 (просто никогда не засчитывались в earned_points), из-за
                # чего процент был заниженным даже при идеальной части 1:
                # общий знаменатель включал недостижимые для гостя баллы.
                total_points += task.points
                if task.task_type == TaskType.short_answer:
                    is_correct = _answer_matches(answer.answer_text, task.correct_answer_text)
                else:
                    is_correct = answer.selected_index == task.correct_index
                if is_correct:
                    earned_points += task.points

                review_tasks.append(
                    ProbnikReviewTaskOut(
                        id=task.id, task_type=task.task_type.value, part=task.part,
                        task_number=task.task_number, question=task.question,
                        options=task.options, image_urls=task.image_urls, file_urls=task.file_urls,
                        points=task.points,
                        selected_index=answer.selected_index,
                        answer_text=answer.answer_text,
                        answered=True,
                        is_correct=is_correct,
                        correct_index=task.correct_index,
                        correct_answer_text=_display_correct_answer(task.correct_answer_text),
                        explanation=task.explanation,
                    )
                )
            else:
                # Часть 2 — как и у залогиненных, автоматически не проверяется.
                # Показываем эталон/критерии для самопроверки, но НЕ добавляем
                # в earned_points — без сохранённой сессии самооценку сохранить
                # негде, а угадывать баллы за гостя нечестно (см. правило
                # проекта "либо реальные данные, либо честная пометка").
                review_tasks.append(
                    ProbnikReviewTaskOut(
                        id=task.id, task_type=task.task_type.value, part=task.part,
                        task_number=task.task_number, question=task.question,
                        options=task.options, image_urls=task.image_urls, file_urls=task.file_urls,
                        points=task.points,
                        correct_answer_text=_display_correct_answer(task.correct_answer_text),
                        explanation=task.explanation,
                        criteria=task.criteria,
                    )
                )

        percent = round(earned_points / total_points * 100) if total_points else 0

        return ProbnikGuestGradeOut(
            subject_name=subject.name,
            tasks=review_tasks,
            total_points=total_points,
            earned_points=earned_points,
            percent=percent,
            note=(
                "Результат посчитан только по части 1 и нигде не сохранён — как гость, ты не теряешь "
                "к нему доступ, но и продолжить позже не сможешь. Часть 2 показана для самопроверки, "
                "но не учтена в баллах: официальный вторичный балл считается по всему экзамену. "
                "Войди через Telegram, чтобы получить баллы по обеим частям, официальный вторичный балл "
                "и сохранённую историю пробников."
            ),
        )


@router.post("/{session_id}/answer", dependencies=[Depends(rate_limit(20, 60))])
async def answer_probnik_task(session_id: int, payload: ProbnikAnswerIn, user: User = Depends(get_current_user)):
    async with async_session() as session:
        exam = await session.get(ExamSession, session_id)
        if exam is None or exam.user_telegram_id != user.telegram_id:
            raise HTTPException(status_code=404, detail="Сессия пробника не найдена")
        if exam.finished_at is not None:
            raise HTTPException(status_code=400, detail="Этот пробник уже завершён")

        task = await session.get(Task, payload.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Задание не найдено")

        if task.part == 2:
            raise HTTPException(
                status_code=400,
                detail="Задания части 2 не проверяются автоматически — оцениваются в разборе после сдачи",
            )

        existing_attempt = (
            await session.execute(
                select(Attempt).where(
                    Attempt.exam_session_id == exam.id,
                    Attempt.task_id == task.id,
                    Attempt.user_telegram_id == user.telegram_id,
                )
            )
        ).scalar_one_or_none()
        if existing_attempt is not None:
            # Двойной клик/повторная отправка — уже сохранено, тихо
            # подтверждаем вместо пугающей ошибки на фронте.
            return {"saved": True}

        if task.task_type == TaskType.short_answer:
            is_correct = _answer_matches(payload.answer_text, task.correct_answer_text)
        else:
            is_correct = payload.selected_index == task.correct_index

        session.add(
            Attempt(
                user_telegram_id=user.telegram_id, task_id=task.id,
                exam_session_id=exam.id, is_correct=is_correct,
                selected_index=payload.selected_index, answer_text=payload.answer_text,
            )
        )
        await session.commit()

        # НОВОЕ: is_correct/эталон здесь больше НЕ возвращаем — результат
        # скрыт до сдачи пробника (фронт больше не показывает верно/неверно
        # сразу). Отдаём просто подтверждение сохранения.
        return {"saved": True}


async def _build_review(session, exam: ExamSession) -> ProbnikReviewOut:
    subject = await session.get(Subject, exam.subject_id)

    session_tasks = (
        await session.execute(
            select(ExamSessionTask, Task)
            .join(Task, Task.id == ExamSessionTask.task_id)
            .where(ExamSessionTask.exam_session_id == exam.id)
            .order_by(ExamSessionTask.order_index)
        )
    ).all()

    task_ids = [t.id for _, t in session_tasks]

    attempts_by_task = {
        a.task_id: a
        for a in (
            await session.execute(
                select(Attempt).where(Attempt.exam_session_id == exam.id, Attempt.task_id.in_(task_ids))
            )
        ).scalars().all()
    } if task_ids else {}

    grades_by_task = {
        g.task_id: g.points
        for g in (
            await session.execute(
                select(ProbnikPart2Grade).where(
                    ProbnikPart2Grade.exam_session_id == exam.id, ProbnikPart2Grade.task_id.in_(task_ids)
                )
            )
        ).scalars().all()
    } if task_ids else {}

    review_tasks: list[ProbnikReviewTaskOut] = []
    total_points = 0
    earned_points = 0

    for _, task in session_tasks:
        total_points += task.points
        if task.part == 1:
            attempt = attempts_by_task.get(task.id)
            is_correct = attempt.is_correct if attempt else None
            if attempt and attempt.is_correct:
                earned_points += task.points
            review_tasks.append(
                ProbnikReviewTaskOut(
                    id=task.id, task_type=task.task_type.value, part=task.part,
                    task_number=task.task_number, question=task.question,
                    options=task.options, image_urls=task.image_urls, file_urls=task.file_urls,
                    points=task.points,
                    selected_index=attempt.selected_index if attempt else None,
                    answer_text=attempt.answer_text if attempt else None,
                    answered=attempt is not None,
                    is_correct=is_correct,
                    correct_index=task.correct_index,
                    correct_answer_text=_display_correct_answer(task.correct_answer_text),
                    explanation=task.explanation,
                )
            )
        else:
            graded = grades_by_task.get(task.id)
            if graded is not None:
                earned_points += min(graded, task.points)
            review_tasks.append(
                ProbnikReviewTaskOut(
                    id=task.id, task_type=task.task_type.value, part=task.part,
                    task_number=task.task_number, question=task.question,
                    options=task.options, image_urls=task.image_urls, file_urls=task.file_urls,
                    points=task.points,
                    self_graded_points=graded,
                    correct_answer_text=_display_correct_answer(task.correct_answer_text),
                    explanation=task.explanation,
                    criteria=task.criteria,
                )
            )

    percent = round(earned_points / total_points * 100) if total_points else 0

    subject_slug = subject.slug if subject else None
    secondary_score, math_basic_grade = _compute_secondary(subject_slug, earned_points)

    return ProbnikReviewOut(
        session_id=exam.id, subject_name=subject.name if subject else "",
        tasks=review_tasks, total_points=total_points, earned_points=earned_points, percent=percent,
        secondary_score=secondary_score, math_basic_grade=math_basic_grade,
    )


def _compute_secondary(subject_slug: str | None, earned_points: int) -> tuple[int | None, int | None]:
    """Возвращает (secondary_score, math_basic_grade) — ровно одно из
    них не None, в зависимости от предмета."""
    if subject_slug == "math_basic":
        return None, get_math_basic_grade(earned_points)
    if subject_slug is not None:
        return get_secondary_score(subject_slug, earned_points), None
    return None, None



@router.get("/{session_id}/review", response_model=ProbnikReviewOut)
async def review_probnik(session_id: int, user: User = Depends(get_current_user)) -> ProbnikReviewOut:
    async with async_session() as session:
        exam = await session.get(ExamSession, session_id)
        if exam is None or exam.user_telegram_id != user.telegram_id:
            raise HTTPException(status_code=404, detail="Сессия пробника не найдена")
        return await _build_review(session, exam)


@router.post("/{session_id}/self-grade", response_model=ProbnikPart2GradeOut)
async def self_grade_probnik_task(
    session_id: int, payload: ProbnikPart2GradeIn, user: User = Depends(get_current_user)
) -> ProbnikPart2GradeOut:
    async with async_session() as session:
        exam = await session.get(ExamSession, session_id)
        if exam is None or exam.user_telegram_id != user.telegram_id:
            raise HTTPException(status_code=404, detail="Сессия пробника не найдена")

        task = await session.get(Task, payload.task_id)
        if task is None or task.part != 2:
            raise HTTPException(status_code=400, detail="Это задание не относится к части 2")

        belongs = await session.scalar(
            select(func.count()).select_from(ExamSessionTask).where(
                ExamSessionTask.exam_session_id == exam.id, ExamSessionTask.task_id == task.id
            )
        )
        if not belongs:
            raise HTTPException(status_code=400, detail="Это задание не входит в данный пробник")

        points = max(0, min(payload.points, task.points))

        existing = await session.get(ProbnikPart2Grade, {"exam_session_id": exam.id, "task_id": task.id})
        if existing:
            existing.points = points
            existing.graded_at = dt.datetime.now(dt.timezone.utc)
        else:
            session.add(ProbnikPart2Grade(exam_session_id=exam.id, task_id=task.id, points=points))
        await session.commit()

        review = await _build_review(session, exam)
        return ProbnikPart2GradeOut(
            task_id=task.id, points=points,
            total_points=review.total_points, earned_points=review.earned_points, percent=review.percent,
            secondary_score=review.secondary_score, math_basic_grade=review.math_basic_grade,
        )


@router.post("/{session_id}/finish", response_model=ProbnikFinishOut)
async def finish_probnik(session_id: int, user: User = Depends(get_current_user)) -> ProbnikFinishOut:
    async with async_session() as session:
        exam = await session.get(ExamSession, session_id)
        if exam is None or exam.user_telegram_id != user.telegram_id:
            raise HTTPException(status_code=404, detail="Сессия пробника не найдена")

        subject = await session.get(Subject, exam.subject_id)

        rows = (
            await session.execute(
                select(Attempt.task_id, Attempt.is_correct)
                .where(Attempt.exam_session_id == exam.id)
            )
        ).all()
        answered_task_ids = [r[0] for r in rows]
        correct_count = sum(1 for r in rows if r[1])

        total_points = 0
        earned_points = 0
        if answered_task_ids:
            tasks = (
                await session.execute(select(Task).where(Task.id.in_(answered_task_ids)))
            ).scalars().all()
            points_by_id = {t.id: t.points for t in tasks}
            for task_id, is_correct in rows:
                total_points += points_by_id.get(task_id, 0)
                if is_correct:
                    earned_points += points_by_id.get(task_id, 0)

        if exam.finished_at is None:
            exam.finished_at = dt.datetime.now(dt.timezone.utc)
            await award_xp(session, user.telegram_id, len(answered_task_ids) * XP_PER_PROBNIK_TASK)
            await session.commit()

        percent = round(correct_count / exam.total_tasks * 100) if exam.total_tasks else 0

        subject_slug = subject.slug if subject else None
        secondary_score, math_basic_grade = _compute_secondary(subject_slug, earned_points)

        return ProbnikFinishOut(
            session_id=exam.id,
            subject_name=subject.name if subject else "",
            total_tasks=exam.total_tasks,
            correct_count=correct_count,
            total_points=total_points,
            earned_points=earned_points,
            percent=percent,
            secondary_score=secondary_score,
            math_basic_grade=math_basic_grade,
        )


@router.get("/history", response_model=list[ProbnikHistoryItem])
async def probnik_history(user: User = Depends(get_current_user)) -> list[ProbnikHistoryItem]:
    async with async_session() as session:
        exams = (
            await session.execute(
                select(ExamSession)
                .where(ExamSession.user_telegram_id == user.telegram_id, ExamSession.finished_at.is_not(None))
                .order_by(ExamSession.finished_at.desc())
                .limit(20)
            )
        ).scalars().all()

        result = []
        for exam in exams:
            subject = await session.get(Subject, exam.subject_id)
            correct_count = await session.scalar(
                select(func.count())
                .select_from(Attempt)
                .where(Attempt.exam_session_id == exam.id, Attempt.is_correct.is_(True))
            ) or 0
            percent = round(correct_count / exam.total_tasks * 100) if exam.total_tasks else 0
            result.append(
                ProbnikHistoryItem(
                    session_id=exam.id,
                    subject_name=subject.name if subject else "",
                    started_at=exam.started_at.isoformat(),
                    total_tasks=exam.total_tasks,
                    correct_count=correct_count,
                    percent=percent,
                )
            )
        return result