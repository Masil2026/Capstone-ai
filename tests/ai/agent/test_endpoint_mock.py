"""
POST /api/v1/ai-messages 엔드포인트 Mock 테스트

외부 API·LLM·Redis·DB 전부 Mock. 엔드포인트 로직만 검증.
검증 항목:
  - 신규 일정 생성: done.itinerary.dayPlans 전체 날짜 포함
  - 일정 수정: 수정 날짜만 dayPlans 반환
  - Redis save_memory 호출 인자 검증
  - chat 타입: ai_summary 변화 없으면 memory=null, save_memory 미호출

실행:
  pytest tests/ai/agent/test_endpoint_mock.py -s
"""
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.schemas.ai_message import DayPlanItem, OrchestratorResult

# ── 공통 상수 ─────────────────────────────────────────────────────────────────

_HEADERS = {
    "X-Internal-Token": settings.INTERNAL_TOKEN,
    "Content-Type": "application/json",
}
_ROOM_ID = "test-room-mock-001"
_FIXED_EMBEDDING = [0.1] * 1536

_EXISTING_AI_SUMMARY = (
    "1. 도쿄 3박 4일 일정 생성 (5월 15일~18일, 성인 2명)\n"
    "2. 참치회 & 라멘 식당 요청 반영"
)
_EXISTING_PREFERENCES = {"food": ["참치회", "라멘"]}
_EXISTING_ITINERARY = {
    "destination": "도쿄",
    "start_date": "2026-05-15",
    "end_date": "2026-05-18",
    "total_days": 4,
    "budget": 1500000.0,
    "adult_count": 2,
    "child_count": 0,
    "child_ages": [],
    "day_plans": {
        "2026-05-15": [
            {"plan_name": "아사쿠사 관광", "time": "14:00 ~ 17:00", "place": "아사쿠사", "note": "", "cost": None},
            {"plan_name": "저녁 - 참치회", "time": "18:30 ~ 20:00", "place": "스시 오마카세", "note": "", "cost": None},
        ],
        "2026-05-16": [
            {"plan_name": "신주쿠 쇼핑", "time": "10:00 ~ 13:00", "place": "신주쿠", "note": "", "cost": None},
            {"plan_name": "점심 - 라멘", "time": "13:00 ~ 14:00", "place": "신주쿠 라멘", "note": "", "cost": None},
        ],
        "2026-05-17": [
            {"plan_name": "하라주쿠 방문", "time": "10:00 ~ 12:00", "place": "하라주쿠", "note": "", "cost": None},
        ],
        "2026-05-18": [
            {"plan_name": "공항 이동", "time": "09:00 ~ 11:00", "place": "나리타 공항", "note": "", "cost": None},
        ],
    },
}


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _make_ctx(*, day_plans=None, use_existing_memory=True):
    """load_context 반환값 픽스처"""
    itinerary = {**_EXISTING_ITINERARY, "day_plans": day_plans} if day_plans is not None else _EXISTING_ITINERARY
    return {
        "user_embedding": _FIXED_EMBEDDING,
        "history": [],
        "ai_summary": _EXISTING_AI_SUMMARY if use_existing_memory else None,
        "preferences": _EXISTING_PREFERENCES if use_existing_memory else None,
        "similar_messages": [],
        "current_itinerary": itinerary,
    }


def _mock_cls(request_type: str):
    r = MagicMock()
    r.output.type = request_type
    return r


async def _call_endpoint(content: str) -> list[dict]:
    """SSE 스트림 전체 수신 → 파싱된 이벤트 목록 반환"""
    from main import app
    events: list[dict] = []
    current_event: str | None = None
    data_lines: list[str] = []

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST", "/api/v1/ai-messages",
            headers=_HEADERS,
            json={"roomId": _ROOM_ID, "content": content},
            timeout=30.0,
        ) as resp:
            assert resp.status_code == 200, f"HTTP {resp.status_code}"
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    current_event = line[7:].strip()
                    data_lines = []
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
                elif line == "" and current_event and data_lines:
                    try:
                        events.append({"event": current_event, "data": json.loads("\n".join(data_lines))})
                    except json.JSONDecodeError:
                        pass
                    current_event = None
                    data_lines = []
    return events


def _done(events: list[dict]) -> dict:
    d = next((e["data"] for e in events if e["event"] == "done"), None)
    assert d is not None, "done 이벤트 없음"
    return d


def _assert_no_error(events: list[dict]):
    errs = [e for e in events if e["event"] == "error"]
    if errs:
        pytest.fail(f"에러 이벤트 발생: {errs[0]['data']}")


def _print_done(name: str, done: dict):
    print(f"\n{'='*60}\n[{name}]")
    print(f"  type    : {done.get('type')}")
    print(f"  message : {done.get('assistantMessage', {}).get('content', '')[:120]}")
    print(f"  memory  : {done.get('memory')}")
    if dp := (done.get("itinerary") or {}).get("dayPlans"):
        print(f"  dayPlans 날짜: {list(dp.keys())}")
    print(f"{'='*60}\n")


