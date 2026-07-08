from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from typing import Optional
import os

class Settings(BaseSettings):
    # --- LLM Provider ---
    # "vertexai" 고정. .env에서만 변경한다.
    LLM_PROVIDER: str = "vertexai"
    # None이면 provider 기본값 사용 (vertexai: gemini-2.5-pro / gemini-2.5-flash)
    ORCHESTRATOR_MODEL: Optional[str] = None
    PREPROCESSOR_MODEL: Optional[str] = None

    GOOGLE_CLOUD_PROJECT: Optional[str] = None
    GOOGLE_CLOUD_REGION: str = "us-central1"
    GOOGLE_APPLICATION_CREDENTIALS: Optional[str] = None  # GCP 서비스 계정 키 파일 경로

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

    # --- Duffel API (참고용/legacy — 현재 항공·숙소는 Booking으로 대체, FlightAdapter·AccommodationAdapter 전용) ---
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

    # --- Vertex AI Rate Limit ---
    VERTEX_AI_RPM: int = 100  # Vertex AI 분당 요청 쿼터 (GCP 콘솔 할당량과 맞춤)
    PREPROCESSOR_SKIP_MAX_LEN: int = 100  # 이 글자 수 이하면 전처리 LLM 호출 생략 (검색 실패 수준)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

# 싱글톤 패턴: 캐시를 사용하여 설정을 한 번만 로드함
@lru_cache()
def get_settings():
    return Settings()

# 프로젝트 어디서든 import settings로 사용 가능
settings = get_settings()

# google-auth가 os.environ에서 직접 읽으므로 명시적으로 설정
if settings.GOOGLE_APPLICATION_CREDENTIALS:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS
