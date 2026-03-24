from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from typing import Optional

class Settings(BaseSettings):
    # --- OpenAI & LangChain ---
    GOOGLE_API_KEY: str
    LANGCHAIN_TRACING_V2: str   
    LANGCHAIN_ENDPOINT: str  
    LANGCHAIN_API_KEY: str
    LANGCHAIN_PROJECT: str

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