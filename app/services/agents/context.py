# app/services/agents/context.py
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from typing import Any

import json as _json

import vertexai
from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel
from pydantic_ai.messages import ModelMessagesTypeAdapter
from sqlalchemy import text
from google.oauth2 import service_account

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from .memory import save_memory, save_raw_history, save_pg_history

_creds = None
if settings.GOOGLE_APPLICATION_CREDENTIALS:
    _creds = service_account.Credentials.from_service_account_file(
        settings.GOOGLE_APPLICATION_CREDENTIALS,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
vertexai.init(project=settings.GOOGLE_CLOUD_PROJECT, location=settings.GOOGLE_CLOUD_REGION, credentials=_creds)
_EMBEDDING_MODEL = "text-embedding-004"
_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 임베딩 생성
# ---------------------------------------------------------------------------

async def get_user_embedding(text_input: str) -> list[float]:
    """Vertex AI text-embedding-004로 임베딩 생성 (dim=768)"""
    model = TextEmbeddingModel.from_pretrained(_EMBEDDING_MODEL)
    inputs = [TextEmbeddingInput(text_input, task_type="RETRIEVAL_QUERY")]
    embeddings = await asyncio.to_thread(model.get_embeddings, inputs)
    return embeddings[0].values


# ---------------------------------------------------------------------------
# DB 조회 (비동기)
# ---------------------------------------------------------------------------

async def _query_recent_history(room_id: str) -> tuple[list, list[dict]]:
    """chat_messages에서 최근 20개 조회 → (ModelMessage 리스트, raw dict 리스트) 반환"""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT role, content, created_at FROM chat_messages "
                "WHERE room_id = :room_id "
                "ORDER BY created_at DESC LIMIT 20"
            ),
            {"room_id": room_id},
        )
        rows = result.fetchall()

    raw_simple: list[dict] = []
    raw = []
    for row in reversed(rows):
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


async def _query_similar_messages(room_id: str, embedding: list[float]) -> list[dict]:
    """pgvector 코사인 유사도 기준 상위 5개 과거 메시지 조회"""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT role, content "
                "FROM chat_messages "
                "WHERE room_id = :room_id AND embedding IS NOT NULL "
                "ORDER BY embedding <=> CAST(:emb AS vector) "
                "LIMIT 5"
            ),
            {"room_id": room_id, "emb": str(embedding)},
        )
        rows = result.fetchall()
        return [{"role": r.role, "content": r.content} for r in rows]


async def _query_chat_room_memory(room_id: str) -> dict:
    """chat_rooms 테이블에서 ai_summary, preferences 조회"""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text("SELECT ai_summary, preferences FROM chat_rooms WHERE id = :room_id"),
            {"room_id": room_id},
        )
        row = result.fetchone()
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
    return str(value)[:10]


async def _query_reservations(room_id: str) -> list[dict]:
    """채팅방의 활성 예약 목록을 조회한다 (취소되지 않은 것만)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT r.id, r.type, r.status, r.external_ref_id, r.detail, "
                "r.total_price, r.currency, r.reserved_at "
                "FROM reservations r "
                "JOIN itineraries i ON r.itinerary_id = i.id "
                "WHERE i.room_id = :room_id AND r.status != 'cancelled' "
                "ORDER BY r.created_at DESC"
            ),
            {"room_id": room_id},
        )
        rows = result.fetchall()
        result_list = []
        for row in rows:
            detail = row.detail
            if isinstance(detail, str):
                detail = json.loads(detail)
            result_list.append({
                "id": str(row.id),
                "type": row.type,
                "status": row.status,
                "external_ref_id": row.external_ref_id,
                "detail": detail,
                "total_price": float(row.total_price) if row.total_price else None,
                "currency": row.currency,
                "reserved_at": row.reserved_at.isoformat() if row.reserved_at else None,
            })
        return result_list


async def _query_current_itinerary(room_id: str) -> dict | None:
    """roomId로 현재 여행 일정 전체 조회"""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT destinations, start_date, end_date, total_days, "
                "budget, adult_count, child_count, child_ages, day_plans, origin "
                "FROM itineraries "
                "WHERE room_id = :room_id "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"room_id": room_id},
        )
        row = result.fetchone()

        if row is None:
            return None

        destinations = row.destinations
        if isinstance(destinations, str):
            destinations = json.loads(destinations)
        destinations = destinations or []

        if not destinations or not row.start_date:
            return None

        day_plans = row.day_plans
        if isinstance(day_plans, str):
            day_plans = json.loads(day_plans)

        child_ages = row.child_ages
        if isinstance(child_ages, str):
            child_ages = json.loads(child_ages)

        origin = row.origin
        if isinstance(origin, str):
            origin = json.loads(origin)
        origin_city = origin.get("city") if origin else None

        return {
            "destinations": destinations,
            "start_date": _to_date_str(row.start_date),
            "end_date": _to_date_str(row.end_date),
            "total_days": row.total_days,
            "budget": float(row.budget) if row.budget is not None else None,
            "adult_count": row.adult_count,
            "child_count": row.child_count,
            "child_ages": child_ages or [],
            "day_plans": day_plans,
            "origin": origin_city,
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
    # 임베딩 + 장기 메모리 병렬 조회
    try:
        user_embedding, db_memory = await asyncio.gather(
            get_user_embedding(user_message),
            _query_chat_room_memory(room_id),
        )
    except Exception:
        user_embedding = await get_user_embedding(user_message)
        db_memory = {"ai_summary": None, "preferences": None}

    # Java 백엔드가 DB에 썼을 수 있으므로 매 요청마다 Redis 갱신 (fire-and-forget)
    asyncio.ensure_future(save_memory(room_id, db_memory["ai_summary"], db_memory["preferences"]))

    # 나머지 4개 쿼리 병렬 실행 — 개별 실패가 전체를 막지 않도록 return_exceptions 사용
    raw = await asyncio.gather(
        _query_similar_messages(room_id, user_embedding),
        _query_current_itinerary(room_id),
        _query_reservations(room_id),
        _query_recent_history(room_id),
        return_exceptions=True,
    )

    similar_messages  = raw[0] if not isinstance(raw[0], BaseException) else []
    current_itinerary = raw[1] if not isinstance(raw[1], BaseException) else None
    reservations      = raw[2] if not isinstance(raw[2], BaseException) else []
    history_tuple     = raw[3] if not isinstance(raw[3], BaseException) else ([], [])

    if isinstance(raw[1], BaseException):
        _log.error("current_itinerary 로드 실패", exc_info=raw[1])
    if isinstance(raw[2], BaseException):
        _log.error("reservations 로드 실패", exc_info=raw[2])
    if isinstance(raw[3], BaseException):
        _log.error("history 로드 실패", exc_info=raw[3])

    history, raw_history = history_tuple

    asyncio.ensure_future(save_pg_history(room_id, similar_messages))
    asyncio.ensure_future(save_raw_history(room_id, raw_history))

    print(
        f"\n[load_context] DB 조회 결과"
        f"\n  room_id          : {room_id}"
        f"\n  ai_summary       : {db_memory['ai_summary']}"
        f"\n  preferences      : {db_memory['preferences']}"
        f"\n  history          : {len(history)}건"
        f"\n  similar_messages : {len(similar_messages)}건"
        f"\n  reservations     : {len(reservations)}건"
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
        "reservations": reservations,
    }
