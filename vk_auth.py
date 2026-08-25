import base64
import hashlib
import secrets
import datetime as dt

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from auth_dependency import get_current_user
from config import settings
from db import async_session
from models import User, UserSession
from oauth_common import create_state, pop_state


router = APIRouter(prefix="/api/auth/vk", tags=["auth-vk"])

VK_AUTHORIZE_URL = "https://id.vk.ru/authorize"
VK_TOKEN_URL = "https://id.vk.ru/oauth2/auth"
VK_USERINFO_URL = "https://id.vk.ru/oauth2/user_info"

FRONTEND_URL = "https://myschooler.ru"


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


@router.get("/link/start")
async def vk_link_start(user: User = Depends(get_current_user)):
    """Пользователь уже вошёл через Telegram — привязываем VK ID к его аккаунту."""
    verifier, challenge = _pkce_pair()
    state = create_state("link", telegram_id=user.telegram_id, code_verifier=verifier)
    url = (
        f"{VK_AUTHORIZE_URL}?response_type=code&client_id={settings.vk_client_id}"
        f"&redirect_uri={settings.vk_redirect_uri}&state={state}"
        f"&code_challenge={challenge}&code_challenge_method=S256&scope=email"
    )
    return {"authorize_url": url}


@router.get("/login/start")
async def vk_login_start():
    """Вход существующего пользователя, у которого VK уже привязан ранее."""
    verifier, challenge = _pkce_pair()
    state = create_state("login", code_verifier=verifier)
    url = (
        f"{VK_AUTHORIZE_URL}?response_type=code&client_id={settings.vk_client_id}"
        f"&redirect_uri={settings.vk_redirect_uri}&state={state}"
        f"&code_challenge={challenge}&code_challenge_method=S256&scope=email"
    )
    return {"authorize_url": url}


@router.get("/callback")
async def vk_callback(code: str, state: str):
    data = pop_state(state)
    if data is None:
        return RedirectResponse(f"{FRONTEND_URL}/?auth_error=vk_state")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(VK_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.vk_client_id,
            "redirect_uri": settings.vk_redirect_uri,
            "code_verifier": data["code_verifier"],
        })
        if token_resp.status_code != 200:
            return RedirectResponse(f"{FRONTEND_URL}/?auth_error=vk_token")
        token_data = token_resp.json()

        userinfo_resp = await client.post(VK_USERINFO_URL, data={
            "client_id": settings.vk_client_id,
            "access_token": token_data["access_token"],
        })
        if userinfo_resp.status_code != 200:
            return RedirectResponse(f"{FRONTEND_URL}/?auth_error=vk_userinfo")
        vk_user = userinfo_resp.json().get("user", {})
        vk_id = int(vk_user["user_id"])

    async with async_session() as session:
        if data["mode"] == "link":
            user = await session.scalar(select(User).where(User.telegram_id == data["telegram_id"]))
            if user is None:
                return RedirectResponse(f"{FRONTEND_URL}/?auth_error=vk_no_account")
            user.vk_id = vk_id
            await session.commit()
            return RedirectResponse(f"{FRONTEND_URL}/?vk_linked=1")

        # mode == "login"
        user = await session.scalar(select(User).where(User.vk_id == vk_id))
        if user is None:
            return RedirectResponse(f"{FRONTEND_URL}/?auth_error=vk_not_linked")

        token = secrets.token_urlsafe(32)
        session.add(UserSession(
            token=token, user_telegram_id=user.telegram_id,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30),
        ))
        await session.commit()
        return RedirectResponse(f"{FRONTEND_URL}/?wa_token={token}")