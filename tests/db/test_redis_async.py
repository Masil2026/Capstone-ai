import pytest
from redis.asyncio import Redis
from app.core.config import settings

@pytest.mark.asyncio
async def test_docker_redis_connection():
    """Docker Redis 연결 테스트 (비동기 방식)"""
    redis_client = Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT)

    try:
        # PING 날리기
        ping_response = await redis_client.ping()
        assert ping_response is True
        print("\n[Redis] Docker Redis 연결 성공!")
    finally:
        await redis_client.aclose()
