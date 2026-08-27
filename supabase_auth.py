"""
Регистрация/вход через email+пароль — реализовано через Supabase Auth
(их собственный сервис управляет паролями, подтверждением email и т.п.,
мы это не переизобретаем).

Поток:
  1. Фронтенд регистрирует/логинит пользователя НАПРЯМУЮ через Supabase JS
     SDK (supabase.auth.signUp / signInWithPassword) — сам процесс
     регистрации полностью на стороне Supabase.
  2. Получив успешную Supabase-сессию, фронтенд шлёт её access_token сюда.
  3. Мы проверяем подпись токена через JWKS Supabase (асимметричные ключи,
     см. https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json),
     достаём email/sub и заводим-или-находим соответствующего User.
  4. Выдаём тот же UserSession-токен, что и везде (Telegram/VK/Яндекс) —
     остальному бэкенду (auth_dependency, profile.py, probnik.py и т.д.)
     вообще не важно, как пользователь вошёл.

Ключевой архитектурный трюк: User.telegram_id — обязательное уникальное
поле, на которое ссылаются ВСЕ остальные таблицы проекта. Чтобы не
переписывать весь бэкенд на User.id, для email-пользователей выдаём
СИНТЕТИЧЕСКИЙ отрицательный telegram_id (настоящие Telegram id всегда
положительные — коллизий быть не может). Для всех остальных таблиц и
роутеров такой пользователь неотличим от обычного.
"""
import datetime as dt
import secrets

import jwt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from config import settings
from db import async_session
from models import User, UserSession

router = APIRouter(prefix="/api/auth/supabase", tags=["auth-supabase"])

# Ленивый клиент — сам JWKS не запрашивает при создании объекта, только при
# первой реальной проверке токена (и дальше кэширует ключи в памяти).
_jwks_client = jwt.PyJWKClient(settings.supabase_jwks_url)


def _new_synthetic_telegram_id() -> int:
    # Настоящие Telegram id всегда положительные — берём гарантированно
    # отрицательное 62-битное случайное число (диапазон ~4.6*10^18).
    # Коллизия практически невозможна; на уникальность всё равно стоит
    # UNIQUE-констрейнт в БД как последний рубеж.
    return -secrets.randbits(62)


class SupabaseAuthIn(BaseModel):
    access_token: str


@router.post("")
async def supabase_login(payload: SupabaseAuthIn) -> dict:
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(payload.access_token)
        claims = jwt.decode(
            payload.access_token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Недействительный токен Supabase: {exc}")

    supabase_user_id = claims.get("sub")
    email = claims.get("email")
    if not supabase_user_id:
        raise HTTPException(status_code=401, detail="В токене нет идентификатора пользователя")

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.supabase_user_id == supabase_user_id))

        if user is None:
            user = User(
                telegram_id=_new_synthetic_telegram_id(),
                first_name=(email.split("@")[0] if email else "Ученик"),
                username=None,
                supabase_user_id=supabase_user_id,
                email=email,
            )
            session.add(user)
        elif email and user.email != email:
            user.email = email  # email мог смениться в Supabase — синхронизируем

        token = secrets.token_urlsafe(32)
        await session.flush()  # гарантирует user.telegram_id доступен, если User только что создан
        session.add(UserSession(
            token=token,
            user_telegram_id=user.telegram_id,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30),
        ))
        await session.commit()

        return {
            "status": "confirmed",
            "name": user.first_name,
            "username": user.username,
            "telegram_id": user.telegram_id,
            "session_token": token,
        }