# ── 테스트: 신규 일정 생성 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_itinerary_new_all_dates_returned():
    """신규 일정: dayPlans 전체 날짜 포함 + ai_summary/preferences Redis 업데이트"""
    new_summary = (
        "1. 도쿄 3박 4일 일정 생성 (5월 15일~18일, 성인 2명)\n"
        "2. 참치회 & 규카츠 식당 요청 반영"
    )
    new_prefs = {"food": ["참치회", "규카츠"]}
    mock_result = OrchestratorResult(
        message="도쿄 3박 4일 일정을 생성했습니다. 1일차 아사쿠사, 2일차 신주쿠 쇼핑 코스입니다.",
        ai_summary=new_summary,
        preferences=new_prefs,
        day_plans={
            "2026-05-15": [DayPlanItem(plan_name="공항 → 호텔 체크인", time="14:00 ~ 15:00", place="신주쿠 호텔")],
            "2026-05-16": [DayPlanItem(plan_name="아사쿠사 관광", time="10:00 ~ 13:00", place="아사쿠사")],
            "2026-05-17": [DayPlanItem(plan_name="신주쿠 쇼핑", time="10:00 ~ 13:00", place="신주쿠")],
            "2026-05-18": [DayPlanItem(plan_name="공항 이동", time="09:00 ~ 11:00", place="나리타 공항")],
        },
    )
    ctx = _make_ctx(day_plans=None, use_existing_memory=False)

    async def _mock_pipeline_new(*_, **__):
        yield mock_result.message
        yield mock_result

    with patch("app.controller.aiMessageController.load_context", new=AsyncMock(return_value=ctx)), \
         patch("app.controller.aiMessageController.classification_agent") as mock_cls_agent, \
         patch("app.controller.aiMessageController.run_itinerary_pipeline", new=_mock_pipeline_new), \
         patch("app.controller.aiMessageController.get_user_embedding", new=AsyncMock(return_value=_FIXED_EMBEDDING)), \
         patch("app.controller.aiMessageController.save_memory") as mock_save, \
         patch("app.services.adapters.currency_converter.to_krw", new=AsyncMock(return_value=10000)):

        mock_cls_agent.run = AsyncMock(return_value=_mock_cls("itinerary"))
        events = await _call_endpoint("도쿄 일정 새로 짜줘. 참치회랑 규카츠 먹고 싶어.")

    _assert_no_error(events)
    done = _done(events)
    _print_done("itinerary_new", done)

    assert done["type"] == "itinerary"

    day_plans = done["itinerary"]["dayPlans"]
    assert set(day_plans.keys()) == {"2026-05-15", "2026-05-16", "2026-05-17", "2026-05-18"}, \
        f"신규 생성 시 전체 날짜 포함 필요: {list(day_plans.keys())}"

    assert done["memory"]["aiSummary"] == new_summary
    assert done["memory"]["preferences"] == new_prefs

    mock_save.assert_awaited_once()
    _, saved_summary, saved_prefs = mock_save.call_args.args
    assert saved_summary == new_summary
    assert saved_prefs == new_prefs


# ── 테스트: 일정 수정 (수정 날짜만 반환) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_itinerary_modify_only_modified_date_returned():
    """일정 수정: 수정 날짜(2일차)만 dayPlans 반환 + memory 누적 업데이트"""
    updated_summary = (
        "1. 도쿄 3박 4일 일정 생성 (5월 15일~18일, 성인 2명)\n"
        "2. 참치회 & 라멘 식당 요청 반영\n"
        "3. 2일차 점심을 규카츠 식당으로 변경"
    )
    updated_prefs = {"food": ["참치회", "라멘", "규카츠"]}
    mock_result = OrchestratorResult(
        message="2일차 점심을 규카츠 식당으로 변경했습니다.",
        ai_summary=updated_summary,
        preferences=updated_prefs,
        day_plans={
            "2026-05-16": [
                DayPlanItem(plan_name="신주쿠 쇼핑", time="10:00 ~ 13:00", place="신주쿠"),
                DayPlanItem(plan_name="점심 - 규카츠", time="13:00 ~ 14:00", place="신주쿠 규카츠 맛집"),
            ],
        },
    )
    ctx = _make_ctx()  # 기존 전체 일정 포함

    async def _mock_pipeline_modify(*_, **__):
        yield mock_result.message
        yield mock_result

    with patch("app.controller.aiMessageController.load_context", new=AsyncMock(return_value=ctx)), \
         patch("app.controller.aiMessageController.classification_agent") as mock_cls_agent, \
         patch("app.controller.aiMessageController.run_itinerary_pipeline", new=_mock_pipeline_modify), \
         patch("app.controller.aiMessageController.get_user_embedding", new=AsyncMock(return_value=_FIXED_EMBEDDING)), \
         patch("app.controller.aiMessageController.save_memory") as mock_save, \
         patch("app.services.adapters.currency_converter.to_krw", new=AsyncMock(return_value=10000)):

        mock_cls_agent.run = AsyncMock(return_value=_mock_cls("itinerary"))
        events = await _call_endpoint("2일차 점심을 규카츠로 바꿔줘.")

    _assert_no_error(events)
    done = _done(events)
    _print_done("itinerary_modify", done)

    day_plans = done["itinerary"]["dayPlans"]

    # 수정된 날짜만 반환됐는지
    assert list(day_plans.keys()) == ["2026-05-16"], \
        f"수정 시 요청 날짜만 반환돼야 함. 실제: {list(day_plans.keys())}"
    plan_names = [i["plan_name"] for i in day_plans["2026-05-16"]]
    assert any("규카츠" in n for n in plan_names), f"규카츠 항목 없음: {plan_names}"

    # ai_summary 누적 확인
    assert "3. 2일차 점심을 규카츠 식당으로 변경" in done["memory"]["aiSummary"]
    # preferences 병합 확인
    assert "규카츠" in done["memory"]["preferences"]["food"]
    assert "참치회" in done["memory"]["preferences"]["food"]  # 기존 값 유지

    # Redis 업데이트 인자 검증
    mock_save.assert_awaited_once()
    _, saved_summary, saved_prefs = mock_save.call_args.args
    assert "3. 2일차 점심을 규카츠 식당으로 변경" in saved_summary
    assert "규카츠" in saved_prefs["food"]
    assert "참치회" in saved_prefs["food"]


