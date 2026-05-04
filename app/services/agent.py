# app/services/agent.py
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelMessage
import redis
import json
from datetime import datetime, timezone
from app.core.config import settings

# ---------------------------------------------------------------------------
# 모델 팩토리
# ---------------------------------------------------------------------------
# 모델/프로바이더를 바꾸려면 .env의 LLM_PROVIDER / ORCHESTRATOR_MODEL / PREPROCESSOR_MODEL만 수정하면 된다.
# 코드 변경 불필요.

_PROVIDER_DEFAULTS = {
    "openai":  {"orchestrator": "gpt-4.1",        "preprocessor": "gpt-4o-mini"},
    "gemini":  {"orchestrator": "gemini-2.5-pro", "preprocessor": "gemini-2.0-flash"},
}

def _build_model(role: str):
    """role: 'orchestrator' | 'preprocessor'"""
    provider = settings.LLM_PROVIDER
    defaults = _PROVIDER_DEFAULTS.get(provider)
    if defaults is None:
        raise ValueError(f"지원하지 않는 LLM_PROVIDER: {provider!r}. 'openai' 또는 'gemini'를 사용하세요.")

    if role == "orchestrator":
        model_name = settings.ORCHESTRATOR_MODEL or defaults["orchestrator"]
    elif role == "preprocessor":
        model_name = settings.PREPROCESSOR_MODEL or defaults["preprocessor"]
    else:
        raise ValueError(f"알 수 없는 role: {role!r}")

    if provider == "openai":
        from pydantic_ai.models.openai import OpenAIModel
        from pydantic_ai.providers.openai import OpenAIProvider
        return OpenAIModel(model_name, provider=OpenAIProvider(api_key=settings.GPT_API_KEY))

    # provider == "gemini"
    from pydantic_ai.models.gemini import GeminiModel
    from pydantic_ai.providers.google_gla import GoogleGLAProvider
    return GeminiModel(model_name, provider=GoogleGLAProvider(api_key=settings.GOOGLE_API_KEY))

# ---------------------------------------------------------------------------
# 에이전트
# ---------------------------------------------------------------------------

# 전처리 에이전트 — 비정형 데이터(Tavily) 전처리·요약 전용
preprocessor_agent = Agent(model=_build_model("preprocessor"))

# 오케스트레이터 에이전트 — 의도 파악·도구 선택·최종 응답 생성
orchestrator_agent = Agent(model=_build_model("orchestrator"))

# ---------------------------------------------------------------------------
# Redis 클라이언트
# ---------------------------------------------------------------------------

_redis = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    password=settings.REDIS_PASSWORD,
    ssl=True,
    decode_responses=False,
)

# 세션당 유지할 최대 메시지 수 — 초과 시 오래된 것부터 제거
MAX_HISTORY_MESSAGES = 20

# ---------------------------------------------------------------------------
# 대화 히스토리 (chat_history:{chat_room_id})
# ---------------------------------------------------------------------------
# TODO: chat_room_id는 chat_rooms 테이블의 PK(id)를 사용한다.
#       chat_rooms 테이블 구현 완료 후 호출부에서 chat_room_id를 전달받아야 함.

def load_history(chat_room_id: str) -> list[ModelMessage]:
    """Redis에서 대화 히스토리 로드"""
    data = _redis.get(f"chat_history:{chat_room_id}")
    if not data:
        return []
    return ModelMessagesTypeAdapter.validate_json(data)


def save_history(chat_room_id: str, messages: list[ModelMessage]) -> None:
    """대화 히스토리를 Redis에 저장. MAX_HISTORY_MESSAGES 초과 시 오래된 메시지부터 제거"""
    trimmed = messages[-MAX_HISTORY_MESSAGES:]
    _redis.set(f"chat_history:{chat_room_id}", ModelMessagesTypeAdapter.dump_json(trimmed))


# ---------------------------------------------------------------------------
# 장기 메모리 (memory:{chat_room_id})
# ---------------------------------------------------------------------------
# 구조: { "ai_summary": str, "preferences": dict, "loaded_at": ISO 8601 }
#
# TODO: load_memory 호출 시 chat_rooms.updated_at을 Java 백엔드에서 받아와
#       memory.loaded_at과 비교 후 재로딩 여부를 판단해야 함.
#       현재는 Redis에 데이터가 없을 때만 DB에서 로드하는 단순 구조로 구현.

def load_memory(chat_room_id: str) -> dict | None:
    """Redis에서 장기 메모리(ai_summary, preferences) 로드"""
    data = _redis.get(f"memory:{chat_room_id}")
    if not data:
        return None
    return json.loads(data)


def save_memory(chat_room_id: str, ai_summary: str, preferences: dict) -> None:
    """장기 메모리를 Redis에 저장. loaded_at을 현재 시각으로 기록"""
    payload = {
        "ai_summary": ai_summary,
        "preferences": preferences,
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    }
    _redis.set(f"memory:{chat_room_id}", json.dumps(payload, ensure_ascii=False))


def is_memory_stale(chat_room_id: str, db_updated_at: datetime) -> bool:
    """DB의 updated_at이 Redis memory.loaded_at보다 최신이면 True(재로딩 필요)"""
    memory = load_memory(chat_room_id)
    if not memory:
        return True
    loaded_at = datetime.fromisoformat(memory["loaded_at"])
    return db_updated_at > loaded_at
