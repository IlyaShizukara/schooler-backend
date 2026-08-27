import datetime as dt
import enum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db import Base

from typing import Optional


class AuthStatus(str, enum.Enum):
    pending = "pending"      # код создан, ждём, пока пользователь нажмёт Start в боте
    confirmed = "confirmed"  # пользователь подтвердил вход через Telegram
    expired = "expired"      # код протух, не был подтверждён вовремя


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(128))
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    vk_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True, index=True)
    yandex_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    # ⚠️ Добавлено для регистрации email+паролем через Supabase Auth (см.
    # supabase_auth.py). Оба nullable — у Telegram/VK/Яндекс-пользователей
    # их не будет. telegram_id у email-пользователей — синтетический
    # отрицательный (см. _new_synthetic_telegram_id в supabase_auth.py),
    # поэтому вся остальная схема (UserProfile, Attempt, ExamSession и
    # т.д.), ключующаяся на user_telegram_id, продолжает работать как есть.
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True, index=True)
    supabase_user_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)


class AuthSession(Base):
    """Одноразовый КОД ВХОДА, которым связываем открытый экран приложения с
    Telegram-аккаунтом. Живёт всего 10 минут и используется только для
    хендшейка (deep-link → бот подтверждает → мы знаем telegram_id).

    После подтверждения он НЕ используется как токен сессии — вместо этого
    выпускается отдельный долгоживущий session_token (см. UserSession ниже),
    который и кладётся в это же поле session_token для однократной выдачи
    приложению при поллинге /api/auth/session/{code}.
    """

    __tablename__ = "auth_sessions"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[AuthStatus] = mapped_column(Enum(AuthStatus), default=AuthStatus.pending)
    telegram_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), nullable=True
    )
    session_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    """Настоящий токен сессии — то, чем приложение реально авторизуется в API
    после логина. В отличие от кода входа (AuthSession): имеет собственный
    TTL (по умолчанию 30 дней) и может быть отозван (logout) без ожидания
    истечения срока."""

    __tablename__ = "user_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Subject(Base):
    """Предмет ЕГЭ/ОГЭ (Математика, Русский язык и т.д.)."""

    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, index=True)  # "math_basic", "russian" и т.п.
    name: Mapped[str] = mapped_column(String(100))
    icon: Mapped[str] = mapped_column(String(50))   # имя иконки, фронтенд сам мапит на ft.Icons.*
    color: Mapped[str] = mapped_column(String(20))  # hex-цвет для карточки


