# app/services/agents/orchestrator.py
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from pydantic_ai import Agent

_log = logging.getLogger(__name__)

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
    system_prompt=(
        "당신은 여행 계획 전문 AI 어시스턴트입니다.\n"
        "사용자 요청에 따라 적절한 도구를 활용하고, 구조화된 JSON(OrchestratorResult 형식)으로 응답합니다."
    ),
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
- `message`: 아래 기준으로 작성한다.
  - **신규 생성**: 날짜별 주요 코스를 간략히 소개한다.
    예) "1일차는 아사쿠사 → 센소지 → 나카미세 거리 코스로, 저녁에는 원하신 참치회 식당을 배치했습니다. 2일차는 신주쿠 → 하라주쿠 쇼핑 코스로 구성했습니다."
  - **수정**: 반영한 요청과 변경 결과를 구체적으로 설명한다.
    예) "해산물 요청을 반영해 1일차 저녁을 해산물 식당으로 변경했습니다. 3일차에는 시장 방문 코스를 새로 추가했습니다."
- `ai_summary`: 번호 목록 형식. 아래 [메모리 업데이트] 참고.

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
- `message`: 무엇이 어떻게 변경되었는지 구체적으로 안내한다.
  예) "여행 기간을 5월 3일~7일로 변경하고, 예산을 50만원으로 조정했습니다."
- `ai_summary`: 번호 목록 형식. 아래 [메모리 업데이트] 참고.

처리:
1. 외부 API 도구는 호출하지 않는다.
2. 사용자 메시지에서 변경된 필드만 추출하여 change 필드에 작성한다.""",

    "reservation": """\
## 이번 요청: 예약 (reservation)

**[응답 형식 — 반드시 준수]**
반환 JSON의 필드를 아래와 같이 채워야 한다:
- `reservation.reservation_type`: "flight" 또는 "hotel"
- `reservation.detail`: **반드시 JSON 객체(dict)로 작성한다. 문자열 금지.**
  숙소 예시: {"name": "신주쿠 그랜드 호텔", "check_in": "2026-05-01", "check_out": "2026-05-03", "rooms": 1}
  항공 예시: {"airline": "대한항공", "flight_no": "KE705", "departure": "ICN", "arrival": "NRT", "date": "2026-05-01"}
- `reservation.total_price`: 숫자 (소수점 허용)
- `reservation.currency`: 통화 코드 (예: "KRW")
- `message`: 예약 결과를 간략히 요약한다. 숙소명·항공편명·예약 번호·날짜·금액 등 핵심 정보를 포함한다.
  예) "신주쿠 그랜드 호텔 5월 1일~3일(2박) 예약이 완료되었습니다. 약 번호: HTL-20260501-0042 / 총 요금: 320,000원"
  예) "대한항공 KE705 (인천→나리타, 5월 1일 10:00) 예약이 완료되었습니다. 예약 번호: KE-20260501-7823 / 총 요금: 450,000원"

처리:
1. 항공권이면 book_flight, 숙소이면 book_hotel을 호출한다.
   (현재 미구현 — status: todo 반환. 향후 Duffel booking API 연결 예정)
2. 결과를 reservation 필드에 작성한다.""",

    "cancel": """\
## 이번 요청: 예약 취소 (cancel)

**[응답 형식 — 반드시 준수]**
반환 JSON의 필드를 아래와 같이 채워야 한다:
- `cancel`: 취소 정보 (reservation_id, cancelled_at)
- `message`: 어떤 예약이 취소되었는지 핵심 정보를 포함해 안내한다. 숙소명·항공편명·예약 번호를 명시한다.
  예) "신주쿠 그랜드 호텔 예약(예약 번호: HTL-20260501-0042)이 취소되었습니다."
  예) "대한항공 KE705편 예약(예약 번호: KE-20260501-7823)이 취소 처리되었습니다."

처리:
1. 항공권이면 cancel_flight, 숙소이면 cancel_hotel을 호출한다.
   (현재 미구현 — status: todo 반환. 향후 Duffel cancel API 연결 예정)
2. 결과를 cancel 필드에 작성한다.""",

    "chat": """\
## 이번 요청: 일반 대화/질문 (chat)

**[응답 형식]**
- `message`: 반드시 실제 내용을 담은 텍스트 응답. "확인해드릴게요" 같은 안내 문구만 쓰고 끝내지 말 것.
- day_plans·change·reservation·cancel 필드는 null로 둔다.

**[일정 관련 질문]**
- 여행 날짜·목적지·인원·예산 등 기본 정보를 묻는 질문이면 `## 현재 여행 일정` 섹션의 데이터를 그대로 읽어 구체적으로 답한다.
- 이미 주입된 컨텍스트로 답할 수 있으면 외부 API 도구를 호출하지 않는다.
- 현재 일정이 없으면(current_itinerary = null) 없다고 명확히 안내한다.

**[그 외 질문]**
- 필요 시 search_web, get_weather 등 도구를 활용한다.""",
}

_MEMORY_INSTRUCTION = """\
## 메모리 업데이트

