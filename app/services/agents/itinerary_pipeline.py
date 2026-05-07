# app/services/agents/itinerary_pipeline.py
"""
일정 생성 4단계 파이프라인 — LLM 호출 최소화.

Phase 1 (LLM 0회): 웹 검색·날씨·항공(출발+귀국)·숙소 병렬 호출
Phase 2 (LLM 1회): 플래너 — 항공/숙소 선택 + 날짜별 방문 순서 확정 (ordered_queries)
Phase 3 (LLM 0회): 장소 검색 + 동선 계산 병렬 호출
Phase 4 (LLM 1회): 합성기 — 최종 OrchestratorResult 작성
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime

_log = logging.getLogger(__name__)

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from app.schemas.ai_message import OrchestratorResult
from app.services.adapters.accommodation_api import AccommodationAdapter
from app.services.adapters.flight_api import FlightAdapter
from app.services.adapters.google_maps import GoogleMapsAdapter
from app.services.adapters.tavily_search import TavilySearchAdapter
from app.services.adapters.weather_api import WeatherAdapter
from app.services.travel_agent_service import TravelAgentService
from ._base import _build_model, preprocessor_agent

_service = TravelAgentService({
    "duffel_flight":        FlightAdapter(),
    "duffel_accommodation": AccommodationAdapter(),
    "tavily_search":        TavilySearchAdapter(),
    "weather":              WeatherAdapter(),
    "google_maps":          GoogleMapsAdapter(),
})

_DEFAULT_ORIGIN = "Seoul"


# ── Phase 2 플래너 출력 스키마 ───────────────────────────────────────────

class SelectedFlight(BaseModel):
    direction: str          # "depart" | "return"
    airline: str
    origin: str
    destination: str
    departing_at: str       # ISO 8601
    arriving_at: str
    price_original: float
    currency: str
    price_krw: int
    stops: int = 0

class SelectedHotel(BaseModel):
    city: str
    name: str
    address: str
    check_in: str           # YYYY-MM-DD
    check_out: str
    price_original: float
    currency: str
    price_krw: int
    rating: float | None = None

class DaySchedule(BaseModel):
    date: str               # YYYY-MM-DD
    ordered_queries: list[str]
    # 방문 순서대로 관광지 + 식사 장소 검색어 (Google Maps search_place에 사용)
    # 예) ["breakfast cafe Asakusa Tokyo", "Senso-ji Temple Tokyo",
    #       "Nakamise Street Asakusa Tokyo", "ramen Asakusa Tokyo",
    #       "Ueno Park Tokyo", "conveyor belt sushi Ueno Tokyo"]

class PlannerOutput(BaseModel):
    days: list[DaySchedule]
    selected_flights: list[SelectedFlight] = Field(default_factory=list)
    selected_hotels: list[SelectedHotel] = Field(default_factory=list)


# ── Phase 2 플래너 에이전트 ──────────────────────────────────────────────

@dataclass
class PlannerDeps:
    itinerary_info: dict        # destination, start_date, end_date, budget, adults…
    web_summary: str
    weather: list[dict]
    flights_depart: dict
    flights_return: dict
    hotels: dict
    preferences: dict | None
    ai_summary: str | None
    today: str
    similar_messages: list[dict]


planner_agent = Agent(
    model=_build_model("orchestrator"),
    deps_type=PlannerDeps,
    result_type=PlannerOutput,
    system_prompt="당신은 여행 일정 플래너입니다. 제공된 데이터를 바탕으로 PlannerOutput JSON을 반환하라.",
)


def _build_planner_prompt(d: PlannerDeps) -> str:
    info = d.itinerary_info
    print(
        f"\n[planner_agent] _build_planner_prompt 호출\n"
        f"  destination={info.get('destination')}, start={info.get('start_date')}, end={info.get('end_date')}\n"
        f"  budget={info.get('budget')}, adults={info.get('adult_count')}, children={info.get('child_count')}\n"
        f"  ai_summary       : {d.ai_summary}\n"
        f"  preferences      : {d.preferences}\n"
        f"  similar_messages : {len(d.similar_messages)}건\n"
        f"  weather          : {len(d.weather)}건\n"
        f"  flights_depart   : status={d.flights_depart.get('status')}\n"
        f"  flights_return   : status={d.flights_return.get('status')}\n"
        f"  hotels           : status={d.hotels.get('status')}",
        flush=True,
    )
    dest = info.get("destination", "여행지")
    start = info.get("start_date", "")
    end = info.get("end_date", "")
    total_days = info.get("total_days", 1)
    budget = info.get("budget")
    adults = info.get("adult_count", 1)
    children = info.get("child_count", 0)
    child_ages = info.get("child_ages", [])

    budget_str = f"{budget:,.0f}원" if budget else "제한 없음"
    child_str = f"어린이 {children}명 (나이: {child_ages})" if children else "없음"

    existing_plans = info.get("day_plans")

    lines = [
        "당신은 여행 일정 플래너입니다.",
        f"여행지: {dest} | 기간: {start} ~ {end} ({total_days}일) | 오늘: {d.today}",
        f"인원: 성인 {adults}명, 어린이 {child_str} | 총 예산: {budget_str}",
        "",
        "## 역할",
        "아래 데이터를 바탕으로 3가지를 결정하고 PlannerOutput을 반환하라:",
        "1. selected_flights: 출발편·귀국편 각 1개 선택 (direction='depart'/'return')",
        "2. selected_hotels: 예산에 맞는 숙소 1개 선택",
        "3. days: ordered_queries (방문 순서대로 장소 검색어 목록)",
        "   - 기존 일정이 없으면: 전체 날짜에 대해 작성",
        "   - 기존 일정이 있으면: **사용자가 수정 요청한 날짜만** 작성 (나머지 날짜는 days에 포함하지 않는다)",
        "",
        "## ordered_queries 작성 규칙",
        "- 방문 순서 그대로: 아침식사 → 관광지 → 이동 → 점심 → 관광지 → 저녁 순",
        "- 하루 총 7~10개 항목 (관광지 3~5개 + 식사 3개)",
        "- 같은 지역 거점 내 장소끼리 묶어 이동 최소화",
        "- 비 예보(강수확률 50% 이상): 실내 관광지 우선",
        "- 1일차(신규): 출발편 도착 시간 이후부터 일정 시작",
        "- 마지막 날(신규): 귀국편 탑승 2~3시간 전까지 일정 종료",
        "- 검색어 형식: '장소명 도시명 (영문)' — Google Maps 검색에 사용",
        "  예) 'Senso-ji Temple Asakusa Tokyo', 'tonkotsu ramen Shinjuku Tokyo lunch'",
    ]

    if existing_plans:
        lines += ["", "## 기존 일정 (반드시 이 내용을 기준으로, 요청된 날짜만 수정할 것)"]
        for date_key, items in existing_plans.items():
            lines.append(f"### {date_key}")
            for item in items:
                lines.append(f"  - {item.get('time','')} {item.get('plan_name','')} ({item.get('place','')})")

    if d.preferences:
        lines += ["", "## 사용자 취향", json.dumps(d.preferences, ensure_ascii=False, indent=2)]
    if d.ai_summary:
        lines += ["", "## 이전 대화 요약", d.ai_summary]
    if d.similar_messages:
        msgs = "\n".join(f"[{m['role']}] {m['content']}" for m in d.similar_messages)
        lines += ["", "## 참고할 과거 대화", msgs]

    lines += ["", "## 날씨"]
    for w in d.weather:
        lines.append(
            f"- {w.get('date')}: {w.get('weather','')} "
            f"최고 {w.get('temperature_max', w.get('temperature_2m_max', '?'))}°C "
            f"강수 {w.get('precipitation_probability_max', w.get('precipitation_sum', '?'))}%"
        )

    lines += ["", "## 항공편 — 출발편 옵션"]
    if d.flights_depart.get("status") == "success":
        for f in d.flights_depart.get("data", [])[:6]:
            lines.append(
                f"- {f.get('airline')} | {f.get('origin')}→{f.get('destination')} | "
                f"{f.get('departing_at','')} ~ {f.get('arriving_at','')} | "
                f"{f.get('price_krw',0):,}원 | {f.get('stops',0)}회 경유"
            )
    else:
        lines.append("- 검색 실패")

    lines += ["", "## 항공편 — 귀국편 옵션"]
    if d.flights_return.get("status") == "success":
        for f in d.flights_return.get("data", [])[:6]:
            lines.append(
                f"- {f.get('airline')} | {f.get('origin')}→{f.get('destination')} | "
                f"{f.get('departing_at','')} ~ {f.get('arriving_at','')} | "
                f"{f.get('price_krw',0):,}원 | {f.get('stops',0)}회 경유"
            )
    else:
        lines.append("- 검색 실패")

    lines += ["", "## 숙소 옵션"]
    if d.hotels.get("status") == "success":
        for h in d.hotels.get("data", [])[:8]:
            lines.append(
                f"- {h.get('name')} | {h.get('address','')} | "
                f"{h.get('price_krw',0):,}원 | 평점 {h.get('rating','?')}"
            )
    else:
        lines.append("- 검색 실패")

    lines += ["", "## 여행지 정보", d.web_summary]

    return "\n".join(lines)


# ── Phase 4 합성기 에이전트 ──────────────────────────────────────────────

@dataclass
class SynthesizerDeps:
    itinerary_info: dict
    planner_output: PlannerOutput
    place_results: dict[str, dict]   # query → search_place 결과
    route_results: dict[str, dict]   # "orig||dest" → find_route 결과
    weather: list[dict]
    web_summary: str
    preferences: dict | None
    ai_summary: str | None
    today: str
    similar_messages: list[dict]


synthesizer_agent = Agent(
    model=_build_model("orchestrator"),
    deps_type=SynthesizerDeps,
    result_type=OrchestratorResult,
    system_prompt="당신은 여행 일정 완성 전문가입니다. 제공된 데이터를 바탕으로 OrchestratorResult JSON을 반환하라.",
)


def _build_synthesizer_prompt(d: SynthesizerDeps) -> str:
    info = d.itinerary_info
    po = d.planner_output
    print(
        f"\n[synthesizer_agent] _build_synthesizer_prompt 호출\n"
        f"  destination={info.get('destination')}, start={info.get('start_date')}, end={info.get('end_date')}\n"
        f"  budget={info.get('budget')}, adults={info.get('adult_count')}\n"
        f"  ai_summary       : {d.ai_summary}\n"
        f"  preferences      : {d.preferences}\n"
        f"  similar_messages : {len(d.similar_messages)}건\n"
        f"  planner_output   : days={len(po.days)}일, flights={len(po.selected_flights)}편, hotels={len(po.selected_hotels)}개\n"
        f"  place_results    : {len(d.place_results)}건\n"
        f"  route_results    : {len(d.route_results)}건\n"
        f"  existing_day_plans: {list(info['day_plans'].keys()) if info.get('day_plans') else None}",
        flush=True,
    )
    dest = info.get("destination", "여행지")
    budget = info.get("budget")
    adults = info.get("adult_count", 1)

    lines = [
        "당신은 여행 일정 완성 전문가입니다.",
        "플래너가 확정한 항공·숙소·방문 순서와 장소 검색·동선 데이터를 바탕으로",
        "완전한 OrchestratorResult를 작성하라.",
        "",
        "## 필수 출력",
        "- `message`: 아래 기준으로 작성한다.",
        "  - 기존 일정(## 기존 일정)이 없으면 신규 생성: 날짜별 주요 코스를 간략히 소개한다.",
        "    예) '1일차는 아사쿠사 → 센소지 → 나카미세 거리 코스로, 저녁에는 원하신 참치회 식당을 배치했습니다. 2일차는 신주쿠 쇼핑 코스로 구성했습니다.'",
        "  - 기존 일정(## 기존 일정)이 있으면 수정: 반영한 요청과 변경 결과를 구체적으로 설명한다.",
        "    예) '해산물 요청을 반영해 1일차 저녁을 해산물 식당으로 변경했습니다. 3일차에는 시장 방문 코스를 새로 추가했습니다.'",
        "- `day_plans`: 키='YYYY-MM-DD'. 신규 생성이면 모든 날짜, 수정이면 요청된 날짜만 반환 (나머지는 포함하지 않는다).",
        "- `ai_summary`: 번호 목록 형식으로 작성한다.",
        "  형식: 각 항목을 '1. 2. 3.' 번호로 나열. 항목당 한 줄로 핵심 사실만 기술.",
        "  예) '1. 제주도 3박 4일 일정 생성 (5월 1일~3일, 성인 2명, 예산 30만원)\\n2. 1일차 저녁 해산물 식당 요청 반영\\n3. 숙소: 제주 그랜드 호텔 (5월 1일~3일)'",
        "  이전 대화 요약(## 이전 대화 요약)이 있으면 기존 항목을 유지하고 이번 내용을 새 번호로 추가한다.",
        "- `preferences`: 아래 [## preferences 추출 규칙] 참고. 반드시 작성할 것.",
        "",
        "## preferences 추출 규칙",
        "⚠️ 반드시 지켜야 할 원칙: **사용자가 직접 말한 내용(사용자 메시지)에서만 추출한다.**",
        "AI가 생성한 일정 내용(day_plans, 관광지 목록, 여행 스타일 등)을 보고 취향을 역추론하지 말 것.",
        "예) 사용자가 '1일차에 참치회 먹고 싶어'라고만 했으면 food: ['참치회']만 기록.",
        "    AI가 일정에 '동물원'을 넣었다고 해서 activities에 '동물원'을 추가하면 안 됨.",
        "",
        "추출 가능한 카테고리 (키 예시):",
        "- food: 사용자가 직접 언급한 음식·식재료 (예: ['참치회', '규카츠'])",
        "- food_avoid: 사용자가 싫다고 한 음식 (예: '고수')",
        "- transport: 사용자가 선호한다고 말한 이동 수단",
        "- accommodation: 사용자가 선호한다고 말한 숙박 스타일",
        "- activities: 사용자가 하고 싶다고 말한 활동",
        "- pace: 사용자가 원한다고 말한 여행 속도",
        "- budget_style: 사용자가 언급한 예산 방식",
        "- 그 외 사용자가 직접 말한 취향도 적절한 키로 추가한다.",
        "",
        "기존 ## 사용자 취향이 있으면 그 내용을 포함한 전체 dict를 반환한다.",
        "사용자 메시지에서 새로 감지된 취향이 없고 기존 취향도 없으면 빈 dict {}를 반환한다.",
        '출력 예시 (사용자가 "참치회랑 규카츠 먹고 싶어"라고 했을 때): {"food": ["참치회", "규카츠"]}',
        "",
        "## day_plans 각 항목 형식",
        '{"plan_name":"...", "time":"HH:MM ~ HH:MM", "place":"...", "note":"...", "cost":null 또는 {"amount":숫자,"currency":"코드"}}',
        "",
        "## 시간 배분",
        "- 식사 3회: 아침(08:00~09:00), 점심(12:00~13:30), 저녁(18:30~20:00)",
        "- 이동 5분 이상: 별도 이동 항목 삽입",
        "- plan_name: '{출발} → {도착} 이동 ({수단)'",
        "- 비 예보 날: 실내 위주 배치 후 note에 날씨 안내",
        "",
        "## cost 작성 규칙",
        "- 항공·숙소(API 데이터): {amount:가격, currency:통화코드, amount_krw:한화정수}",
        "- 식사·교통·입장료(현지 물가): {amount:가격, currency:통화코드} — amount_krw 생략(자동 변환)",
        "- 국내: {amount:가격, currency:'KRW'} — amount_krw=null",
        "- 무료: cost=null",
    ]

    existing_plans = info.get("day_plans")
    if existing_plans:
        lines += ["", "## 기존 일정 (수정 요청이면 이 일정을 기준으로 변경할 것)"]
        for date_key, items in existing_plans.items():
            lines.append(f"### {date_key}")
            for item in items:
                lines.append(f"  - {item.get('time','')} {item.get('plan_name','')} ({item.get('place','')})")

    lines += ["", "## 선택된 항공편"]
    for fl in po.selected_flights:
        lines.append(
            f"- [{fl.direction}] {fl.airline} | {fl.origin}→{fl.destination} | "
            f"{fl.departing_at} ~ {fl.arriving_at} | {fl.price_krw:,}원 ({fl.currency} {fl.price_original})"
        )

    lines += ["", "## 선택된 숙소"]
    for h in po.selected_hotels:
        lines.append(
            f"- {h.city}: {h.name} | {h.address} | "
            f"{h.check_in} ~ {h.check_out} | {h.price_krw:,}원"
        )

    lines += ["", "## 날씨"]
    for w in d.weather:
        lines.append(
            f"- {w.get('date')}: {w.get('weather','')} "
            f"최고{w.get('temperature_max', w.get('temperature_2m_max','?'))}°C "
            f"강수{w.get('precipitation_probability_max', w.get('precipitation_sum','?'))}%"
        )

    lines += ["", "## 장소 검색 결과 (search_place)"]
    for query, result in d.place_results.items():
        if result.get("status") == "success":
            places = result.get("data", {}).get("places", [])
            if places:
                p = places[0]
                lines.append(
                    f"- [{query}] → {p.get('name')} | {p.get('formatted_address','')} | "
                    f"평점 {p.get('rating','?')} ({p.get('user_ratings_total','?')}명)"
                )

    lines += ["", "## 동선 결과 (find_route)"]
    for key, result in d.route_results.items():
        orig, dest_place = key.split("||", 1)
        if result.get("status") == "success":
            routes = result.get("data", {}).get("routes", [])
            if routes:
                r = routes[0]
                lines.append(f"- {orig} → {dest_place}: {r.get('duration','')} ({r.get('distance','')})")

    if budget:
        lines += ["", f"## 예산 제약: 총 {budget:,.0f}원 (성인 {adults}명 기준)"]

    if d.preferences:
        lines += ["", "## 사용자 취향", json.dumps(d.preferences, ensure_ascii=False, indent=2)]
    if d.ai_summary:
        lines += ["", "## 이전 대화 요약", d.ai_summary]
    if d.similar_messages:
        msgs = "\n".join(f"[{m['role']}] {m['content']}" for m in d.similar_messages)
        lines += ["", "## 참고할 과거 대화", msgs]

    lines += ["", "## 여행지 정보 요약", d.web_summary]

    return "\n".join(lines)


# ── Phase 1 헬퍼 함수들 ──────────────────────────────────────────────────

async def _fetch_web_summary(destination: str, preferences: dict | None) -> str:
    queries = [
        f"{destination} tourist attractions sightseeing must-visit",
        f"{destination} local food restaurants best places to eat",
    ]
    raw_list = await asyncio.gather(
        *[_service.process_task("tavily_search", "search", {
            "query": q, "search_depth": "basic", "max_results": 10,
        }) for q in queries],
        return_exceptions=True,
    )
    snippets = []
    for r in raw_list:
        if isinstance(r, Exception) or r.get("status") != "success":
            continue
        for item in r.get("data", [])[:5]:
            if item.get("score", 0) >= 0.4:
                snippets.append(f"[{item['title']}]\n{item['content']}")
    if not snippets:
        return f"{destination} 여행 정보를 찾지 못했습니다."
    combined = "\n\n".join(snippets[:10])
    pref_hint = ""
    if preferences:
        pref_hint = f"\n\n사용자 취향: {json.dumps(preferences, ensure_ascii=False)}\n위 취향에 맞는 정보를 우선적으로 포함해줘."
    result = await preprocessor_agent.run(
        f"아래 검색 결과를 여행 계획에 유용한 핵심 정보 위주로 간결하게 요약해줘.{pref_hint}\n\n{combined}"
    )
    return result.data


async def _fetch_weather(destination: str, start_date: str, end_date: str, today: str) -> list[dict]:
    try:
        city = destination.split(",")[0].strip()
        start_dt = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_date[:10], "%Y-%m-%d").date()
        today_dt = datetime.strptime(today[:10], "%Y-%m-%d").date()
        total_days = max((end_dt - start_dt).days + 1, 1)
        days_until = (start_dt - today_dt).days

        if days_until <= 16:
            result = await _service.process_task("weather", "get_weather", {
                "city": city,
                "forecast_days": min(total_days, 16),
            })
        else:
            last_year_start = start_dt.replace(year=start_dt.year - 1)
            last_year_end = end_dt.replace(year=end_dt.year - 1)
            result = await _service.process_task("weather", "get_historical_weather", {
                "city": city,
                "start_date": str(last_year_start),
                "end_date": str(last_year_end),
            })
        return result.get("data", []) if result.get("status") == "success" else []
    except Exception:
        return []


async def _fetch_flights(
    destination: str, start_date: str, end_date: str,
    adults: int, children: int, child_ages: list,
) -> tuple[dict, dict]:
    city = destination.split(",")[0].strip()
    depart, ret = await asyncio.gather(
        _service.process_task("duffel_flight", "search_flights", {
            "origin": _DEFAULT_ORIGIN, "destination": city,
            "departure_date": start_date[:10],
            "adults": adults, "children": children, "child_ages": child_ages,
        }),
        _service.process_task("duffel_flight", "search_flights", {
            "origin": city, "destination": _DEFAULT_ORIGIN,
            "departure_date": end_date[:10],
            "adults": adults, "children": children, "child_ages": child_ages,
        }),
        return_exceptions=True,
    )
    if isinstance(depart, Exception):
        depart = {"status": "error", "message": str(depart)}
    if isinstance(ret, Exception):
        ret = {"status": "error", "message": str(ret)}
    return depart, ret


async def _fetch_hotels(
    destination: str, start_date: str, end_date: str,
    adults: int, children: int, child_ages: list,
) -> dict:
    city = destination.split(",")[0].strip()
    try:
        return await _service.process_task("duffel_accommodation", "search_hotels", {
            "city_name": city,
            "check_in": start_date[:10],
            "check_out": end_date[:10],
            "adults": adults,
            "children": children,
            "child_ages": child_ages,
        })
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Phase 3 헬퍼 함수들 ──────────────────────────────────────────────────

async def _fetch_places(planner_output: PlannerOutput) -> dict[str, dict]:
    queries: list[str] = []
    for day in planner_output.days:
        queries.extend(day.ordered_queries)
    queries = list(dict.fromkeys(queries))  # 순서 유지 중복 제거

    results = await asyncio.gather(
        *[_service.process_task("google_maps", "search_place", {"query": q}) for q in queries],
        return_exceptions=True,
    )
    return {
        q: (r if not isinstance(r, Exception) else {"status": "error"})
        for q, r in zip(queries, results)
    }


def _best_address(place_result: dict) -> str | None:
    if place_result.get("status") != "success":
        return None
    places = place_result.get("data", {}).get("places", [])
    if not places:
        return None
    p = places[0]
    return p.get("formatted_address") or p.get("name")


async def _fetch_routes(
    planner_output: PlannerOutput,
    place_results: dict[str, dict],
) -> dict[str, dict]:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    for day in planner_output.days:
        addrs = [
            _best_address(place_results.get(q, {}))
            for q in day.ordered_queries
        ]
        addrs = [a for a in addrs if a]  # None 제거
        for i in range(len(addrs) - 1):
            key = f"{addrs[i]}||{addrs[i+1]}"
            if key not in seen:
                seen.add(key)
                pairs.append((addrs[i], addrs[i + 1]))

    if not pairs:
        return {}

    results = await asyncio.gather(
        *[_service.process_task("google_maps", "find_route", {
            "origin": orig, "dest": dest, "mode": "transit",
        }) for orig, dest in pairs],
        return_exceptions=True,
    )
    return {
        f"{orig}||{dest}": (r if not isinstance(r, Exception) else {"status": "error"})
        for (orig, dest), r in zip(pairs, results)
    }


# ── 메인 파이프라인 ───────────────────────────────────────────────────────

async def run_itinerary_pipeline(
    deps,           # OrchestratorDeps
    user_message: str,
    history: list,
) -> OrchestratorResult | None:
    """
    current_itinerary(destination, dates, adults 등)가 없으면 None 반환.
    None이면 호출자가 orchestrator로 폴백.
    """
    itinerary = deps.current_itinerary
    if not itinerary or not itinerary.get("destination") or not itinerary.get("start_date"):
        return None

    destination = itinerary["destination"]
    start_date = itinerary["start_date"]
    end_date = itinerary.get("end_date", start_date)
    adults = itinerary.get("adult_count") or 1
    children = itinerary.get("child_count") or 0
    child_ages = itinerary.get("child_ages") or []

    # ── Phase 1: 병렬 데이터 수집 ──────────────────────────────────────
    web_summary, weather, (flights_depart, flights_return), hotels = await asyncio.gather(
        _fetch_web_summary(destination, deps.preferences),
        _fetch_weather(destination, start_date, end_date, deps.today),
        _fetch_flights(destination, start_date, end_date, adults, children, child_ages),
        _fetch_hotels(destination, start_date, end_date, adults, children, child_ages),
    )

    # ── Phase 2: 플래너 LLM 1회 ────────────────────────────────────────
    planner_deps = PlannerDeps(
        itinerary_info=itinerary,
        web_summary=web_summary,
        weather=weather,
        flights_depart=flights_depart,
        flights_return=flights_return,
        hotels=hotels,
        preferences=deps.preferences,
        ai_summary=deps.ai_summary,
        today=deps.today,
        similar_messages=deps.similar_messages,
    )
    print(
        f"\n[run_itinerary_pipeline] PlannerDeps 조립 완료"
        f"\n  destination={itinerary.get('destination')}, start={itinerary.get('start_date')}, end={itinerary.get('end_date')}"
        f"\n  ai_summary  : {deps.ai_summary}"
        f"\n  preferences : {deps.preferences}"
        f"\n  weather     : {len(weather)}건"
        f"\n  flights_depart status={flights_depart.get('status')}"
        f"\n  flights_return status={flights_return.get('status')}"
        f"\n  hotels status={hotels.get('status')}",
        flush=True,
    )
    planner_context = _build_planner_prompt(planner_deps)
    planner_result = await planner_agent.run(
        f"{planner_context}\n\n---\n\n사용자 메시지: {user_message}",
        deps=planner_deps,
        message_history=history,
    )
    planner_output: PlannerOutput = planner_result.data

    # ── Phase 3: 장소 검색 + 동선 병렬 ────────────────────────────────
    place_results = await _fetch_places(planner_output)
    route_results = await _fetch_routes(planner_output, place_results)

    # ── Phase 4: 합성기 LLM 1회 ────────────────────────────────────────
    synth_deps = SynthesizerDeps(
        itinerary_info=itinerary,
        planner_output=planner_output,
        place_results=place_results,
        route_results=route_results,
        weather=weather,
        web_summary=web_summary,
        preferences=deps.preferences,
        ai_summary=deps.ai_summary,
        today=deps.today,
        similar_messages=deps.similar_messages,
    )
    print(
        f"\n[run_itinerary_pipeline] SynthesizerDeps 조립 완료"
        f"\n  destination={itinerary.get('destination')}, start={itinerary.get('start_date')}, end={itinerary.get('end_date')}"
        f"\n  ai_summary    : {deps.ai_summary}"
        f"\n  preferences   : {deps.preferences}"
        f"\n  planner days  : {len(planner_output.days)}일"
        f"\n  flights       : {len(planner_output.selected_flights)}편"
        f"\n  hotels        : {len(planner_output.selected_hotels)}개"
        f"\n  place_results : {len(place_results)}건"
        f"\n  route_results : {len(route_results)}건",
        flush=True,
    )
    synth_context = _build_synthesizer_prompt(synth_deps)
    synth_result = await synthesizer_agent.run(
        f"{synth_context}\n\n---\n\n사용자 메시지: {user_message}",
        deps=synth_deps,
        message_history=history,
    )
    return synth_result.data
