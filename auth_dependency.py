import datetime as dt

from fastapi import Header, HTTPException
from sqlalchemy import select

from db import async_session
from models import User, UserSession


async def get_current_user(authorization: str = Header(...)) -> User:
    """Достаёт текущего пользователя из заголовка `Authorization: Bearer <session_token>`.

    <session_token> — настоящий токен сессии (модель UserSession), выданный
    один раз при подтверждении входа через Telegram (см. bot.py). В отличие
    от кода входа (AuthSession) у него есть TTL и его можно отозвать — logout
    помечает соответствующую запись revoked=True.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Не передан токен авторизации")

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Пустой токен авторизации")

    async with async_session() as session:
        user_session = await session.get(UserSession, token)
        if user_session is None or user_session.revoked:
            raise HTTPException(status_code=401, detail="Сессия недействительна — войдите через Telegram заново")

        if user_session.expires_at < dt.datetime.now(dt.timezone.utc):
            raise HTTPException(status_code=401, detail="Сессия истекла — войдите через Telegram заново")

        result = await session.execute(
            select(User).where(User.telegram_id == user_session.user_telegram_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=401, detail="Пользователь не найден")

        return user


async def get_current_user_optional(authorization: str | None = Header(default=None)) -> User | None:
    """Как get_current_user, но для эндпоинтов, которые должны отдавать
    контент и гостю: при отсутствии заголовка, невалидном или истёкшем
    токене просто возвращает None вместо 401 — роут сам решает, что
    показать неавторизованному (обычно — тот же контент без персонализации,
    без записи Attempt/XP).

    ⚠️ Не путать с get_current_user: для действий, которые обязаны быть
    привязаны к пользователю (профиль, XP, история пробников), используем
    строгую версию — эта только для честно-гостевых сценариев.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None

    async with async_session() as session:
        user_session = await session.get(UserSession, token)
        if user_session is None or user_session.revoked:
            return None

        if user_session.expires_at < dt.datetime.now(dt.timezone.utc):
            return None

        result = await session.execute(
            select(User).where(User.telegram_id == user_session.user_telegram_id)
        )
        return result.scalar_one_or_none()