### ai_summary
- itinerary·change 처리 후에는 `ai_summary` 필드에 반드시 작성한다.
- **형식: 번호 목록.** 각 항목은 한 줄로 핵심 사실만 기술한다.
  예)
  1. 제주도 3박 4일 일정 생성 (5월 1일~3일, 성인 2명, 예산 30만원)
  2. 1일차 저녁 해산물 식당 요청 반영
  3. 숙소: 제주 그랜드 호텔 (5월 1일~3일)
- 이전 대화 요약(## 이전 대화 요약)이 있으면 기존 항목을 유지하고, 이번 대화 내용을 새 번호로 추가한다.
  예) 기존 항목 1~3이 있고 이번에 날짜 변경 요청 시 → 4. 여행 기간 5월 3일~7일로 변경
- chat·reservation·cancel 타입에서 ai_summary 변화 없으면 null로 둔다.

### preferences — 사용자가 직접 말한 것만 추출
⚠️ **AI가 응답을 생성하면서 선택한 것(추천 장소, 이동 수단, 일정 스타일 등)을 취향으로 기록하면 안 된다.**
반드시 **사용자 메시지에 실제로 포함된 표현**에서만 추출한다.

추출 가능 카테고리 (키 예시):
- `food` : 사용자가 먹고 싶다고 말한 음식 (예: ["해산물", "참치회"])
- `food_avoid` : 사용자가 싫다고 한 음식 (예: "고수")
- `transport` : 사용자가 선호한다고 말한 이동 수단
- `accommodation` : 사용자가 선호한다고 말한 숙박 스타일
- `activities` : 사용자가 하고 싶다고 직접 말한 활동
- `pace` : 사용자가 원한다고 말한 여행 속도
- `budget_style` : 사용자가 언급한 예산 방식
- `travel_with` : 사용자가 언급한 동행 특성
- 사용자가 직접 말한 다른 취향도 적절한 키로 추가한다.

출력 예시 (사용자가 "해산물이랑 참치회 먹고 싶어"라고만 했을 때):
```json
{"food": ["해산물", "참치회"]}
```

**기존 ## 사용자 취향이 있으면 그 내용을 그대로 포함하고, 새 항목을 추가/수정한 전체 dict를 반환한다.**
새로 감지된 취향이 없어도 기존 취향이 있으면 기존 값을 그대로 반환한다.
사용자 메시지에 취향 관련 내용이 없고 기존 취향도 없으면 빈 dict {}를 반환한다."""


def build_context_prompt(deps: OrchestratorDeps) -> str:
    """OrchestratorDeps를 읽어 컨텍스트 블록 문자열을 반환한다.
    orchestrator_agent.run() 호출 전에 user_message 앞에 붙인다.
    """
    print(
        f"\n[orchestrator_agent] build_context_prompt 호출\n"
        f"  request_type     : {deps.request_type}\n"
        f"  today            : {deps.today}\n"
        f"  ai_summary       : {deps.ai_summary}\n"
        f"  preferences      : {deps.preferences}\n"
        f"  similar_messages : {len(deps.similar_messages)}건\n"
        f"  current_itinerary: "
        f"{({k: v for k, v in deps.current_itinerary.items() if k != 'day_plans'} if deps.current_itinerary else None)}",
        flush=True,
    )
    sections: list[str] = []
    sections.append(f"오늘 날짜: {deps.today}")

    if deps.current_itinerary:
        it = deps.current_itinerary
        child_ages = it.get("child_ages") or []
        child_str = f"{it.get('child_count')}명 (나이: {child_ages})" if it.get("child_count") else "없음"
        budget = it.get("budget")
        budget_str = f"{int(budget):,}원" if budget else "미설정"
        day_plans = it.get("day_plans")
        day_plans_str = f"{len(day_plans)}일치 일정 존재" if day_plans else "아직 없음"
        sections.append(
            "## 현재 여행 기본 정보 (DB에서 조회된 실제 값 — 반드시 이 데이터를 기준으로 답변할 것)\n"
            f"- 여행지: {it.get('destination')}\n"
            f"- 여행 기간: {it.get('start_date')} ~ {it.get('end_date')} ({it.get('total_days')}일)\n"
            f"- 예산: {budget_str}\n"
            f"- 성인: {it.get('adult_count')}명\n"
            f"- 어린이: {child_str}\n"
            f"- day_plans: {day_plans_str}"
        )
    else:
        sections.append("## 현재 여행 기본 정보\n아직 여행 일정이 등록되지 않았습니다.")

    if deps.ai_summary:
        sections.append(f"## 이전 대화 요약\n{deps.ai_summary}")

    if deps.preferences:
        sections.append(f"## 사용자 취향\n{json.dumps(deps.preferences, ensure_ascii=False, indent=2)}")

    if deps.similar_messages:
        msgs = "\n".join(f"[{m['role']}] {m['content']}" for m in deps.similar_messages)
        sections.append(f"## 참고할 과거 대화\n{msgs}")

    sections.append(_TYPE_INSTRUCTIONS.get(deps.request_type, _TYPE_INSTRUCTIONS["chat"]))
    sections.append(_MEMORY_INSTRUCTION)

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


