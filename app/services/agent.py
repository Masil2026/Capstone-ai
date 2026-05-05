# app/services/agent.py
from __future__ import annotations

from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelMessage
from redis import asyncio as aioredis
import json
from datetime import datetime, timezone
from app.core.config import settings
from app.schemas.ai_message import ResponseClassification

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
# OrchestratorDeps — 매 요청마다 시스템 프롬프트에 주입되는 컨텍스트
# ---------------------------------------------------------------------------

@dataclass
class OrchestratorDeps:
    ai_summary: str | None          # 이전 대화 전체 요약 (Redis memory)
    preferences: dict | None        # 사용자 취향 JSON (Redis memory)
    today: str                      # YYYY-MM-DD — 날짜 계산 기준
    similar_messages: list[dict]    # pgvector 유사 과거 메시지 최대 5개
    current_itinerary: dict | None  # 현재 여행 일정 dayPlans (DB read-only, roomId 기준)

# ---------------------------------------------------------------------------
# 에이전트
# ---------------------------------------------------------------------------

# 전처리 에이전트 — 비정형 데이터(Tavily) 전처리·요약 전용
preprocessor_agent = Agent(model=_build_model("preprocessor"))

# 오케스트레이터 에이전트 — 의도 파악·도구 선택·최종 응답 생성
orchestrator_agent = Agent(model=_build_model("orchestrator"), deps_type=OrchestratorDeps)

# 타입 판별 에이전트 — 스트리밍 완료 후 응답 의도 분류 (구조화 출력, 스트리밍 없음)
_CLASSIFICATION_SYSTEM_PROMPT = """\
당신은 여행 AI 어시스턴트(orchestrator)가 이미 완료한 작업의 응답 텍스트를 분석하여
어떤 작업이 수행됐는지 분류하고, 응답에 포함된 구조화 데이터를 추출하는 역할입니다.
직접 무언가를 실행하거나 생성하지 않습니다. 오직 orchestrator의 응답을 보고 분류·추출합니다.

## 분류 기준

| type | 기준 |
|------|------|
| itinerary | orchestrator가 여행 일정을 신규 생성하거나 기존 일정을 수정한 경우 |
| change | orchestrator가 여행 날짜·예산·인원(성인 수·아이 수·아이 나이)을 변경한 경우 (목적지 변경 없음) |
| reservation | orchestrator가 항공권 또는 숙소 예약을 완료한 경우 |
| cancel | orchestrator가 예약을 취소 완료한 경우 |
| chat | 위 4가지에 해당하지 않는 일반 대화·질문·정보 제공 |

## 추출 규칙

- **itinerary**: type만 "itinerary"로 설정. dayPlans는 orchestrator가 submit_itinerary 도구로 이미 별도 전달했으므로 여기서 추출하지 않는다.
- **change**: orchestrator 응답에서 변경된 값만 추출. 언급되지 않은 필드는 null. 추출 가능 필드: startDate(YYYY-MM-DD), endDate(YYYY-MM-DD), budget(숫자), adultCount, childCount, childAges(나이 배열).
- **reservation**: orchestrator가 예약 완료 후 응답에 포함한 예약 정보를 reservation 객체로 추출.
- **cancel**: orchestrator 응답에서 취소된 reservationId와 cancelledAt을 추출.
- **chat**: 타입별 조건부 필드는 모두 null.

## 메모리 갱신 규칙

- ai_summary: 이번 대화 전체를 반영한 새 요약. 새롭게 기억할 정보가 없으면 null.
- preferences: orchestrator 응답에서 감지된 사용자 취향 전체 (기존 + 신규 병합). 변화 없으면 null.
- type이 chat이더라도 취향 정보가 발견되면 갱신한다.
"""

classification_agent = Agent(
    model=_build_model("preprocessor"),
    result_type=ResponseClassification,
    system_prompt=_CLASSIFICATION_SYSTEM_PROMPT,
)

# ---------------------------------------------------------------------------
# Redis 클라이언트
# ---------------------------------------------------------------------------

