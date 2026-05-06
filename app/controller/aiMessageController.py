# app/controller/aiMessageController.py
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.core.auth import verify_internal_token
from app.schemas.ai_message import (
    AiMessageRequest,
    CancelPayload,
    ChangePayload,
    DoneEvent,
    ItineraryPayload,
    MessageWithEmbedding,
    MemoryOutput,
)
from app.services.agents.classification import classification_agent
from app.services.agents.context import get_user_embedding, load_context, sync_memory
from app.services.agents.memory import save_history, save_memory
from app.services.agents.orchestrator import OrchestratorDeps, orchestrator_agent

router = APIRouter()


@router.post("/ai-messages")
async def ai_messages(
    body: AiMessageRequest,
    _: None = Depends(verify_internal_token),
):
    return StreamingResponse(
        _stream(body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream(body: AiMessageRequest):
    room_id = body.roomId
    user_message = body.content

    # [1] memory 동기화
    await sync_memory(room_id, body.memory)

    # [2][3] 컨텍스트 로드 (embedding, pgvector, history, itinerary, memory)
    ctx = await load_context(room_id, user_message)

    # [4] 타입 판별
    try:
        cls_result = await classification_agent.run(user_message)
        request_type = cls_result.data.type
    except Exception:
        request_type = "chat"

    # [5] OrchestratorDeps 조립
    deps = OrchestratorDeps(
        ai_summary=ctx["ai_summary"],
        preferences=ctx["preferences"],
        today=date.today().isoformat(),
        similar_messages=ctx["similar_messages"],
        current_itinerary=ctx["current_itinerary"],
        request_type=request_type,
    )

    # [6] 스트리밍 → chunk SSE 전송
    try:
        async with orchestrator_agent.run_stream(
            user_message,
            deps=deps,
            message_history=ctx["history"],
        ) as result:
            async for chunk in result.stream_text(delta=True):
                yield f"event: chunk\ndata: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"

            full_response: str = result.data
            all_messages = result.all_messages()

    except Exception as e:
        yield f"event: chunk\ndata: {json.dumps({'content': f'오류가 발생했습니다: {str(e)}'}, ensure_ascii=False)}\n\n"
        return

    # [7] AI 응답 임베딩 생성
    try:
        assistant_embedding = await get_user_embedding(full_response)
    except Exception:
        assistant_embedding = None

    # [8] memory 캡처 결과 → Redis 업데이트
    if captured_mem := deps.captured.get("memory"):
        await save_memory(
            room_id,
            captured_mem.get("ai_summary"),
            captured_mem.get("preferences"),
        )

    # [9] chat_history 저장
    await save_history(room_id, all_messages)

    # [10] done 이벤트 전송
    done = _build_done_event(
        request_type=request_type,
        user_message=user_message,
        user_embedding=ctx["user_embedding"],
        full_response=full_response,
        assistant_embedding=assistant_embedding,
        captured=deps.captured,
    )
    yield f"event: done\ndata: {done.model_dump_json(exclude_none=True)}\n\n"


def _build_done_event(
    request_type: str,
    user_message: str,
    user_embedding: list[float],
    full_response: str,
    assistant_embedding: list[float] | None,
    captured: dict,
) -> DoneEvent:
    memory_output = None
    if captured_mem := captured.get("memory"):
        memory_output = MemoryOutput(
            aiSummary=captured_mem.get("ai_summary"),
            preferences=captured_mem.get("preferences"),
        )

    itinerary = None
    if payload := captured.get("itinerary"):
        itinerary = ItineraryPayload(dayPlans=payload)

    change = None
    if payload := captured.get("change"):
        change = ChangePayload(
            startDate=payload.get("start_date"),
            endDate=payload.get("end_date"),
            budget=payload.get("budget"),
            adultCount=payload.get("adult_count"),
            childCount=payload.get("child_count"),
            childAges=payload.get("child_ages"),
        )

    cancel = None
    if payload := captured.get("cancel"):
        cancel = CancelPayload(
            reservationId=payload["reservation_id"],
            cancelledAt=payload["cancelled_at"],
        )

    reservation = None
    if payload := captured.get("reservation"):
        reservation = {k: v for k, v in {
            "type": payload.get("reservation_type"),
            "detail": payload.get("detail"),
            "bookingUrl": payload.get("booking_url"),
            "externalRefId": payload.get("external_ref_id"),
            "totalPrice": payload.get("total_price"),
            "currency": payload.get("currency"),
            "reservedAt": payload.get("reserved_at"),
        }.items() if v is not None}

    return DoneEvent(
        type=request_type,
        userMessage=MessageWithEmbedding(content=user_message, embedding=user_embedding),
        assistantMessage=MessageWithEmbedding(content=full_response, embedding=assistant_embedding),
        memory=memory_output,
        itinerary=itinerary,
        change=change,
        reservation=reservation,
        cancel=cancel,
    )
