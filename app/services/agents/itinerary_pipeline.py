# app/services/agents/itinerary_pipeline.py
"""
일정 생성 4단계 파이프라인 — LLM 호출 최소화.

Phase 1 (LLM 0회): 웹 검색·날씨·항공(전체 구간)·숙소 병렬 호출
Phase 2 (LLM 1회): 플래너 — 항공/숙소 선택 + 날짜별 방문 순서 확정 (ordered_queries)
Phase 3 (LLM 0회): 장소 검색 + 동선 계산 병렬 호출
Phase 4 (LLM 1회): 합성기 — 최종 OrchestratorResult 작성

단일 목적지: destinations = [{"city": "Tokyo", ...}]  → 기존과 동일한 경로
다중 목적지: destinations = [{"city": "Paris", ...}, {"city": "Rome", ...}]  → 구간별 처리
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import AsyncGenerator

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


def _all_dates(destinations: list[dict]) -> list[str]:
    """start_date ~ end_date 전체 날짜 목록 반환 (YYYY-MM-DD)."""
    start = datetime.strptime(destinations[0]["start_date"][:10], "%Y-%m-%d").date()
    end = datetime.strptime(destinations[-1]["end_date"][:10], "%Y-%m-%d").date()
    dates = []
    curr = start
    while curr <= end:
        dates.append(str(curr))
        curr += timedelta(days=1)
    return dates


_TRANSPORT_KEYWORDS = frozenset({"항공 이동", "기내 (비행 중)"})


def _is_transport_day(items: list) -> bool:
    """day_plans 하루 항목에 교통 이동(항공·기내) 항목이 있으면 True."""
    for item in items:
        name = item.get("plan_name", "") if isinstance(item, dict) else ""
        if any(kw in name for kw in _TRANSPORT_KEYWORDS):
            return True
    return False


def _get_replan_dates_for_date_change(
    itinerary: dict,
) -> tuple[dict, list[str]]:
    """
    여행 기간(start_date/end_date) 변경을 감지하고,
    기존 day_plans에서 교통 이동일(m)을 제거한 조정된 plans와
    새로 계획해야 할 날짜 목록(m+n일)을 반환한다.

    변경 없으면 (원본 day_plans, []) 반환.
    """
    day_plans = itinerary.get("day_plans") or {}
    destinations = itinerary.get("destinations") or []
    if not day_plans or not destinations:
        return day_plans, []

    new_start = destinations[0]["start_date"][:10]
    new_end = destinations[-1]["end_date"][:10]
    existing_dates = sorted(day_plans.keys())
    if not existing_dates:
        return day_plans, []

    old_start = existing_dates[0]
    old_end = existing_dates[-1]
    if new_start == old_start and new_end == old_end:
        return day_plans, []

    adjusted = dict(day_plans)
    replan_set: set[str] = set()

    # ── 마지막 날짜 변경 ─────────────────────────────────────────────────
    if new_end != old_end:
        new_end_dt = date.fromisoformat(new_end)
        old_end_dt = date.fromisoformat(old_end)

        # 범위 밖 날짜 제거 (단축)
        for d in [d for d in existing_dates if d > new_end]:
            adjusted.pop(d, None)

        # 남은 일정의 끝에서 교통 이동일(m) 제거 → 재계획 대상
        for d in reversed(sorted(adjusted.keys())):
            if _is_transport_day(adjusted.get(d, [])):
                adjusted.pop(d)
                replan_set.add(d)
            else:
                break

        # 신규 날짜 추가 (연장)
        curr = old_end_dt + timedelta(days=1)
        while curr <= new_end_dt:
            replan_set.add(str(curr))
            curr += timedelta(days=1)

        # 단축 시: 새 마지막 날 + 그 전날(야간 비행 대비)을 재계획
        if new_end_dt < old_end_dt:
            replan_set.add(new_end)
            prev = new_end_dt - timedelta(days=1)
            if str(prev) >= new_start:
                replan_set.add(str(prev))

    # ── 시작 날짜 변경 ────────────────────────────────────────────────────
    if new_start != old_start:
        new_start_dt = date.fromisoformat(new_start)
        old_start_dt = date.fromisoformat(old_start)

        # 범위 밖 날짜 제거 (단축)
        for d in [d for d in existing_dates if d < new_start]:
            adjusted.pop(d, None)

        # 남은 일정의 앞에서 교통 이동일(m) 제거 → 재계획 대상
        for d in sorted(adjusted.keys()):
            if _is_transport_day(adjusted.get(d, [])):
                adjusted.pop(d)
                replan_set.add(d)
            else:
                break

        # 신규 날짜 추가 (연장)
        curr = new_start_dt
        while curr < old_start_dt:
            replan_set.add(str(curr))
            curr += timedelta(days=1)

        # 단축 시: 새 첫날 + 그 다음날(야간 도착 대비)을 재계획
        if new_start_dt > old_start_dt:
            replan_set.add(new_start)
            nxt = new_start_dt + timedelta(days=1)
            if str(nxt) <= new_end:
                replan_set.add(str(nxt))

    # new_start ~ new_end 범위 내 날짜만 유효
    replan_dates = sorted(d for d in replan_set if new_start <= d <= new_end)

    if not replan_dates:
        return day_plans, []

    print(
        f"\n[_get_replan_dates_for_date_change] 날짜 변경 감지"
        f"\n  old: {old_start} ~ {old_end}"
        f"\n  new: {new_start} ~ {new_end}"
        f"\n  재계획 날짜({len(replan_dates)}일): {replan_dates}",
        flush=True,
    )
    return adjusted, replan_dates


async def _extract_english_cities(cities: list[str]) -> list[str]:
    """
    도시명 목록에서 영문명을 일괄 추출한다. 순수 한국어 도시는 단일 LLM 호출로 처리.

    처리 순서:
    1. 괄호 제거 후 쉼표·슬래시로 분리 → 영문 토큰이 있으면 바로 사용
       예) "인천, incheon" → "Incheon" | "(서울, Seoul)" → "Seoul" | "Tokyo" → "Tokyo"
    2. 영문 토큰이 없는 도시들은 모아서 preprocessor_agent에 단일 호출
       예) ["도쿄", "오사카"] → 1회 LLM 호출 → ["Tokyo", "Osaka"]
    """
    results: list[str | None] = []
    korean_indices: list[int] = []
    korean_cities: list[str] = []

    for city in cities:
        clean = re.sub(r"[()（）]", " ", city).strip()
        found = None
        for part in re.split(r"[,、/\\]", clean):
            part = part.strip()
            if part and re.match(r"^[A-Za-z][A-Za-z\s\-]*$", part):
                found = part
                break
        results.append(found)
        if found is None:
            korean_indices.append(len(results) - 1)
            korean_cities.append(city)

    if korean_cities:
        city_list = "\n".join(f"{i+1}. {c}" for i, c in enumerate(korean_cities))
        result = await preprocessor_agent.run(
            "아래 도시명들을 영문으로만 답해줘. 번호 그대로 줄바꿈으로 구분하여 반환.\n"
            "영문 도시명 외 다른 텍스트는 출력하지 마.\n"
            "Island, City, Province 같은 지역 접미사 없이 도시명만 짧게 반환.\n"
            "예) '서울' → Seoul | '도쿄' → Tokyo | '제주도' → Jeju | '방콕' → Bangkok\n\n"
            f"{city_list}"
        )
        translated = [
            line.strip().lstrip("0123456789. ")
            for line in result.output.strip().split("\n")
            if line.strip()
        ]
        for idx, eng in zip(korean_indices, translated):
            results[idx] = eng

    return [r or city for r, city in zip(results, cities)]


async def _extract_english_city(raw: str) -> str:
    """단일 도시명 영문 추출 (하위 호환용)."""
    result = await _extract_english_cities([raw])
    return result[0]


# ── Phase 2 플래너 출력 스키마 ───────────────────────────────────────────

class SelectedFlight(BaseModel):
    direction: str      # "depart" | "connect" | "return"
    leg_index: int      # 0=depart, 1..N-1=connect, N=return
    airline: str
    origin: str
    destination: str
    departing_at: str   # ISO 8601
    arriving_at: str
    duration: str = "?"  # tools에서 계산된 비행시간 (예: "13h 15m"). LLM이 계산하지 않음
    price_original: float
    currency: str
    price_krw: int
    stops: int = 0

class SelectedHotel(BaseModel):
    city: str
    name: str
    address: str
    check_in: str       # YYYY-MM-DD
    check_out: str
    price_original: float | None = None
    currency: str
    price_krw: int | None = None
    rating: float | None = None

class DaySchedule(BaseModel):
    date: str               # YYYY-MM-DD
    city: str               # 해당 날짜의 여행 도시 (한국어 원본)
    ordered_queries: list[str]
    # 방문 순서대로 관광지 + 식사 장소 검색어 (Google Maps search_place에 사용)

class PlannerOutput(BaseModel):
    days: list[DaySchedule]
    selected_flights: list[SelectedFlight] = Field(default_factory=list)
    selected_hotels: list[SelectedHotel] = Field(default_factory=list)


# ── Phase 2 플래너 에이전트 ──────────────────────────────────────────────

@dataclass
class PlannerDeps:
    itinerary_info: dict
    web_summaries: dict          # city → 요약 텍스트
    weather_by_city: dict        # city → list[dict]
    flight_legs: list[dict]      # [{leg_index, direction, from, to, data}, ...]
    hotels_by_city: dict         # city → 숙소 검색 결과
    cities_en: list[str]         # 영문 도시명 (destinations 순서와 일치)
    preferences: dict | None
    ai_summary: str | None
    today: str
    similar_messages: list[dict]
    replan_dates: list[str]      # 날짜 변경으로 재계획이 필요한 날짜 목록 (없으면 [])


planner_agent = Agent(
    model=_build_model("orchestrator"),
    deps_type=PlannerDeps,
    output_type=PlannerOutput,
    system_prompt="당신은 여행 일정 플래너입니다. 제공된 데이터를 바탕으로 PlannerOutput JSON을 반환하라.",
)


def _build_planner_prompt(d: PlannerDeps) -> str:
    info = d.itinerary_info
    destinations = info.get("destinations") or []
    dest_str = " → ".join(dest["city"] for dest in destinations) if destinations else "여행지"
    start = destinations[0]["start_date"] if destinations else info.get("start_date", "")
    end = destinations[-1]["end_date"] if destinations else info.get("end_date", "")
    total_days = info.get("total_days", 1)
    budget = info.get("budget")
    adults = info.get("adult_count", 1)
    children = info.get("child_count", 0)
    child_ages = info.get("child_ages", [])
    all_dates = _all_dates(destinations) if destinations else []

    budget_str = f"{budget:,.0f}원" if budget else "제한 없음"
    child_str = f"어린이 {children}명 (나이: {child_ages})" if children else "없음"

    print(
        f"\n[planner_agent] _build_planner_prompt 호출\n"
        f"  destinations={dest_str}, start={start}, end={end}\n"
        f"  budget={budget}, adults={adults}, children={children}\n"
        f"  ai_summary       : {d.ai_summary}\n"
        f"  preferences      : {d.preferences}\n"
        f"  similar_messages : {len(d.similar_messages)}건\n"
        f"  flight_legs      : {len(d.flight_legs)}개 구간\n"
        f"  hotels_by_city   : {list(d.hotels_by_city.keys())}",
        flush=True,
    )

    lines = [
        "당신은 여행 일정 플래너입니다.",
        f"여행 경로: 한국 → {dest_str} → 한국 | 기간: {start} ~ {end} ({total_days}일) | 오늘: {d.today}",
        f"인원: 성인 {adults}명, 어린이 {child_str} | 총 예산: {budget_str}",
        "출발지: 대한민국 — 항공 출발 공항은 인천국제공항(ICN) 또는 김포공항(GMP)이다.",
        "",
        "## 역할",
        "아래 데이터를 바탕으로 3가지를 결정하고 PlannerOutput을 반환하라:",
        "1. selected_flights: 모든 항공 구간 선택 (leg_index별 1개씩)",
    ]

    for leg in d.flight_legs:
        lines.append(
            f"   - leg_index={leg['leg_index']} ({leg['direction']}): "
            f"{leg['from']} → {leg['to']}"
        )

    lines += [
        "2. selected_hotels: 각 도시별 숙소 1개씩 선택",
        "3. days: 날짜별 city 필드와 ordered_queries 목록",
        "   - city: 해당 날짜의 여행 도시명 (destinations 배열 기준으로 배정, 한국어 원본)",
        "   - ordered_queries: 방문 순서대로 관광지 + 식사 장소 검색어",
        "   - 기존 일정이 없으면: 아래 [## 반드시 포함해야 할 전체 날짜 목록]의 날짜를 하나도 빠짐없이 days에 포함할 것.",
        f"   ⚠️ days 배열 길이는 반드시 {len(all_dates)}개여야 한다. 단 1일도 누락 불가.",
        "   ⚠️ 이동일·경유일·항공 탑승일도 포함하되 ordered_queries=[]로 설정. 날짜 누락 절대 금지.",
        "   - 기존 일정 + 날짜 변경 없음: **사용자가 수정 요청한 날짜만** 작성 (나머지 날짜는 포함하지 않는다)",
        "   - 기존 일정 + 날짜 변경 있음: [## 날짜 변경 재계획 대상] 섹션의 날짜를 반드시 작성 (+ 사용자가 추가로 요청한 날짜)",
        "",
        "## 여행 일정 개요 (도시별 체류 기간)",
    ]
    for dest in destinations:
        lines.append(f"- {dest['city']}: {dest['start_date']} ~ {dest['end_date']}")

    lines += ["", "## 항공편 선택 규칙",
        "- 출발 시간 제한 없음. 새벽(00:00~06:00) / 야간(21:00~23:59) 출발도 정상 선택 가능.",
        "- 장거리 국제선(유럽·미주·오세아니아)은 새벽·야간 출발이 일반적이므로 시간대 무관하게 최적 편 선택.",
        "- 가격·경유 횟수·도착 시간을 종합해 최선의 편 선택. 비용이 낮고 경유 적은 편 우선.",
        f"- ⚠️ 귀국편(return) 필수 제약: 한국(ICN/GMP) 도착 일자가 반드시 여행 마지막 날({end})이어야 한다.",
        f"  arriving_at 날짜가 {end}을 초과하는 귀국편은 절대 선택 금지.",
        f"  귀국편 데이터에는 {end} 출발편과 {end} 하루 전 출발편이 모두 포함되어 있다. {end} 당일 도착 가능한 편을 우선 선택.",
        "- ⚠️ '실시간 항공편 없음' 표시된 구간은 selected_flights에 절대 포함하지 말 것. 항공편을 임의로 만들거나 추측하지 말 것.",
        "",
        "## ordered_queries 작성 규칙",
        "- 방문 순서 그대로: 아침식사 → 관광지 → 이동 → 점심 → 관광지 → 저녁 순",
        "- 하루 총 7~10개 항목 (관광지 3~5개 + 식사 3개)",
        "- 같은 지역 거점 내 장소끼리 묶어 이동 최소화",
        "- 비 예보(강수확률 50% 이상): 실내 관광지 우선",
        "- 1일차(신규): 출발편이 당일 도착(+0일)이면 도착 시간 이후 활동 배정. (+1일) 이상이면 ordered_queries=[] (빈 리스트).",
        "  (+1일) 이상 비행: 도착 다음 날짜(arrival_date)에 도착 후 활동 배정. 출발일에는 활동 없음.",
        "- 마지막 날(신규): 귀국편 탑승 2~3시간 전까지 일정 종료",
        "- 도시 이동일: ordered_queries 최소화 (공항 이동 관련 장소만 또는 빈 리스트)",
        "- 검색어 형식: '장소명 도시명 (영문)' — Google Maps 검색에 사용",
        "  예) 'Senso-ji Temple Asakusa Tokyo', 'tonkotsu ramen Shinjuku Tokyo lunch'",
    ]

    existing_plans = info.get("day_plans")
    if not existing_plans and all_dates:
        lines += [
            "",
            f"## 반드시 포함해야 할 전체 날짜 목록 (총 {len(all_dates)}일 — 하나도 빠짐없이 days에 추가)",
        ]
        for dt in all_dates:
            lines.append(f"  - {dt}")

    if d.replan_dates:
        lines += [
            "",
            f"## 날짜 변경 재계획 대상 (총 {len(d.replan_dates)}일 — 반드시 새로 계획)",
            "여행 기간이 변경되어 교통 이동일 포함 아래 날짜들을 새로 계획해야 한다.",
            "⚠️ 아래 날짜는 기존 일정에 있던 내용을 무시하고 반드시 새로 작성.",
            "⚠️ 그 외 날짜는 절대 포함하지 않는다.",
        ]
        for dt in d.replan_dates:
            lines.append(f"  - {dt}")

    if existing_plans:
        lines += ["", "## 기존 일정 (변경 없는 날짜의 참고용 — 재계획 대상 날짜는 무시할 것)"]
        for date_key, items in existing_plans.items():
            lines.append(f"### {date_key}")
            for item in items:
                lines.append(f"  - {item.get('time','')} {item.get('plan_name','')} ({item.get('place','')})")

    lines += ["", "## 항공편 데이터 (구간별)"]
    for leg in d.flight_legs:
        lines.append(
            f"### leg_index={leg['leg_index']} ({leg['direction']}): "
            f"{leg['from']} → {leg['to']}"
        )
        data = leg["data"]
        if data.get("status") == "success":
            offers = data.get("data", [])
            is_fallback = data.get("is_duffel_fallback", False)
            if offers:
                if is_fallback:
                    lines.append("  ⚠️ [DUFFEL FALLBACK] 실제 항공사 결과 없음 — Duffel Airways(테스트용 가상 항공사) 결과를 대신 사용. 이 구간은 반드시 selected_flights에 포함할 것. 임의로 다른 항공사를 만들어 넣지 말 것.")
                for f in offers[:6]:
                    lines.append(
                        f"  - {f.get('airline')} | {f.get('origin')}→{f.get('destination')} | "
                        f"{f.get('departing_at','')} ~ {f.get('arriving_at','')} | "
                        f"비행시간 {f.get('duration','?')} | "
                        f"{f.get('price_original','?')} {f.get('currency','?')} ({f.get('price_krw',0):,}원) | {f.get('stops',0)}회 경유"
                    )
            else:
                lines.append("  - ⚠️ 실시간 항공편 없음 — 이 구간은 selected_flights에 절대 포함하지 말 것. 임의로 항공편을 만들어 넣지 말 것.")
        else:
            lines.append("  - 검색 실패 — 이 구간은 selected_flights에 절대 포함하지 말 것.")

    lines += ["", "## 숙소 데이터 (도시별)"]
    for dest in destinations:
        city = dest["city"]
        hotel_data = d.hotels_by_city.get(city, {})
        lines.append(f"### {city} ({dest['start_date']} ~ {dest['end_date']})")
        if hotel_data.get("status") == "success":
            for h in hotel_data.get("data", [])[:6]:
                lines.append(
                    f"  - {h.get('name')} | {h.get('address','')} | "
                    f"{h.get('price_krw',0):,}원 | 평점 {h.get('rating','?')}"
                )
        else:
            lines.append("  - 검색 실패")

    lines += ["", "## 날씨 (도시별)"]
    for dest in destinations:
        city = dest["city"]
        weather = d.weather_by_city.get(city, [])
        if weather:
            lines.append(f"### {city}")
            for w in weather:
                lines.append(
                    f"  - {w.get('date')}: {w.get('weather','')} "
                    f"최고{w.get('temperature_max', w.get('temperature_2m_max', '?'))}°C "
                    f"강수{w.get('precipitation_probability_max', w.get('precipitation_sum', '?'))}%"
                )

    lines += ["", "## 여행지 정보 (도시별)"]
    for dest in destinations:
        city = dest["city"]
        summary = d.web_summaries.get(city, "정보 없음")
        lines += [f"### {city}", summary]

    if d.preferences:
        lines += [
            "",
            "## 사용자 취향",
            json.dumps(d.preferences, ensure_ascii=False, indent=2),
            "⚠️ 취향 반영 원칙: 취향은 일정에 적절히 반영하되 과도하게 편중되지 않게 한다.",
            "- food: 전체 식사(아침·점심·저녁) 중 1~2회만 포함. 나머지는 현지 다양한 음식으로 구성.",
            "- activities: 선호 활동을 일부 포함하되 관광지·문화 체험 등 다양한 일정과 균형을 맞춤.",
            "- 그 외 취향도 '힌트'로 참고하며, 모든 항목에 적용하지 않는다.",
        ]
    if d.ai_summary:
        lines += ["", "## 이전 대화 요약", d.ai_summary]
    if d.similar_messages:
        msgs = "\n".join(f"[{m['role']}] {m['content']}" for m in d.similar_messages)
        lines += ["", "## 참고할 과거 대화", msgs]

    return "\n".join(lines)


# ── Phase 4 합성기 에이전트 ──────────────────────────────────────────────

@dataclass
class SynthesizerDeps:
    itinerary_info: dict
    planner_output: PlannerOutput
    place_results: dict[str, dict]      # query → search_place 결과
    route_results: dict[str, dict]      # "orig||dest" → find_route 결과
    weather_by_city: dict               # city → list[dict]
    web_summaries: dict                 # city → 요약 텍스트
    preferences: dict | None
    ai_summary: str | None
    today: str
    similar_messages: list[dict]
    attraction_prices: dict[str, str]   # place_name → Tavily 입장료 검색 결과 (없으면 {})
    replan_dates: list[str]             # 날짜 변경으로 재계획이 필요한 날짜 목록 (없으면 [])


synthesizer_agent = Agent(
    model=_build_model("orchestrator"),
    deps_type=SynthesizerDeps,
    output_type=OrchestratorResult,
    system_prompt="당신은 여행 일정 완성 전문가입니다. 제공된 데이터를 바탕으로 OrchestratorResult JSON을 반환하라.",
)


def _build_synthesizer_prompt(d: SynthesizerDeps) -> str:
    info = d.itinerary_info
    po = d.planner_output
    destinations = info.get("destinations") or []
    dest_str = " → ".join(dest["city"] for dest in destinations) if destinations else "여행지"
    budget = info.get("budget")
    adults = info.get("adult_count", 1)
    children = info.get("child_count", 0)
    total_people = adults + children
    all_dates = _all_dates(destinations) if destinations else []

    print(
        f"\n[synthesizer_agent] _build_synthesizer_prompt 호출\n"
        f"  destinations={dest_str}, start={destinations[0]['start_date'] if destinations else ''}, "
        f"end={destinations[-1]['end_date'] if destinations else ''}\n"
        f"  budget={budget}, adults={adults}\n"
        f"  ai_summary       : {d.ai_summary}\n"
        f"  preferences      : {d.preferences}\n"
        f"  similar_messages : {len(d.similar_messages)}건\n"
        f"  planner_output   : days={len(po.days)}일, flights={len(po.selected_flights)}편, hotels={len(po.selected_hotels)}개\n"
        f"  place_results    : {len(d.place_results)}건\n"
        f"  route_results    : {len(d.route_results)}건\n"
        f"  attraction_prices: {len(d.attraction_prices)}건\n"
        f"  existing_day_plans: {list(info['day_plans'].keys()) if info.get('day_plans') else None}",
        flush=True,
    )

    lines = [
        "당신은 여행 일정 완성 전문가입니다.",
        "플래너가 확정한 항공·숙소·방문 순서와 장소 검색·동선 데이터를 바탕으로",
        "완전한 OrchestratorResult를 작성하라.",
        "",
        "## 여행 기본 전제 (항상 준수)",
        "- 이 여행의 출발지는 대한민국이다. 항공 출발 공항은 인천국제공항(ICN) 또는 김포공항(GMP)이다.",
        f"- 여행 경로: 한국 → {dest_str} → 한국",
        "- 1일차 첫 항목: 한국 출발 항공 이동 (leg_index=0 depart 편 사용). cost는 '선택된 항공편' price_original·currency 그대로 사용. cost=null 절대 금지.",
        "  당일 도착 예) {\"plan_name\": \"인천국제공항(ICN) → 도쿄 나리타(NRT) 항공 이동 (XX항공)\", \"time\": \"09:00 ~ 11:30\", \"cost\": {\"amount\": 850000, \"currency\": \"KRW\"}, \"note\": \"출발 09:00 ICN (KST+9) | 도착 11:30 NRT (JST+9) | 총 비행시간 약 2h 30m | 시차 0h\"}",
        "⚠️ 다음날 도착 항공(+1일) 처리 규칙 — 반드시 준수:",
        "   출발일 day_plans 키: 공항 이동 항목 + 항공 이동 항목(time='출발시각 ~ 23:59')만 포함. 체크인·식사·관광 절대 금지.",
        "   출발일 항공 이동 항목 cost: '선택된 항공편' 섹션의 price_original·currency 반드시 기재 (cost=null 절대 금지).",
        "   도착일 day_plans 첫 항목(반드시 추가): {\"plan_name\": \"[항공사] 기내 (비행 중) → [공항코드] 도착\", \"time\": \"00:00 ~ 도착지현지시각\", \"cost\": null}",
        "   도착일 이후 항목: 공항 → 숙소 이동 + 체크인 + 식사 등 도착 후 활동.",
        "   예) '2026-12-20': [{\"plan_name\":\"숙소→ICN 이동\",\"time\":\"09:00~11:00\",...}, {\"plan_name\":\"ICN→STN 항공 이동 (XX항공)\",\"time\":\"11:30~23:59\",\"cost\":{\"amount\":850,\"currency\":\"GBP\",\"amount_krw\":1500000},...}]",
        "       '2026-12-21': [{\"plan_name\":\"XX항공 기내 (비행 중) → STN 도착\",\"time\":\"00:00~16:45\",\"cost\":null}, ...]",
        "- 도시 이동일 첫 항목: 도시 간 이동 항공 (해당 구간 connect 편 사용). cost는 '선택된 항공편' price_original·currency 그대로 사용. cost=null 절대 금지.",
        "  예) {\"plan_name\": \"파리 샤를드골(CDG) → 로마 피우미치노(FCO) 항공 이동 (항공사명)\", \"time\": \"HH:MM ~ HH:MM\", \"cost\": {\"amount\": price_original, \"currency\": currency}}",
        f"- 마지막날 마지막 항목: 한국 귀국 항공 이동 (return 편 사용). cost는 '선택된 항공편' price_original·currency 그대로 사용. cost=null 절대 금지.",
        "  예) {\"plan_name\": \"로마 피우미치노(FCO) → 인천국제공항(ICN) 귀국 항공 (항공사명)\", \"time\": \"HH:MM ~ HH:MM\", \"cost\": {\"amount\": price_original, \"currency\": currency}}",
        f"  ⚠️ 귀국편 도착 날짜는 반드시 여행 마지막 날({destinations[-1]['end_date'][:10]})이어야 한다.",
        f"  한국 도착이 {destinations[-1]['end_date'][:10]} 이후로 넘어가는 일정은 절대 금지.",
        f"  day_plans에 {destinations[-1]['end_date'][:10]} 이후 날짜 키가 생기면 안 된다.",
        "- 각 항공 이동 항목 직전: 공항 이동 항목 삽입 (출발 2~3시간 전 기준, 새벽 출발이면 심야 이동도 그대로 기재)",
        "  예) plan_name='숙소 → 인천국제공항 이동 (공항버스/택시)', time='01:30 ~ 03:00'",
        "",
        "## 도시별 일정 배정",
    ]
    for dest in destinations:
        lines.append(f"- {dest['city']}: {dest['start_date']} ~ {dest['end_date']}")

    lines += [
        "",
        "## 필수 출력",
        "- `message`: 아래 기준으로 작성한다.",
        "  ⚠️ message는 사용자에게 직접 노출되는 자연스러운 한국어 안내문이다.",
        "     day_plans, change, reservation 같은 JSON 필드명·내부 키·기술 용어를 절대 포함하지 않는다.",
        "     시스템 내부 처리 과정(데이터 반환 방식, JSON 구조 등)을 설명하는 문장도 절대 쓰지 않는다.",
        "  - 기존 일정(## 기존 일정)이 없으면 신규 생성: 날짜별 주요 코스를 간략히 소개한다.",
        f"    예) '1일차는 {destinations[0]['city'] if destinations else '첫 번째 도시'} 도착 후 시내 탐방, 2일차는..."
        if destinations else "    예) '1일차는 도착 후 시내 탐방...'",
        "  - 기존 일정(## 기존 일정)이 있으면 수정: 반영한 요청과 변경 결과를 구체적으로 설명한다.",
        "- `day_plans`: 키='YYYY-MM-DD'. 아래 규칙에 따라 반환할 날짜가 결정된다:",
        "  ① 신규 생성(기존 일정 없음): 아래 [## 반드시 포함해야 할 전체 날짜 목록]의 모든 날짜.",
        f"    ⚠️ day_plans 키 수는 반드시 {len(all_dates)}개여야 한다. 단 1일도 누락 불가.",
        "  ② 날짜 변경 수정: [## 날짜 변경 재계획 대상] 섹션의 날짜만 반환. 나머지는 포함하지 않는다.",
        "  ③ 일반 수정(날짜 변경 없음): 사용자가 요청한 날짜만 반환. 나머지는 포함하지 않는다.",
        "  ⚠️ 각 날짜의 값은 비어 있으면 절대 안 됨 — {} 또는 [] 반환 절대 금지.",
        "  이동일·경유일·항공 탑승일도 포함. 활동이 없으면 이동 항목 1개라도 반드시 추가.",
        "- `ai_summary`: 번호 목록 형식으로 작성한다.",
        "  형식: 각 항목을 '1. 2. 3.' 번호로 나열. 항목당 한 줄로 핵심 사실만 기술.",
        "  이전 대화 요약(## 이전 대화 요약)이 있으면 기존 항목을 유지하고 이번 내용을 새 번호로 추가한다.",
        "- `preferences`: 아래 [## preferences 추출 규칙] 참고. 반드시 작성할 것.",
        "",
        "## preferences 추출 규칙",
        "⚠️ 반드시 지켜야 할 원칙: **사용자가 직접 말한 내용에서만 추출한다.**",
        "AI가 생성한 일정 내용을 보고 취향을 역추론하지 말 것.",
        "추출 가능한 카테고리: food, food_avoid, transport, accommodation, activities, pace, budget_style 등.",
        "기존 ## 사용자 취향이 있으면 그 내용을 포함한 전체 dict를 반환한다.",
        "새로 감지된 취향이 없고 기존 취향도 없으면 빈 dict {}를 반환한다.",
        "",
        "## day_plans 각 항목 형식",
        '{"plan_name":"...", "time":"HH:MM ~ HH:MM", "place":"...", "note":"...", "cost":null 또는 {"amount":숫자,"currency":"코드"} 또는 {"amount":숫자,"currency":"비KRW코드","amount_krw":정수}}',
        '  ※ currency="KRW"이면 amount_krw 생략. 식사·교통·입장료는 amount_krw 생략.',
        "⚠️ 각 날짜의 항목 배열은 반드시 시작 시각 오름차순으로 정렬한다.",
        "   예) 잘못된 순서: [항공이동(20:07~23:59), 공항이동(18:30~19:30)] → 올바른 순서: [공항이동(18:30~19:30), 항공이동(20:07~23:59)]",
        "   00:00으로 시작하는 기내 연속 항목은 해당 날짜의 첫 번째 항목.",
        "",
        "## time 필드 규칙 — 반드시 준수",
        '⚠️ time은 반드시 "HH:MM ~ HH:MM" 형식의 24시간제 숫자만 사용한다.',
        "⚠️ '익일 아침', '다음날', '오전 중', '(+1일)', '(+2일)' 같은 텍스트 표현은 time 필드에 절대 사용 금지.",
        "⚠️ 시각 기준 원칙: 모든 time 필드는 해당 활동이 이루어지는 장소의 현지 시각 기준.",
        "   한국 내 활동(출발 준비, 공항 이동, 귀국 후): KST(UTC+9)",
        "   해외 목적지 활동(관광, 식사, 이동, 체크인 등): 해당 도시 현지 시각",
        "   예) 런던 관광 10:00~12:00(GMT): time='10:00 ~ 12:00'  /  취리히 식사 19:30(CET): time='19:30 ~ 21:00'",
        "⚠️ 항공 이동 항목 시각 규칙: 출발 = 출발지 현지 시각, 도착 = 도착지 현지 시각.",
        "   당일 도착: time='출발지현지시각 ~ 도착지현지시각'  예) '09:00 ~ 11:30'",
        "   다음날 도착: 출발일 time='출발지현지시각 ~ 23:59'  예) '20:07 ~ 23:59'",
        "   (도착일 첫 항목으로 '00:00 ~ 도착지현지시각' 기내 연속 항목 추가 — 위 (+1일) 규칙 참고)",
        "   항공 이동 항목 note에 반드시 포함: '출발 HH:MM (공항코드, 시간대) | 도착 HH:MM (공항코드, 시간대) | 총 비행시간 약 Xh Ym | 시차 ±Yh | 직항 or N회 경유'",
        "   예) note='출발 11:30 ICN (KST+9) | 도착 16:45 STN (GMT+0) | 총 비행시간 약 13h 15m | 시차 -9h | 직항'",
        "   예) note='출발 09:00 ICN (KST+9) | 도착 14:30 CDG (CET+1) | 총 비행시간 약 12h 30m | 시차 -8h | 1회 경유'",
        "⚠️ 비행시간: ## 선택된 항공편의 duration 값을 그대로 사용. 직접 계산 절대 금지.",
        "⚠️ 하루의 끝은 23:59로 표기한다. 24:00은 절대 사용하지 않는다. 다음 날 시작은 반드시 00:00 사용.",
        "   예) 전날 항공: time='20:07 ~ 23:59' / 다음날 기내 연속: time='00:00 ~ 21:22'",
        "⚠️ 항공 외 일반 일정은 자정을 넘으면 별도 항목 분리.",
        "올바른 예) '09:00 ~ 10:30', '20:07 ~ 23:59', '00:00 ~ 21:22', '23:30 ~ 23:59', '00:00 ~ 02:15'",
        "",
        "## 식사 배치 규칙",
        "- 일반 날: 아침(08:00~09:00), 점심(12:00~13:30), 저녁(18:30~20:00) 3회 포함",
        "- 항공 도착 날: 도착 시간 + 시내 이동(약 1.5h) 이후부터 가능한 첫 식사부터 시작",
        "- 항공 출발 날: 아침 09:00부터 시작. 탑승 시간 2~3시간 전 공항 이동 전까지 관광·식사 일정 진행.",
        "  ⚠️ 출발 전 빈 시간이 생기면 '자유 시간 (산책·쇼핑 등)' 항목으로 반드시 채울 것. 공항 이동 항목만 덜렁 있으면 절대 안 됨.",
        "- 이동 5분 이상: 별도 이동 항목 삽입 (plan_name: '{출발} → {도착} 이동 ({수단})')",
        "- 이동 수단: 대중교통(지하철·버스·트램·KTX·SRT·열차·고속버스·시외버스) / 도보 / 택시.",
        "- 자차·자가용·렌터카는 사용자가 직접 요청한 경우에만 이동 항목으로 넣는다. 사용자가 말하지 않았으면 임의로 넣지 않는다.",
        "- 비 예보 날: 실내 위주 배치 후 note에 날씨 안내",
        "",
        "## cost 작성 규칙",
        "⚠️ 모든 cost는 전체 인원 합산 금액 기준.",
        "⚠️ cost=null은 진짜 무료인 경우만. 금액 모를 때도 null. 절대 0 금액 쓰지 말 것.",
        "⚠️ currency='KRW'이면 amount_krw 필드를 절대 작성하지 말 것 (생략 = null).",
        "⚠️ 식사·교통·입장료는 amount_krw 절대 작성 금지 — 서버가 자동 환산함.",
        "",
        "- 항공 이동 항목: API 검색 결과의 price_original·currency 그대로 사용. cost=null 절대 금지.",
        "  ⚠️ '선택된 항공편' 섹션에서 해당 leg_index의 price_original·currency·price_krw를 반드시 찾아 기재.",
        "  (API가 성인/아이 요금을 구분해 이미 합산한 금액을 반환함 — 인원 수 곱하기 금지)",
        "  price_krw가 있고 currency != 'KRW'이면 amount_krw에 기입. currency='KRW'이면 amount_krw 생략.",
        '  예) currency="JPY", price_original=85000, price_krw=780000',
        '      → {"amount": 85000, "currency": "JPY", "amount_krw": 780000}',
        '  예) currency="KRW", price_original=775570',
        '      → {"amount": 775570, "currency": "KRW"}  ← amount_krw 없음',
        "",
        "- 숙소 체크인 항목: API 검색 결과의 price_original·currency 그대로 사용.",
        "  (API가 전체 숙박 기간 합산 금액을 반환함 — 박 수 곱하기 금지)",
        "  currency != 'KRW'이고 price_krw 있으면 amount_krw에 기입. price_krw 없으면 cost=null.",
        '  예) currency="EUR", price_original=300, price_krw=453000',
        '      → {"amount": 300, "currency": "EUR", "amount_krw": 453000}',
        "",
        "- 식사: 현지 물가 기준 1인 추정액 × 전체 인원. amount_krw 절대 작성 금지.",
        f"  성인 {adults}명 + 어린이 {children}명 = 총 {total_people}명",
        "",
        "- 대중교통(지하철·버스·트램 등): 동선 결과에 fare(1인 요금)가 있으면 반드시 그 값 사용. amount_krw 절대 작성 금지.",
        f"  fare 있음: 성인 요금(fare.value) × {adults} + 어린이 요금(fare.value × 0.5~0.7) × {children}",
        f"  fare 없음: 현지 물가 기준 추정. 성인 요금 × {adults} + 어린이 요금(성인의 약 50~70%) × {children}",
        "  5세 이하 무료인 경우 많음. 무료 확실하면 cost=null.",
        "- 택시: 1대 요금(인원 무관, 탑승 인원 곱하기 금지). 동선 결과의 distance_text 기준으로 아래 참고 요금 적용.",
        "  서울 기본 4,800원 + 약 100원/100m | 도쿄 730JPY + 약 100JPY/300m",
        "  파리 4EUR + 약 1.5EUR/km | 뉴욕 3USD + 약 2USD/km | 방콕 35THB + 약 6THB/km",
        "  distance_text 없으면 현지 물가 기준 추정. amount_krw 절대 작성 금지.",
        "- 자차·자가용: 별도 요금을 확정할 수 없으면 cost=null. 주차비·통행료가 명확한 경우만 합산 금액 작성.",
        "- 렌터카: 사용자가 직접 요청한 경우에만 작성. 요금이 명확하지 않으면 cost=null.",
        "- 입장료: Tavily 검색 결과가 있으면 그 값 우선 사용. 없으면 price_level(0=무료·1=저렴·2=보통·3=비쌈·4=매우 비쌈) 참고해 추정.",
        "  price_level=0이면 cost=null. 성인·아이 요금 구분 적용. amount_krw 절대 작성 금지.",
        "  [부분 유료 규칙] Tavily 결과에 무료 입장 + 유료 구역 혼재(예: 공원 입장 무료·특별관 유료, 외부 무료·내부 유료)가 언급된 경우:",
        f"    cost는 유료 구역 기준 금액(성인 {adults}명 + 어린이 {children}명 합산)으로 작성한다.",
        "    note에 '입장 자체는 무료이나 [유료 구역/시설명] 등 일부는 별도 요금 발생' 형식으로 설명을 추가한다.",
        "",
        "  ⚠️ 통화별 단가 규모 — 반드시 이 범위를 지킬 것:",
        "     EUR: 식사 1인당 15~50 EUR / 지하철 1인당 2~5 EUR / 입장료 1인당 10~30 EUR",
        "     USD: 식사 1인당 15~50 USD / 지하철 1인당 2~5 USD",
        "     JPY: 식사 1인당 800~3,000 JPY / 지하철 1인당 200~600 JPY / 입장료 1인당 500~2,000 JPY",
        "     KRW: 식사 1인당 8,000~20,000 KRW / 지하철 1인당 1,400~2,000 KRW",
        f'  예) 뮌헨 저녁 성인 1인 35 EUR × {adults}명 + 어린이 25 EUR × {children}명',
        f'      → {{"amount": {35 * adults + 25 * children}, "currency": "EUR"}}',
        f'  예) 도쿄 지하철 성인 230 JPY × {adults}명 + 어린이 120 JPY × {children}명',
        f'      → {{"amount": {230 * adults + 120 * children}, "currency": "JPY"}}',
        "- 무료(공원·야경·산책 등): cost=null",
    ]

    existing_plans = info.get("day_plans")
    if not existing_plans and all_dates:
        lines += [
            "",
            f"## 반드시 포함해야 할 전체 날짜 목록 (총 {len(all_dates)}일 — day_plans 키로 하나도 빠짐없이 추가)",
        ]
        for dt in all_dates:
            lines.append(f"  - {dt}")

    if d.replan_dates:
        lines += [
            "",
            f"## 날짜 변경 재계획 대상 (총 {len(d.replan_dates)}일 — 반드시 새로 계획)",
            "여행 기간이 변경되어 교통 이동일 포함 아래 날짜들을 새로 계획해야 한다.",
            "⚠️ 아래 날짜는 기존 일정에 있던 내용을 무시하고 반드시 새로 작성.",
            "⚠️ 그 외 날짜는 절대 포함하지 않는다.",
        ]
        for dt in d.replan_dates:
            lines.append(f"  - {dt}")

    if existing_plans:
        lines += ["", "## 기존 일정 (변경 없는 날짜의 참고용 — 재계획 대상 날짜는 무시할 것)"]
        for date_key, items in existing_plans.items():
            lines.append(f"### {date_key}")
            for item in items:
                lines.append(f"  - {item.get('time','')} {item.get('plan_name','')} ({item.get('place','')})")

    lines += ["", "## 선택된 항공편"]
    for fl in po.selected_flights:
        stops_str = "직항" if fl.stops == 0 else f"{fl.stops}회 경유"
        lines.append(
            f"- [leg_index={fl.leg_index}, {fl.direction}] {fl.airline} | "
            f"{fl.origin}→{fl.destination} | "
            f"{fl.departing_at} ~ {fl.arriving_at} | 비행시간 {fl.duration} | {fl.price_krw:,}원 ({fl.currency} {fl.price_original}) | {stops_str}"
        )

    lines += ["", "## 선택된 숙소"]
    for h in po.selected_hotels:
        price_str = f"{h.price_krw:,}원" if h.price_krw else "가격정보없음"
        lines.append(
            f"- {h.city}: {h.name} | {h.address} | "
            f"{h.check_in} ~ {h.check_out} | {price_str} ({h.currency} {h.price_original})"
        )

    lines += ["", "## 날씨 (도시별)"]
    for dest in destinations:
        city = dest["city"]
        weather = d.weather_by_city.get(city, [])
        if weather:
            lines.append(f"### {city}")
            for w in weather:
                lines.append(
                    f"  - {w.get('date')}: {w.get('weather','')} "
                    f"최고{w.get('temperature_max', w.get('temperature_2m_max','?'))}°C "
                    f"강수{w.get('precipitation_probability_max', w.get('precipitation_sum','?'))}%"
                )

    lines += ["", "## 장소 검색 결과 (search_place)"]
    for query, result in d.place_results.items():
        if result.get("status") == "success":
            places = result.get("data", {}).get("places", [])
            if places:
                p = places[0]
                price_label = p.get("price_level_label")
                price_str = f" | 가격대: {price_label}(0~4 상대 척도)" if price_label else ""
                lines.append(
                    f"- [{query}] → {p.get('name')} | {p.get('formatted_address','')} | "
                    f"평점 {p.get('rating','?')} ({p.get('user_ratings_total','?')}명){price_str}"
                )

    if d.attraction_prices:
        lines += [
            "",
            "## 관광지 입장료 (Tavily 검색 결과)",
            f"⚠️ cost 산출 시 반드시 성인 {adults}명 + 어린이 {children}명 인원 합산 금액으로 작성한다.",
            "⚠️ 아래 정보에 '부분 유료'·'일부 무료'·'free admission'·'paid sections' 등이 있으면 아래 [부분 유료 규칙]을 적용한다.",
        ]
        for name, content in d.attraction_prices.items():
            lines.append(f"- {name}: {content}")

    lines += ["", "## 동선 결과 (find_route)"]
    for key, result in d.route_results.items():
        orig, dest_place = key.split("||", 1)
        if result.get("status") == "success":
            routes = result.get("data", {}).get("routes", [])
            if routes:
                r = routes[0]
                fare = r.get("fare")
                fare_info = f" | 1인 요금: {fare['text']} ({fare['currency']})" if fare else ""
                lines.append(f"- {orig} → {dest_place}: {r.get('duration_text','')} ({r.get('distance_text','')}){fare_info}")

    if budget:
        lines += ["", f"## 예산 제약: 총 {budget:,.0f}원 (성인 {adults}명 기준)"]

    if d.preferences:
        lines += [
            "",
            "## 사용자 취향",
            json.dumps(d.preferences, ensure_ascii=False, indent=2),
            "⚠️ 취향 반영 원칙: 취향은 일정에 적절히 반영하되 과도하게 편중되지 않게 한다.",
            "- food: 전체 식사(아침·점심·저녁) 중 1~2회만 포함. 나머지는 현지 다양한 음식으로 구성.",
            "- activities: 선호 활동을 일부 포함하되 관광지·문화 체험 등 다양한 일정과 균형을 맞춤.",
            "- 그 외 취향도 '힌트'로 참고하며, 모든 항목에 적용하지 않는다.",
        ]
    if d.ai_summary:
        lines += ["", "## 이전 대화 요약", d.ai_summary]
    if d.similar_messages:
        msgs = "\n".join(f"[{m['role']}] {m['content']}" for m in d.similar_messages)
        lines += ["", "## 참고할 과거 대화", msgs]

    lines += ["", "## 여행지 정보 요약 (도시별)"]
    for dest in destinations:
        city = dest["city"]
        summary = d.web_summaries.get(city, "정보 없음")
        lines += [f"### {city}", summary]

    return "\n".join(lines)


# ── Phase 1 헬퍼 함수들 ──────────────────────────────────────────────────

async def _fetch_web_summary(city: str, preferences: dict | None) -> str:
    queries = [
        f"{city} tourist attractions sightseeing must-visit",
        f"{city} local food restaurants best places to eat",
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
        return f"{city} 여행 정보를 찾지 못했습니다."
    combined = "\n\n".join(snippets[:10])
    pref_hint = ""
    if preferences:
        pref_hint = f"\n\n사용자 취향: {json.dumps(preferences, ensure_ascii=False)}\n위 취향을 참고하여 관련 정보를 일부 포함하되, 현지 대표 음식·명소 등 다양한 여행 정보의 균형을 유지해줘."
    result = await preprocessor_agent.run(
        f"아래 검색 결과를 여행 계획에 유용한 핵심 정보 위주로 간결하게 요약해줘.{pref_hint}\n\n{combined}"
    )
    return result.output


async def _fetch_web_summaries(destinations: list[dict], preferences: dict | None) -> dict[str, str]:
    """모든 목적지의 웹 요약을 병렬로 수집한다. 반환: {city → 요약}"""
    cities = [d["city"] for d in destinations]
    results = await asyncio.gather(
        *[_fetch_web_summary(city, preferences) for city in cities],
        return_exceptions=True,
    )
    return {
        city: (r if not isinstance(r, Exception) else f"{city} 여행 정보를 찾지 못했습니다.")
        for city, r in zip(cities, results)
    }


async def _fetch_weather(city: str, start_date: str, end_date: str, today: str) -> list[dict]:
    try:
        city_short = city.split(",")[0].strip()
        start_dt = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_date[:10], "%Y-%m-%d").date()
        today_dt = datetime.strptime(today[:10], "%Y-%m-%d").date()
        total_days = max((end_dt - start_dt).days + 1, 1)
        days_until = (start_dt - today_dt).days

        if days_until <= 16:
            result = await _service.process_task("weather", "get_weather", {
                "city": city_short,
                "forecast_days": min(total_days, 16),
            })
        else:
            last_year_start = start_dt.replace(year=start_dt.year - 1)
            last_year_end = end_dt.replace(year=end_dt.year - 1)
            result = await _service.process_task("weather", "get_historical_weather", {
                "city": city_short,
                "start_date": str(last_year_start),
                "end_date": str(last_year_end),
            })
        return result.get("data", []) if result.get("status") == "success" else []
    except Exception:
        return []


async def _fetch_weather_all(destinations: list[dict], today: str) -> dict[str, list[dict]]:
    """모든 목적지의 날씨를 병렬로 수집한다. 반환: {city → list[dict]}"""
    results = await asyncio.gather(
        *[_fetch_weather(d["city"], d["start_date"], d["end_date"], today) for d in destinations],
        return_exceptions=True,
    )
    return {
        d["city"]: (r if not isinstance(r, Exception) else [])
        for d, r in zip(destinations, results)
    }


async def _fetch_flight_legs(
    destinations: list[dict],
    cities_en: list[str],
    adults: int,
    children: int,
    child_ages: list,
) -> list[dict]:
    """모든 항공 구간을 병렬로 검색한다.

    구간 구성 (N개 도시):
      leg 0       : 한국(Seoul) → cities_en[0]          depart
      leg 1..N-1  : cities_en[i-1] → cities_en[i]       connect
      leg N       : cities_en[-1] → 한국(Seoul)          return
    """
    leg_info: list[dict] = []
    tasks = []

    # Depart
    tasks.append(_service.process_task("duffel_flight", "search_flights", {
        "origin": _DEFAULT_ORIGIN,
        "destination": cities_en[0],
        "departure_date": destinations[0]["start_date"][:10],
        "adults": adults, "children": children, "child_ages": child_ages,
    }))
    leg_info.append({"leg_index": 0, "direction": "depart", "from": _DEFAULT_ORIGIN, "to": cities_en[0]})

    # Connect legs
    for i in range(1, len(destinations)):
        tasks.append(_service.process_task("duffel_flight", "search_flights", {
            "origin": cities_en[i - 1],
            "destination": cities_en[i],
            "departure_date": destinations[i]["start_date"][:10],
            "adults": adults, "children": children, "child_ages": child_ages,
        }))
        leg_info.append({"leg_index": i, "direction": "connect", "from": cities_en[i - 1], "to": cities_en[i]})

    # Return — end_date와 end_date-1 양쪽 검색 후 합산 (장거리 노선 대응)
    end_dt = datetime.strptime(destinations[-1]["end_date"][:10], "%Y-%m-%d").date()
    return_common = {
        "origin": cities_en[-1],
        "destination": _DEFAULT_ORIGIN,
        "adults": adults, "children": children, "child_ages": child_ages,
    }
    tasks.append(_service.process_task("duffel_flight", "search_flights", {
        **return_common, "departure_date": str(end_dt),
    }))
    tasks.append(_service.process_task("duffel_flight", "search_flights", {
        **return_common, "departure_date": str(end_dt - timedelta(days=1)),
    }))
    return_leg_info = {
        "leg_index": len(destinations), "direction": "return",
        "from": cities_en[-1], "to": _DEFAULT_ORIGIN,
    }

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 귀국편 두 날짜 결과 병합
    non_return = results[:-2]
    r_end, r_prev = results[-2], results[-1]
    data_end = r_end.get("data", []) if not isinstance(r_end, Exception) and r_end.get("status") == "success" else []
    data_prev = r_prev.get("data", []) if not isinstance(r_prev, Exception) and r_prev.get("status") == "success" else []
    merged_data = data_end + data_prev

    # end_date 이내 도착편만 남김 (arriving_at[:10] <= end_date)
    end_date_str = str(end_dt)
    valid_data = [f for f in merged_data if f.get("arriving_at", "")[:10] <= end_date_str]
    if valid_data:
        if len(valid_data) < len(merged_data):
            _log.info(
                "[return flight] end_date(%s) 초과 도착편 제외: %d → %d개",
                end_date_str, len(merged_data), len(valid_data),
            )
        merged_data = valid_data
    else:
        _log.warning("[return flight] end_date(%s) 이내 도착편 없음 — 전체 결과 제공", end_date_str)

    merged_return: dict = {
        "status": "success" if merged_data else "error",
        "data": merged_data,
        "count": len(merged_data),
    }
    if not merged_data:
        merged_return["message"] = "귀국편 검색 결과 없음"

    legs = [
        {**info, "data": (r if not isinstance(r, Exception) else {"status": "error", "message": str(r)})}
        for info, r in zip(leg_info, non_return)
    ]
    legs.append({**return_leg_info, "data": merged_return})
    return legs


async def _fetch_hotels(
    city_en: str,
    start_date: str,
    end_date: str,
    adults: int,
    children: int,
    child_ages: list,
) -> dict:
    try:
        return await _service.process_task("duffel_accommodation", "search_hotels", {
            "city_name": city_en,
            "check_in": start_date[:10],
            "check_out": end_date[:10],
            "adults": adults,
            "children": children,
            "child_ages": child_ages,
        })
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def _fetch_hotels_all(
    cities_en: list[str],
    destinations: list[dict],
    adults: int,
    children: int,
    child_ages: list,
) -> dict[str, dict]:
    """모든 목적지의 숙소를 병렬로 검색한다. 반환: {city(한국어) → 숙소 결과}"""
    results = await asyncio.gather(
        *[_fetch_hotels(city_en, d["start_date"], d["end_date"], adults, children, child_ages)
          for city_en, d in zip(cities_en, destinations)],
        return_exceptions=True,
    )
    return {
        d["city"]: (r if not isinstance(r, Exception) else {"status": "error"})
        for d, r in zip(destinations, results)
    }


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


_ATTRACTION_TYPES = frozenset({
    "tourist_attraction", "museum", "amusement_park", "zoo", "aquarium",
    "art_gallery", "stadium", "bowling_alley", "movie_theater",
})


async def _fetch_attraction_prices(place_results: dict[str, dict]) -> dict[str, str]:
    """attraction 타입 장소의 입장료·부분 유료 여부를 Tavily로 검색."""
    queries: dict[str, str] = {}
    for result in place_results.values():
        if result.get("status") != "success":
            continue
        places = result.get("data", {}).get("places", [])
        if not places:
            continue
        p = places[0]
        if not (_ATTRACTION_TYPES & set(p.get("types", []))):
            continue
        name = p.get("name", "")
        if name and name not in queries:
            queries[name] = f"{name} admission fee entrance ticket price free partial paid sections 2026"

    if not queries:
        return {}

    print(f"\n[_fetch_attraction_prices] {len(queries)}개 관광지 입장료 검색: {list(queries.keys())}", flush=True)
    results = await asyncio.gather(
        *[_service.process_task("tavily_search", "search", {
            "query": q, "search_depth": "basic", "max_results": 3,
        }) for q in queries.values()],
        return_exceptions=True,
    )

    attraction_prices: dict[str, str] = {}
    for name, result in zip(queries.keys(), results):
        if isinstance(result, Exception) or result.get("status") != "success":
            continue
        items = result.get("data", [])
        if items:
            combined = " | ".join(item.get("content", "")[:200] for item in items[:3])
            attraction_prices[name] = combined[:600]

    return attraction_prices


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
        addrs = [a for a in addrs if a]
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

def _estimate_total_krw(day_plans: dict) -> int:
    """day_plans의 모든 cost를 원화로 합산. amount_krw 없는 외화 항목은 제외(보수적 추정)."""
    total = 0
    for items in day_plans.values():
        for item in items:
            cost = item.cost if hasattr(item, "cost") else item.get("cost")
            if cost is None:
                continue
            currency = cost.currency if hasattr(cost, "currency") else cost.get("currency")
            amount = cost.amount if hasattr(cost, "amount") else cost.get("amount", 0)
            amount_krw = cost.amount_krw if hasattr(cost, "amount_krw") else cost.get("amount_krw")
            if currency == "KRW":
                total += int(amount)
            elif amount_krw is not None:
                total += int(amount_krw)
    return total


async def run_itinerary_pipeline(
    deps,           # OrchestratorDeps
    user_message: str,
    history: list,
) -> AsyncGenerator[str | OrchestratorResult, None]:
    """
    destinations 배열이 없거나 start_date가 없으면 아무것도 yield하지 않고 종료.
    호출자는 OrchestratorResult를 받지 못하면 orchestrator로 폴백한다.
    """
    itinerary = deps.current_itinerary
    if not itinerary:
        return

    destinations = itinerary.get("destinations") or []
    if not destinations or not itinerary.get("start_date"):
        return

    adults = itinerary.get("adult_count") or 1
    children = itinerary.get("child_count") or 0
    child_ages = itinerary.get("child_ages") or []
    budget = itinerary.get("budget")

    # 날짜 변경 감지: 기존 day_plans가 있으면 교통 이동일 제거 + 재계획 날짜 산출
    replan_dates: list[str] = []
    if itinerary.get("day_plans"):
        adjusted_day_plans, replan_dates = _get_replan_dates_for_date_change(itinerary)
        if replan_dates:
            itinerary = {**itinerary, "day_plans": adjusted_day_plans}

    # 도시명 영문 변환 (전체 목적지 일괄 처리 — 단일 LLM 호출)
    cities_kr = [d["city"] for d in destinations]
    cities_en = await _extract_english_cities(cities_kr)

    print(
        f"\n[run_itinerary_pipeline] 파이프라인 시작"
        f"\n  destinations : {cities_kr}"
        f"\n  cities_en    : {cities_en}"
        f"\n  start={destinations[0]['start_date']}, end={destinations[-1]['end_date']}"
        f"\n  adults={adults}, children={children}",
        flush=True,
    )

    # ── Phase 1: 병렬 데이터 수집 ──────────────────────────────────────
    web_summaries, weather_by_city, flight_legs, hotels_by_city = await asyncio.gather(
        _fetch_web_summaries(destinations, deps.preferences),
        _fetch_weather_all(destinations, deps.today),
        _fetch_flight_legs(destinations, cities_en, adults, children, child_ages),
        _fetch_hotels_all(cities_en, destinations, adults, children, child_ages),
    )

    print(
        f"\n[run_itinerary_pipeline] Phase 1 완료"
        f"\n  web_summaries  : {list(web_summaries.keys())}"
        f"\n  weather_by_city: {[(k, len(v)) for k, v in weather_by_city.items()]}"
        f"\n  flight_legs    : {[(l['direction'], l['data'].get('status')) for l in flight_legs]}"
        f"\n  hotels_by_city : {[(k, v.get('status')) for k, v in hotels_by_city.items()]}",
        flush=True,
    )

    # ── Phase 2: 플래너 LLM 1회 ────────────────────────────────────────
    planner_deps = PlannerDeps(
        itinerary_info=itinerary,
        web_summaries=web_summaries,
        weather_by_city=weather_by_city,
        flight_legs=flight_legs,
        hotels_by_city=hotels_by_city,
        cities_en=cities_en,
        preferences=deps.preferences,
        ai_summary=deps.ai_summary,
        today=deps.today,
        similar_messages=deps.similar_messages,
        replan_dates=replan_dates,
    )
    planner_context = _build_planner_prompt(planner_deps)
    planner_result = await planner_agent.run(
        f"{planner_context}\n\n---\n\n사용자 메시지: {user_message}",
        deps=planner_deps,
        message_history=history,
    )
    planner_output: PlannerOutput = planner_result.output

    # 초기 일정 생성 시 플래너가 날짜를 누락한 경우 코드 레벨 보정
    if not itinerary.get("day_plans"):
        all_dates = _all_dates(destinations)
        existing_date_set = {day.date for day in planner_output.days}
        missing_dates = [dt for dt in all_dates if dt not in existing_date_set]
        if missing_dates:
            _log.warning("[planner] 누락 날짜 발견, 자동 보완: %s", missing_dates)
            # 누락된 날짜에 해당하는 도시 배정 (날짜 범위 기준)
            city_for_date: dict[str, str] = {}
            for dest in destinations:
                d_start = datetime.strptime(dest["start_date"][:10], "%Y-%m-%d").date()
                d_end = datetime.strptime(dest["end_date"][:10], "%Y-%m-%d").date()
                curr = d_start
                while curr <= d_end:
                    city_for_date[str(curr)] = dest["city"]
                    curr += timedelta(days=1)
            added = [
                DaySchedule(date=dt, city=city_for_date.get(dt, destinations[0]["city"]), ordered_queries=[])
                for dt in missing_dates
            ]
            combined = planner_output.days + added
            combined.sort(key=lambda x: x.date)
            planner_output = PlannerOutput(
                days=combined,
                selected_flights=planner_output.selected_flights,
                selected_hotels=planner_output.selected_hotels,
            )

    print(
        f"\n[run_itinerary_pipeline] Phase 2 완료"
        f"\n  days={len(planner_output.days)}일"
        f"\n  flights={len(planner_output.selected_flights)}편"
        f"\n  hotels={len(planner_output.selected_hotels)}개",
        flush=True,
    )

    # ── Phase 3: 장소 검색 + 동선 + 관광지 입장료 병렬 ─────────────────
    place_results = await _fetch_places(planner_output)
    route_results, attraction_prices = await asyncio.gather(
        _fetch_routes(planner_output, place_results),
        _fetch_attraction_prices(place_results),
    )

    print(
        f"\n[run_itinerary_pipeline] Phase 3 완료"
        f"\n  place_results    ={len(place_results)}건"
        f"\n  route_results    ={len(route_results)}건"
        f"\n  attraction_prices={len(attraction_prices)}건",
        flush=True,
    )

    # ── Phase 4: 합성기 LLM 1회 ────────────────────────────────────────
    synth_deps = SynthesizerDeps(
        itinerary_info=itinerary,
        planner_output=planner_output,
        place_results=place_results,
        route_results=route_results,
        weather_by_city=weather_by_city,
        web_summaries=web_summaries,
        preferences=deps.preferences,
        ai_summary=deps.ai_summary,
        today=deps.today,
        similar_messages=deps.similar_messages,
        attraction_prices=attraction_prices,
        replan_dates=replan_dates,
    )
    synth_context = _build_synthesizer_prompt(synth_deps)
    async with synthesizer_agent.run_stream(
        f"{synth_context}\n\n---\n\n사용자 메시지: {user_message}",
        deps=synth_deps,
        message_history=history,
    ) as stream:
        prev_msg = ""
        async for partial in stream.stream_output():
            msg = getattr(partial, "message", None) or ""
            if len(msg) > len(prev_msg):
                yield msg[len(prev_msg):]
                prev_msg = msg

        result = await stream.get_output()

    # ── 예산 초과 확인 — cost 합산 후 초과 시 업데이트 제안 ─────────────
    if budget and result.day_plans:
        total_krw = _estimate_total_krw(result.day_plans)
        if total_krw > budget:
            budget_msg = (
                f"\n\n💡 현재 일정 기준 예상 총 비용은 약 {total_krw:,.0f}원이에요 "
                f"(설정 예산: {int(budget):,.0f}원). "
                f"여행 예산을 {total_krw:,.0f}원으로 업데이트할까요?"
            )
            yield budget_msg
            result = result.model_copy(update={"message": result.message + budget_msg})

    yield result
