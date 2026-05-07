# app/services/agents/context.py
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from typing import Any

import json as _json

from openai import AsyncOpenAI
from pydantic_ai.messages import ModelMessagesTypeAdapter
from sqlalchemy import text

from app.core.config import settings
from app.core.database import SessionLocal
from .memory import save_memory, save_raw_history, save_pg_history

_openai_client = AsyncOpenAI(api_key=settings.GPT_API_KEY)
_EMBEDDING_MODEL = "text-embedding-3-small"
_log = logging.getLogger(__name__)


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

def _query_recent_history(room_id: str) -> tuple[list, list[dict]]:
    """chat_messages에서 최근 20개 조회 → (ModelMessage 리스트, raw dict 리스트) 반환"""
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT role, content, created_at FROM chat_messages "
                "WHERE room_id = :room_id "
                "ORDER BY created_at DESC LIMIT 20"
            ),
            {"room_id": room_id},
        ).fetchall()
        rows = list(rows)  # Row 데이터를 세션 종료 전에 Python 객체로 완전히 복사

    raw_simple: list[dict] = []
    raw = []
    for row in reversed(rows):  # 오래된 순으로
        ts = row.created_at.isoformat() if hasattr(row.created_at, "isoformat") else str(row.created_at)
        raw_simple.append({"role": row.role, "content": row.content})
        if row.role == "user":
            raw.append({
                "kind": "request",
                "parts": [{"part_kind": "user-prompt", "content": row.content, "timestamp": ts}],
            })
        elif row.role == "assistant":
            raw.append({
                "kind": "response",
                "parts": [{"part_kind": "text", "content": row.content}],
                "model_name": "db",
                "timestamp": ts,
            })
    if not raw:
        return [], raw_simple
    try:
        return ModelMessagesTypeAdapter.validate_json(_json.dumps(raw)), raw_simple
    except Exception:
        return [], raw_simple


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


def _to_date_str(value) -> str:
    """datetime / date / str 모두 'YYYY-MM-DD' 형식으로 정규화"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]  # "2026-04-30T15:00:00+00:00" → "2026-04-30"


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

        if not row.destination or not row.start_date:
            return None

        day_plans = row.day_plans
        if isinstance(day_plans, str):
            day_plans = json.loads(day_plans)

        child_ages = row.child_ages
        if isinstance(child_ages, str):
            child_ages = json.loads(child_ages)

        return {
            "destination": row.destination,
            "start_date": _to_date_str(row.start_date),
            "end_date": _to_date_str(row.end_date),
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

    # 임베딩과 DB 조회 병렬 실행 — ai_summary/preferences는 매 요청 DB에서 직접 조회
    user_embedding_fut = get_user_embedding(user_message)
    db_memory_fut = loop.run_in_executor(None, _query_chat_room_memory, room_id)

    try:
        user_embedding, db_memory = await asyncio.gather(user_embedding_fut, db_memory_fut)
    except Exception:
        user_embedding = await user_embedding_fut
        db_memory = {"ai_summary": None, "preferences": None}

    # DB 값으로 Redis 동기화 — Java 백엔드가 DB에 썼을 수 있으므로 매 요청마다 갱신 (fire-and-forget)
    asyncio.ensure_future(save_memory(room_id, db_memory["ai_summary"], db_memory["preferences"]))

    # pgvector 검색 · itinerary 조회 · 대화 히스토리 병렬 실행
    similar_fut = loop.run_in_executor(None, _query_similar_messages, room_id, user_embedding)
    itinerary_fut = loop.run_in_executor(None, _query_current_itinerary, room_id)
    history_fut = loop.run_in_executor(None, _query_recent_history, room_id)

    try:
        similar_messages = await similar_fut
    except Exception:
        similar_messages = []

    asyncio.ensure_future(save_pg_history(room_id, similar_messages))

    try:
        current_itinerary = await itinerary_fut
    except Exception:
        _log.error("current_itinerary 로드 실패", exc_info=True)
        current_itinerary = None

    try:
        history, raw_history = await history_fut
    except Exception:
        _log.error("history 로드 실패", exc_info=True)
        history = []
        raw_history = []

    # DB에서 읽은 히스토리를 Redis에 저장 (비동기 fire-and-forget)
    asyncio.ensure_future(save_raw_history(room_id, raw_history))

    print(
        f"\n[load_context] DB 조회 결과"
        f"\n  room_id          : {room_id}"
        f"\n  ai_summary       : {db_memory['ai_summary']}"
        f"\n  preferences      : {db_memory['preferences']}"
        f"\n  history          : {len(history)}건"
        f"\n  similar_messages : {len(similar_messages)}건"
        f"\n  current_itinerary: {({k: v for k, v in current_itinerary.items() if k != 'day_plans'} if current_itinerary else None)}",
        flush=True,
    )

    return {
        "user_embedding": user_embedding,
        "history": history,
        "ai_summary": db_memory["ai_summary"],
        "preferences": db_memory["preferences"],
        "similar_messages": similar_messages,
        "current_itinerary": current_itinerary,
    }
