# app/services/agents/memory.py
from __future__ import annotations

import json

from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelMessage
from redis import asyncio as aioredis

from app.core.config import settings

_redis = aioredis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    password=settings.REDIS_PASSWORD,
    ssl=True,
    decode_responses=False,
)

MAX_HISTORY_MESSAGES = 20

# ---------------------------------------------------------------------------
# 대화 히스토리 (chat_history:{room_id})
# ---------------------------------------------------------------------------

async def load_history(room_id: str) -> list[ModelMessage]:
    data = await _redis.get(f"chat_history:{room_id}")
    if not data:
        return []
    return ModelMessagesTypeAdapter.validate_json(data)


async def save_history(room_id: str, messages: list[ModelMessage]) -> None:
    trimmed = messages[-MAX_HISTORY_MESSAGES:]
    await _redis.set(f"chat_history:{room_id}", ModelMessagesTypeAdapter.dump_json(trimmed))


# ---------------------------------------------------------------------------
# 장기 메모리 (memory:{room_id})
# 구조: {"ai_summary": str | null, "preferences": dict | null}
# 최초 요청 시 DB에서 로드 후 저장. AI가 update_memory 호출 시 갱신.
# ---------------------------------------------------------------------------

async def load_memory(room_id: str) -> dict | None:
    data = await _redis.get(f"memory:{room_id}")
    if not data:
        return None
    return json.loads(data)


async def save_memory(room_id: str, ai_summary: str | None, preferences: dict | None) -> None:
    payload = {"ai_summary": ai_summary, "preferences": preferences}
    await _redis.set(f"memory:{room_id}", json.dumps(payload, ensure_ascii=False))
