# app/services/agents/orchestrator.py
from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext

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
# 동적 시스템 프롬프트
# ---------------------------------------------------------------------------

_TYPE_INSTRUCTIONS: dict[str, str] = {
    "itinerary": """\
## 이번 요청: 일정 생성/수정 (itinerary)

처리 순서:
1. current_itinerary가 있으면 기존 일정을 수정 기준으로 참고한다. 없으면 전체 일정을 새로 생성한다.
2. 필요한 정보를 수집한다.
   - search_web: 여행지 관광 정보·현지 팁·트렌드
   - get_weather / get_historical_weather: 여행 기간 날씨 (16일 초과면 작년 같은 시기 참고)
   - search_place: 식당·관광지 장소 정보
   - find_route: 장소 간 이동 경로·소요 시간
   - search_flights: 항공편 검색 (이동 구간이 포함된 일정일 때)
   - search_hotels: 숙소 검색 (숙박 정보가 필요할 때)
3. 일정이 완성되면 반드시 submit_itinerary(day_plans)를 호출한다.
   - day_plans 키 형식: "1일차", "2일차", ...
   - 각 항목 필드: plan_name, time("HH:MM ~ HH:MM"), place, note
4. 텍스트 응답으로 일정 요약과 주요 추천 이유를 설명한다.""",

    "change": """\
## 이번 요청: 여행 기본 정보 변경 (change)

처리 순서:
1. 외부 API 도구(search_flights, search_hotels, search_web 등)는 호출하지 않는다.
2. 사용자 메시지에서 변경된 필드만 추출한다.
   - 변경 가능 필드: start_date, end_date, budget, adult_count, child_count, child_ages
   - 변경하지 않은 필드는 전달하지 않는다.
3. submit_change(변경된 필드만 포함)를 호출한다.
4. 텍스트 응답으로 변경 내용을 확인해준다.""",

    "reservation": """\
## 이번 요청: 예약 (reservation)

처리 순서:
1. 항공권 요청이면 search_flights, 숙소 요청이면 search_hotels를 호출한다.
2. 검색 결과를 사용자에게 보여주고 선택을 유도한다.
3. 예약이 확정되면 submit_reservation(reservation_type, detail, booking_url, total_price, currency 등)을 호출한다.""",

    "cancel": """\
## 이번 요청: 예약 취소 (cancel)

처리 순서:
1. 취소할 예약 ID와 취소 시각을 확인한다.
2. submit_cancel(reservation_id, cancelled_at)을 호출한다.
3. 텍스트 응답으로 취소 완료를 안내한다.""",

    "chat": """\
## 이번 요청: 일반 대화/질문 (chat)

처리 순서:
1. submit_itinerary, submit_change, submit_reservation, submit_cancel은 호출하지 않는다.
2. 질문 내용에 따라 search_web, get_weather 등을 활용한다.
3. 친절하고 유익한 텍스트 응답을 제공한다.""",
}

_MEMORY_INSTRUCTION = """\
## 메모리 업데이트 (모든 타입 공통)
대화 중 사용자의 취향(음식 선호·이동 수단·숙박 스타일 등)이나 기억할 정보가 감지되면
update_memory(ai_summary=..., preferences=...)를 호출한다.
감지되지 않으면 호출하지 않는다."""