class Topic(Base):
    __tablename__ = "topics"
    id: Mapped[int] = mapped_column(primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    difficulty: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    task_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    task_number_to: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class TaskType(str, enum.Enum):
    mcq = "mcq"                    # тест: выбор из вариантов
    short_answer = "short_answer"  # краткий ответ: число/слово без вариантов


class Task(Base):
    """Одно задание — тестовое (с вариантами) или с кратким ответом."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"), index=True)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"), nullable=True, index=True)

    task_type: Mapped[TaskType] = mapped_column(Enum(TaskType), default=TaskType.mcq)
    task_number: Mapped[int | None] = mapped_column(Integer, nullable=True)  # номер задания в структуре КИМ

    # Часть экзамена: 1 — краткий ответ/тест, проверяется автоматически по
    # correct_index/correct_answer_text; 2 — развёрнутый ответ (сочинение,
    # решение с обоснованием и т.п.), автоматически НЕ проверяется — вместо
    # этого пользователю просто показывается эталон/критерии по кнопке
    # "Показать ответ", без сохранения попытки в Attempt.
    part: Mapped[int] = mapped_column(Integer, default=1)

    question: Mapped[str] = mapped_column(Text)

    # Для task_type == mcq:
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)          # список строк-вариантов
    correct_index: Mapped[int | None] = mapped_column(Integer, nullable=True)  # индекс правильного варианта

    # Для task_type == short_answer (а для part == 2 это же поле используется
    # как модельный/эталонный развёрнутый ответ — просто показывается, а не
    # сверяется с ответом пользователя):
    correct_answer_text: Mapped[str | None] = mapped_column(String(255), nullable=True)

    points: Mapped[int] = mapped_column(Integer, default=1)          # баллов за задание
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_urls: Mapped[list | None] = mapped_column(JSON, nullable=True)  # список прямых ссылок на картинки к заданию
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)          # откуда взято задание
    file_urls: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # [{"name": "24.txt", "url": "https://kompege.ru/files/..."}]
      # для тем-диапазонов вроде 19-21
    criteria: Mapped[str | None] = mapped_column(Text, nullable=True)  # критерии оценивания (только part == 2);
                                                                          # отдельно от explanation, которое — решение

class UserProfile(Base):
    """Данные, которые пользователь заполняет при онбординге: имя, тип
    экзамена, класс, дата экзамена, дневная цель и целевой балл. Отдельная
    таблица от User (который привязан к Telegram-логину) — пока профиль не
    заполнен, строки для пользователя просто не существует."""

    __tablename__ = "user_profiles"

    user_telegram_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), primary_key=True
    )
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    exam_type: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "ОГЭ" | "ЕГЭ"
    grade: Mapped[int | None] = mapped_column(Integer, nullable=True)         # 9, 10, 11
    exam_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    daily_goal: Mapped[int] = mapped_column(Integer, default=15)     # заданий в день
    target_score: Mapped[int] = mapped_column(Integer, default=80)  # целевой балл ЕГЭ/ОГЭ
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False)


class UserSelectedSubject(Base):
    """Какие предметы пользователь выбрал при онбординге (many-to-many)."""

    __tablename__ = "user_selected_subjects"

    user_telegram_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), primary_key=True
    )
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"), primary_key=True)


class ExamSession(Base):
    """Один запуск пробника — набор заданий по предмету, которые пользователь
    проходит подряд, с итоговым результатом в конце.

    total_tasks здесь означает количество ПРОВЕРЯЕМЫХ заданий (part == 1) в
    этом пробнике, а не общее число заданий — так процент в конце считается
    только по тому, что реально можно проверить автоматически. Задания части 2
    всё равно входят в сам пробник и показываются пользователю, просто не
    участвуют в счёте (см. probnik.py::start_probnik)."""

    __tablename__ = "exam_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_tasks: Mapped[int] = mapped_column(Integer, default=0)


class Attempt(Base):
    """Попытка пользователя ответить на конкретное задание."""

    __tablename__ = "attempts"
    __table_args__ = (
        # Все наши агрегирующие запросы (список предметов, сводка прогресса)
        # фильтруют по user_telegram_id и джойнятся через task_id — составной
        # индекс покрывает это заметно эффективнее, чем два независимых.
        Index("ix_attempts_user_task", "user_telegram_id", "task_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    # Если попытка сделана в рамках пробника — здесь id сессии; обычная
    # практика (решение через "Темы") оставляет это поле пустым.
    exam_session_id: Mapped[int | None] = mapped_column(ForeignKey("exam_sessions.id"), nullable=True, index=True)
    is_correct: Mapped[bool] = mapped_column(Boolean)

    selected_index: Mapped[int | None] = mapped_column(Integer, nullable=True)  # mcq — что выбрал пользователь
    answer_text: Mapped[str | None] = mapped_column(String(255), nullable=True)  # short_answer — что ввёл пользователь
    answered_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )



class UserStats(Base):
    __tablename__ = "user_stats"

    user_telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id"), primary_key=True
    )
    xp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    current_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    longest_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_activity_date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)


class ExamSessionTask(Base):
    """Полный список заданий, вошедших в конкретный пробник (часть 1 И часть 2).
    Нужна, чтобы после сдачи построить постраничный разбор и посчитать итог
    с учётом самооценки части 2 — без неё негде взять список заданий пробника
    задним числом (сейчас он живёт только на фронте в момент прохождения)."""

    __tablename__ = "exam_session_tasks"

    exam_session_id: Mapped[int] = mapped_column(ForeignKey("exam_sessions.id"), primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)


class ProbnikPart2Grade(Base):
    """Самооценка баллов за задание части 2 внутри конкретного пробника.
    Часть 2 не проверяется автоматически — пользователь сам сверяется с
    критериями и выставляет себе баллы (0..Task.points) на итоговом экране."""

    __tablename__ = "probnik_part2_grades"

    exam_session_id: Mapped[int] = mapped_column(ForeignKey("exam_sessions.id"), primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    graded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
