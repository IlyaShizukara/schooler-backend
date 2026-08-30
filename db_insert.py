"""
Функция для добавления заданий в БД напрямую из вашего кода — без
промежуточного Excel-файла. Вызывайте add_task() для каждого сгенерированного
задания.

Как вставить картинку ПРЯМО ВНУТРИ текста задания (например, вместо числа
в середине предложения) — используйте метки {img1}, {img2}, ... прямо в
question, а картинки перечисляйте в image_urls в ТОМ ЖЕ порядке:

    question="В треугольнике ABC сторона AB равна {img1}, угол C равен 120°. "
             "Используя чертёж {img2}, найдите радиус описанной окружности.",
    image_urls=["https://.../formula.png", "https://.../triangle.png"],

{img1} — это первая картинка в списке, {img2} — вторая, и т.д. Если картинки
не нужны внутри текста (а просто идут как приложение под вопросом) — метки
можно не использовать вообще, просто заполните image_urls.

correct_answer_text (только для short_answer) можно передать тремя способами —
проверка ответа пользователя одинаково понимает все три:

    correct_answer_text="Москва"                       # просто строка
    correct_answer_text=24                              # число (можно и "24" строкой — без разницы)
    correct_answer_text=["24", "двадцать четыре", 24]   # список допустимых вариантов

Правильным считается любое совпадение из списка, причём числа сравниваются
именно как числа (например "24" и "24.0" — один и тот же ответ), а строки —
без учёта регистра/лишних пробелов/ё-е. Хранится это в БД в виде JSON,
руками ничего кодировать не нужно — просто передайте значение в удобном виде.

part=1 (по умолчанию) — задание части 1: короткий ответ или тест, проверяется
автоматически, как описано выше. part=2 — задание части 2 (развёрнутый ответ,
сочинение, решение с обоснованием и т.п.): автоматически НЕ проверяется,
приложение просто покажет эталон/критерии по кнопке "Показать ответ" без
сохранения попытки. Для part=2 передавайте эталон/критерии в те же поля
correct_answer_text (текстом, без списка — сверка не выполняется, это просто
текст для показа) и explanation.

Пример использования в вашем скрипте:

    import asyncio
    from db_insert import add_task

    async def main():
        # тестовое задание (mcq), часть 1
        await add_task(
            subject_slug="math_profile",
            topic="Планиметрия",
            question="В треугольнике DEF сторона DE равна 12, угол F равен 150°. "
                     "Найдите диаметр окружности, описанной около этого треугольника.",
            task_type="short_answer",
            correct_answer_text=24,  # или "24", или ["24", "24.0"] — что удобнее
            explanation="По теореме синусов 2R = DE / sin F = 12 / 0.5 = 24.",
            image_urls=["https://i.ibb.co/xxxx/triangle-def.png"],
            source="Переработано на основе открытого банка ФИПИ",
        )

        # задание части 2 (развёрнутый ответ, без автопроверки)
        await add_task(
            subject_slug="russian",
            question="Напишите сочинение-рассуждение по прочитанному тексту (не менее 150 слов)...",
            task_type="short_answer",
            part=2,
            correct_answer_text="Пример сочинения: ...",       # эталонный текст — просто для показа
            explanation="Критерии оценивания: К1 — формулировка проблемы (1 балл), ...",
            points=25,
        )

    asyncio.run(main())

Слаги предметов: math_basic, math_profile, russian, physics, chemistry,
biology, history, social, informatics, english, geography
"""
import asyncio
import json

from sqlalchemy import select

from db import async_session, init_db
from models import Subject, Task, TaskType, Topic


