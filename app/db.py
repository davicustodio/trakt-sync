from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Base

settings = get_settings()

engine: AsyncEngine = create_async_engine(settings.database_url, future=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_ensure_phone_profiles_columns)
        await connection.run_sync(_ensure_chat_states_columns)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def _ensure_phone_profiles_columns(connection) -> None:
    inspector = inspect(connection)
    columns = {column["name"] for column in inspector.get_columns("phone_profiles")}
    if "telegram_access_granted" not in columns:
        default_false = "FALSE" if connection.dialect.name == "postgresql" else "0"
        connection.execute(
            text(
                f"ALTER TABLE phone_profiles ADD COLUMN telegram_access_granted BOOLEAN NOT NULL DEFAULT {default_false}"
            )
        )


def _ensure_chat_states_columns(connection) -> None:
    inspector = inspect(connection)
    columns = {column["name"] for column in inspector.get_columns("chat_states")}
    if "pending_identification" not in columns:
        connection.execute(text("ALTER TABLE chat_states ADD COLUMN pending_identification JSON"))
