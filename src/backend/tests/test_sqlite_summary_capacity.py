"""File-backed SQLite must leave room for summary workers and lease heartbeats."""

import asyncio
from contextlib import AsyncExitStack

from sqlalchemy import text

from app.core.db import get_engine


async def test_twenty_workers_leave_connections_for_heartbeats_and_dispatcher(app):
    engine = get_engine()
    async with AsyncExitStack() as stack:
        # Hold the connections as real LLM calls do, then request extra connections
        # for dispatcher and heartbeat work. The old default pool stalls at 15.
        for _ in range(22):
            connection = await asyncio.wait_for(stack.enter_async_context(engine.connect()), 2)
            assert await connection.scalar(text("SELECT 1")) == 1
