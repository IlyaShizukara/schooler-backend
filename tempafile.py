from sqlalchemy import text
from db import async_session
import asyncio

BACKEND_PROD_URL = "https://schooler2-shizukara.amvera.io"  # подставьте настоящий

async def run():
    async with async_session() as session:
        result = await session.execute(
            text("UPDATE tasks SET question = REPLACE(question, :old, :new) "
                 "WHERE question LIKE :pattern"),
            {"old": "http://127.0.0.1:8000", "new": BACKEND_PROD_URL, "pattern": "%127.0.0.1:8000%"}
        )
        await session.commit()
        print(f"Обновлено строк: {result.rowcount}")

asyncio.run(run())