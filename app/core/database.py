from sqlalchemy import create_engine, URL
from sqlalchemy.orm import sessionmaker, declarative_base
from app.core.config import settings
from sqlalchemy.pool import NullPool

# 정보를 쪼개서 안전하게 URL 객체 생성
connection_url = URL.create(
    drivername="postgresql",
    username=settings.DB_USER,
    password=settings.DB_PASSWORD,
    host=settings.DB_HOST,
    port=settings.DB_PORT,
    database=settings.DB_NAME,
)

# 1. 동기 DB 엔진 생성
engine = create_engine(
    connection_url,
    echo=True,
    poolclass=NullPool,
)

# 2. 동기 세션 생성기
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# 3. 모델(Entity)의 베이스 클래스
Base = declarative_base()

# 4. 의존성 주입(Dependency Injection)을 위한 함수
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()