"""
오케스트레이터 도구 선택 테스트

검증 목적: LLM이 request_type과 사용자 메시지를 보고 올바른 도구를 선택하는지 확인
모킹 범위: _service.process_task (외부 API 호출) + preprocessor_agent.run (Tavily 요약)
           → LLM 호출은 실제로 수행 (gpt-4.1)

주의: GPT-4.1 rate limit으로 인해 개별 실행 권장
  pytest tests/ai/agent/test_orchestrator_tools.py::test_이름 -s
"""
import asyncio
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic_ai.messages import ModelResponse, ToolCallPart

import app.services.agents.orchestrator as _orch
from app.services.agents.orchestrator import orchestrator_agent, OrchestratorDeps


# ---------------------------------------------------------------------------
# 모킹용 더미 응답
# ---------------------------------------------------------------------------

_MOCK_FLIGHTS = {
    "status": "success", "count": 2,
    "data": [
        {"offer_id": "off_KE705_mock", "airline": "Korean Air",
         "origin": "ICN", "destination": "NRT",
         "total_amount": "500000 KRW", "stops": 0,
         "departing_at": "2026-05-15T10:00:00", "arriving_at": "2026-05-15T12:30:00"},
    ],
}
_MOCK_HOTELS = {
    "status": "success", "count": 2,
    "data": [
        {"hotel_id": "prop_shinjuku_mock", "name": "Shinjuku Grand Hotel",
         "price": "150000 KRW/night", "rating": 4.5, "address": "Shinjuku, Tokyo"},
    ],
}
_MOCK_WEATHER = {
    "status": "success", "forecast_type": "daily", "count": 4,
    "data": [
        {"date": "2026-05-15", "temperature_max": 22, "temperature_min": 15,
         "precipitation_probability_max": 20, "weather": "맑음", "uv_index_max": 5},
    ],
}
_MOCK_PLACE = {
    "status": "success",
    "data": {"count": 1, "places": [
        {"name": "Senso-ji Temple", "formatted_address": "Asakusa, Tokyo",
         "lat": 35.71, "lng": 139.79, "rating": 4.7, "user_ratings_total": 10000, "types": ["tourist_attraction"]},
    ]},
}
_MOCK_ROUTE = {
    "status": "success",
    "data": {"count": 1, "routes": [
        {"start_address": "Shinjuku, Tokyo", "end_address": "Asakusa, Tokyo",
         "distance": "15 km", "duration": "35분", "steps": []},
    ]},
}
_MOCK_TAVILY = {
    "status": "success", "count": 3,
    "data": [
        {"title": "Tokyo Travel Guide", "content": "Tokyo is a vibrant city...",
         "url": "http://example.com", "score": 0.9},
    ],
}


def _mock_process_task(tool_name: str, action: str, params: dict) -> dict:
    mapping = {
        "duffel_flight":        _MOCK_FLIGHTS,
        "duffel_accommodation": _MOCK_HOTELS,
        "weather":              _MOCK_WEATHER,
        "google_maps":          _MOCK_PLACE if action == "search_place" else _MOCK_ROUTE,
        "tavily_search":        _MOCK_TAVILY,
    }
    return mapping.get(tool_name, {"status": "success", "data": []})


@pytest.fixture(autouse=True)
async def rate_limit_guard():
    """GPT-4.1 rate limit 방지 — 각 테스트 후 20초 대기"""
    yield
    await asyncio.sleep(20)


