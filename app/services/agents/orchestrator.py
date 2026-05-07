# app/services/agents/orchestrator.py
from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext

from app.schemas.ai_message import OrchestratorResult
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
    current_itinerary: dict | None  # 현재 여행 일정 (DB read-only)
    request_type: str               # classification_agent 판별 결과

# ---------------------------------------------------------------------------
# 오케스트레이터 에이전트
# ---------------------------------------------------------------------------

orchestrator_agent = Agent(
    model=_build_model("orchestrator"),
    deps_type=OrchestratorDeps,
    result_type=OrchestratorResult,
)

# ---------------------------------------------------------------------------
# 동적 시스템 프롬프트
# ---------------------------------------------------------------------------

_TYPE_INSTRUCTIONS: dict[str, str] = {
    "itinerary": """\
## 이번 요청: 여행 일정 생성/수정 (itinerary)

**[응답 형식 — 반드시 준수]**
반환 JSON의 필드를 아래와 같이 채워야 한다:
- `day_plans`: 날짜별 일정 (키='YYYY-MM-DD'). 모든 날짜 필수.
- `message`: 일정 핵심 포인트 소개 (내용 재나열 금지, 2~3문장)
- `ai_summary`: 한 문장 요약. 예) "도쿄 3박4일 일정 생성. 성인 2명."

처리:
1. current_itinerary(여행 기본 정보)가 있으면 반드시 참고한다.
2. current_itinerary가 없거나 destination·start_date가 비어있으면 일정을 생성하지 말고,
   사용자에게 여행지·날짜·인원·예산을 먼저 물어봐라. (day_plans는 null로 둔다)
3. 기본 정보가 모두 있으면 get_weather, search_web, search_place, find_route 도구를 활용해 일정을 구성한다.
4. 기존 day_plans가 있으면 사용자 요청에 따라 해당 부분만 수정하고 나머지는 유지한다.""",

    "change": """\
## 이번 요청: 여행 기본 정보 변경 (change)

**[응답 형식 — 반드시 준수]**
반환 JSON의 필드를 아래와 같이 채워야 한다:
- `change`: 변경된 필드만 포함 (변경하지 않은 필드는 null)
  가능한 필드: start_date, end_date, budget, adult_count, child_count, child_ages
- `message`: 변경 완료 확인 메시지
- `ai_summary`: "여행 날짜를 5월 1일~4일로 변경." 형식 한 문장 요약

처리:
1. 외부 API 도구는 호출하지 않는다.
2. 사용자 메시지에서 변경된 필드만 추출하여 change 필드에 작성한다.""",

    "reservation": """\
## 이번 요청: 예약 (reservation)

**[응답 형식 — 반드시 준수]**
반환 JSON의 필드를 아래와 같이 채워야 한다:
- `reservation`: 예약 정보 (reservation_type, detail, total_price, currency 등)
- `message`: 예약 완료 안내 메시지

처리:
1. 항공권이면 book_flight, 숙소이면 book_hotel을 호출한다.
   (현재 미구현 — status: todo 반환. 향후 Duffel booking API 연결 예정)
2. 결과를 reservation 필드에 작성한다.""",

    "cancel": """\
## 이번 요청: 예약 취소 (cancel)

**[응답 형식 — 반드시 준수]**
반환 JSON의 필드를 아래와 같이 채워야 한다:
- `cancel`: 취소 정보 (reservation_id, cancelled_at)
- `message`: 취소 접수 완료 안내 메시지

처리:
1. 항공권이면 cancel_flight, 숙소이면 cancel_hotel을 호출한다.
   (현재 미구현 — status: todo 반환. 향후 Duffel cancel API 연결 예정)
2. 결과를 cancel 필드에 작성한다.""",

    "chat": """\
## 이번 요청: 일반 대화/질문 (chat)

**[응답 형식]**
- `message`: 친절하고 유익한 텍스트 응답
- day_plans·change·reservation·cancel 필드는 null로 둔다.
- 질문 내용에 따라 search_web, get_weather 등 도구를 활용한다.""",
}

