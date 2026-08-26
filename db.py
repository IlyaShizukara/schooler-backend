from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import settings


class Base(DeclarativeBase):
    pass


# Neon (и большинство облачных Postgres) добавляют в строку подключения
# libpq-специфичные параметры (sslmode, channel_binding и т.п.), которые
# предназначены для psycopg/libpq — а asyncpg о них не знает и падает с
# TypeError при попытке передать их как есть. Поэтому:
#   - sslmode=require/verify-*  -> конвертируем в connect_args={"ssl": True}
#   - остальные подобные параметры (channel_binding и любые новые, которые
#     появятся в будущем) -> просто убираем из URL, asyncpg их не поддерживает
#     и не может использовать через query string.
# Это же справедливо и для Supabase — их connection string тоже иногда
# включает sslmode/channel_binding, логика ниже общая для обоих провайдеров.
_LIBPQ_ONLY_PARAMS = ["sslmode", "channel_binding", "target_session_attrs", "options"]

_url = make_url(settings.database_url)

# Провайдеры (Neon, Supabase и т.п.) в своих дашбордах выдают "сырую" строку
# вида postgresql://... или postgres://... — без +asyncpg, потому что это
# общий формат для любого клиента (psql, другие языки и т.п.), а не
# специфика конкретно нашего async-стека. Если просто вставить её как есть в
# DATABASE_URL, create_async_engine ниже упадёт с ошибкой на этапе импорта
# db.py — ещё до старта FastAPI, то есть 500 будет вообще на каждый роут.
# Нормализуем схему сами, чтобы такая (крайне вероятная) человеческая
# ошибка при копировании строки из панели провайдера не роняла бэкенд.
if _url.drivername in ("postgres", "postgresql"):
    _url = _url.set(drivername="postgresql+asyncpg")

_connect_args: dict = {}
if _url.query.get("sslmode") in ("require", "verify-ca", "verify-full"):
    _connect_args["ssl"] = True

_params_to_strip = [p for p in _LIBPQ_ONLY_PARAMS if p in _url.query]
if _params_to_strip:
    _url = _url.difference_update_query(_params_to_strip)

# ⚠️ ВАЖНО ПРИ ПЕРЕЕЗДЕ НА SUPABASE: если DATABASE_URL указывает на их
# transaction-mode пулер (Supavisor/pgbouncer, порт 6543 — именно его нужно
# использовать на serverless/Vercel, чтобы не упереться в лимит прямых
# соединений), asyncpg по умолчанию всё равно пытается использовать
# prepared statements — а transaction-режим пулера их не поддерживает
# (соединение может достаться другому клиенту между запросами одной и той
# же "сессии"). Результат без этой настройки — случайные ошибки вида
# "prepared statement ... already exists" под нагрузкой.
# statement_cache_size=0 отключает кэш подготовленных запросов у asyncpg —
# рекомендованный официальный обход именно для этого сценария (transaction
# pooling + asyncpg). Для Neon (прямое соединение, без такого пулера) эта
# настройка безвредна — просто чуть больше работы на перепланирование
# запроса на каждый вызов, для типичной веб-нагрузки это не заметно.
_connect_args["statement_cache_size"] = 0

engine = create_async_engine(
    _url,
    echo=False,
    pool_pre_ping=True,   # проверяет соединение "living-ness" перед выдачей из пула
    pool_recycle=1800,    # пересоздаёт соединения раз в 30 мин, чтобы не протухали
    connect_args=_connect_args,
)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет. Для прод-проекта лучше заменить на Alembic-миграции."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