@orchestrator_agent.system_prompt
async def build_system_prompt(ctx: RunContext[OrchestratorDeps]) -> str:
    deps = ctx.deps
    sections: list[str] = []

    sections.append(
        "당신은 여행 계획 전문 AI 어시스턴트입니다.\n"
        "사용자 요청에 따라 적절한 도구를 선택하고, 자연스러운 텍스트 응답과 함께 "
        "구조화 데이터를 submit_* 도구로 시스템에 전달합니다."
    )
    sections.append(f"오늘 날짜: {deps.today}")
    sections.append(_TYPE_INSTRUCTIONS.get(deps.request_type, _TYPE_INSTRUCTIONS["chat"]))
    sections.append(_MEMORY_INSTRUCTION)

    if deps.ai_summary:
        sections.append(f"## 이전 대화 요약\n{deps.ai_summary}")

    if deps.preferences:
        sections.append(
            f"## 사용자 취향\n{json.dumps(deps.preferences, ensure_ascii=False, indent=2)}"
        )

    if deps.current_itinerary:
        sections.append(
            f"## 현재 여행 일정\n"
            f"{json.dumps(deps.current_itinerary, ensure_ascii=False, indent=2)}"
        )

    if deps.similar_messages:
        msgs = "\n".join(f"[{m['role']}] {m['content']}" for m in deps.similar_messages)
        sections.append(f"## 참고할 과거 대화\n{msgs}")

    return "\n\n".join(sections)


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
    """항공권 검색.

    - origin/destination: 영문 도시명(Seoul, Tokyo, Osaka) 또는 IATA 코드(ICN, NRT, KIX) 모두 허용
    - departure_date: YYYY-MM-DD 형식. 예) "2026-05-15"
    - children >= 1이면 child_ages에 각 아이 나이를 반드시 포함. 개수 불일치 시 에러.
      예) children=2, child_ages=[5, 8]
    - 반환: {status, count, data: [{airline, origin, destination, total_amount, stops, departing_at}]}
    """
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
    """숙소 검색.

    - city_name: 영문 또는 한글 도시명. 예) "Tokyo", "Osaka", "도쿄"
    - check_in/check_out: YYYY-MM-DD 형식. 예) check_in="2026-06-15", check_out="2026-06-18" (3박)
    - children >= 1이면 child_ages에 각 아이 나이를 반드시 포함. 개수 불일치 시 에러.
      예) children=1, child_ages=[7]
    - 반환: {status, count, data: [{name, price, rating, address}]} 최대 10개
    """
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
    """Tavily 웹 검색. 여행지 관광 정보·현지 팁·뉴스·트렌드 등 비정형 정보 수집 후 GPT-4o-mini로 요약 반환.

    - query: 검색어. 예) "오사카 3박 4일 여행 명소", "도쿄 5월 날씨 옷차림", "교토 맛집 트렌드"
    - search_depth: "basic"(크레딧 1) / "advanced"(크레딧 2, 더 깊은 검색). 일반 조회는 "basic" 사용.
    - 반환: {status, summary(핵심 정보 요약 텍스트), source_count}
    """
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
    """날씨 예보 조회. 여행일이 오늘부터 16일 이내일 때 사용.

    - city: 반드시 영문 도시명. 예) "Seoul", "Tokyo", "Osaka" (한국어 입력 시 에러)
    - forecast_days: 1~16 사이. 예) 3박 4일이면 4
    - 반환 (forecast_days <= 4): {forecast_type="hourly", data: [{time, temperature, apparent_temperature, precipitation_probability, weather, windspeed}]}
    - 반환 (forecast_days >= 5): {forecast_type="daily",  data: [{date, temperature_max, temperature_min, precipitation_probability_max, weather, uv_index_max}]}
    """
    return await _service.process_task("weather", "get_weather", {
        "city": city,
        "forecast_days": forecast_days,
    })


@orchestrator_agent.tool_plain
async def get_historical_weather(city: str, start_date: str, end_date: str) -> dict:
    """과거 날씨 조회. 여행일이 오늘부터 16일 초과일 때 작년 같은 시기 데이터를 참고용으로 사용.

    - city: 반드시 영문 도시명. 예) "Seoul", "Tokyo" (한국어 입력 시 에러)
    - start_date/end_date: YYYY-MM-DD 형식. 여행 날짜의 작년 같은 기간으로 설정.
      예) 여행이 2026-08-01~2026-08-05이면 start_date="2025-08-01", end_date="2025-08-05"
    - 반환: {forecast_type="historical", count, data: [{date, temperature_max, temperature_min, apparent_temperature_max, apparent_temperature_min, precipitation_sum, weather, uv_index_max}]}
    """
    return await _service.process_task("weather", "get_historical_weather", {
        "city": city,
        "start_date": start_date,
        "end_date": end_date,
    })


@orchestrator_agent.tool_plain
async def find_route(origin: str, dest: str, mode: str = "transit") -> dict:
    """Google Maps 경로 및 소요 시간 조회.

    - origin/dest: 영문 장소명 또는 주소. 예) origin="Gyeongbokgung Palace, Seoul", dest="Namsan Tower, Seoul"
    - mode: "transit"(대중교통, 기본값) / "driving"(자동차) / "walking"(도보) / "bicycling"(자전거)
    - 반환: {status, data: {count, routes: [{start_address, end_address, distance(텍스트), duration(텍스트), steps}]}}
    """
    return await _service.process_task("google_maps", "find_route", {
        "origin": origin,
        "dest": dest,
        "mode": mode,
    })


@orchestrator_agent.tool_plain
async def search_place(query: str) -> dict:
    """Google Maps 장소 검색. 식당·관광지·카페 등 위치·평점·주소 정보 조회.

    - query: 장소명 또는 키워드. 예) "신주쿠 라멘 맛집", "오사카 도톤보리", "교토 금각사"
    - 반환: {status, data: {count, places: [{name, formatted_address, lat, lng, rating, user_ratings_total, types}]}}
    """
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
