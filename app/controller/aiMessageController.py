# app/controller/aiMessageController.py
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, Query
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
    OrchestratorResult,
)
from app.services.adapters.currency_converter import to_krw
from app.services.agents.classification import classification_agent
from app.services.agents.context import get_user_embedding, load_context
from app.services.agents.itinerary_pipeline import run_itinerary_pipeline
from app.services.agents.memory import save_memory
from app.services.agents.orchestrator import OrchestratorDeps, orchestrator_agent, build_context_prompt

router = APIRouter()


def _sse(event: str, payload: dict) -> str:
    """SSE 이벤트를 pretty-printed JSON으로 직렬화. 각 JSON 줄에 'data: ' 접두사."""
    lines = json.dumps(payload, ensure_ascii=False, indent=2).split("\n")
    data_block = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event}\n{data_block}\n\n"


_SSE_EXAMPLE = (
    "event: chunk\n"
    'data: {"content": "날씨가 맑고 여행하기 좋은 날씨입니다!"}\n\n'
    "event: done\n"
    "data: {\n"
    'data:   "type": "chat",\n'
    'data:   "userMessage": {\n'
    'data:     "content": "날씨 어때?",\n'
    'data:     "embedding": ["...(1536차원)"]\n'
    "data:   },\n"
    'data:   "assistantMessage": {\n'
    'data:     "content": "날씨가 맑고 여행하기 좋은 날씨입니다!",\n'
    'data:     "embedding": ["...(1536차원)"]\n'
    "data:   },\n"
    'data:   "memory": null\n'
    "data: }\n\n"
)


