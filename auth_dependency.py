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