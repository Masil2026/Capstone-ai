"""
오케스트레이터 실제 API 통합 테스트

외부 API 모킹 없이 실제 호출 — 도구 파라미터·반환값·최종 captured 데이터 확인용.
비용이 발생하므로 수동 실행만 (pytest.ini: -m "not llm"으로 기본 제외됨)

실행:
  pytest tests/ai/agent/test_orchestrator_real_api.py::test_real_itinerary_new -s -m llm
  pytest tests/ai/agent/test_orchestrator_real_api.py::test_real_itinerary_modify -s -m llm
"""
import json
import asyncio
import pytest
from datetime import date
from unittest.mock import patch

pytestmark = pytest.mark.llm

from pydantic_ai.messages import ModelResponse, ToolCallPart

import app.services.agents.orchestrator as _orch
from app.services.agents.orchestrator import orchestrator_agent, OrchestratorDeps


# ---------------------------------------------------------------------------
# 실제 process_task 호출을 가로채어 로깅만 추가하는 래퍼
# ---------------------------------------------------------------------------

def _make_logging_wrapper(call_log: list):
    original = _orch._service.process_task

    async def _wrapper(tool_name: str, action: str, params: dict) -> dict:
        result = await original(tool_name, action, params)
        call_log.append({
            "tool_name": tool_name,
            "action": action,
            "params": params,
            "result": result,
        })
        return result

    return _wrapper


# ---------------------------------------------------------------------------
# 출력 헬퍼
# ---------------------------------------------------------------------------

def _print_full_flow(test_name: str, result, deps: OrchestratorDeps, api_call_log: list) -> None:
    SEP = "=" * 70
    print(f"\n{SEP}")
    print(f"[{test_name}] 오케스트레이터 실제 API 흐름")
    print(SEP)

    # 1. 실제 외부 API 호출 로그
    if api_call_log:
        print(f"\n▶ 외부 API 실제 호출 ({len(api_call_log)}회)")
        for i, entry in enumerate(api_call_log, 1):
            print(f"\n  [{i}] {entry['tool_name']} / {entry['action']}")
            print(f"  파라미터: {json.dumps(entry['params'], ensure_ascii=False)}")
            result_str = json.dumps(entry['result'], ensure_ascii=False, default=str)
            preview = result_str[:300]
            print(f"  반환값:   {preview}{'...' if len(result_str) > 300 else ''}")
    else:
        print("\n▶ 외부 API 호출 없음")

    # 2. LLM이 호출한 도구 전체 + 파라미터
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

    # 3. 최종 캡처 데이터
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
    )


@pytest.fixture(autouse=True)
async def rate_limit_guard():
    yield
    await asyncio.sleep(20)


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_real_itinerary_new():
    """신규 일정 생성 — 실제 API 호출로 항공·숙소·날씨·장소·경로 수집 후 일정 생성"""
    api_call_log = []
    wrapper = _make_logging_wrapper(api_call_log)

    with patch.object(_orch._service, "process_task", side_effect=wrapper):
        deps = _make_deps("itinerary", current_itinerary=None)
        result = await orchestrator_agent.run(
            "상하이 3박 4일 여행 일정 짜줘. 5월 20일 출발, 성인 2명이야. 출발지는 인천이야.",
            deps=deps,
        )

    _print_full_flow("real_itinerary_new", result, deps, api_call_log)

    assert deps.captured.get("itinerary") is not None, "submit_itinerary가 호출되지 않음"


@pytest.mark.asyncio
async def test_real_itinerary_modify():
    """실제 일정 수정 — 장소 변경 후 관련 정보만 재수집"""
    api_call_log = []
    wrapper = _make_logging_wrapper(api_call_log)

    sample_itinerary = {
        "destination": "도쿄",
        "start_date": "2026-05-20",
        "end_date": "2026-05-23",
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

    with patch.object(_orch._service, "process_task", side_effect=wrapper):
        deps = _make_deps("itinerary", current_itinerary=sample_itinerary)
        result = await orchestrator_agent.run(
            "2일차 아사쿠사를 우에노 공원으로 바꿔줘.",
            deps=deps,
        )

    _print_full_flow("real_itinerary_modify", result, deps, api_call_log)

    assert deps.captured.get("itinerary") is not None, "submit_itinerary가 호출되지 않음"
