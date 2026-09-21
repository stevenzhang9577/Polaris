"""Isolated real API/DB/router for the browser routing regression (no lifespan jobs)."""

import os
from contextlib import asynccontextmanager

from fastapi import Depends

import app.models  # noqa: F401
from app.api.auth import current_active_user
from app.core.db import Base, get_engine
from app.core.llm.base import Message
from app.core.llm.router import get_llm_router
from app.main import create_app

app = create_app()


@asynccontextmanager
async def lifespan(_app):
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app.router.lifespan_context = lifespan


@app.post("/api/test-routing/call")
async def routed_call(user=Depends(current_active_user)):
    result = await get_llm_router().complete(
        "librarian", [Message(role="user", content="routing regression")], user_id=user.id,
    )
    return {"model": result.model}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["POLARIS_TEST_API_PORT"]))
