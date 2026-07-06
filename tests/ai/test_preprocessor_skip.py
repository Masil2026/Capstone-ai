# tests/ai/test_preprocessor_skip.py
"""
PREPROCESSOR_SKIP_MAX_LEN 길이 체크 테스트.
검색 결과가 짧으면 LLM 호출을 생략하고, 길면 run_with_retry를 호출하는지 검증한다.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.config import settings
from app.services.agents.itinerary_pipeline import _fetch_web_summary
from app.services.agents.orchestrator import search_web


# 임계값 기준 길이 설정
_THRESHOLD = settings.PREPROCESSOR_SKIP_MAX_LEN
SHORT_CONTENT = "서울 남산타워, 경복궁, 명동"          # 명백히 임계값 미만
LONG_CONTENT = "여행 정보입니다. " * (_THRESHOLD // 8 + 10)  # 명백히 임계값 초과


def _tavily_ok(content: str) -> dict:
    return {
        "status": "success",
        "data": [{"title": "테스트 제목", "content": content, "score": 0.9}],
    }


# ── _fetch_web_summary ────────────────────────────────────────────────────────

async def test_fetch_web_summary_skips_llm_for_short_content():
    """combined ≤ PREPROCESSOR_SKIP_MAX_LEN → run_with_retry 호출 없음."""
    with patch(
        "app.services.agents.itinerary_pipeline._service.process_task",
        new_callable=AsyncMock,
        return_value=_tavily_ok(SHORT_CONTENT),
    ):
        with patch(
            "app.services.agents.itinerary_pipeline.run_with_retry",
            new_callable=AsyncMock,
        ) as mock_retry:
            result = await _fetch_web_summary("Seoul", None)

    mock_retry.assert_not_called()
    assert SHORT_CONTENT in result


async def test_fetch_web_summary_calls_llm_for_long_content():
    """combined > PREPROCESSOR_SKIP_MAX_LEN → run_with_retry 1회 호출."""
    mock_llm = MagicMock(output="LLM 요약 결과")

    with patch(
        "app.services.agents.itinerary_pipeline._service.process_task",
        new_callable=AsyncMock,
        return_value=_tavily_ok(LONG_CONTENT),
    ):
        with patch(
            "app.services.agents.itinerary_pipeline.run_with_retry",
            new_callable=AsyncMock,
            return_value=mock_llm,
        ) as mock_retry:
            result = await _fetch_web_summary("Seoul", None)

    mock_retry.assert_called_once()
    assert result == "LLM 요약 결과"


async def test_fetch_web_summary_threshold_boundary():
    """combined이 정확히 임계값이면 LLM 생략.

    스니펫 형식: f"[{title}]\\n{content}" → prefix 길이만큼 content를 줄여야 combined이 정확히 THRESHOLD.
    """
    title = "t"
    prefix_len = len(f"[{title}]\n")          # "[t]\n" = 4자
    content = "A" * (_THRESHOLD - prefix_len)  # combined = prefix + content = THRESHOLD

    with patch(
        "app.services.agents.itinerary_pipeline._service.process_task",
        new_callable=AsyncMock,
        side_effect=[
            {"status": "success", "data": [{"title": title, "content": content, "score": 0.9}]},
            {"status": "success", "data": []},
        ],
    ):
        with patch(
            "app.services.agents.itinerary_pipeline.run_with_retry",
            new_callable=AsyncMock,
        ) as mock_retry:
            result = await _fetch_web_summary("Seoul", None)

    mock_retry.assert_not_called()  # len(combined) == THRESHOLD → 스킵
    assert content in result


async def test_fetch_web_summary_one_over_threshold_calls_llm():
    """combined이 임계값 + 1자이면 LLM 호출."""
    title = "t"
    prefix_len = len(f"[{title}]\n")
    content = "A" * (_THRESHOLD - prefix_len + 1)  # combined = THRESHOLD + 1
    mock_llm = MagicMock(output="요약됨")

    with patch(
        "app.services.agents.itinerary_pipeline._service.process_task",
        new_callable=AsyncMock,
        side_effect=[
            {"status": "success", "data": [{"title": title, "content": content, "score": 0.9}]},
            {"status": "success", "data": []},
        ],
    ):
        with patch(
            "app.services.agents.itinerary_pipeline.run_with_retry",
            new_callable=AsyncMock,
            return_value=mock_llm,
        ) as mock_retry:
            result = await _fetch_web_summary("Seoul", None)

    mock_retry.assert_called_once()
    assert result == "요약됨"


# ── search_web ────────────────────────────────────────────────────────────────

async def test_search_web_skips_llm_for_short_content():
    """snippets ≤ PREPROCESSOR_SKIP_MAX_LEN → run_with_retry 호출 없음."""
    with patch(
        "app.services.agents.orchestrator._service.process_task",
        new_callable=AsyncMock,
        return_value=_tavily_ok(SHORT_CONTENT),
    ):
        with patch(
            "app.services.agents.orchestrator.run_with_retry",
            new_callable=AsyncMock,
        ) as mock_retry:
            result = await search_web("도쿄 여행")

    mock_retry.assert_not_called()
    assert result["status"] == "success"
    assert SHORT_CONTENT in result["summary"]
    assert result["source_count"] == 1


async def test_search_web_calls_llm_for_long_content():
    """snippets > PREPROCESSOR_SKIP_MAX_LEN → run_with_retry 1회 호출."""
    mock_llm = MagicMock(output="LLM 요약 결과")

    with patch(
        "app.services.agents.orchestrator._service.process_task",
        new_callable=AsyncMock,
        return_value=_tavily_ok(LONG_CONTENT),
    ):
        with patch(
            "app.services.agents.orchestrator.run_with_retry",
            new_callable=AsyncMock,
            return_value=mock_llm,
        ) as mock_retry:
            result = await search_web("도쿄 여행")

    mock_retry.assert_called_once()
    assert result["status"] == "success"
    assert result["summary"] == "LLM 요약 결과"


async def test_search_web_no_results_skips_llm():
    """Tavily 결과가 없으면 LLM 없이 기본 메시지 반환."""
    with patch(
        "app.services.agents.orchestrator._service.process_task",
        new_callable=AsyncMock,
        return_value={"status": "success", "data": []},
    ):
        with patch(
            "app.services.agents.orchestrator.run_with_retry",
            new_callable=AsyncMock,
        ) as mock_retry:
            result = await search_web("존재하지않는쿼리")

    mock_retry.assert_not_called()
    assert result["status"] == "success"
    assert result["source_count"] == 0
