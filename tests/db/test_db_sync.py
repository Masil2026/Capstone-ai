import pytest
from sqlalchemy import text
from app.core.database import AsyncSessionLocal

@pytest.mark.asyncio
async def test_postgres_connection():
    """Docker PostgreSQL 연결 테스트 (비동기 방식)"""
    async with AsyncSessionLocal() as db:
        result = await db.execute(text("SELECT 1"))
        assert result.scalar() == 1
        print("\n[DB] Docker PostgreSQL 비동기 연결 성공!")