_MEMORY_INSTRUCTION = """\
## 메모리 업데이트
- itinerary·change 처리 후에는 `ai_summary` 필드에 반드시 작성한다.
  **이전 대화 요약(## 이전 대화 요약)이 있으면 그 내용과 이번 대화를 합쳐 전체 대화 흐름을 하나의 요약으로 다시 작성한다.**
  예) 이전: "제주도 3박4일 일정 생성. 5월 1일~3일." + 현재 변경 →
      결과: "제주도 3박4일 일정 생성. 5월 1일~3일. 숙소를 한림으로 변경 요청."
- 새로운 취향이 감지되면 `preferences` 필드를 채운다 (음식·이동 수단·숙박 스타일 등).
  **기존 preferences(## 사용자 취향)가 있으면 그 내용을 그대로 포함하고 새 항목을 추가/수정한 전체 dict를 반환한다.**
- chat·reservation·cancel에서 변화 없으면 ai_summary·preferences는 null로 둔다."""


@orchestrator_agent.system_prompt
async def build_system_prompt(ctx: RunContext[OrchestratorDeps]) -> str:
    deps = ctx.deps
    sections: list[str] = []

    sections.append(
        "당신은 여행 계획 전문 AI 어시스턴트입니다.\n"
        "사용자 요청에 따라 적절한 도구를 활용하고, 구조화된 JSON(OrchestratorResult 형식)으로 응답합니다."
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
    "tavily_search": TavilySearchAdapter(),
    "weather":       WeatherAdapter(),
    "google_maps":   GoogleMapsAdapter(),
})

