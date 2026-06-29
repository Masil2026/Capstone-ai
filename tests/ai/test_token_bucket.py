# tests/ai/test_token_bucket.py
"""
_TokenBucket 단위 테스트.
time.monotonic과 asyncio.sleep을 mock하여 실제 대기 없이 동작을 검증한다.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.agents._base import _TokenBucket, _llm_bucket, run_with_retry


# ── 즉시 통과 ─────────────────────────────────────────────────────────────────

async def test_acquire_returns_immediately_when_tokens_available():
    """토큰이 있으면 즉시 반환, sleep 없음."""
    bucket = _TokenBucket(rate=1.0, capacity=5.0)
    bucket._tokens = 5.0

    with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await bucket.acquire()

    mock_sleep.assert_not_called()
    assert bucket._tokens == pytest.approx(4.0)


async def test_burst_within_capacity_passes_without_waiting():
    """capacity 내에서 연속 호출은 대기 없이 모두 통과한다."""
    bucket = _TokenBucket(rate=1.0, capacity=5.0)
    bucket._tokens = 5.0

    with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        for _ in range(5):
            await bucket.acquire()

    mock_sleep.assert_not_called()
    assert bucket._tokens == pytest.approx(0.0, abs=0.01)


# ── 대기 동작 ─────────────────────────────────────────────────────────────────

async def test_acquire_sleeps_when_bucket_empty():
    """토큰 없으면 보충 시간만큼 sleep 후 통과한다.

    rate=2.0, tokens=0 → wait = 1/2.0 = 0.5s
    sleep 후 0.5s 경과 → tokens = 0 + 0.5*2 = 1.0 → 통과
    """
    bucket = _TokenBucket(rate=2.0, capacity=1.0)
    bucket._tokens = 0.0
    t0 = 1000.0
    bucket._last = t0

    # 1회차: t0 그대로 → 토큰 없음 → sleep(0.5)
    # 2회차: t0+0.5 → 토큰 1.0 보충 → 통과
    with patch("app.services.agents._base.time.monotonic", side_effect=[t0, t0 + 0.5]):
        with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await bucket.acquire()

    mock_sleep.assert_called_once_with(pytest.approx(0.5, abs=0.01))
    assert bucket._tokens == pytest.approx(0.0, abs=0.01)


async def test_wait_time_proportional_to_deficit():
    """부족한 토큰량에 비례해 대기 시간이 계산된다.

    rate=4.0, tokens=0.5 → 부족량=0.5 → wait = 0.5/4.0 = 0.125s
    """
    bucket = _TokenBucket(rate=4.0, capacity=2.0)
    bucket._tokens = 0.5
    t0 = 1000.0
    bucket._last = t0

    with patch("app.services.agents._base.time.monotonic", side_effect=[t0, t0 + 0.125]):
        with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await bucket.acquire()

    mock_sleep.assert_called_once_with(pytest.approx(0.125, abs=0.005))


# ── 토큰 보충 ─────────────────────────────────────────────────────────────────

async def test_tokens_replenish_over_elapsed_time():
    """시간이 지나면 rate에 비례해 토큰이 보충된다.

    rate=1.0, 3초 경과 → 3토큰 보충 → sleep 없이 즉시 통과
    """
    bucket = _TokenBucket(rate=1.0, capacity=5.0)
    bucket._tokens = 0.0
    t0 = 1000.0
    bucket._last = t0

    with patch("app.services.agents._base.time.monotonic", return_value=t0 + 3.0):
        with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await bucket.acquire()

    mock_sleep.assert_not_called()
    assert bucket._tokens == pytest.approx(2.0)  # 3 보충 - 1 소비 = 2


async def test_tokens_capped_at_capacity():
    """오랜 유휴 후에도 capacity를 초과하지 않는다.

    rate=10.0, capacity=5.0, 100초 경과 → 이론상 1000토큰이지만 cap=5
    """
    bucket = _TokenBucket(rate=10.0, capacity=5.0)
    bucket._tokens = 0.0
    t0 = 1000.0
    bucket._last = t0

    with patch("app.services.agents._base.time.monotonic", return_value=t0 + 100.0):
        with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock):
            await bucket.acquire()

    assert bucket._tokens == pytest.approx(4.0)  # min(5, 1000) - 1 = 4


# ── settings 연동 ─────────────────────────────────────────────────────────────

def test_llm_bucket_rate_matches_vertex_ai_rpm():
    """모듈 레벨 _llm_bucket이 VERTEX_AI_RPM으로 초기화됐는지 확인."""
    from app.core.config import settings

    assert _llm_bucket._rate == pytest.approx(settings.VERTEX_AI_RPM / 60)
    assert _llm_bucket._capacity == pytest.approx(max(5.0, settings.VERTEX_AI_RPM / 10))


# ── run_with_retry 통합 ────────────────────────────────────────────────────────

async def test_run_with_retry_calls_acquire_once_even_with_retries():
    """429 재시도가 있어도 acquire는 최초 1회만 호출된다.

    재시도는 백오프 sleep이 간격을 보장하므로 acquire 중복 불필요.
    """
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(side_effect=[
        Exception("429 RESOURCE_EXHAUSTED"),
        MagicMock(output="ok"),
    ])

    acquire_count = 0

    async def fake_acquire():
        nonlocal acquire_count
        acquire_count += 1

    with patch.object(_llm_bucket, "acquire", side_effect=fake_acquire):
        with patch("app.services.agents._base.asyncio.sleep", new_callable=AsyncMock):
            with patch("app.services.agents._base.random.uniform", return_value=0.0):
                await run_with_retry(mock_agent, "프롬프트", role="test")

    assert acquire_count == 1        # acquire는 1회
    assert mock_agent.run.call_count == 2  # LLM 호출은 2회 (초기 + 재시도)


async def test_run_with_retry_each_independent_call_acquires_separately():
    """독립적인 두 요청은 각각 acquire를 호출해 rate limit을 개별 적용받는다."""
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=MagicMock(output="ok"))

    acquire_count = 0

    async def fake_acquire():
        nonlocal acquire_count
        acquire_count += 1

    with patch.object(_llm_bucket, "acquire", side_effect=fake_acquire):
        await run_with_retry(mock_agent, "첫 번째 요청", role="test")
        await run_with_retry(mock_agent, "두 번째 요청", role="test")

    assert acquire_count == 2
