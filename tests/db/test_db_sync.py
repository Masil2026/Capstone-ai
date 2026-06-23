import pytest
import os
from sqlalchemy import text
from app.core.database import SessionLocal, engine
from app.core.config import settings

def test_postgres_connection():
    """Docker PostgreSQL 연결 테스트 (동기 방식)"""
    # 1. 세션 생성
    db = SessionLocal()
    try:
        # 2. SELECT 1 쿼리로 DB 생존 확인 (동기 실행)
        result = db.execute(text("SELECT 1"))
        assert result.scalar() == 1
        print("\n[DB] Docker PostgreSQL 동기 연결 성공!")
    except Exception as e:
        print(f"\n[DB] 연결 실패: {e}")
        raise e
    finally:
        # 3. 세션 닫기
        db.close()
