"""
오케스트레이터 실제 API 통합 테스트

외부 API 모킹 없이 실제 호출 — 도구 파라미터·반환값·최종 captured 데이터 확인용.
Redis에서 채워지는 ai_summary·preferences·similar_messages도 샘플 데이터로 주입.
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
# 샘플 Redis 데이터 (실제 서비스에서 load_context()가 채워주는 값들)
# ---------------------------------------------------------------------------

# 이전 대화 전체 요약 (Redis memory: ai_summary)
_SAMPLE_AI_SUMMARY = (
    "사용자는 2명이 함께 여행하며, 주로 도시 여행을 선호한다. "
    "이전 대화에서 도쿄 3박 4일 일정을 논의했고, 항공편은 대한항공을 선호한다고 밝혔다."
)

# 사용자 취향 (Redis memory: preferences)
_SAMPLE_PREFERENCES = {
    "food": "라멘, 스시 등 일식 선호. 매운 음식은 피함.",
    "transport": "대중교통 선호. 택시는 비상시에만.",
    "accommodation": "시내 중심가, 교통 편리한 곳. 4성급 이상.",
    "activity": "문화·역사 명소 위주. 쇼핑보다 관광 선호.",
    "airline": "대한항공 선호. 직항 우선.",
}

# pgvector 유사 과거 메시지 (최대 5개)
_SAMPLE_SIMILAR_MESSAGES = [
    {
        "role": "user",
        "content": "도쿄 여행할 때 신주쿠랑 아사쿠사 중 어디가 더 좋아?",
    },
    {
        "role": "assistant",
        "content": "신주쿠는 쇼핑과 현대적인 분위기, 아사쿠사는 전통 문화 체험에 좋습니다. "
                   "문화·역사 선호라면 아사쿠사를 추천드립니다.",
    },
    {
        "role": "user",
        "content": "항공편은 직항으로 하고 싶어.",
    },
]

# 기존 일정 (DB에서 읽어오는 current_itinerary — 수정 테스트용)
_SAMPLE_ITINERARY = {
    "destination": "상하이",
    "start_date": "2026-05-20",
    "end_date": "2026-05-23",
    "total_days": 4,
    "budget": 2000000,
    "adult_count": 2,
    "child_count": 0,
    "child_ages": [],
    "day_plans": {
        "1일차": [
            {"plan_name": "와이탄 야경 감상", "time": "19:00 ~ 21:00", "place": "와이탄", "note": "황푸강 야경 명소"},
        ],
        "2일차": [
            {"plan_name": "예원 관광", "time": "10:00 ~ 12:00", "place": "예원", "note": "전통 정원"},
            {"plan_name": "난징루 쇼핑", "time": "14:00 ~ 17:00", "place": "난징루", "note": ""},
        ],
        "3일차": [
            {"plan_name": "주자자오 수향마을", "time": "09:00 ~ 14:00", "place": "주자자오", "note": "당일치기"},
        ],
        "4일차": [
            {"plan_name": "공항 이동", "time": "10:00 ~ 12:00", "place": "푸둥 국제공항", "note": ""},
        ],
    },
}


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

    # 0. 주입된 deps (Redis에서 왔을 데이터)
    print("\n▶ 주입된 컨텍스트 (Redis → OrchestratorDeps)")
    print(f"  ai_summary    : {deps.ai_summary or '(없음)'}")
    print(f"  preferences   : {json.dumps(deps.preferences, ensure_ascii=False) if deps.preferences else '(없음)'}")
    print(f"  similar_msgs  : {len(deps.similar_messages)}개")
    if deps.similar_messages:
        for m in deps.similar_messages:
            role = m.get("role", "?")
            content = m.get("content", "")[:60]
            print(f"    [{role}] {content}{'...' if len(m.get('content','')) > 60 else ''}")
    print(f"  current_itinerary: {'있음' if deps.current_itinerary else '없음 (신규)'}")
    if deps.current_itinerary:
        days = list(deps.current_itinerary.get("day_plans", {}).keys())
        print(f"    → {deps.current_itinerary.get('destination')} "
              f"{deps.current_itinerary.get('start_date')}~{deps.current_itinerary.get('end_date')} "
              f"/ {days}")

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

    # 3. 최종 캡처 데이터 (done 이벤트로 프론트에 전달될 데이터)
    print(f"\n▶ 캡처된 최종 데이터 (deps.captured → done 이벤트)")
    if deps.captured:
        print(json.dumps(deps.captured, ensure_ascii=False, default=str, indent=2))
    else:
        print("  (없음 — submit_* 도구 미호출)")

    # 4. LLM 텍스트 응답
    print(f"\n▶ LLM 응답 텍스트")
    print(result.data)
    print(f"\n{SEP}\n")


def _make_deps(
    request_type: str,
    ai_summary: str | None = None,
    preferences: dict | None = None,
    similar_messages: list | None = None,
    current_itinerary: dict | None = None,
) -> OrchestratorDeps:
    return OrchestratorDeps(
        ai_summary=ai_summary,
        preferences=preferences,
        today=str(date.today()),
        similar_messages=similar_messages or [],
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
    """신규 일정 생성 — Redis 컨텍스트(취향·요약·과거 대화) 반영 여부 + 실제 API 결과 확인"""
    api_call_log = []
    wrapper = _make_logging_wrapper(api_call_log)

    with patch.object(_orch._service, "process_task", side_effect=wrapper):
        deps = _make_deps(
            request_type="itinerary",
            ai_summary=_SAMPLE_AI_SUMMARY,
            preferences=_SAMPLE_PREFERENCES,
            similar_messages=_SAMPLE_SIMILAR_MESSAGES,
            current_itinerary=None,
        )
        result = await orchestrator_agent.run(
            "상하이 3박 4일 여행 일정 짜줘. 5월 20일 출발, 성인 2명이야. 출발지는 인천이야.",
            deps=deps,
        )

    _print_full_flow("real_itinerary_new", result, deps, api_call_log)

    assert deps.captured.get("itinerary") is not None, "submit_itinerary가 호출되지 않음"


@pytest.mark.asyncio
async def test_real_itinerary_modify():
    """기존 일정 수정 — 현재 일정 + Redis 컨텍스트 주입 후 장소 변경 테스트"""
    api_call_log = []
    wrapper = _make_logging_wrapper(api_call_log)

    with patch.object(_orch._service, "process_task", side_effect=wrapper):
        deps = _make_deps(
            request_type="itinerary",
            ai_summary=_SAMPLE_AI_SUMMARY,
            preferences=_SAMPLE_PREFERENCES,
            similar_messages=_SAMPLE_SIMILAR_MESSAGES,
            current_itinerary=_SAMPLE_ITINERARY,
        )
        result = await orchestrator_agent.run(
            "3일차 주자자오 대신 상하이 디즈니랜드로 바꿔줘.",
            deps=deps,
        )

    _print_full_flow("real_itinerary_modify", result, deps, api_call_log)

    assert deps.captured.get("itinerary") is not None, "submit_itinerary가 호출되지 않음"
