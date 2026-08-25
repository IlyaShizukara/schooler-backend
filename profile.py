import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select

from auth_dependency import get_current_user
from db import async_session
from models import Subject, User, UserProfile, UserSelectedSubject
from schemas import OnboardingIn, ProfileOut

router = APIRouter(prefix="/api/profile", tags=["profile"])


async def _build_profile_out(user: User) -> ProfileOut:
    async with async_session() as session:
        profile = await session.get(UserProfile, user.telegram_id)

        selected_slugs = (
            await session.execute(
                select(Subject.slug)
                .join(UserSelectedSubject, UserSelectedSubject.subject_id == Subject.id)
                .where(UserSelectedSubject.user_telegram_id == user.telegram_id)
            )
        ).scalars().all()

        if profile is None:
            return ProfileOut(
                display_name=user.first_name,
                subject_slugs=list(selected_slugs),
            )

        return ProfileOut(
            display_name=profile.display_name or user.first_name,
            exam_type=profile.exam_type,
            grade=profile.grade,
            exam_date=profile.exam_date.isoformat() if profile.exam_date else None,
            daily_goal=profile.daily_goal,
            target_score=profile.target_score,
            onboarding_completed=profile.onboarding_completed,
            subject_slugs=list(selected_slugs),
        )


@router.get("", response_model=ProfileOut)
async def get_profile(user: User = Depends(get_current_user)) -> ProfileOut:
    return await _build_profile_out(user)


@router.post("/onboarding", response_model=ProfileOut)
async def complete_onboarding(payload: OnboardingIn, user: User = Depends(get_current_user)) -> ProfileOut:
    async with async_session() as session:
        profile = await session.get(UserProfile, user.telegram_id)
        if profile is None:
            profile = UserProfile(user_telegram_id=user.telegram_id)
            session.add(profile)

        profile.display_name = payload.display_name.strip() or user.first_name
        profile.exam_type = payload.exam_type
        profile.grade = payload.grade
        profile.exam_date = dt.date.fromisoformat(payload.exam_date) if payload.exam_date else None
        profile.daily_goal = max(1, payload.daily_goal)
        profile.target_score = max(0, payload.target_score)
        profile.onboarding_completed = True

        # перезаписываем набор выбранных предметов целиком
        await session.execute(
            delete(UserSelectedSubject).where(UserSelectedSubject.user_telegram_id == user.telegram_id)
        )
        if payload.subject_slugs:
            subjects = (
                await session.execute(select(Subject).where(Subject.slug.in_(payload.subject_slugs)))
            ).scalars().all()
            for s in subjects:
                session.add(UserSelectedSubject(user_telegram_id=user.telegram_id, subject_id=s.id))

        await session.commit()

    return await _build_profile_out(user)