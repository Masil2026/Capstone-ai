# app/controller/aiMessageController.py
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.core.auth import verify_internal_token
from app.schemas.ai_message import (
    AiMessageRequest,
    CancelFields,
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
from app.services.agents.itinerary_patch import try_patch_itinerary_item
from app.services.agents.itinerary_pipeline import run_itinerary_pipeline
from app.services.agents.memory import save_memory
from app.services.agents.orchestrator import OrchestratorDeps, orchestrator_agent, build_context_prompt
from app.services.agents._base import run_with_retry, _is_rate_limit_error, _retry_wait

router = APIRouter()

# ---------------------------------------------------------------------------
# cancel 선처리 — 오케스트레이터 호출 전 가로채기
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r'(?:^|\s)\d+\s*번|첫\s*번째|두\s*번째|세\s*번째')
_IATA_RE = re.compile(r'\b[A-Z]{3}\b')
_ORDINAL_INDEX = {
    "첫": 0,
    "두": 1,
    "세": 2,
}
_ITINERARY_CHANGE_KEYWORDS = (
    "숙소",
    "호텔",
    "체크인",
    "항공",
    "항공편",
    "비행편",
    "비행기",
    "출발",
    "도착",
    "공항",
    "역",
    "터미널",
    "이동",
    "교통수단",
    "이동수단",
    "택시",
    "버스",
    "공항버스",
    "리무진",
    "고속버스",
    "시외버스",
    "지하철",
    "기차",
    "열차",
    "KTX",
    "ktx",
    "SRT",
    "srt",
    "대중교통",
    "자차",
    "자가용",
    "렌터카",
    "렌트카",
)
_RESERVATION_ITEM_CHANGE_KEYWORDS = (
    "숙소",
    "호텔",
    "체크인",
    "항공",
    "항공편",
    "비행편",
    "비행기",
    "출발",
    "도착",
)
_CHANGE_INTENT_KEYWORDS = (
    "바꿔",
    "바꿀",
    "변경",
    "수정",
    "교체",
    "다른",
    "새로",
    "말고",
    "대신",
    "타고",
    "이용",
)
_CANCEL_INTENT_KEYWORDS = ("취소", "캔슬")
_RESERVATION_INTENT_KEYWORDS = ("예약", "예매", "새로 잡아", "다시 잡아")
_RESERVATION_CHANGE_CONFIRM_MESSAGE = "기존 예약을 취소하고 새로 예약할까요?"


def _build_cancel_list_message(reservations: list[dict]) -> str:
    """취소 후보 목록 메시지 생성 (구조화된 reservations 데이터 기반)."""
    lines = ["현재 예약 내역입니다:\n"]
    for i, r in enumerate(reservations, 1):
        detail = r.get("detail") or {}
        rtype_str = "항공" if r.get("type") == "flight" else "숙소"
        name = detail.get("name") or detail.get("airline") or "알 수 없음"
        ref = r.get("external_ref_id") or "없음"
        price_str = f"{int(r['total_price']):,} {r['currency']}" if r.get("total_price") else "가격정보없음"
        if r.get("type") == "flight":
            dep = detail.get("departure", "")
            arr = detail.get("arrival", "")
            dep_at = (detail.get("departing_at") or "")[:10]
            lines.append(f"{i}. [{rtype_str}] {name} {dep}→{arr} ({dep_at}) | 예약번호: {ref} | {price_str}")
        else:
            check_in = detail.get("check_in", "")
            check_out = detail.get("check_out", "")
            lines.append(f"{i}. [{rtype_str}] {name} ({check_in}~{check_out}) | 예약번호: {ref} | {price_str}")
    lines.append("\n어떤 항목을 취소해드릴까요? 시스템 특성상 한 번에 하나씩만 처리할 수 있어요 😊")
    return "\n".join(lines)


def _user_targets_cancel_item(user_msg: str, reservations: list[dict]) -> bool:
    """사용자 메시지가 특정 예약을 지목하는지 확인."""
    return _select_cancel_reservation(user_msg, reservations) is not None


def _select_cancel_reservation(user_msg: str, reservations: list[dict]) -> dict | None:
    """사용자 메시지에서 지목된 취소 대상 예약을 찾는다."""
    lower = user_msg.lower()

    # "N번" 번호 선택
    number_match = re.search(r'(?:^|\s)(\d+)\s*번', lower)
    if number_match:
        index = int(number_match.group(1)) - 1
        if 0 <= index < len(reservations):
            return reservations[index]

    # "첫 번째", "두 번째", "세 번째" 순서 선택
    ordinal_match = re.search(r'(첫|두|세)\s*번째', lower)
    if ordinal_match:
        index = _ORDINAL_INDEX[ordinal_match.group(1)]
        if 0 <= index < len(reservations):
            return reservations[index]

    # IATA 코드 (예: "ICN→NRT 취소해줘")
    iata_codes = set(_IATA_RE.findall(user_msg))
    if iata_codes:
        for r in reservations:
            detail = r.get("detail") or {}
            reservation_codes = {
                str(detail.get("departure") or "").upper(),
                str(detail.get("arrival") or "").upper(),
            }
            if iata_codes & reservation_codes:
                return r

    # 예약명·항공사명·예약번호 매칭
    for r in reservations:
        detail = r.get("detail") or {}
        identifiers = [
            detail.get("name") or "",
            detail.get("airline") or "",
            r.get("external_ref_id") or "",
        ]
        for ident in identifiers:
            if ident and len(ident) >= 3 and ident.lower() in lower:
                return r
    return None


def _build_cancel_done_message(reservation: dict) -> str:
    """선택된 예약에 대한 취소 요청 메시지를 생성한다."""
    detail = reservation.get("detail") or {}
    ref = reservation.get("external_ref_id") or reservation.get("id") or "알 수 없음"
    if reservation.get("type") == "flight":
        name = detail.get("airline") or "항공편"
        dep = detail.get("departure", "")
        arr = detail.get("arrival", "")
        route = f" {dep}→{arr}" if dep or arr else ""
        return f"{name}{route} 예약 취소 요청을 접수했습니다. 예약번호: {ref}"

    name = detail.get("name") or "숙소"
    return f"{name} 예약 취소 요청을 접수했습니다. 예약번호: {ref}"


def _has_any_keyword(user_message: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in user_message for keyword in keywords)


def _is_itinerary_item_change_request(user_message: str) -> bool:
    return (
        _has_any_keyword(user_message, _ITINERARY_CHANGE_KEYWORDS)
        and _has_any_keyword(user_message, _CHANGE_INTENT_KEYWORDS)
    )


def _is_reservation_item_change_request(user_message: str) -> bool:
    return (
        _has_any_keyword(user_message, _RESERVATION_ITEM_CHANGE_KEYWORDS)
        and _has_any_keyword(user_message, _CHANGE_INTENT_KEYWORDS)
    )


def _has_explicit_cancel_or_reservation_intent(user_message: str) -> bool:
    return (
        _has_any_keyword(user_message, _CANCEL_INTENT_KEYWORDS)
        or _has_any_keyword(user_message, _RESERVATION_INTENT_KEYWORDS)
    )


def _should_confirm_reservation_change(user_message: str, reservations: list[dict]) -> bool:
    """활성 예약이 있는 항공/숙소 변경 요청은 취소 여부를 먼저 확인한다."""
    return (
        bool(reservations)
        and _is_reservation_item_change_request(user_message)
        and not _has_explicit_cancel_or_reservation_intent(user_message)
    )


def _correct_request_type(request_type: str, user_message: str, current_itinerary: dict | None) -> str:
    """classification 결과를 런타임 컨텍스트로 안전하게 보정한다."""
    if (
        current_itinerary
        and _is_itinerary_item_change_request(user_message)
        and not _has_explicit_cancel_or_reservation_intent(user_message)
    ):
        return "itinerary"
    if request_type == "cancel" and not _has_any_keyword(user_message, _CANCEL_INTENT_KEYWORDS):
        return "itinerary" if _is_itinerary_item_change_request(user_message) else "chat"
    if (
        request_type != "reservation"
        and not _has_any_keyword(user_message, _CANCEL_INTENT_KEYWORDS)
        and _has_any_keyword(user_message, _RESERVATION_INTENT_KEYWORDS)
    ):
        return "reservation"
    return request_type


def _get_cancel_intercept_message(user_message: str, reservations: list[dict]) -> str | None:
    """
    cancel 요청을 선처리.
    - 예약 없음 → 안내 메시지 반환
    - 막연한 취소 요청 → 목록 반환
    - 특정 항목 지목 → None (직접 cancel payload 생성)
    """
    if not reservations:
        return "취소할 수 있는 예약 내역이 없어요."
    if _user_targets_cancel_item(user_message, reservations):
        return None
    return _build_cancel_list_message(reservations)


def _sse(event: str, payload: dict) -> str:
    """SSE 이벤트를 pretty-printed JSON으로 직렬화. 각 JSON 줄에 'data: ' 접두사."""
    lines = json.dumps(payload, ensure_ascii=False, indent=2).split("\n")
    data_block = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event}\n{data_block}\n\n"


def _exclude_none(obj, keep_null_keys: frozenset[str] = frozenset()) -> object:
    """None 값을 재귀적으로 제거하되, keep_null_keys에 포함된 키는 None이어도 유지한다."""
    if isinstance(obj, dict):
        return {
            k: _exclude_none(v, keep_null_keys)
            for k, v in obj.items()
            if v is not None or k in keep_null_keys
        }
    if isinstance(obj, list):
        return [_exclude_none(item, keep_null_keys) for item in obj]
    return obj


def _normalize_ai_summary(ai_summary: str | list[str] | None) -> str | None:
    """ai_summary를 저장/응답에 쓰는 문자열 형태로 정규화한다."""
    if ai_summary is None:
        return None
    if isinstance(ai_summary, list):
        return "\n".join(str(item) for item in ai_summary if item is not None)
    return ai_summary


def _has_payload(payload: object) -> bool:
    """액션 payload에 실제로 내려보낼 데이터가 있는지 확인한다."""
    if payload is None:
        return False
    if isinstance(payload, dict):
        return bool(_exclude_none(payload))
    if isinstance(payload, (ChangePayload, CancelPayload, ItineraryPayload)):
        return bool(_exclude_none(payload.model_dump()))
    return True


def _resolve_done_type(
    request_type: str,
    *,
    itinerary: ItineraryPayload | None,
    change: ChangePayload | None,
    reservation: dict | None,
    cancel: CancelPayload | None,
) -> str:
    """액션 타입인데 해당 payload가 비어 있으면 chat 타입으로 보정한다."""
    payload_by_type = {
        "itinerary": itinerary,
        "change": change,
        "reservation": reservation,
        "cancel": cancel,
    }
    payload = payload_by_type.get(request_type)
    if request_type in payload_by_type and not _has_payload(payload):
        return "chat"
    return request_type


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
        cls_result = await run_with_retry(classification_agent, user_message, role="classification")
        request_type = cls_result.output.type
    except Exception:
        request_type = "chat"
    request_type = _correct_request_type(request_type, user_message, ctx["current_itinerary"])

    if _should_confirm_reservation_change(user_message, ctx.get("reservations", [])):
        yield _sse("chunk", {"content": _RESERVATION_CHANGE_CONFIRM_MESSAGE})
        try:
            assistant_embedding = await get_user_embedding(_RESERVATION_CHANGE_CONFIRM_MESSAGE)
        except Exception:
            assistant_embedding = None
        done = _build_done_event(
            request_type="chat",
            user_message=user_message,
            user_embedding=ctx["user_embedding"],
            full_response=_RESERVATION_CHANGE_CONFIRM_MESSAGE,
            assistant_embedding=assistant_embedding,
            orch_result=OrchestratorResult(message=_RESERVATION_CHANGE_CONFIRM_MESSAGE),
            merged_summary=ctx["ai_summary"],
            merged_prefs=ctx["preferences"],
        )
        if hide_embedding:
            done.userMessage.embedding = None
            done.assistantMessage.embedding = None
        yield _sse("done", _exclude_none(done.model_dump(), keep_null_keys=frozenset({"amount_krw"})))
        return

    # [3] OrchestratorDeps 조립
    deps = OrchestratorDeps(
        ai_summary=ctx["ai_summary"],
        preferences=ctx["preferences"],
        today=date.today().isoformat(),
        similar_messages=ctx["similar_messages"],
        current_itinerary=ctx["current_itinerary"],
        request_type=request_type,
        reservations=ctx.get("reservations", []),
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

    # [3.5] cancel 막연한 요청 선처리 — 오케스트레이터 호출 전 가로채기
    if request_type == "cancel":
        reservations = ctx.get("reservations", [])
        selected_cancel = _select_cancel_reservation(user_message, reservations)
        if selected_cancel is not None:
            response_msg = _build_cancel_done_message(selected_cancel)
            print(f"[_stream] cancel 직접 payload 생성: {selected_cancel.get('id')!r}", flush=True)
            yield _sse("chunk", {"content": response_msg})
            try:
                assistant_embedding = await get_user_embedding(response_msg)
            except Exception:
                assistant_embedding = None
            done = _build_done_event(
                request_type="cancel",
                user_message=user_message,
                user_embedding=ctx["user_embedding"],
                full_response=response_msg,
                assistant_embedding=assistant_embedding,
                orch_result=OrchestratorResult(
                    message=response_msg,
                    cancel=CancelFields(
                        reservation_id=selected_cancel.get("id") or selected_cancel.get("external_ref_id") or "",
                        cancelled_at=datetime.now().astimezone().isoformat(),
                    ),
                ),
                merged_summary=ctx["ai_summary"],
                merged_prefs=ctx["preferences"],
            )
            if hide_embedding:
                done.userMessage.embedding = None
                done.assistantMessage.embedding = None
            yield _sse("done", _exclude_none(done.model_dump(), keep_null_keys=frozenset({"amount_krw"})))
            return

        intercept_msg = _get_cancel_intercept_message(user_message, reservations)
        if intercept_msg is not None:
            print(f"[_stream] cancel 선처리 인터셉트: {intercept_msg[:60]!r}", flush=True)
            yield _sse("chunk", {"content": intercept_msg})
            try:
                assistant_embedding = await get_user_embedding(intercept_msg)
            except Exception:
                assistant_embedding = None
            done = _build_done_event(
                request_type="chat",
                user_message=user_message,
                user_embedding=ctx["user_embedding"],
                full_response=intercept_msg,
                assistant_embedding=assistant_embedding,
                orch_result=OrchestratorResult(message=intercept_msg),
                merged_summary=ctx["ai_summary"],
                merged_prefs=ctx["preferences"],
            )
            if hide_embedding:
                done.userMessage.embedding = None
                done.assistantMessage.embedding = None
            yield _sse("done", _exclude_none(done.model_dump(), keep_null_keys=frozenset({"amount_krw"})))
            return

    # [4] 에이전트 실행 — itinerary는 파이프라인, 그 외는 오케스트레이터 실시간 스트리밍
    print(f"\n[_stream] [4] 에이전트 실행 시작. request_type={request_type}", flush=True)
    try:
        context_block = build_context_prompt(deps)
        prompt = f"{context_block}\n\n---\n\n사용자 메시지: {user_message}"

        if request_type == "itinerary":
            print("[_stream] 부분 itinerary 패치 시도", flush=True)
            orch_result = await try_patch_itinerary_item(deps, user_message)
            if orch_result is not None:
                yield _sse("chunk", {"content": orch_result.message})
            else:
                print("[_stream] run_itinerary_pipeline 호출", flush=True)
                async for item in run_itinerary_pipeline(deps, user_message, ctx["history"]):
                    if isinstance(item, str):
                        yield _sse("chunk", {"content": item})
                    else:
                        orch_result = item

            if orch_result is None:
                print("[_stream] pipeline None → orchestrator 스트리밍 폴백", flush=True)
                for attempt in range(4):
                    yielded_any = False
                    try:
                        prev_msg = ""
                        async with orchestrator_agent.run_stream(
                            prompt, deps=deps, message_history=ctx["history"]
                        ) as stream_result:
                            async for partial in stream_result.stream_output():
                                msg = getattr(partial, "message", None) or ""
                                if len(msg) > len(prev_msg):
                                    yield _sse("chunk", {"content": msg[len(prev_msg):]})
                                    prev_msg = msg
                                    yielded_any = True
                            orch_result = await stream_result.get_output()
                        break
                    except Exception as e:
                        if _is_rate_limit_error(e) and attempt < 3 and not yielded_any:
                            wait = _retry_wait(attempt)
                            print(f"[orchestrator] 429 재시도 {attempt + 1}/3, {wait:.1f}s 대기", flush=True)
                            await asyncio.sleep(wait)
                        else:
                            raise
        else:
            print("[_stream] orchestrator_agent.run_stream() 호출", flush=True)
            for attempt in range(4):
                yielded_any = False
                try:
                    prev_msg = ""
                    async with orchestrator_agent.run_stream(
                        prompt, deps=deps, message_history=ctx["history"]
                    ) as stream_result:
                        async for partial in stream_result.stream_output():
                            msg = getattr(partial, "message", None) or ""
                            if len(msg) > len(prev_msg):
                                yield _sse("chunk", {"content": msg[len(prev_msg):]})
                                prev_msg = msg
                                yielded_any = True
                        orch_result = await stream_result.get_output()
                    break
                except Exception as e:
                    if _is_rate_limit_error(e) and attempt < 3 and not yielded_any:
                        wait = _retry_wait(attempt)
                        print(f"[orchestrator] 429 재시도 {attempt + 1}/3, {wait:.1f}s 대기", flush=True)
                        await asyncio.sleep(wait)
                    else:
                        raise
            print(f"[_stream] 스트리밍 완료. message={orch_result.message[:80]!r}", flush=True)

        full_response: str = orch_result.message

    except Exception as e:
        import traceback
        print(f"[_stream] 에이전트 오류: {e}\n{traceback.format_exc()}", flush=True)
        yield _sse("error", {"message": f"에이전트 오류: {e}"})
        return

    # [5] chunk 이벤트는 [4]의 스트리밍 중 실시간 전송됨

    # [6] day_plans cost.amount_krw 자동 변환
    # KRW: 항상 null / 비KRW: LLM 값 무시하고 항상 서버에서 재계산
    if orch_result.day_plans:
        for items in orch_result.day_plans.values():
            for item in items:
                if item.cost:
                    if item.cost.currency == "KRW":
                        item.cost.amount_krw = None
                    else:
                        try:
                            item.cost.amount_krw = await to_krw(item.cost.amount, item.cost.currency)
                        except Exception:
                            item.cost.amount_krw = None

    # [7] AI 응답 임베딩 생성
    try:
        assistant_embedding = await get_user_embedding(full_response)
    except Exception:
        assistant_embedding = None

    # [8] memory 갱신 — Redis에만 저장, DB는 Java 백엔드가 done 이벤트의 memory 필드를 보고 씀
    merged_summary = _normalize_ai_summary(
        orch_result.ai_summary if orch_result.ai_summary is not None else ctx["ai_summary"]
    )
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
    yield _sse("done", _exclude_none(done.model_dump(), keep_null_keys=frozenset({"amount_krw"})))


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
            aiSummary=_normalize_ai_summary(merged_summary),
            preferences=merged_prefs or {},
        )

    itinerary = None
    if orch_result.day_plans:
        itinerary = ItineraryPayload(dayPlans=orch_result.day_plans)

    change = None
    if orch_result.change:
        c = orch_result.change
        destinations_data = [d.model_dump() for d in c.destinations] if c.destinations else None
        change = ChangePayload(
            destinations=destinations_data,
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

    done_type = _resolve_done_type(
        request_type,
        itinerary=itinerary,
        change=change,
        reservation=reservation,
        cancel=cancel,
    )

    return DoneEvent(
        type=done_type,
        userMessage=MessageWithEmbedding(content=user_message, embedding=user_embedding),
        assistantMessage=MessageWithEmbedding(content=full_response, embedding=assistant_embedding),
        memory=memory_output,
        itinerary=itinerary,
        change=change,
        reservation=reservation,
        cancel=cancel,
    )
