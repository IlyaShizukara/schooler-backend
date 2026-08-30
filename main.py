import asyncio
import contextlib
import datetime as dt
import os
import secrets
import sys
import httpx


# На Windows asyncio по умолчанию использует ProactorEventLoop, с которым у
# asyncpg известны проблемы: соединение периодически рвётся с ошибкой
# "connection was closed in the middle of operation". Форсируем SelectorEventLoop —
# именно то, что рекомендует сам asyncpg для Windows. Это должно стоять
# ДО первого создания event loop, поэтому — в самом верху файла.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from telegram import Update

from auth_dependency import get_current_user
from bot import build_bot_application
from config import settings
from content import router as content_router
from probnik import router as probnik_router
from profile import router as profile_router
from db import async_session, init_db
from models import AuthSession, AuthStatus, User, UserSession
from schemas import AuthStartResponse, SessionStatusResponse
from xp import router as xp_router
from media_proxy import router as media_proxy_router
from rate_limit import rate_limit
from telegram_webapp import validate_init_data
from pydantic import BaseModel
from vk_auth import router as vk_auth_router
from yandex_auth import router as yandex_auth_router
from supabase_auth import router as supabase_auth_router
from ai_tutor import router as ai_tutor

bot_app = build_bot_application()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Раньше здесь был _start_bot_with_retry() с bot_app.updater.start_polling() —
    # на serverless (Vercel) держать постоянный long-polling-луп нельзя,
    # функция не живёт достаточно долго между вызовами. Вместо этого один
    # раз регистрируем вебхук у Telegram: дальше апдейты будут приходить
    # POST-запросами на /api/telegram/webhook, см. обработчик ниже.
    #
    # set_webhook идемпотентен — повторный вызов с тем же URL безопасен,
    # так что не страшно, что lifespan выполняется на каждом холодном
    # старте функции (Vercel переиспользует тёплые инстансы между
    # запросами, так что это не на каждый запрос).
    bot_started = False
    try:
        await bot_app.initialize()
        await bot_app.start()
        bot_started = True

        # Сначала read-only проверка — если вебхук уже указывает на нужный
        # URL, ничего не переотправляем. Без этой проверки КАЖДЫЙ холодный
        # старт функции звал set_webhook заново; если Vercel поднимает
        # несколько инстансов почти одновременно (всплеск трафика/деплой),
        # параллельные set_webhook попадают под flood control Telegram
        # (429 Too Many Requests).
        webhook_url = f"{settings.public_base_url.rstrip('/')}/api/telegram/webhook"
        current = await bot_app.bot.get_webhook_info()
        if current.url != webhook_url:
            await bot_app.bot.set_webhook(
                url=webhook_url,
                secret_token=settings.telegram_webhook_secret,
                allowed_updates=Update.ALL_TYPES,
            )
    except Exception as exc:
        # Бот — best-effort: initialize()/start() сами дёргают Telegram API
        # (getMe и т.п.), и если Telegram сейчас лимитирует этот бот-токен
        # (flood control) или временно недоступен — это НЕ должно ронять
        # весь бэкенд. Остальной API (auth, profile, subjects...) с ботом
        # никак не связан и обязан продолжать работать. Бот восстановится
        # сам на следующем холодном старте, когда Telegram отпустит.
        print(f"[bot] инициализация не удалась, бот будет недоступен до следующего холодного старта: {exc}")
    try:
        yield
    finally:
        if bot_started:
            try:
                await bot_app.stop()
                await bot_app.shutdown()
            except Exception as exc:
                print(f"[bot] ошибка при остановке (не критично): {exc}")


app = FastAPI(title="Schooler Auth API", lifespan=lifespan)

# GZip сжимает JSON-ответы (списки тем/предметов, разборы пробников и т.п.)
# перед отправкой — безопасный выигрыш по скорости независимо от того,
# персонализированы данные или нет (в отличие от HTTP-кэширования, которое
# для персональных данных вроде /api/subjects небезопасно, см. ниже).
# minimum_size — не сжимать совсем маленькие ответы, там оверхед на сжатие
# не окупается.
app.add_middleware(GZipMiddleware, minimum_size=500)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[*settings.cors_origins_list, 'http://localhost:3000'],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(content_router)
app.include_router(probnik_router)
app.include_router(profile_router)
app.include_router(xp_router)
app.include_router(media_proxy_router)

app.include_router(vk_auth_router)
app.include_router(yandex_auth_router)
app.include_router(supabase_auth_router)
app.include_router(ai_tutor.router)

# ⚠️ ВАЖНО ПЕРЕД ПЕРЕЕЗДОМ НА VERCEL: serverless-функции не имеют
# постоянной файловой системы между запросами/инстансами — всё, что
# записывается сюда во время работы (а не лежит в репозитории на момент
# деплоя), не переживёт следующий холодный старт и не будет видно другим
# инстансам. Если где-то в проекте есть код, который ДОЗАПИСЫВАЕТ файлы в
# static/images или static/kompege_images во время работы (а не только
# читает то, что закоммичено) — это сломается на Vercel. Такие файлы нужно
# заранее перенести в объектное хранилище (например, то же, что уже
# используется для формул/аудио на selstorage.ru, или Supabase Storage).
#
# Дополнительно: рантайм на Vercel может быть read-only, и если этих папок
# нет в самом репозитории на момент деплоя — os.makedirs упадёт с
# исключением на уровне ИМПОРТА МОДУЛЯ, а значит любой роут API отдаст 500,
# даже никак не связанный со статикой. Оборачиваем в try/except, чтобы сбой
# монтирования статики не ронял весь бэкенд целиком.
def _mount_static(path: str, directory: str, name: str) -> None:
    try:
        os.makedirs(directory, exist_ok=True)
        app.mount(path, StaticFiles(directory=directory), name=name)
    except OSError as exc:
        print(f"[static] не удалось подключить {path} -> {directory}: {exc}")