# ── 테스트: chat 타입 — memory 변화 없음 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_memory_unchanged_save_memory_not_called():
    """chat 타입, ai_summary=null → done.memory=null, save_memory 미호출"""
    mock_result = OrchestratorResult(
        message="도쿄 5월은 평균 기온 20도로 여행하기 좋습니다!",
        ai_summary=None,
        preferences={},
        day_plans=None,
    )
    ctx = _make_ctx()

    async def _stream_output():
        yield mock_result

    @asynccontextmanager
    async def _mock_run_stream(*_, **__):
        m = MagicMock()
        m.stream_output = _stream_output
        m.get_output = AsyncMock(return_value=mock_result)
        yield m

    with patch("app.controller.aiMessageController.load_context", new=AsyncMock(return_value=ctx)), \
         patch("app.controller.aiMessageController.classification_agent") as mock_cls_agent, \
         patch("app.controller.aiMessageController.orchestrator_agent") as mock_orch, \
         patch("app.controller.aiMessageController.get_user_embedding", new=AsyncMock(return_value=_FIXED_EMBEDDING)), \
         patch("app.controller.aiMessageController.save_memory") as mock_save:

        mock_cls_agent.run = AsyncMock(return_value=_mock_cls("chat"))
        mock_orch.run_stream = _mock_run_stream

        events = await _call_endpoint("도쿄 5월 날씨 어때?")

    _assert_no_error(events)
    done = _done(events)
    _print_done("chat_memory_unchanged", done)

    assert done["type"] == "chat"
    assert done.get("memory") is None, \
        f"chat에서 변화 없으면 memory=null이어야 함: {done.get('memory')}"
    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_summary_list_normalized_before_done_and_save_memory():
    """ai_summary가 list로 들어와도 done 이벤트와 save_memory에는 문자열로 정규화된다."""
    summary_list = [
        "1. 제주도 3박 4일 일정 생성",
        "2. 해산물 식당 요청 반영",
    ]
    expected_summary = "\n".join(summary_list)
    mock_result = OrchestratorResult(
        message="제주도 3박 4일 일정을 생성했습니다.",
        ai_summary=summary_list,
        preferences={},
        day_plans={
            "2026-05-15": [
                DayPlanItem(plan_name="공항 도착", time="14:00 ~ 15:00", place="제주공항"),
            ],
        },
    )
    ctx = _make_ctx(day_plans=None, use_existing_memory=False)

    async def _mock_pipeline_list_summary(*_, **__):
        yield mock_result.message
        yield mock_result

    with patch("app.controller.aiMessageController.load_context", new=AsyncMock(return_value=ctx)), \
         patch("app.controller.aiMessageController.classification_agent") as mock_cls_agent, \
         patch("app.controller.aiMessageController.run_itinerary_pipeline", new=_mock_pipeline_list_summary), \
         patch("app.controller.aiMessageController.get_user_embedding", new=AsyncMock(return_value=_FIXED_EMBEDDING)), \
         patch("app.controller.aiMessageController.save_memory") as mock_save:

        mock_cls_agent.run = AsyncMock(return_value=_mock_cls("itinerary"))
        events = await _call_endpoint("제주도 3박 4일 일정 짜줘.")

    _assert_no_error(events)
    done = _done(events)

    assert done["type"] == "itinerary"
    assert done["memory"]["aiSummary"] == expected_summary
    mock_save.assert_awaited_once()
    _, saved_summary, _ = mock_save.call_args.args
    assert saved_summary == expected_summary
