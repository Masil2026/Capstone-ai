# tests/ai/agent/test_safety_gate.py
"""위험 여행지 안전 게이트 — LLM 호출 없이 분기 로직만 검증."""
import pytest
from unittest.mock import AsyncMock, patch

from app.schemas.ai_message import OrchestratorResult
from app.services.agents.itinerary_pipeline import (
    SafetyVerdict,
    _check_safety,
    run_itinerary_pipeline,
)
from app.services.agents.orchestrator import OrchestratorDeps


def _make_deps(ai_summary=None):
    return OrchestratorDeps(
        ai_summary=ai_summary,
        preferences=None,
        today="2026-07-11",
        similar_messages=[],
        current_itinerary={
            "destinations": [{"city": "키이우", "start_date": "2026-08-01", "end_date": "2026-08-05"}],
            "start_date": "2026-08-01",
            "end_date": "2026-08-05",
            "adult_count": 2,
        },
        request_type="itinerary",
        reservations=[],
    )


@pytest.mark.asyncio
async def test_unsafe_destination_yields_warning_and_stops():
    """unsafe & 미동의 → 경고 메시지 + day_plans 없는 결과만 yield하고 종료."""
    verdict = SafetyVerdict(unsafe=True, risk_summary="전쟁 중인 지역입니다.", user_consented=False)

    with patch(
        "app.services.agents.itinerary_pipeline._check_safety",
        new=AsyncMock(return_value=verdict),
    ):
        items = [item async for item in run_itinerary_pipeline(_make_deps(), "키이우 일정 짜줘", [])]

    assert len(items) == 2
    warning, result = items
    assert "전쟁 중인 지역입니다." in warning
    assert "그럼에도 일정을 만들어드릴까요?" in warning
    assert isinstance(result, OrchestratorResult)
    assert result.day_plans is None
    assert "경고 안내함" in result.ai_summary


@pytest.mark.asyncio
async def test_check_safety_fails_open_on_error():
    """검색 실패 등 예외 발생 시 None 반환 — 파이프라인을 막지 않는다."""
    with patch(
        "app.services.agents.itinerary_pipeline._service.process_task",
        new=AsyncMock(side_effect=RuntimeError("tavily down")),
    ):
        result = await _check_safety(
            [{"city": "도쿄"}], "일정 짜줘", None,
        )

    assert result is None