# ---------------------------------------------------------------------------
# 도구 등록
# ---------------------------------------------------------------------------

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

    [다중 지역 호출 패턴] 여행 중 도시 이동이 있으면 지역별로 체류 기간만큼 분리 호출:
      1~2일차 도쿄: get_weather("Tokyo", 2)
      3~4일차 오사카: get_weather("Osaka", 2)
    단일 도시 전체 기간: get_weather("Tokyo", 4)  ← 3박 4일

    - city: 반드시 영문 도시명. 예) "Seoul", "Tokyo", "Osaka" (한국어 입력 시 에러)
    - forecast_days: 1~16 사이. 여행 기간 일수와 일치시킬 것.
    - 반환: {forecast_type="daily", data: [{date, temperature_max, temperature_min, precipitation_probability_max, weather}]}
    - 날씨 결과를 각 날짜 일정에 반영: 강수확률 50% 이상이면 실내 활동 우선
    """
    return await _service.process_task("weather", "get_weather", {
        "city": city,
        "forecast_days": forecast_days,
    })


@orchestrator_agent.tool_plain
async def get_historical_weather(city: str, start_date: str, end_date: str) -> dict:
    """과거/장기 날씨 조회. 다음 두 경우에 사용:
    (1) 여행일이 오늘부터 16일 초과인 미래 — 작년 같은 기간 데이터를 참고용으로 사용
    (2) 여행일이 이미 지난 날짜 — 그 기간의 실제 날씨 데이터 조회

    [다중 지역 호출 패턴] 도시 이동이 있으면 지역별로 분리 호출:
      1~2일차 도쿄(2026-08-01~02): get_historical_weather("Tokyo", "2025-08-01", "2025-08-02")
      3~4일차 오사카(2026-08-03~04): get_historical_weather("Osaka", "2025-08-03", "2025-08-04")

    - city: 반드시 영문 도시명. 예) "Seoul", "Tokyo" (한국어 입력 시 에러)
    - start_date/end_date:
        미래 여행: 여행 날짜의 작년 같은 기간. 예) 여행 2026-08-01~05 → "2025-08-01", "2025-08-05"
        과거 여행: 여행 날짜 그대로. 예) 여행 2026-05-01~03 → "2026-05-01", "2026-05-03"
    - 반환: {forecast_type="historical", data: [{date, temperature_max, temperature_min, precipitation_sum, weather, uv_index_max}]}
    - 날씨 결과를 각 날짜 일정에 반영: 강수 가능성 높으면 실내 활동 우선
    """
    return await _service.process_task("weather", "get_historical_weather", {
        "city": city,
        "start_date": start_date,
        "end_date": end_date,
    })


@orchestrator_agent.tool_plain
async def find_route(origin: str, dest: str, mode: str = "transit") -> dict:
    """Google Maps 경로 및 소요 시간 조회. 하루 일정의 연속 방문 장소 쌍마다 각각 호출한다.

    [필수 호출 패턴] 하루에 A→B→C→D를 방문하면 반드시 3번 호출:
      find_route(A, B), find_route(B, C), find_route(C, D)
    이동 시간을 각 항목의 time 필드에 반영하여 현실적인 시간표를 구성한다.

    - origin/dest: 영문 장소명 + 도시명. 예) "Senso-ji Temple, Tokyo", "Shinjuku Station, Tokyo"
    - mode: "transit"(대중교통, 기본값) / "walking"(도보, 1km 이내) / "driving" / "bicycling"
    - 반환: {status, data: {routes: [{distance, duration, steps}]}}
    - 이동 소요 시간을 일정 time에 반영: 예) 이동 30분이면 앞 일정 종료 후 30분 버퍼 추가
    """
    return await _service.process_task("google_maps", "find_route", {
        "origin": origin,
        "dest": dest,
        "mode": mode,
    })


@orchestrator_agent.tool_plain
async def search_place(query: str) -> dict:
    """Google Maps 장소 검색. 방문 예정인 관광지·식당·카페를 개별 검색하여 위치·평점 확인.

    - query: 구체적인 장소명 또는 키워드. 예) "Senso-ji Temple Tokyo", "도쿄 신주쿠 라멘 맛집"
    - 검색 결과의 rating·user_ratings_total로 장소 품질 판단. 평점 3.5 미만이면 대안 검색 권장.
    - 반환: {status, data: {places: [{name, formatted_address, lat, lng, rating, user_ratings_total, types}]}}
    - 확인한 장소명·주소를 find_route 호출 시 origin/dest로 사용
    """
    return await _service.process_task("google_maps", "search_place", {
        "query": query,
    })


# ---------------------------------------------------------------------------
# 예약/취소 실행 도구 (Duffel API 미구현 — placeholder)
# ---------------------------------------------------------------------------

@orchestrator_agent.tool_plain
async def book_flight(
    origin: str,
    destination: str,
    departure_date: str,
    adults: int = 1,
    children: int = 0,
    child_ages: list[int] | None = None,
) -> dict:
    """항공권 검색 + 예약을 한 번에 처리. reservation 타입 전용.

    내부 동작: search_flights로 옵션 조회 → 최적 항공편 선택 → Duffel create_order 호출
    LLM이 search/book을 분리해서 호출할 필요 없이 이 도구 하나로 완료.

    - origin/destination: 영문 도시명 또는 IATA 코드. 예) "Seoul", "ICN", "Tokyo", "NRT"
    - departure_date: YYYY-MM-DD 형식
    - children >= 1이면 child_ages 개수 일치 필요. 예) children=2, child_ages=[5, 8]
    - 반환: {status: "todo"} — FlightAdapter.create_order 연결 후 실제 예약 처리 예정
    """
    return {"status": "todo", "message": "항공권 예약 API는 현재 개발 중입니다."}


@orchestrator_agent.tool_plain
async def book_hotel(
    city_name: str,
    check_in: str,
    check_out: str,
    adults: int = 1,
    rooms: int = 1,
    children: int = 0,
    child_ages: list[int] | None = None,
) -> dict:
    """숙소 검색 + 예약을 한 번에 처리. reservation 타입 전용.

    내부 동작: search_hotels로 옵션 조회 → 최적 숙소 선택 → Duffel create_booking 호출
    LLM이 search/book을 분리해서 호출할 필요 없이 이 도구 하나로 완료.

    - city_name: 영문 또는 한글 도시명. 예) "Tokyo", "도쿄"
    - check_in/check_out: YYYY-MM-DD 형식
    - children >= 1이면 child_ages 개수 일치 필요. 예) children=1, child_ages=[7]
    - 반환: {status: "todo"} — AccommodationAdapter.create_booking 연결 후 실제 예약 처리 예정
    """
    return {"status": "todo", "message": "숙소 예약 API는 현재 개발 중입니다."}


@orchestrator_agent.tool_plain
async def cancel_flight(order_id: str) -> dict:
    """항공권 예약 취소. Duffel Air order_id로 항공권 취소 요청.

    - order_id: 취소할 항공권 예약의 Duffel order ID (book_flight 또는 예약 완료 시 반환된 ID)
    - 반환: {status: "todo"} — FlightAdapter.cancel_booking 연결 후 실제 취소 처리 예정
    """
    return {"status": "todo", "message": "항공권 취소 API는 현재 개발 중입니다."}


@orchestrator_agent.tool_plain
async def cancel_hotel(booking_id: str) -> dict:
    """숙소 예약 취소. Duffel Stays booking_id로 숙소 취소 요청.

    - booking_id: 취소할 숙소 예약의 Duffel booking ID (book_hotel 또는 예약 완료 시 반환된 ID)
    - 반환: {status: "todo"} — AccommodationAdapter.cancel_booking 연결 후 실제 취소 처리 예정
    """
    return {"status": "todo", "message": "숙소 취소 API는 현재 개발 중입니다."}


