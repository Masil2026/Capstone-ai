# app/services/agents/orchestrator.py
from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic_ai import Agent, RunContext

from app.schemas.ai_message import DayPlanItem
from app.services.adapters.currency_converter import to_krw
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
    captured: dict = field(default_factory=dict)  # submit_*/update_memory 도구 호출 결과 캡처

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

> **중요**: 도구 호출이 끝나면 즉시 submit_itinerary를 호출하고 텍스트로 마무리한다.
> "이어서 작성", "다음 단계에서 안내" 같은 표현으로 응답을 분리하지 않는다.

---

### [STEP 1] 정보 수집 — current_itinerary가 None이면 전부, 있으면 변경된 부분만

#### 1-1. 웹 검색 (search_web)
- 여행지 관광 명소·현지 팁·계절 트렌드·예산 가이드 검색
- 지역이 여러 곳이면 각 지역별로 search_web 호출

#### 1-2. 날씨 (get_weather / get_historical_weather)
- 여행 출발일로부터 귀국일까지 **모든 날짜** 커버
- 여행 중 **지역 이동이 있으면 각 지역 체류 날짜에 해당 지역 날씨를 별도 호출**
  예) 1~2일차 도쿄 → get_weather("Tokyo", 2), 3~4일차 오사카 → get_weather("Osaka", 2)
- 여행일이 오늘부터 16일 이내: get_weather / 16일 초과: get_historical_weather(작년 같은 시기)
- 날씨 결과를 일정에 반영: 비/폭염 날은 실내 활동 우선 배치

#### 1-3. 항공편 (search_flights) — 신규 생성 또는 항공편 변경 요청 시
- **출발편**: origin=출발지, destination=목적지, departure_date=출발일
- **귀국편**: origin=목적지, destination=출발지, departure_date=귀국일
  → 반드시 두 번 호출한다. 귀국편 미검색은 오류다.
- 검색된 항공편 중 최적(가격·소요시간 고려)을 1일차와 마지막 날 일정에 포함
- note: 항공사·편명·출도착 시각. cost: {"amount": price_original, "currency": currency, "amount_krw": price_krw} 형식으로 기재

#### 1-4. 숙소 (search_hotels) — 신규 생성 또는 숙소 변경 요청 시
- **지역별로 분리 호출**: 같은 도시에 연속 체류하면 해당 기간만큼 호출
  예) 도쿄 2박(check_in=1일차, check_out=3일차) + 오사카 2박(check_in=3일차, check_out=5일차)
- current_itinerary의 budget이 있으면 가격대에 맞는 숙소 선택
- 각 일차 일정에 체크인/체크아웃 항목 추가. note: 숙소명·주소. cost: {"amount": price_original, "currency": currency, "amount_krw": price_krw} 형식으로 기재

#### 1-5. 장소 검색 (search_place)
- 방문 예정인 주요 관광지·식당·카페를 개별 검색하여 위치·평점 확인
- 검색 결과로 평점 낮은 장소는 대안으로 교체 검토

#### 1-6. 동선 계산 (find_route) — 하루 일정 내 장소 간 이동 시간
- 하루 일정에서 **연속으로 방문하는 장소 쌍마다 각각 호출**
  예) A→B, B→C, C→D가 있으면 find_route를 3번 호출
- mode는 기본 "transit"(대중교통). 거리가 짧으면 "walking" 고려
- 이동 시간을 time 필드에 반영하여 현실적인 시간표 구성

---

### [STEP 2] 일정 구성 규칙 — 모든 날짜에 빠짐없이 작성

#### 시간 배분
- **하루 시작**: 09:00 / **하루 종료**: 22:00
- **식사 3회 필수**:
  - 아침식사: 08:00 ~ 09:00 (숙소 조식 또는 근처 카페)
  - 점심식사: 12:00 ~ 13:30
  - 저녁식사: 18:30 ~ 20:00
- **간식**: 오전·오후 이동 중 또는 카페 방문 시 추가 가능
- 야간 행사(야경·야시장·바 등)는 20:30 이후 배치 가능, 22:00 전 종료

#### 예산 반영
- current_itinerary의 budget(총 예산)이 있으면 숙소·항공·식사·입장료에 균형 배분
- 모든 항목의 cost 필드 합산이 budget 이내가 되도록 조정 (인원 수 곱셈 주의)
- 식사는 현지 물가 기준 적정 가격대 식당 선택. 예산 부족 시 저가 식당으로 대체

#### 이동 항목 — 장소 간 이동은 반드시 별도 plan item으로 추가
연속 방문 장소 사이에 이동 시간이 5분 이상이면 이동 항목을 독립적으로 삽입한다.
find_route 결과(distance, duration, steps)를 그대로 반영한다.