_redis = aioredis.Redis(
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

async def load_history(chat_room_id: str) -> list[ModelMessage]:
    """Redis에서 대화 히스토리 로드"""
    data = await _redis.get(f"chat_history:{chat_room_id}")
    if not data:
        return []
    return ModelMessagesTypeAdapter.validate_json(data)


async def save_history(chat_room_id: str, messages: list[ModelMessage]) -> None:
    """대화 히스토리를 Redis에 저장. MAX_HISTORY_MESSAGES 초과 시 오래된 메시지부터 제거"""
    trimmed = messages[-MAX_HISTORY_MESSAGES:]
    await _redis.set(f"chat_history:{chat_room_id}", ModelMessagesTypeAdapter.dump_json(trimmed))


# ---------------------------------------------------------------------------
# 장기 메모리 (memory:{chat_room_id})
# ---------------------------------------------------------------------------
# 구조: { "ai_summary": str, "preferences": dict, "loaded_at": ISO 8601 }
#
# TODO: load_memory 호출 시 chat_rooms.updated_at을 Java 백엔드에서 받아와
#       memory.loaded_at과 비교 후 재로딩 여부를 판단해야 함.
#       현재는 Redis에 데이터가 없을 때만 DB에서 로드하는 단순 구조로 구현.

async def load_memory(chat_room_id: str) -> dict | None:
    """Redis에서 장기 메모리(ai_summary, preferences) 로드"""
    data = await _redis.get(f"memory:{chat_room_id}")
    if not data:
        return None
    return json.loads(data)


async def save_memory(chat_room_id: str, ai_summary: str, preferences: dict) -> None:
    """장기 메모리를 Redis에 저장. loaded_at을 현재 시각으로 기록"""
    payload = {
        "ai_summary": ai_summary,
        "preferences": preferences,
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    }
    await _redis.set(f"memory:{chat_room_id}", json.dumps(payload, ensure_ascii=False))


async def is_memory_stale(chat_room_id: str, db_updated_at: datetime) -> bool:
    """DB의 updated_at이 Redis memory.loaded_at보다 최신이면 True(재로딩 필요)"""
    memory = await load_memory(chat_room_id)
    if not memory:
        return True
    loaded_at = datetime.fromisoformat(memory["loaded_at"])
    return db_updated_at > loaded_at


# ---------------------------------------------------------------------------
# 어댑터 싱글턴 + 서비스
# ---------------------------------------------------------------------------

from app.services.adapters.flight_api import FlightAdapter
from app.services.adapters.accommodation_api import AccommodationAdapter
from app.services.adapters.tavily_search import TavilySearchAdapter
from app.services.adapters.weather_api import WeatherAdapter
from app.services.adapters.google_maps import GoogleMapsAdapter
from app.services.travel_agent_service import TravelAgentService

_service = TravelAgentService({
    "duffel_flight":         FlightAdapter(),
    "duffel_accommodation":  AccommodationAdapter(),
    "tavily_search":         TavilySearchAdapter(),
    "weather":               WeatherAdapter(),
    "google_maps":           GoogleMapsAdapter(),
})

# ---------------------------------------------------------------------------
# orchestrator_agent 도구 등록
# ---------------------------------------------------------------------------

@orchestrator_agent.tool_plain
async def search_flights(
    origin: str,
    destination: str,
    departure_date: str,
    adults: int = 1,
    children: int = 0,
    child_ages: list[int] | None = None,
) -> dict:
    """항공권 검색. origin/destination은 도시명 또는 IATA 코드 모두 허용."""
    return await _service.process_task("duffel_flight", "search_flights", {
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "adults": adults,
        "children": children,
        "child_ages": child_ages or [],
    })


@orchestrator_agent.tool_plain
async def search_hotels(
    city_name: str,
    check_in: str,
    check_out: str,
    adults: int = 1,
    rooms: int = 1,
    children: int = 0,
    child_ages: list[int] | None = None,
) -> dict:
    """숙소 검색. check_in/check_out은 YYYY-MM-DD 형식."""
    return await _service.process_task("duffel_accommodation", "search_hotels", {
        "city_name": city_name,
        "check_in": check_in,
        "check_out": check_out,
        "adults": adults,
        "rooms": rooms,
        "children": children,
        "child_ages": child_ages or [],
    })


@orchestrator_agent.tool_plain
async def search_web(
    query: str,
    search_depth: str = "basic",
    max_results: int = 15,
) -> dict:
    """Tavily 웹 검색. 여행지 정보·뉴스·트렌드 등 비정형 정보 수집."""
    return await _service.process_task("tavily_search", "search", {
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
    })


@orchestrator_agent.tool_plain
async def get_weather(city: str, forecast_days: int = 7) -> dict:
    """날씨 예보 조회. city는 영문 도시명, forecast_days는 1~16일."""
    return await _service.process_task("weather", "get_weather", {
        "city": city,
        "forecast_days": forecast_days,
    })


@orchestrator_agent.tool_plain
async def get_historical_weather(city: str, start_date: str, end_date: str) -> dict:
    """과거 날씨 조회 (여행일이 16일 초과일 때 작년 같은 시기 참고). 날짜 형식 YYYY-MM-DD."""
    return await _service.process_task("weather", "get_historical_weather", {
        "city": city,
        "start_date": start_date,
        "end_date": end_date,
    })


@orchestrator_agent.tool_plain
async def find_route(origin: str, dest: str, mode: str = "transit") -> dict:
    """Google Maps 경로 조회. mode: transit(대중교통)/driving/walking/bicycling."""
    return await _service.process_task("google_maps", "find_route", {
        "origin": origin,
        "dest": dest,
        "mode": mode,
    })


@orchestrator_agent.tool_plain
async def search_place(query: str) -> dict:
    """Google Maps 장소 검색. 식당·관광지·숙소 등 장소 정보 조회."""
    return await _service.process_task("google_maps", "search_place", {
        "query": query,
    })
