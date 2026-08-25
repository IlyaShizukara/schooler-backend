from pydantic import BaseModel


class AuthStartResponse(BaseModel):
    code: str
    deep_link: str
    expires_in: int  # секунд


class SessionStatusResponse(BaseModel):
    status: str  # pending | confirmed | expired
    name: str | None = None
    username: str | None = None
    telegram_id: int | None = None
    session_token: str | None = None  # выдаётся один раз при status=confirmed


class SubjectOut(BaseModel):
    slug: str
    name: str
    icon: str
    color: str
    solved: int        # сколько заданий этого предмета пользователь решил (хотя бы 1 попытка)
    total: int         # всего заданий в этом предмете в банке
    total_points: int  # сумма баллов за все задания предмета
    percent: int       # solved/total в процентах, для прогресс-бара
    accuracy: int       # процент правильных ответов среди попыток пользователя



class TopicOut(BaseModel):
    name: str
    topic_id: int | None
    difficulty: str | None
    task_number: int | None = None   # новое поле — для отображения "N: Название" на фронте
    solved: int
    total: int
    total_points: int
    percent: int
    accuracy: int
    task_number_to: int | None = None


class TaskOut(BaseModel):
    id: int
    subject_slug: str
    topic: str | None = None
    task_type: str  # "mcq" | "short_answer"
    part: int = 1    # 1 — проверяется автоматически, 2 — развёрнутый ответ, только показ эталона
    question: str
    options: list[str] | None = None  # только для mcq
    points: int
    image_urls: list[str] | None = None
    # Заполняются ТОЛЬКО для part == 2 (эталон/критерии показываются сразу,
    # без проверки и без отправки ответа). Для part == 1 всегда None здесь —
    # чтобы не раскрывать правильный ответ до того, как пользователь ответил
    # (он приходит отдельно, в AnswerOut, после POST /tasks/{id}/answer).
    correct_answer_text: str | None = None
    explanation: str | None = None
    file_urls: list[dict] | None = None
    task_number: int | None = None  # новое поле — для отображения "N: Название" на фронте

class AnswerIn(BaseModel):
    selected_index: int | None = None  # для mcq
    answer_text: str | None = None     # для short_answer


class AnswerOut(BaseModel):
    is_correct: bool
    correct_index: int | None = None
    correct_answer_text: str | None = None
    explanation: str | None = None


class WeeklyActivityPoint(BaseModel):
    day: str    # "Пн", "Вт", ...
    count: int


class WeakSpotOut(BaseModel):
    subject_slug: str
    subject_name: str
    percent: int  # accuracy по этому предмету
    color: str


class ProgressSummaryOut(BaseModel):
    total_solved: int
    accuracy: int
    probniks_count: int
    weekly_activity: list[WeeklyActivityPoint]
    weak_spots: list[WeakSpotOut]
    by_subject: list[SubjectOut]


class ProbnikStartIn(BaseModel):
    subject_slug: str
    task_count: int | None = None
    topic_id: int | None = None  # сколько заданий запросили; реально может быть меньше, если в банке столько нет
    parts: list[str] | None = None  # какие части включить в пробник; по умолчанию обе


class ProbnikStartOut(BaseModel):
    session_id: int
    subject_slug: str
    subject_name: str
    tasks: list[TaskOut]
    total_points: int  # сумма баллов только по проверяемым (part == 1) заданиям пробника


class ProbnikAnswerIn(BaseModel):
    task_id: int
    selected_index: int | None = None
    answer_text: str | None = None


class ProbnikFinishOut(BaseModel):
    session_id: int
    subject_name: str
    total_tasks: int
    correct_count: int
    total_points: int
    earned_points: int
    percent: int
    secondary_score: int | None = None      # тестовый балл 0-100 (None для math_basic и предметов без шкалы)
    math_basic_grade: int | None = None      # оценка 2..5 — только для math_basic, иначе None


class ProbnikHistoryItem(BaseModel):
    session_id: int
    subject_name: str
    started_at: str
    total_tasks: int
    correct_count: int
    percent: int


class OnboardingIn(BaseModel):
    display_name: str
    exam_type: str            # "ОГЭ" | "ЕГЭ"
    grade: int                # 9, 10, 11
    subject_slugs: list[str]  # хотя бы один
    exam_date: str | None = None   # "YYYY-MM-DD", можно не указывать
    daily_goal: int = 15
    target_score: int = 80


class ProfileOut(BaseModel):
    display_name: str | None = None
    exam_type: str | None = None
    grade: int | None = None
    exam_date: str | None = None
    daily_goal: int = 15
    target_score: int = 80
    onboarding_completed: bool = False
    subject_slugs: list[str] = []


class HeatmapPoint(BaseModel):
    date: str   # ISO-дата, например "2026-08-01"
    count: int


class ProbnikPart2GradeIn(BaseModel):
    task_id: int
    points: int


class ProbnikPart2GradeOut(BaseModel):
    task_id: int
    points: int
    total_points: int
    earned_points: int
    percent: int
    secondary_score: int | None = None
    math_basic_grade: int | None = None


class ProbnikReviewTaskOut(BaseModel):
    id: int
    task_type: str
    part: int
    task_number: int | None = None
    question: str
    options: list[str] | None = None
    image_urls: list[str] | None = None
    file_urls: list[dict] | None = None
    points: int  # максимум баллов за задание
    # часть 1:
    selected_index: int | None = None
    answer_text: str | None = None
    answered: bool = False
    is_correct: bool | None = None
    correct_index: int | None = None
    correct_answer_text: str | None = None
    # часть 2:
    self_graded_points: int | None = None
    explanation: str | None = None
    criteria: str | None = None


class ProbnikReviewOut(BaseModel):
    session_id: int
    subject_name: str
    tasks: list[ProbnikReviewTaskOut]
    total_points: int
    earned_points: int
    percent: int
    secondary_score: int | None = None      # тестовый балл 0-100 (None для math_basic и предметов без шкалы)
    math_basic_grade: int | None = None      # оценка 2..5 — только для math_basic, иначе None