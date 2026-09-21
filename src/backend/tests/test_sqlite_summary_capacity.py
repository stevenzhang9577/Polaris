"""File-backed SQLite must leave room for summary workers and lease heartbeats."""

import asyncio
import sqlite3
from contextlib import AsyncExitStack

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import dispose_engine, get_engine


async def test_twenty_workers_leave_connections_for_heartbeats_and_dispatcher(app):
    engine = get_engine()
    async with AsyncExitStack() as stack:
        # Hold the connections as real LLM calls do, then request extra connections
        # for dispatcher and heartbeat work. The old default pool stalls at 15.
        for _ in range(22):
            connection = await asyncio.wait_for(stack.enter_async_context(engine.connect()), 2)
            assert await connection.scalar(text("SELECT 1")) == 1


async def test_sqlite_readers_and_writers_do_not_block_each_other(app):
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text('CREATE TABLE contention_probe (value INTEGER)'))
        await conn.execute(text('INSERT INTO contention_probe VALUES (1)'))
    try:
        async with engine.connect() as reader, engine.connect() as writer:
            assert await reader.scalar(text('PRAGMA journal_mode')) == 'wal'
            assert await writer.scalar(text('PRAGMA busy_timeout')) == 10000
            assert await writer.scalar(text('PRAGMA foreign_keys')) == 1
            # A list request holds a read snapshot while a worker commits progress.
            await reader.execute(text('BEGIN'))
            assert await reader.scalar(text('SELECT value FROM contention_probe')) == 1
            await writer.execute(text('UPDATE contention_probe SET value = 2'))
            await asyncio.wait_for(writer.commit(), 1)
            assert await reader.scalar(text('SELECT value FROM contention_probe')) == 1
            await reader.rollback()
            assert await reader.scalar(text('SELECT value FROM contention_probe')) == 2
            await reader.rollback()

            # A new request can still read during an unfinished write transaction.
            await writer.execute(text('BEGIN EXCLUSIVE'))
            await writer.execute(text('UPDATE contention_probe SET value = 3'))
            async with engine.connect() as fresh_reader:
                assert await asyncio.wait_for(
                    fresh_reader.scalar(text('SELECT value FROM contention_probe')), 1
                ) == 2
            await writer.rollback()
    finally:
        async with engine.begin() as conn:
            await conn.execute(text('DROP TABLE contention_probe'))


async def test_existing_rollback_database_preserves_rows_when_enabling_wal(tmp_path, monkeypatch):
    database = tmp_path / "existing.db"
    with sqlite3.connect(database) as original:
        assert original.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        original.execute("CREATE TABLE existing_papers (title TEXT)")
        original.execute("INSERT INTO existing_papers VALUES ('Keep this paper')")
    await dispose_engine()
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{database}")
    try:
        async with get_engine().connect() as connection:
            assert await connection.scalar(text("PRAGMA journal_mode")) == "wal"
            title = await connection.scalar(text("SELECT title FROM existing_papers"))
            assert title == "Keep this paper"
            assert await connection.scalar(text("PRAGMA quick_check")) == "ok"
    finally:
        await dispose_engine()


async def test_in_memory_sqlite_keeps_shared_database(monkeypatch):
    await dispose_engine()
    monkeypatch.setattr(get_settings(), "database_url", "sqlite+aiosqlite:///:memory:")
    try:
        async with get_engine().begin() as connection:
            assert await connection.scalar(text("PRAGMA journal_mode")) == "memory"
            await connection.execute(text("CREATE TABLE retained (value INTEGER)"))
            await connection.execute(text("INSERT INTO retained VALUES (42)"))
        async with get_engine().connect() as connection:
            assert await connection.scalar(text("SELECT value FROM retained")) == 42
    finally:
        await dispose_engine()
