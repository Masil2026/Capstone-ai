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


# ---------------------------------------------------------------------------
# 대화 히스토리 캐시 (chat_history:{room_id})
# DB(chat_messages)에서 매 요청마다 로드한 결과를 Redis에 mirror
# ---------------------------------------------------------------------------

async def save_history(room_id: str, messages: list[ModelMessage]) -> None:
    """DB에서 읽은 히스토리를 Redis에 저장 (mirror). 다음 요청에서도 동일 데이터 조회 가능."""
    if not messages:
        return
    await _redis.set(
        f"chat_history:{room_id}",
        ModelMessagesTypeAdapter.dump_json(messages),
    )


async def save_raw_history(room_id: str, messages: list[dict]) -> None:
    """DB에서 읽은 raw 히스토리(role+content)를 단순 JSON으로 Redis에 저장 (테스트용). 없으면 빈 배열."""
    await _redis.set(
        f"chatroom_history:{room_id}",
        json.dumps(messages, ensure_ascii=False),
    )
    print(f"[save_raw_history] chatroom_history:{room_id} → {len(messages)}건 저장", flush=True)


async def save_pg_history(room_id: str, messages: list[dict]) -> None:
    """pgvector 유사도 검색으로 찾은 메시지(role+content)를 Redis에 저장 (테스트용). 없으면 빈 배열."""
    await _redis.set(
        f"pgchatroom_history:{room_id}",
        json.dumps(messages, ensure_ascii=False),
    )
    print(f"[save_pg_history] pgchatroom_history:{room_id} → {len(messages)}건 저장", flush=True)


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