async def add_task(
    subject_slug: str,
    question: str,
    task_type: str = "mcq",              # "mcq" или "short_answer"
    part: int = 1,                        # 1 — проверяется автоматически, 2 — эталон только для показа
    topic: str | None = None,
    topic_difficulty: str | None = None,  # "лёгкое" | "среднее" | "сложное" — задаётся теме, не заданию
    task_number: int | None = None,
    options: list[str] | None = None,        # только для mcq: список вариантов
    correct_index: int | None = None,        # только для mcq: индекс правильного (с 0!)
    correct_answer_text: str | int | float | list[str | int | float] | None = None,  # только для short_answer
    points: int = 1,
    explanation: str | None = None,
    image_urls: list[str] | None = None,
    file_urls: list[dict] | None = None,  
    source: str | None = None,
) -> int:
    """Добавляет одно задание в БД. Возвращает id созданной записи.

    correct_answer_text принимает строку, число или список вариантов —
    подробности и примеры смотрите в docstring модуля выше. part=2 отключает
    автопроверку (в correct_answer_text тогда просто эталонный текст).

    Дубли не проверяются на уровне этой функции (в отличие от import_tasks.py) —
    если зовёте add_task() в цикле из своего скрипта, сами следите, чтобы не
    вызвать дважды для одного и того же задания (например, храните у себя,
    какие id уже сгенерированы и залиты).
    """
    if task_type not in ("mcq", "short_answer"):
        raise ValueError(f"task_type должен быть 'mcq' или 'short_answer', получено: {task_type!r}")

    if part not in (1, 2):
        raise ValueError(f"part должен быть 1 или 2, получено: {part!r}")

    if task_type == "mcq":
        if not options or correct_index is None:
            raise ValueError("Для mcq обязательны options и correct_index")
    else:
        # 0 и 0.0 — валидные ответы (число), поэтому сравниваем с None явно,
        # а не через `not correct_answer_text` (иначе 0 отбросило бы напрасно)
        if correct_answer_text is None or correct_answer_text == "" or correct_answer_text == []:
            raise ValueError("Для short_answer обязателен correct_answer_text")

    async with async_session() as session:
        subject = (
            await session.execute(select(Subject).where(Subject.slug == subject_slug))
        ).scalar_one_or_none()
        if subject is None:
            raise ValueError(
                f"Неизвестный предмет: {subject_slug!r}. "
                f"Убедитесь, что запускали seed.py (там создаются предметы)."
            )

        topic_id = None
        if topic:
            existing_topic = (
                await session.execute(
                    select(Topic).where(Topic.subject_id == subject.id, Topic.name == topic)
                )
            ).scalar_one_or_none()
            if existing_topic is None:
                existing_topic = Topic(subject_id=subject.id, name=topic, difficulty=topic_difficulty)
                session.add(existing_topic)
                await session.flush()
            elif topic_difficulty and not existing_topic.difficulty:
                # не перезаписываем уже заданную сложность, только заполняем пустую
                existing_topic.difficulty = topic_difficulty
            topic_id = existing_topic.id

        task = Task(
            subject_id=subject.id,
            topic_id=topic_id,
            task_type=TaskType(task_type),
            part=part,
            task_number=task_number,
            question=question,
            options=options if task_type == "mcq" else None,
            correct_index=correct_index if task_type == "mcq" else None,
            correct_answer_text=(
                json.dumps(correct_answer_text, ensure_ascii=False)
                if task_type == "short_answer" else None
            ),
            points=points,
            explanation=explanation,
            image_urls=image_urls,
            file_urls=file_urls,
            source=source,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


async def _ensure_ready() -> None:
    """Вызовите один раз перед первым add_task(), если ещё не запускали seed.py —
    создаёт таблицы (но НЕ предметы, их всё равно нужно сначала завести через seed.py)."""
    await init_db()


if __name__ == "__main__":
    # Небольшая самопроверка — добавляет одно тестовое задание.
    async def _demo():
        await _ensure_ready()
        new_id = await add_task(
            subject_slug="math_profile",
            topic="Планиметрия",
            question="Демо-задание из db_insert.py — можно удалить из БД вручную.",
            task_type="short_answer",
            correct_answer_text=["42", "сорок два"],
            source="db_insert.py self-test",
        )
        print(f"[db_insert] создано тестовое задание id={new_id}")

    asyncio.run(_demo())