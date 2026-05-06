# app/services/agents/context.py
from __future__ import annotations

import asyncio
import json
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import text

from app.core.config import settings
from app.core.database import SessionLocal
from .memory import load_history, load_memory, save_memory

_openai_client = AsyncOpenAI(api_key=settings.GPT_API_KEY)
_EMBEDDING_MODEL = "text-embedding-3-small"


# ---------------------------------------------------------------------------
# 임베딩 생성
# ---------------------------------------------------------------------------

async def get_user_embedding(text_input: str) -> list[float]:
    """OpenAI text-embedding-3-small으로 임베딩 생성 (dim=1536)"""
    response = await _openai_client.embeddings.create(
        model=_EMBEDDING_MODEL,
        input=text_input,
    )
    return response.data[0].embedding


# ---------------------------------------------------------------------------
# DB 조회 (동기 — run_in_executor에서 실행)
# ---------------------------------------------------------------------------

def _query_similar_messages(room_id: str, embedding: list[float]) -> list[dict]:
    """pgvector 코사인 유사도 기준 상위 5개 과거 메시지 조회"""
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT role, content "
                "FROM chat_messages "
                "WHERE room_id = :room_id AND embedding IS NOT NULL "
                "ORDER BY embedding <=> CAST(:emb AS vector) "
                "LIMIT 5"
            ),
            {"room_id": room_id, "emb": str(embedding)},
        ).fetchall()
    return [{"role": r.role, "content": r.content} for r in rows]


def _query_chat_room_memory(room_id: str) -> dict:
    """chat_rooms 테이블에서 ai_summary, preferences 조회"""
    with SessionLocal() as db:
        row = db.execute(
            text("SELECT ai_summary, preferences FROM chat_rooms WHERE id = :room_id"),
            {"room_id": room_id},
        ).fetchone()
    if row is None:
        return {"ai_summary": None, "preferences": None}
    preferences = row.preferences
    if isinstance(preferences, str):
        preferences = json.loads(preferences)
    return {"ai_summary": row.ai_summary, "preferences": preferences}


def _query_current_itinerary(room_id: str) -> dict | None:
    """roomId로 현재 여행 일정 전체 조회"""
    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT destination, start_date, end_date, total_days, "
                "budget, adult_count, child_count, child_ages, day_plans "
                "FROM itineraries "
                "WHERE room_id = :room_id "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"room_id": room_id},
        ).fetchone()

    if row is None:
        return None

    day_plans = row.day_plans
    if isinstance(day_plans, str):
        day_plans = json.loads(day_plans)

    child_ages = row.child_ages
    if isinstance(child_ages, str):
        child_ages = json.loads(child_ages)

    return {
        "destination": row.destination,
        "start_date": str(row.start_date),
        "end_date": str(row.end_date),
        "total_days": row.total_days,
        "budget": float(row.budget) if row.budget is not None else None,
        "adult_count": row.adult_count,
        "child_count": row.child_count,
        "child_ages": child_ages or [],
        "day_plans": day_plans,
    }


# ---------------------------------------------------------------------------
# 컨텍스트 조립
# ---------------------------------------------------------------------------

async def load_context(room_id: str, user_message: str) -> dict[str, Any]:
    """OrchestratorDeps 구성에 필요한 모든 컨텍스트를 로드합니다.

    반환 키:
        user_embedding    : 사용자 메시지 임베딩 (done 이벤트 + pgvector용)
        history           : 최근 20개 대화 히스토리 (pydantic-ai ModelMessage)
        ai_summary        : chat_rooms.ai_summary (DB)
        preferences       : chat_rooms.preferences (DB)
        similar_messages  : pgvector 유사 메시지 최대 5개 [{"role", "content"}]
        current_itinerary : 현재 여행 일정 전체 (없으면 None)
    """
    loop = asyncio.get_running_loop()

    # 임베딩·히스토리·Redis memory는 독립적이므로 병렬 로드
    user_embedding, history, redis_memory = await asyncio.gather(
        get_user_embedding(user_message),
        load_history(room_id),
        load_memory(room_id),
    )

    # Redis miss → DB에서 로드 후 Redis에 저장
    if redis_memory is None:
        try:
            redis_memory = await loop.run_in_executor(None, _query_chat_room_memory, room_id)
        except Exception:
            redis_memory = {"ai_summary": None, "preferences": None}
        await save_memory(room_id, redis_memory["ai_summary"], redis_memory["preferences"])

    # pgvector 검색과 itinerary 조회는 임베딩 이후 병렬 실행
    similar_fut = loop.run_in_executor(None, _query_similar_messages, room_id, user_embedding)
    itinerary_fut = loop.run_in_executor(None, _query_current_itinerary, room_id)

    try:
        similar_messages = await similar_fut
    except Exception:
        similar_messages = []

    try:
        current_itinerary = await itinerary_fut
    except Exception:
        current_itinerary = None

    return {
        "user_embedding": user_embedding,
        "history": history,
        "ai_summary": redis_memory["ai_summary"],
        "preferences": redis_memory["preferences"],
        "similar_messages": similar_messages,
        "current_itinerary": current_itinerary,
    }
