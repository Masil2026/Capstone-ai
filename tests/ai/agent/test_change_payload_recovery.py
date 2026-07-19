"""change 타입에서 LLM이 완료를 주장하며 change=null을 반환할 때의 복구 가드 테스트 (LLM Mock)."""
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.controller.aiMessageController import _CHANGE_CLAIM_RE, _recover_missing_change
from app.schemas.ai_message import ChangeFields, OrchestratorResult
from app.services.agents.orchestrator import OrchestratorDeps


def _deps() -> OrchestratorDeps:
    return OrchestratorDeps(
        ai_summary=None,
        preferences=None,
        today=date.today().isoformat(),
        similar_messages=[],
        current_itinerary=None,
        request_type="change",
        reservations=[],
    )


def test_change_claim_detects_completion_phrases():
    assert _CHANGE_CLAIM_RE.search("여행 출발 날짜를 8월 16일로 변경했어요.")
    assert _CHANGE_CLAIM_RE.search("종료일은 8월 19일로 조정되었습니다.")
    assert _CHANGE_CLAIM_RE.search("예산을 150만원으로 수정했습니다.")


def test_change_claim_ignores_questions():
    assert not _CHANGE_CLAIM_RE.search("여행 시작일을 8월 20일로 변경할까요?")
    assert not _CHANGE_CLAIM_RE.search("여행 종료일 또는 총 여행 기간을 알려주세요.")


@pytest.mark.asyncio
async def test_recover_grafts_change_payload_on_extract_success():
    """추출기가 필드를 채우면 payload만 이식하고 이미 스트리밍된 message는 유지한다."""
    original = OrchestratorResult(message="여행 출발 날짜를 8월 16일로 변경했어요.")
    extracted = ChangeFields(start_date="2026-08-16", end_date="2026-08-19")

    with patch(
        "app.controller.aiMessageController.run_with_retry",
        new=AsyncMock(return_value=MagicMock(output=extracted)),
    ):
        result = await _recover_missing_change("출발 날짜를 8월 16일로 바꿔줘", _deps(), original)

    assert result.change is not None
    assert result.change.start_date == "2026-08-16"
    assert result.message == "여행 출발 날짜를 8월 16일로 변경했어요."  # 원본 message 유지


@pytest.mark.asyncio
async def test_recover_keeps_original_when_extractor_finds_nothing():
    original = OrchestratorResult(message="여행 날짜를 변경했어요.")

    with patch(
        "app.controller.aiMessageController.run_with_retry",
        new=AsyncMock(return_value=MagicMock(output=ChangeFields())),
    ):
        result = await _recover_missing_change("여행 날짜 바꿔줘", _deps(), original)

    assert result.change is None
    assert result.message == "여행 날짜를 변경했어요."


@pytest.mark.asyncio
async def test_recover_swallows_extractor_exception():
    """추출기 호출이 실패해도 응답 스트림을 깨지 않고 원본을 반환한다."""
    original = OrchestratorResult(message="여행 날짜를 변경했어요.")

    with patch(
        "app.controller.aiMessageController.run_with_retry",
        new=AsyncMock(side_effect=RuntimeError("429")),
    ):
        result = await _recover_missing_change("여행 날짜 바꿔줘", _deps(), original)

    assert result.change is None
    assert result.message == "여행 날짜를 변경했어요."
