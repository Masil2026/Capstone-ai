# app/services/agents/orchestrator.py
from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent

from app.schemas.ai_message import DayPlanItem
from app.services.adapters.flight_api import FlightAdapter
from app.services.adapters.accommodation_api import AccommodationAdapter
from app.services.adapters.tavily_search import TavilySearchAdapter
from app.services.adapters.weather_api import WeatherAdapter
from app.services.adapters.google_maps import GoogleMapsAdapter
from app.services.travel_agent_service import TravelAgentService
from ._base import _build_model, preprocessor_agent

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
    request_type: str               # classification_agent 판별 결과

# ---------------------------------------------------------------------------
# 오케스트레이터 에이전트
# ---------------------------------------------------------------------------

orchestrator_agent = Agent(model=_build_model("orchestrator"), deps_type=OrchestratorDeps)

# ---------------------------------------------------------------------------
# 어댑터 싱글턴 + 서비스
# ---------------------------------------------------------------------------

_service = TravelAgentService({
    "duffel_flight":        FlightAdapter(),
    "duffel_accommodation": AccommodationAdapter(),
    "tavily_search":        TavilySearchAdapter(),
    "weather":              WeatherAdapter(),
    "google_maps":          GoogleMapsAdapter(),
})

# ---------------------------------------------------------------------------
# 도구 등록
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
    """Tavily 웹 검색 후 preprocessor_agent로 요약. 여행지 정보·뉴스·트렌드 등 비정형 정보 수집."""
    raw = await _service.process_task("tavily_search", "search", {
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
    })
    if raw.get("status") != "success":
        return raw

    results = raw.get("data", [])
    filtered = [r for r in results if r.get("score", 0) >= 0.5][:10]
    if not filtered:
        return {"status": "success", "summary": "관련 정보를 찾지 못했습니다.", "source_count": 0}

    snippets = "\n\n".join(
        f"[{r['title']}]\n{r['content']}" for r in filtered
    )
    result = await preprocessor_agent.run(
        f"아래 검색 결과를 여행 계획에 유용한 핵심 정보 위주로 간결하게 요약해줘.\n\n{snippets}"
    )
    return {"status": "success", "summary": result.data, "source_count": len(filtered)}


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


@orchestrator_agent.tool_plain
async def submit_itinerary(day_plans: dict[str, list[DayPlanItem]]) -> dict:
    """itinerary 타입 전용. 일정 생성/수정 완료 시 반드시 호출. 구조화된 dayPlans를 시스템에 전달한다."""
    return {"status": "success", "message": "일정이 저장되었습니다."}


@orchestrator_agent.tool_plain
async def submit_change(
    start_date: str | None = None,
    end_date: str | None = None,
    budget: float | None = None,
    adult_count: int | None = None,
    child_count: int | None = None,
    child_ages: list[int] | None = None,
) -> dict:
    """change 타입 전용. 변경된 여행 기본 정보를 시스템에 전달한다. 변경된 필드만 포함."""
    return {"status": "success", "message": "변경 정보가 저장되었습니다."}


@orchestrator_agent.tool_plain
async def submit_reservation(
    reservation_type: str,
    detail: dict,
    booking_url: str | None = None,
    external_ref_id: str | None = None,
    total_price: float | None = None,
    currency: str | None = None,
    reserved_at: str | None = None,
) -> dict:
    """reservation 타입 전용. 예약 완료 후 예약 정보를 시스템에 전달한다."""
    return {"status": "success", "message": "예약 정보가 저장되었습니다."}


@orchestrator_agent.tool_plain
async def submit_cancel(reservation_id: str, cancelled_at: str) -> dict:
    """cancel 타입 전용. 취소 완료 후 취소 정보를 시스템에 전달한다."""
    return {"status": "success", "message": "취소 정보가 저장되었습니다."}


@orchestrator_agent.tool_plain
async def update_memory(
    ai_summary: str | None = None,
    preferences: dict | None = None,
) -> dict:
    """모든 타입 공통. 대화 중 기억할 정보(취향·요약)가 감지될 때 호출한다."""
    return {"status": "success", "message": "메모리가 갱신되었습니다."}