@router.post(
    "/ai-messages",
    summary="AI Agent 스트리밍 요청",
    responses={
        200: {
            "description": "SSE 스트림. chunk(0~N회) → done(1회) 순서로 전송. 오류 시 error 이벤트.",
            "content": {"text/event-stream": {"example": _SSE_EXAMPLE}},
        },
        403: {"description": "유효하지 않은 X-Internal-Token"},
    },
)
async def ai_messages(
    body: AiMessageRequest,
    _: None = Depends(verify_internal_token),
    hide_embedding: bool = Query(False, description="개발용: true이면 done 이벤트의 embedding 생략"),
):
    return StreamingResponse(
        _stream(body, hide_embedding=hide_embedding),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream(body: AiMessageRequest, hide_embedding: bool = False):
    room_id = body.roomId
    user_message = body.content

    # [1] 컨텍스트 로드
    try:
        ctx = await load_context(room_id, user_message)
    except Exception as e:
        yield _sse("error", {"message": f"컨텍스트 로드 실패: {e}"})
        return

    # [2] 타입 판별
    try:
        cls_result = await classification_agent.run(user_message)
        request_type = cls_result.data.type
    except Exception:
        request_type = "chat"

    # [3] OrchestratorDeps 조립
    deps = OrchestratorDeps(
        ai_summary=ctx["ai_summary"],
        preferences=ctx["preferences"],
        today=date.today().isoformat(),
        similar_messages=ctx["similar_messages"],
        current_itinerary=ctx["current_itinerary"],
        request_type=request_type,
    )
    print(
        f"\n[_stream] OrchestratorDeps 조립 완료"
        f"\n  request_type     : {request_type}"
        f"\n  ai_summary       : {deps.ai_summary}"
        f"\n  preferences      : {deps.preferences}"
        f"\n  similar_messages : {len(deps.similar_messages)}건"
        f"\n  current_itinerary: {({k: v for k, v in deps.current_itinerary.items() if k != 'day_plans'} if deps.current_itinerary else None)}",
        flush=True,
    )
    # [4] 에이전트 실행 — itinerary는 파이프라인, 그 외는 오케스트레이터
    print(f"\n[_stream] [4] 에이전트 실행 시작. request_type={request_type}", flush=True)
    try:
        context_block = build_context_prompt(deps)

        if request_type == "itinerary":
            print("[_stream] run_itinerary_pipeline 호출", flush=True)
            orch_result = await run_itinerary_pipeline(deps, user_message, ctx["history"])
            if orch_result is None:
                print("[_stream] pipeline None → orchestrator 폴백", flush=True)
                run_result = await orchestrator_agent.run(
                    f"{context_block}\n\n---\n\n사용자 메시지: {user_message}",
                    deps=deps,
                    message_history=ctx["history"],
                )
                orch_result = run_result.data
        else:
            print("[_stream] orchestrator_agent.run() 호출", flush=True)
            run_result = await orchestrator_agent.run(
                f"{context_block}\n\n---\n\n사용자 메시지: {user_message}",
                deps=deps,
                message_history=ctx["history"],
            )
            print(f"[_stream] orchestrator_agent.run() 완료. message={run_result.data.message[:80]!r}", flush=True)
            orch_result = run_result.data

        full_response: str = orch_result.message

    except Exception as e:
        import traceback
        print(f"[_stream] 에이전트 오류: {e}\n{traceback.format_exc()}", flush=True)
        yield _sse("error", {"message": f"에이전트 오류: {e}"})
        return

    # [5] 텍스트를 chunk 이벤트로 전송 (구조화 출력이므로 한 번에 전송)
    yield _sse("chunk", {"content": full_response})

    # [6] day_plans cost.amount_krw 자동 변환
    if orch_result.day_plans:
        for items in orch_result.day_plans.values():
            for item in items:
                if item.cost and item.cost.currency != "KRW" and item.cost.amount_krw is None:
                    try:
                        item.cost.amount_krw = await to_krw(item.cost.amount, item.cost.currency)
                    except Exception:
                        pass

    # [7] AI 응답 임베딩 생성
    try:
        assistant_embedding = await get_user_embedding(full_response)
    except Exception:
        assistant_embedding = None

    # [8] memory 갱신 — Redis에만 저장, DB는 Java 백엔드가 done 이벤트의 memory 필드를 보고 씀
    merged_summary = orch_result.ai_summary if orch_result.ai_summary is not None else ctx["ai_summary"]
    # preferences: 빈 dict {}는 "변화 없음"으로 처리 → 기존 값 유지
    merged_prefs = orch_result.preferences if orch_result.preferences else ctx["preferences"]
    if orch_result.ai_summary is not None or orch_result.preferences:
        await save_memory(room_id, merged_summary, merged_prefs)

    # [9] done 이벤트 전송
    done = _build_done_event(
        request_type=request_type,
        user_message=user_message,
        user_embedding=ctx["user_embedding"],
        full_response=full_response,
        assistant_embedding=assistant_embedding,
        orch_result=orch_result,
        merged_summary=merged_summary,
        merged_prefs=merged_prefs,
    )
    if hide_embedding:
        done.userMessage.embedding = None
        done.assistantMessage.embedding = None
    yield _sse("done", done.model_dump(exclude_none=True))


def _build_done_event(
    request_type: str,
    user_message: str,
    user_embedding: list[float],
    full_response: str,
    assistant_embedding: list[float] | None,
    orch_result: OrchestratorResult,
    merged_summary: str | None,
    merged_prefs: dict | None,
) -> DoneEvent:
    memory_output = None
    if orch_result.ai_summary is not None or orch_result.preferences:
        memory_output = MemoryOutput(
            aiSummary=merged_summary,
            preferences=merged_prefs or {},
        )

    itinerary = None
    if orch_result.day_plans:
        itinerary = ItineraryPayload(dayPlans=orch_result.day_plans)

    change = None
    if orch_result.change:
        c = orch_result.change
        change = ChangePayload(
            startDate=c.start_date,
            endDate=c.end_date,
            budget=c.budget,
            adultCount=c.adult_count,
            childCount=c.child_count,
            childAges=c.child_ages,
        )

    cancel = None
    if orch_result.cancel:
        cancel = CancelPayload(
            reservationId=orch_result.cancel.reservation_id,
            cancelledAt=orch_result.cancel.cancelled_at,
        )

    reservation = None
    if orch_result.reservation:
        r = orch_result.reservation
        reservation = {k: v for k, v in {
            "type": r.reservation_type,
            "detail": r.detail,
            "bookingUrl": r.booking_url,
            "externalRefId": r.external_ref_id,
            "totalPrice": r.total_price,
            "currency": r.currency,
            "reservedAt": r.reserved_at,
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
