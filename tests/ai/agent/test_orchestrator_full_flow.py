"""
오케스트레이터 전체 흐름 테스트

검증 목적: 도구 이름 + 전달된 파라미터 + API 반환값 + 최종 captured 데이터를 한눈에 확인
모킹 범위: _service.process_task + preprocessor_agent.run
           → LLM 호출은 실제 수행 (gpt-4.1)

실행:
  pytest tests/ai/agent/test_orchestrator_full_flow.py::test_full_itinerary_new -s
  pytest tests/ai/agent/test_orchestrator_full_flow.py::test_full_itinerary_modify -s
  pytest tests/ai/agent/test_orchestrator_full_flow.py::test_full_change -s
  pytest tests/ai/agent/test_orchestrator_full_flow.py::test_full_chat -s
"""
import json
import asyncio
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.llm

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

import app.services.agents.orchestrator as _orch
from app.services.agents.orchestrator import orchestrator_agent, OrchestratorDeps


# ---------------------------------------------------------------------------
# 모킹용 더미 응답 (어댑터 반환값)
# ---------------------------------------------------------------------------

_MOCK_FLIGHTS = {
    "status": "success", "count": 2,
    "data": [
        {"offer_id": "off_KE705_mock", "airline": "Korean Air",
         "origin": "ICN", "destination": "NRT",
         "total_amount": "500000 KRW", "stops": 0,
         "departing_at": "2026-05-15T10:00:00", "arriving_at": "2026-05-15T12:30:00"},
        {"offer_id": "off_OZ101_mock", "airline": "Asiana",
         "origin": "ICN", "destination": "NRT",
         "total_amount": "450000 KRW", "stops": 0,
         "departing_at": "2026-05-15T14:00:00", "arriving_at": "2026-05-15T16:30:00"},
    ],
}
_MOCK_HOTELS = {
    "status": "success", "count": 2,
    "data": [
        {"hotel_id": "prop_shinjuku_mock", "name": "Shinjuku Grand Hotel",
         "price": "150000 KRW/night", "rating": 4.5, "address": "Shinjuku, Tokyo"},
        {"hotel_id": "prop_asakusa_mock", "name": "Asakusa View Hotel",
         "price": "120000 KRW/night", "rating": 4.2, "address": "Asakusa, Tokyo"},
    ],
}
_MOCK_WEATHER = {
    "status": "success", "forecast_type": "daily", "count": 4,
    "data": [
        {"date": "2026-05-15", "temperature_max": 22, "temperature_min": 15,
         "precipitation_probability_max": 10, "weather": "맑음", "uv_index_max": 5},
        {"date": "2026-05-16", "temperature_max": 24, "temperature_min": 16,
         "precipitation_probability_max": 20, "weather": "구름 조금", "uv_index_max": 6},
        {"date": "2026-05-17", "temperature_max": 21, "temperature_min": 14,
         "precipitation_probability_max": 40, "weather": "흐림", "uv_index_max": 3},
        {"date": "2026-05-18", "temperature_max": 23, "temperature_min": 15,
         "precipitation_probability_max": 10, "weather": "맑음", "uv_index_max": 5},
    ],
}
_MOCK_PLACE = {
    "status": "success",
    "data": {"count": 3, "places": [
        {"name": "Senso-ji Temple", "formatted_address": "Asakusa, Tokyo",
         "lat": 35.71, "lng": 139.79, "rating": 4.7, "user_ratings_total": 10000,
         "types": ["tourist_attraction"]},
        {"name": "Shinjuku Gyoen", "formatted_address": "Shinjuku, Tokyo",
         "lat": 35.68, "lng": 139.71, "rating": 4.6, "user_ratings_total": 8000,
         "types": ["park"]},
    ]},
}
_MOCK_ROUTE = {
    "status": "success",
    "data": {"count": 1, "routes": [
        {"start_address": "Shinjuku, Tokyo", "end_address": "Asakusa, Tokyo",
         "distance": "15 km", "duration": "35분", "steps": ["메트로 E 라인 탑승"]},
    ]},
}
_MOCK_TAVILY = {
    "status": "success", "count": 3,
    "data": [
        {"title": "Tokyo Travel Tips 2026", "content": "Best spots in Tokyo include Senso-ji...",
         "url": "http://example.com/tokyo", "score": 0.92},
        {"title": "도쿄 5월 여행", "content": "5월의 도쿄는 따뜻하고 맑은 날씨...",
         "url": "http://example.com/tokyo-may", "score": 0.85},
    ],
}