이동 항목 작성 형식:
- plan_name: "{출발지} → {도착지} 이동 ({이동수단})"
  예) "신주쿠역 → 아사쿠사역 이동 (지하철 오에도선)"
      "센소지 → 우에노 공원 이동 (도보)"
      "도쿄역 → 교토역 이동 (신칸센 노조미)"
- time: find_route duration 기반. 예) "10:00 ~ 10:35" (35분 소요)
- place: 이동 수단 또는 경유 노선. 예) "Tokyo Metro 오에도선", "JR 신칸센"
- note: 탑승 방법·환승·주의사항. 예) "오에도선 타쿠타마 방향 탑승, 아사쿠사역 하차"
- cost: 이동 요금을 {"amount": 숫자, "currency": "통화코드"} 형식으로 기재
  예) 지하철: {"amount": 280, "currency": "JPY"}
      신칸센: {"amount": 13320, "currency": "JPY"}
  → 무료(도보)이면 cost: null

#### 각 항목별 cost 필드 작성 기준
cost 구조: {"amount": float, "currency": str, "amount_krw": int | null}
- amount: 현지 통화 금액 (1인 기준)
- currency: ISO 4217 코드. "JPY", "USD", "CNY", "KRW" 등
- amount_krw 작성 규칙:
  · currency == "KRW" (국내 여행): amount_krw = null (한화가 곧 amount이므로 불필요)
  · currency != "KRW", API 결과 있음 (항공·숙소): price_krw 값을 그대로 기재
  · currency != "KRW", API 결과 없음 (식사·교통·입장료): amount_krw 생략 → 시스템이 자동 변환
무료 항목은 cost: null.

너가 제출하는 형식 (amount_krw를 모르는 경우 생략):
  항공·숙소 → {"amount": 350.0, "currency": "USD", "amount_krw": 483000}  ← API price_krw 직접 기재
  식사·교통 → {"amount": 280, "currency": "JPY"}                           ← amount_krw 생략
  국내      → {"amount": 15000, "currency": "KRW"}                         ← amount_krw null

시스템이 저장하는 최종 형식 (submit_itinerary가 amount_krw 자동 변환):
  항공·숙소 → {"amount": 350.0, "currency": "USD", "amount_krw": 483000}
  식사·교통 → {"amount": 280, "currency": "JPY", "amount_krw": 2604}       ← 자동 채워짐
  국내      → {"amount": 15000, "currency": "KRW", "amount_krw": null}

| 항목 유형 | 네가 제출하는 cost |
|-----------|-------------------|
| 이동(대중교통, 해외) | {"amount": 280, "currency": "JPY"} |
| 이동(도보) | null |
| 이동(신칸센·장거리) | {"amount": 13320, "currency": "JPY"} |
| 관광지 입장료(해외) | {"amount": 1500, "currency": "JPY"} |
| 무료 관광지 | null |
| 식사(아침, 해외) | {"amount": 800, "currency": "JPY"} |
| 식사(점심, 해외) | {"amount": 1500, "currency": "JPY"} |
| 식사(저녁, 해외) | {"amount": 3000, "currency": "JPY"} |
| 숙소 체크인(API) | {"amount": 15000.0, "currency": "JPY", "amount_krw": price_krw} |
| 항공편(API) | {"amount": price_original, "currency": currency, "amount_krw": price_krw} |
| 국내 이동·식사 | {"amount": 15000, "currency": "KRW"} |
- amount는 항상 숫자(소수점 허용). 문자열 불가

#### plan_name·time·place·note 작성 기준
- plan_name: 구체적 활동명. 예) "센소지 참배 및 나카미세 거리 쇼핑"
- time: "HH:MM ~ HH:MM" 형식. 이동 항목 후 다음 활동 시작 시간 자동 조정
- place: 실제 장소명 (영문 또는 현지명). 예) "Senso-ji Temple, Asakusa"
- note: 날씨 주의사항·예약 필요 여부·팁 등 (비용은 cost 필드에 별도 기재)

#### 날씨 반영
- 강수 확률 50% 이상인 날: 실내 관광지(박물관·쇼핑몰·실내 명소) 우선 배치
- 폭염(최고 기온 35°C 이상): 오전 활동 집중, 오후 실내 휴식 배치
- note에 당일 날씨 요약 기재. 예) "맑음 22°C, 선크림 필수"

---

