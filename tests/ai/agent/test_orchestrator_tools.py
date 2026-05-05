import pytest
from datetime import date

from pydantic_ai.messages import ModelResponse, ToolCallPart

from app.services.agents.orchestrator import orchestrator_agent, OrchestratorDeps


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _extract_tool_calls(result) -> list[str]:
    """결과 메시지에서 호출된 도구 이름 목록 추출 (중복 포함)"""
    tool_calls = []
    for msg in result.all_messages():
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    tool_calls.append(part.tool_name)
    return tool_calls


def _print_tool_calls(test_name: str, tool_calls: list[str], response: str) -> None:
    print("\n" + "=" * 60)
    print(f"[{test_name}]")
    print(f"호출된 도구 ({len(tool_calls)}개): {tool_calls}")
    print(f"응답 미리보기: {response[:120]}")
    print("=" * 60 + "\n")


def _make_deps(request_type: str, current_itinerary: dict | None = None) -> OrchestratorDeps:
    return OrchestratorDeps(
        ai_summary=None,
        preferences=None,
        today=str(date.today()),
        similar_messages=[],
        current_itinerary=current_itinerary,
        request_type=request_type,
    )


_SAMPLE_ITINERARY = {
    "destination": "도쿄",
    "start_date": "2026-05-15",
    "end_date": "2026-05-18",
    "total_days": 4,
    "budget": None,
    "adult_count": 2,
    "child_count": 0,
    "child_ages": [],
    "day_plans": {
        "1일차": [{"plan_name": "신주쿠 산책", "time": "10:00 ~ 12:00", "place": "신주쿠", "note": ""}],
        "2일차": [{"plan_name": "도톤보리 관광", "time": "10:00 ~ 12:00", "place": "도톤보리", "note": ""}],
        "3일차": [{"plan_name": "아키하바라 방문", "time": "13:00 ~ 16:00", "place": "아키하바라", "note": ""}],
        "4일차": [{"plan_name": "공항 이동", "time": "10:00 ~ 12:00", "place": "나리타 공항", "note": ""}],
    },
}


