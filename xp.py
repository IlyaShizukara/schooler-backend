import datetime as dt
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from db import async_session
from models import UserStats, User
from auth_dependency import get_current_user

XP_CORRECT = 10
XP_INCORRECT = 2
XP_PER_PROBNIK_TASK = 1


def xp_to_level(xp: int) -> int:
    return xp // 100 + 1


def xp_for_next_level(xp: int) -> int:
    return xp_to_level(xp) * 100


async def award_xp(
    session: AsyncSession,
    user_telegram_id: int,
    amount: int,
    activity_date: dt.date | None = None,
) -> UserStats:
    """Начисляет XP и обновляет стрик. Не коммитит — вызывающий код должен сам сделать commit."""
    activity_date = activity_date or dt.datetime.now(dt.timezone.utc).date()

    stats = await session.get(UserStats, user_telegram_id)
    if stats is None:
        stats = UserStats(
            user_telegram_id=user_telegram_id,
            xp=0, level=1, current_streak=0, longest_streak=0,
            last_activity_date=None,
        )
        session.add(stats)

    stats.xp += amount
    stats.level = xp_to_level(stats.xp)

    if stats.last_activity_date is None:
        stats.current_streak = 1
    elif stats.last_activity_date == activity_date:
        pass  # уже засчитан сегодня, стрик не трогаем
    elif stats.last_activity_date == activity_date - dt.timedelta(days=1):
        stats.current_streak += 1
    else:
        stats.current_streak = 1  # был разрыв

    stats.last_activity_date = activity_date
    stats.longest_streak = max(stats.longest_streak, stats.current_streak)

    await session.flush()
    return stats


# ---- роутер ----

router = APIRouter(prefix="/api/xp", tags=["xp"])


class XPSummaryOut(BaseModel):
    xp: int
    level: int
    xp_for_next_level: int
    current_streak: int
    longest_streak: int


@router.get("/summary", response_model=XPSummaryOut)
async def xp_summary(user: User = Depends(get_current_user)) -> XPSummaryOut:
    async with async_session() as session:
        stats = await session.get(UserStats, user.telegram_id)
        if stats is None:
            return XPSummaryOut(
                xp=0, level=1, xp_for_next_level=100,
                current_streak=0, longest_streak=0,
            )
        return XPSummaryOut(
            xp=stats.xp,
            level=stats.level,
            xp_for_next_level=xp_for_next_level(stats.xp),
            current_streak=stats.current_streak,
            longest_streak=stats.longest_streak,
        )