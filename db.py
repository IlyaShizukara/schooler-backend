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
_LIBPQ_ONLY_PARAMS = ["sslmode", "channel_binding", "target_session_attrs", "options"]

_url = make_url(settings.database_url)
_connect_args: dict = {}
if _url.query.get("sslmode") in ("require", "verify-ca", "verify-full"):
    _connect_args["ssl"] = True

_params_to_strip = [p for p in _LIBPQ_ONLY_PARAMS if p in _url.query]
if _params_to_strip:
    _url = _url.difference_update_query(_params_to_strip)

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