from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from typing import Optional

class Settings(BaseSettings):
    # --- LLM Provider ---
    # "openai" | "gemini" 중 하나. .env에서만 변경한다.
    LLM_PROVIDER: str = "openai"
    # None이면 provider 기본값 사용 (openai: gpt-4o / gpt-4o-mini, gemini: gemini-2.5-pro / gemini-2.0-flash)
    ORCHESTRATOR_MODEL: Optional[str] = None
    PREPROCESSOR_MODEL: Optional[str] = None

    GPT_API_KEY: Optional[str] = None      # LLM_PROVIDER="openai" 일 때 필요
    GOOGLE_API_KEY: Optional[str] = None   # LLM_PROVIDER="gemini" 일 때 필요

    # --- Database & Redis ---
    DB_USER: str
    DB_PASSWORD: str
    DB_HOST: str
    DB_PORT: int  # 포트는 숫자이므로 int로 설정
    DB_NAME: str
    
    REDIS_HOST: str
    REDIS_PASSWORD: str
    REDIS_PORT: int
    REDIS_URL: str

    # --- Server Settings ---
    PORT: int
    JAVA_BACKEND_URL: str

    # --- Duffel API ---
    DUFFEL_API_KEY: str

    # --- Tavily Search API ---
    TAVILY_API_KEY: str

    # --- Google Maps API ---
    GOOGLE_MAPS_API_KEY: str
    
    # CLERK KEY
    CLERK_ISSUER: str
    CLERK_JWKS_URL: str

    # --- Internal Token (Spring Boot ↔ FastAPI 서버 간 인증) ---
    INTERNAL_TOKEN: str

    # --- OpenAI Embeddings ---
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"


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