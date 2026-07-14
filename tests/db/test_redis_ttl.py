# tests/db/test_redis_ttl.py
"""
Redis 저장 시 TTL(8시간, 28800초)이 항상 적용되는지 검증한다. (이슈 #19)

- 단위(mock) 테스트: 실제 Redis 없이 각 save_* 함수가 ex=REDIS_TTL_SECONDS 인자로
  저장하는지 확인한다.
- 통합 테스트: 실제 Redis에 저장 후 TTL을 읽어와 28800초 근처에서 감소 중인지 확인한다.
  Redis가 떠 있지 않으면 자동 skip 된다. (`pytest -s`로 실측 TTL 값 출력)
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from redis.asyncio import Redis

from app.core.config import settings
from app.services.agents import memory


async def _fresh_redis() -> Redis:
    """TTL 조회·정리에 쓸 별도 Redis 클라이언트. 연결 불가 시 테스트를 skip 한다."""
    client = Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis가 실행 중이 아니라 통합 테스트를 건너뜁니다. (docker compose up -d)")
    return client


def test_ttl_setting_is_8_hours():
    assert settings.REDIS_TTL_SECONDS == 28800


@pytest.mark.asyncio
async def test_save_raw_history_applies_ttl():
    with patch.object(memory._redis, "set", new=AsyncMock()) as mock_set:
        await memory.save_raw_history("room-1", [{"role": "user", "content": "hi"}])

    mock_set.assert_awaited_once()
    assert mock_set.await_args.kwargs["ex"] == 28800
    assert mock_set.await_args.args[0] == "chatroom_history:room-1"


@pytest.mark.asyncio
async def test_save_pg_history_applies_ttl():
    with patch.object(memory._redis, "set", new=AsyncMock()) as mock_set:
        await memory.save_pg_history("room-1", [])

    mock_set.assert_awaited_once()
    assert mock_set.await_args.kwargs["ex"] == 28800
    assert mock_set.await_args.args[0] == "pgchatroom_history:room-1"


@pytest.mark.asyncio
async def test_save_memory_applies_ttl():
    with patch.object(memory._redis, "set", new=AsyncMock()) as mock_set:
        await memory.save_memory("room-1", "요약", {"budget": "economy"})

    mock_set.assert_awaited_once()
    assert mock_set.await_args.kwargs["ex"] == 28800
    assert mock_set.await_args.args[0] == "memory:room-1"


# ---------------------------------------------------------------------------
# 통합 테스트 — 실제 Redis에 저장 후 TTL 실측 (Redis 없으면 자동 skip)
# ---------------------------------------------------------------------------

@pytest.mark.slow  # asyncio.sleep으로 수 초 소요. CI가 느려지면 -m "not slow"로 제외.
@pytest.mark.asyncio
async def test_ttl_counts_down_from_28800_on_real_redis():
    """save 후 TTL이 8시간(28800초)에서 시작해 시간이 지나면 실제로 감소하는지 확인."""
    wait_seconds = 2
    client = await _fresh_redis()
    room_id = "ttl-integration-test"
    keys = [f"memory:{room_id}", f"chatroom_history:{room_id}"]
    try:
        await memory.save_memory(room_id, "요약", {"budget": "economy"})
        await memory.save_raw_history(room_id, [{"role": "user", "content": "hi"}])

        ttl_before = {key: await client.ttl(key) for key in keys}
        for key, ttl in ttl_before.items():
            print(f"\n[TTL 저장직후] {key} → {ttl}초")
            # 방금 저장했으므로 28800에서 몇 초 감소한 값(28800 이하, 28700 초과) 이어야 한다.
            assert 28700 < ttl <= 28800, f"{key}의 초기 TTL이 예상 범위를 벗어남: {ttl}"

        await asyncio.sleep(wait_seconds)

        for key in keys:
            ttl_after = await client.ttl(key)
            print(f"[TTL {wait_seconds}초 후] {key} → {ttl_after}초 (감소량 {ttl_before[key] - ttl_after}초)")
            # 시간이 흘렀으니 반드시 이전보다 줄어 있어야 한다.
            assert ttl_after < ttl_before[key], f"{key}의 TTL이 감소하지 않음: {ttl_before[key]} → {ttl_after}"
    finally:
        await client.delete(*keys)
        await client.aclose()