@pytest.fixture
def mock_tools():
    """외부 API 호출 모킹 — LLM 도구 선택만 테스트"""
    preprocessor_result = MagicMock()
    preprocessor_result.data = "도쿄 여행 핵심 정보 요약 (mock)"

    with patch.object(_orch._service, "process_task", side_effect=AsyncMock(side_effect=_mock_process_task)), \
         patch.object(_orch.preprocessor_agent, "run", new=AsyncMock(return_value=preprocessor_result)):
        yield


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _extract_tool_calls(result) -> list[str]:
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
    print(f"응답 미리보기: {response[:150]}")
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
# itinerary — 신규 생성
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_itinerary_new(mock_tools):
    """신규 일정 생성 → 항공·숙소 포함 수집 도구 + submit_itinerary 호출 확인"""
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
# itinerary — 기존 일정 수정
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_itinerary_modify_place(mock_tools):
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
async def test_orchestrator_itinerary_modify_with_flight(mock_tools):
    """수정 시 항공편 변경 명시 → search_flights 호출 확인 (결과 제시 후 사용자 확인 대기 가능)"""
    deps = _make_deps("itinerary", current_itinerary=_SAMPLE_ITINERARY)
    result = await orchestrator_agent.run(
        "항공편도 새로 검색해서 일정에 포함해줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("itinerary_modify_with_flight", tool_calls, result.data)

    assert "search_flights" in tool_calls


# ---------------------------------------------------------------------------
# change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_change_dates(mock_tools):
    """날짜 변경 → 외부 API 도구 미호출 + submit_change만 호출 확인"""
    deps = _make_deps("change")
    result = await orchestrator_agent.run(
        "여행 날짜 5월 20일부터 24일로 바꿔줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("change_dates", tool_calls, result.data)

    assert "submit_change" in tool_calls
    for tool in ["search_flights", "search_hotels", "search_web",
                 "get_weather", "get_historical_weather", "find_route", "search_place"]:
        assert tool not in tool_calls, f"change 타입에서 {tool}이 호출되면 안 됨"


@pytest.mark.asyncio
async def test_orchestrator_change_budget(mock_tools):
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
# 흐름: search_* → book_*(미구현, todo 반환) → submit_reservation
# search 결과의 offer_id/hotel_id로 즉시 book 호출까지 진행
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_reservation_flight(mock_tools):
    """항공권 예약 → search_flights + book_flight 순서 호출 확인"""
    deps = _make_deps("reservation")
    result = await orchestrator_agent.run(
        "인천에서 도쿄 5월 15일 출발 성인 2명 항공권 예약해줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("reservation_flight", tool_calls, result.data)

    assert "search_flights" in tool_calls
    assert "book_flight" in tool_calls
    assert "search_hotels" not in tool_calls
    assert "book_hotel" not in tool_calls


@pytest.mark.asyncio
async def test_orchestrator_reservation_hotel(mock_tools):
    """숙소 예약 → search_hotels + book_hotel 순서 호출 확인"""
    deps = _make_deps("reservation")
    result = await orchestrator_agent.run(
        "도쿄 신주쿠 5월 15일~18일 성인 2명 숙소 예약해줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("reservation_hotel", tool_calls, result.data)

    assert "search_hotels" in tool_calls
    assert "book_hotel" in tool_calls
    assert "search_flights" not in tool_calls
    assert "book_flight" not in tool_calls


# ---------------------------------------------------------------------------
# cancel
# 흐름: cancel_flight 또는 cancel_hotel(미구현, todo 반환) → submit_cancel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_cancel_flight(mock_tools):
    """항공권 취소 → cancel_flight + submit_cancel 호출 확인"""
    deps = _make_deps("cancel")
    result = await orchestrator_agent.run(
        "order_id가 ord_KE705_001인 항공권 예약 취소해줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("cancel_flight", tool_calls, result.data)

    assert "cancel_flight" in tool_calls
    assert "submit_cancel" in tool_calls
    assert "cancel_hotel" not in tool_calls


@pytest.mark.asyncio
async def test_orchestrator_cancel_hotel(mock_tools):
    """숙소 취소 → cancel_hotel + submit_cancel 호출 확인"""
    deps = _make_deps("cancel")
    result = await orchestrator_agent.run(
        "booking_id가 bk_shinjuku_001인 숙소 예약 취소해줘.",
        deps=deps,
    )
    tool_calls = _extract_tool_calls(result)
    _print_tool_calls("cancel_hotel", tool_calls, result.data)

    assert "cancel_hotel" in tool_calls
    assert "submit_cancel" in tool_calls
    assert "cancel_flight" not in tool_calls


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_chat_question(mock_tools):
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
async def test_orchestrator_chat_weather(mock_tools):
    """날씨 질문 → get_weather 또는 search_web 호출 + submit_* 미호출 확인"""
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