# ---------------------------------------------------------------------------
# 로깅 모크 — 파라미터와 반환값을 모두 기록
# ---------------------------------------------------------------------------

def _make_logging_mock(call_log: list) -> AsyncMock:
    def _get_mock_result(tool_name: str, action: str) -> dict:
        if tool_name == "duffel_flight":
            return _MOCK_FLIGHTS
        if tool_name == "duffel_accommodation":
            return _MOCK_HOTELS
        if tool_name == "weather":
            return _MOCK_WEATHER
        if tool_name == "google_maps":
            return _MOCK_PLACE if action == "search_place" else _MOCK_ROUTE
        if tool_name == "tavily_search":
            return _MOCK_TAVILY
        return {"status": "success", "data": []}

    async def _mock(tool_name: str, action: str, params: dict) -> dict:
        result = _get_mock_result(tool_name, action)
        call_log.append({
            "tool_name": tool_name,
            "action": action,
            "params": params,
            "result": result,
        })
        return result

    return _mock


# ---------------------------------------------------------------------------
# 출력 헬퍼
# ---------------------------------------------------------------------------

def _print_full_flow(test_name: str, result, deps: OrchestratorDeps, api_call_log: list) -> None:
    SEP = "=" * 70
    print(f"\n{SEP}")
    print(f"[{test_name}] 오케스트레이터 전체 흐름")
    print(SEP)

    # 1. 어댑터 API 호출 로그 (파라미터 + 반환값)
    if api_call_log:
        print(f"\n▶ 외부 API 호출 ({len(api_call_log)}회)")
        for i, entry in enumerate(api_call_log, 1):
            print(f"\n  [{i}] {entry['tool_name']} / {entry['action']}")
            print(f"  파라미터: {json.dumps(entry['params'], ensure_ascii=False)}")
            result_preview = str(entry['result'])[:200]
            print(f"  반환값:   {result_preview}{'...' if len(str(entry['result'])) > 200 else ''}")
    else:
        print("\n▶ 외부 API 호출 없음")

    # 2. LLM이 호출한 도구 전체 (submit_* 포함) + 파라미터
    print(f"\n▶ LLM 도구 호출 순서")
    tool_idx = 0
    for msg in result.all_messages():
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    tool_idx += 1
                    try:
                        args = part.args if isinstance(part.args, dict) else json.loads(part.args)
                    except Exception:
                        args = str(part.args)
                    print(f"\n  [{tool_idx}] {part.tool_name}")
                    print(f"  파라미터: {json.dumps(args, ensure_ascii=False, default=str)}")

    # 3. captured 데이터 (submit_* 도구가 저장한 최종 구조체)
    print(f"\n▶ 캡처된 최종 데이터 (deps.captured)")
    if deps.captured:
        print(json.dumps(deps.captured, ensure_ascii=False, default=str, indent=2))
    else:
        print("  (없음 — submit_* 도구 미호출)")

    # 4. LLM 텍스트 응답
    print(f"\n▶ LLM 응답 텍스트")
    print(result.data)
    print(f"\n{SEP}\n")


def _make_deps(request_type: str, current_itinerary: dict | None = None) -> OrchestratorDeps:
    return OrchestratorDeps(
        ai_summary=None,
        preferences=None,
        today=str(date.today()),
        similar_messages=[],
        current_itinerary=current_itinerary,
        request_type=request_type,
        reservations=[],
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
        "2일차": [{"plan_name": "아사쿠사 관광", "time": "10:00 ~ 12:00", "place": "아사쿠사", "note": ""}],
        "3일차": [{"plan_name": "아키하바라", "time": "13:00 ~ 16:00", "place": "아키하바라", "note": ""}],
        "4일차": [{"plan_name": "공항 이동", "time": "10:00 ~ 12:00", "place": "나리타 공항", "note": ""}],
    },
}


