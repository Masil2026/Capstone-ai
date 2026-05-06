# app/services/agents/context.py
from __future__ import annotations

import asyncio
import json
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import text

from app.core.config import settings
from app.core.database import SessionLocal
from app.schemas.ai_message import MemoryInput
from .memory import load_history, load_memory, save_memory

_openai_client = AsyncOpenAI(api_key=settings.GPT_API_KEY)
_EMBEDDING_MODEL = "text-embedding-3-small"


# ---------------------------------------------------------------------------
# 메모리 동기화
# ---------------------------------------------------------------------------

async def sync_memory(room_id: str, request_memory: MemoryInput | None) -> None:
    """요청 body.memory와 Redis memory 비교 후 필요시 Redis 업데이트.

    규칙:
      request_memory 있음 + Redis 없음       → Redis에 저장
      request_memory 있음 + Redis 있음 + 다름 → 요청 기준으로 교체
      request_memory 있음 + Redis 있음 + 같음 → 유지
      request_memory None  + Redis 있음       → Redis 유지
      request_memory None  + Redis 없음       → 아무것도 안 함
    """
    if request_memory is None:
        return

    redis_memory = await load_memory(room_id)

    req_summary = request_memory.aiSummary
    req_prefs = request_memory.preferences

    if redis_memory is None:
        await save_memory(room_id, req_summary, req_prefs)
        return

    if redis_memory.get("ai_summary") != req_summary or redis_memory.get("preferences") != req_prefs:
        await save_memory(room_id, req_summary, req_prefs)


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
        ai_summary        : Redis memory.ai_summary
        preferences       : Redis memory.preferences
        similar_messages  : pgvector 유사 메시지 최대 5개 [{"role", "content"}]
        current_itinerary : 현재 여행 일정 전체 (없으면 None)
    """
    loop = asyncio.get_running_loop()

    # 임베딩·히스토리·메모리는 독립적이므로 병렬 로드
    user_embedding, history, memory = await asyncio.gather(
        get_user_embedding(user_message),
        load_history(room_id),
        load_memory(room_id),
    )

    # pgvector 검색은 임베딩이 있어야 해서 순차 실행
    try:
        similar_messages = await loop.run_in_executor(
            None, _query_similar_messages, room_id, user_embedding
        )
    except Exception:
        similar_messages = []  # 폴백: 빈 리스트로 스트리밍 계속 진행

    # 현재 일정 조회
    try:
        current_itinerary = await loop.run_in_executor(
            None, _query_current_itinerary, room_id
        )
    except Exception:
        current_itinerary = None

    return {
        "user_embedding": user_embedding,
        "history": history,
        "ai_summary": memory.get("ai_summary") if memory else None,
        "preferences": memory.get("preferences") if memory else None,
        "similar_messages": similar_messages,
        "current_itinerary": current_itinerary,
    }
