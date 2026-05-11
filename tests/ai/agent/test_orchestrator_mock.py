"""
오케스트레이터 Mock 테스트 — 기존 memory(ai_summary·preferences·itinerary) 컨텍스트 포함

LLM(GPT-4.1)은 실제 호출, 외부 API만 Mock.
검증 항목:
  - 일정 수정 시 요청된 날짜만 day_plans 반환
  - ai_summary 번호 누적 업데이트
  - 기존 preferences에 새 취향 병합
  - chat 타입: day_plans=null, 기존 일정 내용 참고 응답

실행:
  pytest tests/ai/agent/test_orchestrator_mock.py -s -m llm
  pytest tests/ai/agent/test_orchestrator_mock.py::test_modify_returns_only_requested_date -s
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.llm

import app.services.agents.orchestrator as _orch
from app.services.agents.orchestrator import orchestrator_agent, OrchestratorDeps, build_context_prompt
from app.schemas.ai_message import OrchestratorResult
from pydantic_ai.exceptions import ModelHTTPError


async def _run_agent(deps: OrchestratorDeps, user_message: str):
    """orchestrator_agent.run() 래퍼 — 429 rate limit 발생 시 pytest.skip"""
    context_block = build_context_prompt(deps)
    try:
        return await orchestrator_agent.run(
            f"{context_block}\n\n---\n\n사용자 메시지: {user_message}",
            deps=deps,
        )
    except ModelHTTPError as e:
        if e.status_code == 429:
            pytest.skip(f"GPT-4.1 rate limit (429) — 잠시 후 재실행: {e}")
        raise

# ── 기존 컨텍스트 픽스처 ─────────────────────────────────────────────────────

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


def _make_deps(request_type: str) -> OrchestratorDeps:
    return OrchestratorDeps(
        ai_summary=_EXISTING_AI_SUMMARY,
        preferences=_EXISTING_PREFERENCES,
        today="2026-05-01",
        similar_messages=[],
        current_itinerary=_EXISTING_ITINERARY,
        request_type=request_type,
        reservations=[],
    )


# ── Mock 외부 API ─────────────────────────────────────────────────────────────

_MOCK_WEATHER = {
    "status": "success", "forecast_type": "historical",
    "data": [
        {"date": "2026-05-15", "temperature_max": 22, "temperature_min": 15,
         "precipitation_probability_max": 10, "weather": "맑음"},
        {"date": "2026-05-16", "temperature_max": 20, "temperature_min": 14,
         "precipitation_probability_max": 20, "weather": "구름 조금"},
        {"date": "2026-05-17", "temperature_max": 19, "temperature_min": 13,
         "precipitation_probability_max": 40, "weather": "흐림"},
        {"date": "2026-05-18", "temperature_max": 21, "temperature_min": 14,
         "precipitation_probability_max": 15, "weather": "맑음"},
    ],
}
_MOCK_PLACE = {
    "status": "success",
    "data": {"count": 1, "places": [
        {"name": "규카츠 Kyushu", "formatted_address": "Shinjuku, Tokyo",
         "lat": 35.69, "lng": 139.70, "rating": 4.5, "user_ratings_total": 3000, "types": ["restaurant"]},
    ]},
}
_MOCK_ROUTE = {
    "status": "success",
    "data": {"count": 1, "routes": [
        {"start_address": "Shinjuku, Tokyo", "end_address": "Asakusa, Tokyo",
         "distance": "10 km", "duration": "30분", "steps": []},
    ]},
}
_MOCK_TAVILY = {
    "status": "success",
    "data": [{"title": "Tokyo Guide", "content": "Tokyo is great for food and sightseeing.", "url": "http://example.com", "score": 0.9}],
}


def _mock_process_task(tool_name: str, action: str, params: dict) -> dict:
    if tool_name == "weather":
        return _MOCK_WEATHER
    if tool_name == "google_maps":
        return _MOCK_PLACE if action == "search_place" else _MOCK_ROUTE
    if tool_name == "tavily_search":
        return _MOCK_TAVILY
    return {"status": "success", "data": []}


@pytest.fixture(autouse=True)
async def rate_limit_guard():
    yield
    await asyncio.sleep(35)


@pytest.fixture
def mock_tools():
    """외부 API 호출 Mock — LLM 호출은 실제로 수행"""
    preprocessor_result = MagicMock()
    preprocessor_result.output = "도쿄 관광 핵심 정보 요약 (mock)"
    with patch.object(_orch._service, "process_task", side_effect=AsyncMock(side_effect=_mock_process_task)), \
         patch.object(_orch.preprocessor_agent, "run", new=AsyncMock(return_value=preprocessor_result)):
        yield


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _print_result(name: str, data: OrchestratorResult):
    print(f"\n{'='*60}\n[{name}]")
    print(f"  message     : {data.message[:120]}")
    print(f"  ai_summary  : {data.ai_summary}")
    print(f"  preferences : {data.preferences}")
    if data.day_plans:
        print(f"  day_plans 날짜: {list(data.day_plans.keys())}")
        for date_key, items in data.day_plans.items():
            print(f"    {date_key}: {[i.plan_name for i in items]}")
    print(f"{'='*60}\n")


# ── 테스트: 일정 수정 — 요청 날짜만 반환 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_modify_returns_only_requested_date(mock_tools):
    """기존 4일 일정 중 2일차만 수정 → day_plans에 2026-05-16만 반환"""
    deps = _make_deps("itinerary")
    result = await _run_agent(deps, "2일차 점심을 규카츠 식당으로 바꿔줘.")
    data = result.output
    _print_result("modify_only_2일차", data)

    assert data.day_plans is not None, "day_plans가 null"
    assert "2026-05-16" in data.day_plans, "수정 요청 날짜(2026-05-16)가 day_plans에 없음"

    other_dates = [k for k in data.day_plans if k != "2026-05-16"]
    assert not other_dates, f"수정되지 않은 날짜가 포함됨: {other_dates}"

    plan_names = [i.plan_name for i in data.day_plans["2026-05-16"]]
    assert any("규카츠" in n for n in plan_names), f"규카츠 항목이 없음: {plan_names}"


# ── 테스트: ai_summary 누적 업데이트 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_ai_summary_accumulated_after_modify(mock_tools):
    """기존 ai_summary(1·2번)에 이번 변경이 3번으로 누적됐는지 검증"""
    deps = _make_deps("itinerary")
    result = await _run_agent(deps, "3일차 하라주쿠 대신 우에노 공원으로 바꿔줘.")
    data = result.output
    _print_result("ai_summary_accumulated", data)

    assert data.ai_summary is not None, "ai_summary가 null"
    assert "1." in data.ai_summary, "기존 1번 항목이 누락됨"
    assert "2." in data.ai_summary, "기존 2번 항목이 누락됨"
    assert "3." in data.ai_summary, "이번 변경이 3번으로 추가돼야 함"


# ── 테스트: preferences 병합 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_preferences_merged_with_existing(mock_tools):
    """기존 preferences(참치회·라멘)에 새 취향(규카츠)이 병합됐는지 검증"""
    deps = _make_deps("itinerary")
    result = await _run_agent(deps, "나 규카츠 정말 좋아하거든. 2일차 점심을 규카츠 식당으로 바꿔줘.")
    data = result.output
    _print_result("preferences_merged", data)

    assert data.preferences is not None
    food_list = data.preferences.get("food", [])
    assert "참치회" in food_list, f"기존 '참치회' 취향이 사라짐: {food_list}"
    assert "라멘" in food_list, f"기존 '라멘' 취향이 사라짐: {food_list}"
    assert "규카츠" in food_list, f"새 취향 '규카츠'가 추가되지 않음: {food_list}"


# ── 테스트: chat 타입 — 기존 일정 참고 응답 ───────────────────────────────────

@pytest.mark.asyncio
async def test_chat_references_existing_itinerary(mock_tools):
    """chat: 현재 일정 질문 → day_plans=null, 기존 일정 내용 참고 응답"""
    deps = _make_deps("chat")
    result = await _run_agent(deps, "지금 2일차에 뭐 할 예정이야?")
    data = result.output
    _print_result("chat_itinerary_question", data)

    assert data.day_plans is None, "chat 타입에서 day_plans는 null이어야 함"
    # 기존 2일차 일정(신주쿠 쇼핑, 라멘) 내용 중 하나 이상 언급
    assert any(kw in data.message for kw in ["신주쿠", "라멘", "2일차", "쇼핑"]), \
        f"기존 일정 내용을 참고한 응답이어야 함: {data.message}"