@pytest.fixture(autouse=True)
async def rate_limit_guard():
    yield
    await asyncio.sleep(20)


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_itinerary_new():
    """신규 일정 생성 — 도구 파라미터·반환값·최종 dayPlans 전체 확인"""
    api_call_log = []
    preprocessor_mock = MagicMock()
    preprocessor_mock.data = "도쿄 여행 핵심 정보: 5월은 따뜻하고 관광하기 좋음 (mock 요약)"

    with patch.object(_orch._service, "process_task", side_effect=_make_logging_mock(api_call_log)), \
         patch.object(_orch.preprocessor_agent, "run", new=AsyncMock(return_value=preprocessor_mock)):

        deps = _make_deps("itinerary", current_itinerary=None)
        result = await orchestrator_agent.run(
            "도쿄 3박 4일 여행 일정 짜줘. 5월 15일 출발, 성인 2명이야.",
            deps=deps,
        )

    _print_full_flow("itinerary_new", result, deps, api_call_log)

    assert deps.captured.get("itinerary") is not None, "submit_itinerary가 호출되지 않음"


@pytest.mark.asyncio
async def test_full_itinerary_modify():
    """기존 일정 장소 수정 — 변경된 dayPlans 구조 확인"""
    api_call_log = []
    preprocessor_mock = MagicMock()
    preprocessor_mock.data = "아사쿠사 관광 정보 요약 (mock)"

    with patch.object(_orch._service, "process_task", side_effect=_make_logging_mock(api_call_log)), \
         patch.object(_orch.preprocessor_agent, "run", new=AsyncMock(return_value=preprocessor_mock)):

        deps = _make_deps("itinerary", current_itinerary=_SAMPLE_ITINERARY)
        result = await orchestrator_agent.run(
            "2일차 아사쿠사를 우에노 공원으로 바꿔줘.",
            deps=deps,
        )

    _print_full_flow("itinerary_modify", result, deps, api_call_log)

    assert deps.captured.get("itinerary") is not None, "submit_itinerary가 호출되지 않음"


@pytest.mark.asyncio
async def test_full_change():
    """날짜/예산 변경 — OrchestratorResult.change payload가 반드시 채워지는지 확인.

    회귀 배경: LLM이 message로는 '변경했습니다'라고 답하면서 change=null을 반환해
    백엔드 DB 기본정보 갱신이 조용히 누락되는 문제가 있었다.
    """
    from app.services.agents.orchestrator import build_context_prompt
    from app.services.agents._base import run_with_retry

    deps = _make_deps("change", current_itinerary=_SAMPLE_ITINERARY)
    prompt = (
        f"{build_context_prompt(deps)}\n\n---\n\n"
        "사용자 메시지: 여행 날짜 5월 20일부터 24일로 바꾸고, 예산은 150만원으로 늘려줘."
    )
    result = await run_with_retry(orchestrator_agent, prompt, role="orchestrator", deps=deps)
    out = result.output

    print(f"\n[change] message: {out.message}")
    print(f"[change] change : {out.change.model_dump(exclude_none=True) if out.change else None}")

    assert out.change is not None, f"change payload가 비어 있음. message={out.message!r}"
    assert out.change.start_date == "2026-05-20"
    assert out.change.end_date == "2026-05-24"
    assert out.change.budget == 1500000


@pytest.mark.asyncio
async def test_full_chat():
    """일반 질문 — 어떤 도구를 쓰는지, captured는 비어있는지 확인"""
    api_call_log = []
    preprocessor_mock = MagicMock()
    preprocessor_mock.data = "도쿄 라멘 추천 요약 (mock)"

    with patch.object(_orch._service, "process_task", side_effect=_make_logging_mock(api_call_log)), \
         patch.object(_orch.preprocessor_agent, "run", new=AsyncMock(return_value=preprocessor_mock)):

        deps = _make_deps("chat")
        result = await orchestrator_agent.run(
            "도쿄에서 꼭 먹어야 할 라멘집 추천해줘.",
            deps=deps,
        )

    _print_full_flow("chat", result, deps, api_call_log)

    for key in ["itinerary", "change", "reservation", "cancel"]:
        assert key not in deps.captured, f"chat 타입에서 {key}가 캡처되면 안 됨"
