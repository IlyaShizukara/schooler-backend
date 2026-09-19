"""
Извлечение параметров 3D-геометрии стереометрической задачи — для рендера
интерактивной модели на фронте (Three.js).

Философия: ИИ НЕ рисует геометрию и не считает координаты сам — это
целиком детерминированная математика на фронте (см. lib/geometry-solid.ts:
радиус описанной окружности правильного n-угольника, теорема Пифагора для
высоты через боковое ребро и т.п.). Роль модели — только извлечь
СТРУКТУРИРОВАННЫЕ параметры из текста задачи: какое тело, какое основание,
какие размеры даны, как подписаны вершины. Если модель не уверена (задача
не про правильную пирамиду/призму, или размеров не хватает для однозначного
построения) — эндпоинт возвращает null, и фронт просто не показывает
кнопку 3D-модели. Лучше отсутствие диаграммы, чем неправильная диаграмма.

Поддерживаются намеренно только правильные пирамиды и призмы с основанием
равносторонний треугольник/квадрат/правильный шестиугольник — это
покрывает подавляющее большинство заданий №14 профильной математики.
Другая геометрия (наклонные тела, произвольные основания), другие
предметы — не поддерживаются не по недосмотру, а потому что для них не
получится гарантировать корректность построения.
"""
import json
import logging
import re
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from ai_tutor import MODEL_URI, YANDEX_API_KEY, YANDEX_COMPLETION_URL
from auth_dependency import get_current_user
from db import async_session
from models import Task, TaskGeometryCache, User
from rate_limit import rate_limit

logger = logging.getLogger("ai_geometry")

router = APIRouter(prefix="/api/ai", tags=["ai-geometry"])

# Ниже 0.6 модель сама не уверена, что это правильная пирамида/призма с
# поддерживаемым основанием, или что размеров достаточно для однозначного
# построения — не показываем диаграмму вообще, чем гадать.
CONFIDENCE_THRESHOLD = 0.6

_BASE_SHAPE_SIDES: dict[str, int] = {
    "equilateral_triangle": 3,
    "square": 4,
    "regular_hexagon": 6,
}


class GeometryPoint(BaseModel):
    label: str
    type: Literal["edge_midpoint", "base_center"]
    of: list[str] | None = None

    @model_validator(mode="after")
    def _check_of(self) -> "GeometryPoint":
        if self.type == "edge_midpoint" and (not self.of or len(self.of) != 2):
            raise ValueError("edge_midpoint требует ровно 2 подписи вершин в 'of'")
        return self


class GeometryExtraction(BaseModel):
    solid: Literal["pyramid", "prism"]
    base_shape: Literal["equilateral_triangle", "square", "regular_hexagon"]
    base_labels: list[str]
    apex_label: str | None = None       # только для pyramid
    top_labels: list[str] | None = None  # только для prism, тот же порядок, что base_labels
    base_edge: float = Field(gt=0)
    lateral_edge: float | None = Field(default=None, gt=0)  # для pyramid, если дано вместо height
    height: float | None = Field(default=None, gt=0)
    extra_points: list[GeometryPoint] = []
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _check_consistency(self) -> "GeometryExtraction":
        expected_n = _BASE_SHAPE_SIDES[self.base_shape]
        if len(self.base_labels) != expected_n:
            raise ValueError("base_labels не соответствует числу вершин base_shape")

        if self.solid == "pyramid":
            if not self.apex_label:
                raise ValueError("pyramid требует apex_label")
            if self.lateral_edge is None and self.height is None:
                raise ValueError("pyramid требует lateral_edge или height")
        else:
            if not self.top_labels or len(self.top_labels) != expected_n:
                raise ValueError("prism требует top_labels той же длины, что base_labels")
            if self.height is None:
                raise ValueError("prism требует height")

        known_labels = set(self.base_labels) | set(self.top_labels or [])
        if self.apex_label:
            known_labels.add(self.apex_label)
        for p in self.extra_points:
            for ref in p.of or []:
                if ref not in known_labels:
                    raise ValueError(f"extra_points ссылается на неизвестную вершину: {ref}")

        return self