_mount_static("/images", "static/images", "images")
_mount_static("/kompege-images", "static/kompege_images", "kompege_images")

try:
    app.mount("/tg-webapp", StaticFiles(directory="static", html=True), name="tg_webapp")
except (OSError, RuntimeError) as exc:
    print(f"[static] не удалось подключить /tg-webapp: {exc}")


@app.post("/api/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict:
    """Приём апдейтов от Telegram вместо long-polling.

    Секретный токен в заголовке — обязательная проверка: без неё кто угодно,
    зная URL вебхука, мог бы слать боту поддельные "апдейты" от вашего
    имени. Telegram сам подставляет этот заголовок в каждый запрос, если он
    был передан в set_webhook(secret_token=...) при регистрации (см. lifespan
    выше) — совпадение значений и проверяем."""
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid secret token")

    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return {"ok": True}


@app.post("/api/auth/start", response_model=AuthStartResponse, dependencies=[Depends(rate_limit(5, 60))])
async def auth_start() -> AuthStartResponse:
    """Приложение вызывает это при нажатии «Войти через Telegram» —
    получает код и ссылку, которую нужно открыть (deep link на бота)."""
    code = secrets.token_urlsafe(16)
    ttl_minutes = settings.login_code_ttl_minutes
    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=ttl_minutes)

    async with async_session() as session:
        session.add(AuthSession(code=code, status=AuthStatus.pending, expires_at=expires_at))
        await session.commit()

    return AuthStartResponse(
        code=code,
        deep_link=f"https://t.me/{settings.bot_username}?start={code}",
        expires_in=ttl_minutes * 60,
    )


@app.get("/api/auth/session/{code}", response_model=SessionStatusResponse, dependencies=[Depends(rate_limit(100, 200))])
async def auth_session_status(code: str) -> SessionStatusResponse:
    """Приложение опрашивает этот эндпоинт (поллинг), пока не увидит status=confirmed.
    После входа этот же код можно хранить локально и дергать эндпоинт при каждом
    запуске приложения, чтобы снова получить имя пользователя без повторного логина."""
    async with async_session() as session:
        auth = await session.get(AuthSession, code)
        if auth is None:
            raise HTTPException(status_code=404, detail="Код не найден")

        now = dt.datetime.now(dt.timezone.utc)
        if auth.status == AuthStatus.pending and auth.expires_at < now:
            auth.status = AuthStatus.expired
            await session.commit()

        if auth.status != AuthStatus.confirmed:
            return SessionStatusResponse(status=auth.status.value)

        result = await session.execute(select(User).where(User.telegram_id == auth.telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=500, detail="Пользователь не найден")

        return SessionStatusResponse(
            status="confirmed",
            name=user.first_name,
            username=user.username,
            telegram_id=user.telegram_id,
            session_token=auth.session_token,
        )


@app.get("/api/auth/me", response_model=SessionStatusResponse)
async def auth_me(user: User = Depends(get_current_user)) -> SessionStatusResponse:
    """Приложение вызывает это при каждом запуске с уже сохранённым
    session_token, чтобы проверить его валидность и получить имя пользователя —
    без повторного похода в /api/auth/session/{code}, который был нужен только
    один раз, во время самого логина."""
    return SessionStatusResponse(
        status="confirmed",
        name=user.first_name,
        username=user.username,
        telegram_id=user.telegram_id,
    )


@app.post("/api/auth/logout")
async def logout(authorization: str = Header(default="")) -> dict:
    """Реальный логаут на сервере — отзывает именно тот токен сессии, которым
    был вызван этот эндпоинт (а не все сессии пользователя разом, чтобы не
    разлогинивать его на других устройствах)."""
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            async with async_session() as session:
                user_session = await session.get(UserSession, token)
                if user_session is not None:
                    user_session.revoked = True
                    await session.commit()
    return {"ok": True}


class WebAppAuthIn(BaseModel):
    init_data: str


@app.post("/api/auth/webapp", response_model=SessionStatusResponse, dependencies=[Depends(rate_limit(20, 60))])
async def auth_webapp(payload: WebAppAuthIn) -> SessionStatusResponse:
    data = validate_init_data(payload.init_data, settings.bot_token)
    if data is None or "user" not in data:
        raise HTTPException(status_code=401, detail="Недействительные данные Mini App")

    tg_user = data["user"]
    telegram_id = tg_user["id"]

    async with async_session() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == telegram_id))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                telegram_id=telegram_id,
                first_name=tg_user.get("first_name", ""),
                username=tg_user.get("username"),
            )
            session.add(user)
            await session.flush()

        token = secrets.token_urlsafe(32)
        session.add(UserSession(
            token=token, user_telegram_id=telegram_id,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30),
        ))
        await session.commit()

        return SessionStatusResponse(
            status="confirmed", name=user.first_name, username=user.username,
            telegram_id=telegram_id, session_token=token,
        )



@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}
