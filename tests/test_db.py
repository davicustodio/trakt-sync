from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import _ensure_chat_states_columns


async def test_ensure_chat_states_columns_adds_pending_identification(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE chat_states (
                    id INTEGER PRIMARY KEY,
                    chat_jid VARCHAR(255),
                    requester_phone VARCHAR(32),
                    last_image_message_id INTEGER,
                    last_identified_media_id INTEGER,
                    updated_at DATETIME
                )
                """
            )
        )
        await connection.run_sync(_ensure_chat_states_columns)
        columns = await connection.run_sync(lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns("chat_states")})

    await engine.dispose()
    assert "pending_identification" in columns
