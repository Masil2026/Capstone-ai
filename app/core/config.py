from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from typing import Optional

class Settings(BaseSettings):
    # --- LLM Provider ---
    # "vertexai" 고정. .env에서만 변경한다.
    LLM_PROVIDER: str = "vertexai"
    # None이면 provider 기본값 사용 (vertexai: gemini-3.1-pro-preview / gemini-3.5-flash)
    ORCHESTRATOR_MODEL: Optional[str] = None
    PREPROCESSOR_MODEL: Optional[str] = None

    GOOGLE_CLOUD_PROJECT: Optional[str] = None   # Vertex AI 프로젝트 ID
    GOOGLE_CLOUD_REGION: str = "global"          # LLM 리전 (gemini-3.1-pro-preview는 global 필요)
    GOOGLE_CLOUD_EMBEDDING_REGION: str = "us-central1"  # 임베딩 리전 (text-embedding-004는 regional 필요)

    # --- Database & Redis ---
    DB_USER: str
    DB_PASSWORD: str
    DB_HOST: str
    DB_PORT: int  # 포트는 숫자이므로 int로 설정
    DB_NAME: str

    REDIS_HOST: str
    REDIS_PORT: int
    REDIS_PASSWORD: Optional[str] = None  # Upstash 전용 — Docker Redis는 미사용
    REDIS_URL: Optional[str] = None       # Upstash 전용 — Docker Redis는 미사용

    # --- Server Settings ---
    PORT: int
    JAVA_BACKEND_URL: str

    # --- Duffel API ---
    DUFFEL_API_KEY: str

    # --- Tavily Search API ---
    TAVILY_API_KEY: str

    # --- Google Maps API ---
    GOOGLE_MAPS_API_KEY: str

    # --- 한국관광공사 TourAPI (data.go.kr) ---
    # 공공데이터포털에서 발급받은 "Decoding" 키 사용 (httpx가 자동 URL 인코딩 → 인코딩 키 쓰면 이중 인코딩됨)
    KOREA_TOURISM_API_KEY: Optional[str] = None

    # --- Booking.com (RapidAPI booking-com15) ---
    BOOKING_API_KEY: Optional[str] = None

    # CLERK KEY
    CLERK_ISSUER: str
    CLERK_JWKS_URL: str

    # --- Internal Token (Spring Boot ↔ FastAPI 서버 간 인증) ---
    INTERNAL_TOKEN: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True
    )

# 싱글톤 패턴: 캐시를 사용하여 설정을 한 번만 로드함
@lru_cache()
def get_settings():
    return Settings()

# 프로젝트 어디서든 import settings로 사용 가능
settings = get_settings()
