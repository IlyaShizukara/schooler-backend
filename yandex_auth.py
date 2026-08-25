import secrets

import datetime as dt

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from auth_dependency import get_current_user
from config import settings
from db import async_session
from models import User, UserSession
from oauth_common import create_state, pop_state

router = APIRouter(prefix="/api/auth/yandex", tags=["auth-yandex"])

YANDEX_AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
YANDEX_TOKEN_URL = "https://oauth.yandex.ru/token"
YANDEX_USERINFO_URL = "https://login.yandex.ru/info"

FRONTEND_URL = "https://myschooler.ru"


@router.get("/link/start")
async def yandex_link_start(user: User = Depends(get_current_user)):
    state = create_state("link", telegram_id=user.telegram_id)
    url = (
        f"{YANDEX_AUTHORIZE_URL}?response_type=code&client_id={settings.yandex_client_id}"
        f"&redirect_uri={settings.yandex_redirect_uri}&state={state}"
    )
    return {"authorize_url": url}


@router.get("/login/start")
async def yandex_login_start():
    state = create_state("login")
    url = (
        f"{YANDEX_AUTHORIZE_URL}?response_type=code&client_id={settings.yandex_client_id}"
        f"&redirect_uri={settings.yandex_redirect_uri}&state={state}"
    )
    return {"authorize_url": url}


@router.get("/callback")
async def yandex_callback(code: str, state: str):
    data = pop_state(state)
    if data is None:
        return RedirectResponse(f"{FRONTEND_URL}/?auth_error=yandex_state")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(YANDEX_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.yandex_client_id,
            "client_secret": settings.yandex_client_secret,
        })
        if token_resp.status_code != 200:
            return RedirectResponse(f"{FRONTEND_URL}/?auth_error=yandex_token")
        access_token = token_resp.json()["access_token"]

        userinfo_resp = await client.get(
            YANDEX_USERINFO_URL, params={"format": "json"},
            headers={"Authorization": f"OAuth {access_token}"},
        )
        if userinfo_resp.status_code != 200:
            return RedirectResponse(f"{FRONTEND_URL}/?auth_error=yandex_userinfo")
        yandex_id = userinfo_resp.json()["id"]

    async with async_session() as session:
        if data["mode"] == "link":
            user = await session.scalar(select(User).where(User.telegram_id == data["telegram_id"]))
            if user is None:
                return RedirectResponse(f"{FRONTEND_URL}/?auth_error=yandex_no_account")
            user.yandex_id = yandex_id
            await session.commit()
            return RedirectResponse(f"{FRONTEND_URL}/?yandex_linked=1")

        user = await session.scalar(select(User).where(User.yandex_id == yandex_id))
        if user is None:
            return RedirectResponse(f"{FRONTEND_URL}/?auth_error=yandex_not_linked")

        token = secrets.token_urlsafe(32)
        session.add(UserSession(
            token=token, user_telegram_id=user.telegram_id,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30),
        ))
        await session.commit()
        return RedirectResponse(f"{FRONTEND_URL}/?wa_token={token}")