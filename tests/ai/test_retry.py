# tests/ai/test_retry.py
"""
run_with_retry / _is_rate_limit_error / _retry_wait 단위 테스트.
실제 LLM 호출 없이 AsyncMock으로 Vertex AI 429 동작을 시뮬레이션한다.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch

from app.services.agents._base import (
    _is_rate_limit_error,
    _llm_bucket,
    _retry_wait,
    run_with_retry,
)


# ── _is_rate_limit_error ──────────────────────────────────────────────────────

def test_is_rate_limit_error_detects_429():
    assert _is_rate_limit_error(Exception("HTTP 429 Too Many Requests")) is True

def test_is_rate_limit_error_detects_resource_exhausted():
    assert _is_rate_limit_error(Exception("RESOURCE_EXHAUSTED quota exceeded")) is True

def test_is_rate_limit_error_ignores_other_errors():
    assert _is_rate_limit_error(Exception("500 Internal Server Error")) is False
    assert _is_rate_limit_error(Exception("connection timeout")) is False
    assert _is_rate_limit_error(ValueError("invalid input")) is False


# ── _retry_wait ───────────────────────────────────────────────────────────────

def test_retry_wait_within_expected_range():
    """대기 시간이 base 이상, base*1.3 이하인지 확인 (jitter 범위)."""
    for attempt in range(4):
        base = 2 ** attempt
        wait = _retry_wait(attempt)
        assert base <= wait <= base * 1.3, (
            f"attempt={attempt}: {wait:.2f}s가 [{base}, {base * 1.3:.1f}] 범위 밖"
        )

def test_retry_wait_base_increases_exponentially():
    """attempt가 증가할수록 base 대기 시간이 지수적으로 증가."""
    bases = [2 ** i for i in range(4)]
    assert bases == [1, 2, 4, 8]


# ── run_with_retry — 성공 케이스 ─────────────────────────────────────────────

async def test_success_on_first_try():
    """첫 번째 시도에 성공 → 재시도 없음, sleep 없음."""
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=MagicMock(output="성공"))

    with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await run_with_retry(mock_agent, "프롬프트", role="test")

    assert result.output == "성공"
    mock_agent.run.assert_called_once_with("프롬프트")
    mock_sleep.assert_not_called()


async def test_kwargs_passed_to_agent_run():
    """deps, message_history 등 kwargs가 agent.run()에 그대로 전달됨."""
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=MagicMock(output="ok"))
    mock_deps = MagicMock()
    mock_history = [{"role": "user", "content": "이전 메시지"}]

    await run_with_retry(
        mock_agent, "프롬프트", role="test",
        deps=mock_deps, message_history=mock_history,
    )

    mock_agent.run.assert_called_once_with(
        "프롬프트", deps=mock_deps, message_history=mock_history,
    )


# ── run_with_retry — 429 재시도 케이스 ───────────────────────────────────────

async def test_succeeds_on_second_attempt():
    """429 1회 → 2번째 성공. sleep 1회(1초) 호출."""
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(side_effect=[
        Exception("429 RESOURCE_EXHAUSTED"),
        MagicMock(output="2번째 성공"),
    ])

    with patch.object(_llm_bucket, "acquire", new_callable=AsyncMock):
        with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with patch("app.services.agents._base.random.uniform", return_value=0.0):
                result = await run_with_retry(mock_agent, "프롬프트", role="test")

    assert result.output == "2번째 성공"
    assert mock_agent.run.call_count == 2
    mock_sleep.assert_called_once_with(1.0)  # attempt=0 → base=1, jitter=0


async def test_succeeds_on_third_attempt():
    """429 2회 → 3번째 성공. sleep 2회(1s, 2s) 호출."""
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(side_effect=[
        Exception("429"),
        Exception("RESOURCE_EXHAUSTED"),
        MagicMock(output="3번째 성공"),
    ])

    with patch.object(_llm_bucket, "acquire", new_callable=AsyncMock):
        with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with patch("app.services.agents._base.random.uniform", return_value=0.0):
                result = await run_with_retry(mock_agent, "프롬프트", role="test")

    assert result.output == "3번째 성공"
    assert mock_agent.run.call_count == 3
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list[0] == call(1.0)  # attempt=0 → 1s
    assert mock_sleep.call_args_list[1] == call(2.0)  # attempt=1 → 2s


async def test_backoff_increases_exponentially():
    """대기 시간이 1s → 2s → 4s로 지수 증가하는지 확인."""
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(side_effect=[
        Exception("429"),
        Exception("429"),
        Exception("429"),
        MagicMock(output="ok"),
    ])

    with patch.object(_llm_bucket, "acquire", new_callable=AsyncMock):
        with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with patch("app.services.agents._base.random.uniform", return_value=0.0):
                await run_with_retry(mock_agent, "프롬프트", role="test")

    sleep_values = [c[0][0] for c in mock_sleep.call_args_list]
    assert sleep_values == [1.0, 2.0, 4.0]


# ── run_with_retry — 실패 케이스 ─────────────────────────────────────────────

async def test_raises_after_all_retries_exhausted():
    """max_retries 모두 429 → 마지막 예외를 그대로 raise."""
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(side_effect=Exception("429 quota exceeded"))

    with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock):
        with patch("app.services.agents._base.random.uniform", return_value=0.0):
            with pytest.raises(Exception, match="429"):
                await run_with_retry(mock_agent, "프롬프트", role="test", max_retries=4)

    assert mock_agent.run.call_count == 4  # 최대 횟수만큼 시도


async def test_non_429_raises_immediately_without_retry():
    """429가 아닌 에러는 재시도 없이 즉시 raise."""
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(side_effect=ValueError("잘못된 입력"))

    with patch.object(_llm_bucket, "acquire", new_callable=AsyncMock):
        with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(ValueError, match="잘못된 입력"):
                await run_with_retry(mock_agent, "프롬프트", role="test")

    mock_agent.run.assert_called_once()
    mock_sleep.assert_not_called()


async def test_custom_max_retries():
    """max_retries=2이면 최대 2회만 시도."""
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(side_effect=Exception("429"))

    with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock):
        with patch("app.services.agents._base.random.uniform", return_value=0.0):
            with pytest.raises(Exception):
                await run_with_retry(mock_agent, "프롬프트", role="test", max_retries=2)

    assert mock_agent.run.call_count == 2