class GeometryIn(BaseModel):
    task_id: int | None = None
    # Обязателен, если task_id не передан — сырой текст задачи, каким его
    # описал ученик в общем чате (без привязки к конкретному заданию из
    # банка).
    problem_text: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _check_source(self) -> "GeometryIn":
        if self.task_id is None and not self.problem_text:
            raise ValueError("нужен task_id или problem_text")
        return self


_GEOMETRY_EXTRACTION_PROMPT = (
    "Ты извлекаешь структурированные параметры стереометрической задачи для "
    "построения 3D-модели по правильной пирамиде или призме. НЕ решай "
    "задачу и не считай никакие координаты или производные величины — "
    "извлеки только то, что явно дано в условии текстом. "
    "Отвечай СТРОГО валидным JSON и больше ничем — ни текста до, ни текста "
    "после, ни блока в тройных обратных кавычках ``` (ни с словом json, ни "
    "без него) — просто сам JSON-объект первым символом ответа. Массивы "
    "пиши в обычном JSON-синтаксисе: значения через запятую БЕЗ номеров или "
    "индексов перед ними — [\"A\",\"B\",\"C\"], а НЕ [0:\"A\",1:\"B\",2:\"C\"] "
    "(это невалидный JSON, а не список с индексами). Схема:\n"
    '{"solid": "pyramid" | "prism", '
    '"base_shape": "equilateral_triangle" | "square" | "regular_hexagon", '
    '"base_labels": ["A","B","C", ...] — вершины основания по порядку '
    "обхода, ровно так, как они подписаны в условии, "
    '"apex_label": "S" — только для pyramid, буква вершины пирамиды, иначе null, '
    '"top_labels": ["A1","B1","C1", ...] — только для prism, вершины '
    "верхнего основания в ТОМ ЖЕ порядке, что base_labels, иначе null, "
    '"base_edge": число — длина стороны основания, '
    '"lateral_edge": число или null — боковое ребро (для pyramid, если дано '
    "вместо height), "
    '"height": число или null — высота тела, если дана явно, '
    '"extra_points": [{"label":"M","type":"edge_midpoint","of":["B","C"]}, '
    '{"label":"O","type":"base_center"}] — только точки, реально упомянутые '
    "в условии (например, «M — середина ребра BC»), "
    '"confidence": число от 0 до 1 — насколько ты уверен, что это ИМЕННО '
    "правильная пирамида/призма (апекс или боковые рёбра строго над "
    "центром основания) с поддерживаемым основанием, и что размеров "
    "достаточно для однозначного построения без дополнительных "
    "предположений}\n\n"
    "Если задача не про правильную пирамиду/призму с основанием "
    "равносторонний треугольник/квадрат/правильный шестиугольник, или в "
    "условии не хватает размеров (не дано явно ни высоты, ни бокового "
    "ребра/апофемы, ни стороны основания) — верни confidence меньше 0.3; "
    "JSON всё равно должен соответствовать схеме приблизительными "
    "значениями, раз при низком confidence фронт всё равно не покажет "
    "модель."
)


# Вопреки прямой инструкции промпта ("только JSON, без markdown-блока"),
# YandexGPT на практике иногда всё равно оборачивает ответ в ``` (даже без
# ```json) — срезаем.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.IGNORECASE)

# Вживую модель иногда пишет элементы массива с "индексом" перед значением —
# [0: "A", 1: "B", 2: "C"] вместо валидного ["A", "B", "C"] (похоже на
# спутанный вывод в духе Python enumerate()). Это не markdown-артефакт, а
# по-настоящему невалидный JSON — json.loads падает на нём при любых
# обстоятельствах. Паттерн узнаваемый (число+двоеточие сразу после '['
# или ',') — вырезаем.
_MALFORMED_ARRAY_INDEX_RE = re.compile(r"(?<=[\[,])\s*\d+\s*:\s*")