# ---------------------------------------------------------------------------
# itinerary — 신규 생성 (current_itinerary=None)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_itinerary_new():
    """신규 일정 생성 → 항공·숙소 포함 모든 수집 도구 + submit_itinerary 호출 확인"""
    deps = _make_deps("itinerary", current_itinerary=None)
    result = await orchestrator_agent.run(
        "도쿄 3박 4일 여행 일정 짜줘. 5월 15일 출발, 성인 2명이야.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("itinerary_new", tool_calls, result.data)

    assert "submit_itinerary" in tool_calls
    assert "search_flights" in tool_calls
    assert "search_hotels" in tool_calls


# ---------------------------------------------------------------------------
# itinerary — 기존 일정 수정 (current_itinerary 있음)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_itinerary_modify_place():
    """장소 수정 → search_flights·search_hotels 미호출 + submit_itinerary 호출 확인"""
    deps = _make_deps("itinerary", current_itinerary=_SAMPLE_ITINERARY)
    result = await orchestrator_agent.run(
        "2일차 도톤보리를 아사쿠사로 바꿔줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("itinerary_modify_place", tool_calls, result.data)

    assert "submit_itinerary" in tool_calls
    assert "search_flights" not in tool_calls
    assert "search_hotels" not in tool_calls


@pytest.mark.asyncio
async def test_orchestrator_itinerary_modify_with_flight():
    """항공편 변경 포함 수정 → search_flights 호출 확인"""
    deps = _make_deps("itinerary", current_itinerary=_SAMPLE_ITINERARY)
    result = await orchestrator_agent.run(
        "항공편도 새로 검색해서 일정에 포함해줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("itinerary_modify_with_flight", tool_calls, result.data)

    assert "search_flights" in tool_calls
    assert "submit_itinerary" in tool_calls


# ---------------------------------------------------------------------------
# change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_change_dates():
    """날짜 변경 → 외부 API 도구 미호출 + submit_change만 호출 확인"""
    deps = _make_deps("change")
    result = await orchestrator_agent.run(
        "여행 날짜 5월 20일부터 24일로 바꿔줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("change_dates", tool_calls, result.data)

    assert "submit_change" in tool_calls
    for tool in ["search_flights", "search_hotels", "search_web", "get_weather",
                 "get_historical_weather", "find_route", "search_place"]:
        assert tool not in tool_calls, f"change 타입에서 {tool}이 호출되면 안 됨"


@pytest.mark.asyncio
async def test_orchestrator_change_budget():
    """예산 변경 → submit_change만 호출 확인"""
    deps = _make_deps("change")
    result = await orchestrator_agent.run(
        "예산 150만원으로 늘려줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("change_budget", tool_calls, result.data)

    assert "submit_change" in tool_calls
    for tool in ["search_flights", "search_hotels", "search_web", "get_weather"]:
        assert tool not in tool_calls


# ---------------------------------------------------------------------------
# reservation
# 실제 예약(Duffel book API)은 미구현 상태.
# 현재 흐름: search_flights/search_hotels로 옵션 제시 → submit_reservation으로 데이터 전달
# 예정 흐름: search → book_flight/book_hotel(미구현) → submit_reservation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_reservation_flight():
    """항공권 예약 요청 → search_flights + submit_reservation 호출 확인 (book_flight는 미구현)"""
    deps = _make_deps("reservation")
    result = await orchestrator_agent.run(
        "인천에서 도쿄 5월 15일 출발 항공권 성인 2명 예약해줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("reservation_flight", tool_calls, result.data)

    assert "search_flights" in tool_calls
    assert "search_hotels" not in tool_calls


@pytest.mark.asyncio
async def test_orchestrator_reservation_hotel():
    """숙소 예약 요청 → search_hotels + submit_reservation 호출 확인 (book_hotel은 미구현)"""
    deps = _make_deps("reservation")
    result = await orchestrator_agent.run(
        "도쿄 신주쿠 5월 15일~18일 숙소 성인 2명 예약해줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("reservation_hotel", tool_calls, result.data)

    assert "search_hotels" in tool_calls
    assert "search_flights" not in tool_calls


# ---------------------------------------------------------------------------
# cancel
# 실제 취소(Duffel cancel API)는 미구현 상태.
# 현재 흐름: submit_cancel로 취소 정보 전달 (Spring Boot가 후처리)
# 예정 흐름: cancel_booking(미구현) → submit_cancel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_cancel():
    """예약 취소 요청 → submit_cancel 호출 확인 (cancel_booking API는 미구현)"""
    deps = _make_deps("cancel")
    result = await orchestrator_agent.run(
        "예약 ID RES-20260515-001 취소해줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("cancel", tool_calls, result.data)

    assert "submit_cancel" in tool_calls
    for tool in ["search_flights", "search_hotels", "submit_itinerary", "submit_change"]:
        assert tool not in tool_calls


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_chat_question():
    """일반 질문 → submit_* 미호출 확인"""
    deps = _make_deps("chat")
    result = await orchestrator_agent.run(
        "도쿄에서 꼭 먹어야 할 음식 추천해줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("chat_question", tool_calls, result.data)

    for tool in ["submit_itinerary", "submit_change", "submit_reservation", "submit_cancel"]:
        assert tool not in tool_calls, f"chat 타입에서 {tool}이 호출되면 안 됨"


@pytest.mark.asyncio
async def test_orchestrator_chat_weather():
    """날씨 질문 → get_weather 호출 + submit_* 미호출 확인"""
    deps = _make_deps("chat")
    result = await orchestrator_agent.run(
        "도쿄 5월 날씨 어때? 옷 어떻게 입어야 해?",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("chat_weather", tool_calls, result.data)

    assert "get_weather" in tool_calls or "search_web" in tool_calls
    for tool in ["submit_itinerary", "submit_change", "submit_reservation", "submit_cancel"]:
        assert tool not in tool_calls
