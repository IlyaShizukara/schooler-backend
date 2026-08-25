# backend/migrations/add_task_criteria.py
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import text
from db import engine


async def main():
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS criteria TEXT"))
    print("OK: tasks.criteria")


asyncio.run(main())