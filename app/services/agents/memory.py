# app/services/agents/memory.py
from __future__ import annotations

from datetime import datetime, timezone
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
# 구조: { "ai_summary": str, "preferences": dict, "loaded_at": ISO 8601 }
# ---------------------------------------------------------------------------

async def load_memory(room_id: str) -> dict | None:
    data = await _redis.get(f"memory:{room_id}")
    if not data:
        return None
    return json.loads(data)


async def save_memory(room_id: str, ai_summary: str | None, preferences: dict | None) -> None:
    payload = {
        "ai_summary": ai_summary,
        "preferences": preferences,
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    }
    await _redis.set(f"memory:{room_id}", json.dumps(payload, ensure_ascii=False))


async def is_memory_stale(room_id: str, db_updated_at: datetime) -> bool:
    """DB updated_at이 Redis memory.loaded_at보다 최신이면 True (재로딩 필요)"""
    memory = await load_memory(room_id)
    if not memory:
        return True
    loaded_at = datetime.fromisoformat(memory["loaded_at"])
    return db_updated_at > loaded_at