def _clean_model_json(raw_text: str) -> str:
    text = raw_text.strip()
    text = _CODE_FENCE_RE.sub("", text).strip()
    text = _MALFORMED_ARRAY_INDEX_RE.sub("", text)
    return text


async def _call_geometry_extraction(problem_text: str) -> GeometryExtraction | None:
    request_body = {
        "modelUri": MODEL_URI,
        # stream=False — это единственный не потоковый вызов Yandex
        # Completion API в проекте; ответ ожидается одним JSON-объектом с
        # тем же полем result.alternatives[0].message.text, что и в
        # потоковом режиме в ai_tutor.py, просто без построчной генерации.
        "completionOptions": {"stream": False, "temperature": 0.0, "maxTokens": "500"},
        "messages": [
            {"role": "system", "text": _GEOMETRY_EXTRACTION_PROMPT},
            {"role": "user", "text": problem_text},
        ],
    }
    headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}", "Content-Type": "application/json"}

    raw_text: str | None = None
    cleaned_text: str | None = None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(YANDEX_COMPLETION_URL, json=request_body, headers=headers)
        response.raise_for_status()
        raw_text = response.json()["result"]["alternatives"][0]["message"]["text"]
        cleaned_text = _clean_model_json(raw_text)
        extraction = GeometryExtraction.model_validate(json.loads(cleaned_text))
    except Exception:
        # Сюда попадает и сетевая ошибка, и невалидный JSON от модели (даже
        # после _clean_model_json — она чинит только два известных
        # артефакта, не любой возможный), и несоответствие схеме
        # (model_validator в GeometryExtraction). И raw_text, и cleaned_text
        # в логе — чтобы сразу было видно, это новый артефакт формата или
        # сама очистка не сработала как надо.
        logger.exception(
            "Не удалось извлечь геометрию задачи; сырой ответ модели: %r; после очистки: %r",
            raw_text, cleaned_text,
        )
        return None

    # Логируем ВСЕГДА, а не только при провале ниже порога — иначе
    # "модель уверенно распознала неправильную пирамиду и честно дала
    # confidence 0.2" и "ответ не распарсился" выглядят снаружи одинаково
    # ("Не получилось построить модель"), и без этой строки в логах нельзя
    # понять, какой из двух случаев произошёл на самом деле.
    logger.info(
        "Извлечение геометрии: solid=%s base=%s confidence=%.2f (порог %.2f)",
        extraction.solid, extraction.base_shape, extraction.confidence, CONFIDENCE_THRESHOLD,
    )

    if extraction.confidence < CONFIDENCE_THRESHOLD:
        return None
    return extraction


@router.post("/geometry", response_model=GeometryExtraction | None, dependencies=[Depends(rate_limit(20, 60))])
async def extract_geometry(payload: GeometryIn, user: User = Depends(get_current_user)) -> GeometryExtraction | None:
    problem_text: str

    if payload.task_id is not None:
        async with async_session() as session:
            cached = await session.get(TaskGeometryCache, payload.task_id)
            if cached is not None:
                if cached.extraction_json is None:
                    return None
                return GeometryExtraction.model_validate_json(cached.extraction_json)

            task = await session.get(Task, payload.task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Задание не найдено")
            problem_text = task.question
    else:
        assert payload.problem_text is not None  # гарантировано GeometryIn._check_source
        problem_text = payload.problem_text

    extraction = await _call_geometry_extraction(problem_text)

    if payload.task_id is not None:
        # Кэшируем и отрицательный результат (extraction is None) — иначе
        # каждое открытие чата по этому заданию будет заново звать LLM с
        # тем же результатом "не удалось".
        async with async_session() as session:
            session.add(
                TaskGeometryCache(
                    task_id=payload.task_id,
                    extraction_json=extraction.model_dump_json() if extraction else None,
                )
            )
            await session.commit()

    return extraction