import datetime as dt
import logging
import secrets

from sqlalchemy import select
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import settings
from db import async_session
from models import AuthSession, AuthStatus, User, UserSession

logger = logging.getLogger("bot")

SESSION_TOKEN_TTL_DAYS = 30


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    args = context.args  # payload после ?start= — то есть наш код

    if not args:
        await update.message.reply_text(
            "Привет! Это бот авторизации Schooler 👋\n"
            "Откройте приложение и нажмите «Войти через Telegram» — "
            "оно само пришлёт правильную ссылку с кодом."
        )
        return

    code = args[0]

    async with async_session() as session:
        auth = await session.get(AuthSession, code)

        if auth is None:
            await update.message.reply_text(
                "Код авторизации не найден или устарел. Запросите новую ссылку в приложении."
            )
            return

        if auth.status == AuthStatus.confirmed:
            await update.message.reply_text(
                f"Вы уже вошли как {tg_user.first_name}. Можно возвращаться в приложение ✅"
            )
            return

        now = dt.datetime.now(dt.timezone.utc)
        if auth.expires_at < now:
            auth.status = AuthStatus.expired
            await session.commit()
            await update.message.reply_text("Срок действия ссылки истёк. Запросите новую в приложении.")
            return

        # создаём/обновляем пользователя
        result = await session.execute(select(User).where(User.telegram_id == tg_user.id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=tg_user.id, first_name=tg_user.first_name, username=tg_user.username)
            session.add(user)
        else:
            user.first_name = tg_user.first_name
            user.username = tg_user.username

        # ВАЖНО: явно flush'им, чтобы INSERT в users гарантированно выполнился
        # раньше UPDATE в auth_sessions — иначе SQLAlchemy может упорядочить
        # операции наоборот и упереться в внешний ключ auth_sessions.telegram_id.
        await session.flush()

        # Выпускаем отдельный долгоживущий токен сессии — именно им, а не
        # кодом входа, приложение будет пользоваться дальше.
        session_token = secrets.token_urlsafe(32)
        session.add(
            UserSession(
                token=session_token,
                user_telegram_id=tg_user.id,
                expires_at=now + dt.timedelta(days=SESSION_TOKEN_TTL_DAYS),
            )
        )

        auth.status = AuthStatus.confirmed
        auth.telegram_id = tg_user.id
        auth.session_token = session_token

        await session.commit()

    await update.message.reply_text(
        f"Готово, {tg_user.first_name}! 🎉\nВозвращайтесь в приложение — вы уже авторизованы."
    )


def build_bot_application() -> Application:
    # Таймауты get_updates_* убраны — они относятся только к long-polling
    # (getUpdates), которым мы больше не пользуемся; connect/read timeout
    # для обычных вызовов Bot API (setWebhook, sendMessage и т.п.) остаются.
    application = (
        Application.builder()
        .token(settings.bot_token)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .build()
    )
    application.add_handler(CommandHandler("start", start_handler))
    return application