### [STEP 3] current_itinerary가 있는 경우 — 수정 범위 최소화
1. 사용자가 변경을 요청한 부분만 수정하고 나머지는 그대로 유지
2. 변경된 장소의 날씨·동선은 반드시 재확인 (해당 날짜·지역 날씨 재조회)
3. 새 장소 추가 시 search_place로 정보 확인 후 인접 장소와 find_route로 동선 재계산
4. 수정된 **전체** 일정으로 submit_itinerary(day_plans) 호출 (변경 부분만 아닌 전체)

---

### [공통] submit_itinerary 호출
- 정보 수집이 끝나면 즉시 호출한다. 응답 분리 없이 이 run에서 완료.
- day_plans 키: "YYYY-MM-DD" 형식 실제 날짜. 예) "2026-05-01", "2026-05-02"
- 모든 날짜에 항목이 있어야 함. 빈 날짜 허용 불가
- 텍스트 응답: 일정 전체 요약 + 날씨·동선·예산 포인트 설명""",

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
1. 항공권이면 book_flight, 숙소이면 book_hotel을 호출한다.
   - 검색과 예약이 도구 내부에서 자동으로 처리되므로 search_flights/search_hotels를 별도로 호출하지 않는다.
   - current_itinerary가 있으면 날짜·목적지·인원을 참고한다.
   - preferences에 선호 항공사·호텔 체인 정보가 있으면 참고한다.
   (현재 미구현 — status: todo 반환. 향후 Duffel booking API 연결 예정)
2. submit_reservation(reservation_type, detail, total_price, currency 등)을 호출한다.""",

    "cancel": """\
## 이번 요청: 예약 취소 (cancel)

처리 순서:
1. 취소 대상이 항공권인지 숙소인지 확인한다.
2. 항공권이면 cancel_flight(order_id), 숙소이면 cancel_hotel(booking_id)을 호출한다.
   (현재 미구현 — status: todo 반환. 향후 Duffel cancel API 연결 예정)
3. submit_cancel(reservation_id, cancelled_at)을 호출해 취소 정보를 시스템에 전달한다.
4. 텍스트 응답으로 취소 접수 완료를 안내한다.""",

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
    """항공권 검색. 출발편과 귀국편을 반드시 각각 한 번씩 총 두 번 호출해야 한다.

    [필수 호출 패턴]
    - 출발편: origin=출발지, destination=목적지, departure_date=출발일
    - 귀국편: origin=목적지, destination=출발지, departure_date=귀국일
    귀국편을 빠뜨리면 일정이 불완전하다.

    - origin/destination: 영문 도시명(Seoul, Tokyo, Osaka) 또는 IATA 코드(ICN, NRT, KIX) 모두 허용
    - departure_date: YYYY-MM-DD 형식. 예) "2026-05-15"
    - children >= 1이면 child_ages에 각 아이 나이를 반드시 포함. 개수 불일치 시 에러.
    - 반환: {status, count, data: [{airline, origin, destination, price_original(현지통화), currency, price_krw(한화 정수), stops, departing_at, arriving_at}]}
    - cost 필드: {"amount": price_original, "currency": currency, "amount_krw": price_krw} 형식으로 기재.
    - 반환된 항공편 중 price_krw·소요시간을 고려해 최적 1개를 선택, 1일차(출발편)와 마지막 날(귀국편) 일정에 포함
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
    """숙소 검색. 지역이 여러 곳이면 지역별로 분리하여 각각 호출한다.

    [다중 지역 호출 패턴]
    여행 중 도시 이동이 있으면 도시마다 별도 호출:
      도쿄 2박: city_name="Tokyo", check_in="2026-05-01", check_out="2026-05-03"
      오사카 2박: city_name="Osaka", check_in="2026-05-03", check_out="2026-05-05"

    - city_name: 영문 또는 한글 도시명. 예) "Tokyo", "Osaka", "도쿄"
    - check_in/check_out: YYYY-MM-DD 형식. check_out은 마지막 숙박일 다음 날.
    - children >= 1이면 child_ages에 각 아이 나이를 반드시 포함.
    - 반환: {status, count, data: [{name, price_original(현지통화), currency, price_krw(한화 정수), rating, address}]} 최대 10개
    - cost 필드: {"amount": price_original, "currency": currency, "amount_krw": price_krw} 형식으로 기재.
    - 예산(budget)이 있으면 price_krw 기준으로 가격대 맞는 숙소 선택. 체크인·아웃 항목을 해당 일차 일정에 추가하고 note에 숙소명 기재
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
    """과거 날씨 조회. 여행일이 오늘부터 16일 초과일 때 작년 같은 시기 데이터를 참고용으로 사용.

    [다중 지역 호출 패턴] 도시 이동이 있으면 지역별로 분리 호출:
      1~2일차 도쿄(2026-08-01~02): get_historical_weather("Tokyo", "2025-08-01", "2025-08-02")
      3~4일차 오사카(2026-08-03~04): get_historical_weather("Osaka", "2025-08-03", "2025-08-04")

    - city: 반드시 영문 도시명. 예) "Seoul", "Tokyo" (한국어 입력 시 에러)
    - start_date/end_date: 여행 날짜의 작년 같은 기간. 예) 여행 2026-08-01~05 → "2025-08-01", "2025-08-05"
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


@orchestrator_agent.tool
async def submit_itinerary(ctx: RunContext[OrchestratorDeps], day_plans: dict[str, list[DayPlanItem]]) -> dict:
    """itinerary 타입 전용. 일정 생성/수정 완료 시 반드시 호출. 구조화된 dayPlans를 시스템에 전달한다."""
    # cost.amount_krw 자동 변환: LLM이 현지 통화만 채운 항목을 한화로 변환
    for items in day_plans.values():
        for item in items:
            if item.cost and item.cost.currency != "KRW" and item.cost.amount_krw is None:
                item.cost.amount_krw = await to_krw(item.cost.amount, item.cost.currency)

    ctx.deps.captured["itinerary"] = day_plans
    return {"status": "success", "message": "일정이 저장되었습니다."}


@orchestrator_agent.tool
async def submit_change(
    ctx: RunContext[OrchestratorDeps],
    start_date: str | None = None,
    end_date: str | None = None,
    budget: float | None = None,
    adult_count: int | None = None,
    child_count: int | None = None,
    child_ages: list[int] | None = None,
) -> dict:
    """change 타입 전용. 변경된 여행 기본 정보를 시스템에 전달한다. 변경된 필드만 포함."""
    ctx.deps.captured["change"] = {k: v for k, v in {
        "start_date": start_date,
        "end_date": end_date,
        "budget": budget,
        "adult_count": adult_count,
        "child_count": child_count,
        "child_ages": child_ages,
    }.items() if v is not None}
    return {"status": "success", "message": "변경 정보가 저장되었습니다."}


@orchestrator_agent.tool
async def submit_reservation(
    ctx: RunContext[OrchestratorDeps],
    reservation_type: str,
    detail: dict,
    booking_url: str | None = None,
    external_ref_id: str | None = None,
    total_price: float | None = None,
    currency: str | None = None,
    reserved_at: str | None = None,
) -> dict:
    """reservation 타입 전용. 예약 완료 후 예약 정보를 시스템에 전달한다.

    - reservation_type: "flight" 또는 "hotel"
    - detail: 반드시 dict(객체)여야 한다. 문자열 불가.
      항공권 예시: {"airline": "Korean Air", "origin": "ICN", "destination": "NRT",
                   "departing_at": "2026-05-15T10:00:00", "offer_id": "off_xxx"}
      숙소 예시:  {"name": "Shinjuku Grand Hotel", "address": "Shinjuku, Tokyo",
                   "check_in": "2026-05-15", "check_out": "2026-05-18", "hotel_id": "prop_xxx"}
    - total_price: 숫자형. 예) 450000
    - currency: 통화 코드. 예) "KRW", "USD"
    """
    ctx.deps.captured["reservation"] = {
        "reservation_type": reservation_type,
        "detail": detail,
        "booking_url": booking_url,
        "external_ref_id": external_ref_id,
        "total_price": total_price,
        "currency": currency,
        "reserved_at": reserved_at,
    }
    return {"status": "success", "message": "예약 정보가 저장되었습니다."}


@orchestrator_agent.tool
async def submit_cancel(
    ctx: RunContext[OrchestratorDeps],
    reservation_id: str,
    cancelled_at: str,
) -> dict:
    """cancel 타입 전용. 취소 완료 후 취소 정보를 시스템에 전달한다.

    - reservation_id: 취소된 예약 ID. 예) "RES-20260515-001"
    - cancelled_at: 취소 시각. ISO 8601 형식. 예) "2026-05-05T10:00:00Z"
    """
    ctx.deps.captured["cancel"] = {
        "reservation_id": reservation_id,
        "cancelled_at": cancelled_at,
    }
    return {"status": "success", "message": "취소 정보가 저장되었습니다."}


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


@orchestrator_agent.tool
async def update_memory(
    ctx: RunContext[OrchestratorDeps],
    ai_summary: str | None = None,
    preferences: dict | None = None,
) -> dict:
    """모든 타입 공통. 대화 중 기억할 정보(취향·요약)가 감지될 때 호출한다."""
    ctx.deps.captured["memory"] = {
        "ai_summary": ai_summary,
        "preferences": preferences,
    }
    return {"status": "success", "message": "메모리가 갱신되었습니다."}
