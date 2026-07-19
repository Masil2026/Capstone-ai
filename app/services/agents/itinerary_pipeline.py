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
from typing import Any, AsyncGenerator

_log = logging.getLogger(__name__)

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from app.core.config import settings
from app.schemas.ai_message import OrchestratorResult
from app.services.adapters.booking_api import BookingAdapter
from app.services.adapters.google_maps import GoogleMapsAdapter
from app.services.adapters.korea_tourism_api import KoreaTourismAdapter
from app.services.adapters.tavily_search import TavilySearchAdapter
from app.services.adapters.weather_api import WeatherAdapter
from app.services.travel_agent_service import TravelAgentService
from ._base import _build_model, acquire_llm_slot, preprocessor_agent, run_with_retry, _is_rate_limit_error, _retry_wait

_service = TravelAgentService({
    "booking":              BookingAdapter(),
    "korea_tourism":        KoreaTourismAdapter(),
    "tavily_search":        TavilySearchAdapter(),
    "weather":              WeatherAdapter(),
    "google_maps":          GoogleMapsAdapter(),
})

_DEFAULT_ORIGIN = "Seoul"  # 출발지 미입력 시 폴백 — 기존 동작(서울/대한민국 기준) 유지


def _origin_ctx(origin_raw: str | None) -> dict[str, str]:
    """출발지 관련 프롬프트 문구 조립.

    미입력(origin_raw=None)이면 기존 '대한민국 — 인천국제공항(ICN)/김포공항(GMP)' 문구를 그대로 유지한다
    (하위 호환). 출발지가 있으면 해당 도시명 기준으로 일반화된 문구를 사용한다.
    """
    if not origin_raw:
        return {
            "word": "한국",
            "full": "대한민국",
            "airport_note": "항공 출발 공항은 인천국제공항(ICN) 또는 김포공항(GMP)이다.",
            "return_note": "한국(ICN/GMP)",
        }
    return {
        "word": origin_raw,
        "full": origin_raw,
        "airport_note": "실제 출발 공항은 아래 항공편 검색 결과의 공항 코드를 따른다.",
        "return_note": origin_raw,
    }


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


def _is_day_trip(itinerary: dict) -> bool:
    """여행 시작일과 종료일이 같으면(0박) 당일치기로 판단한다."""
    start = itinerary.get("start_date") or ""
    end = itinerary.get("end_date") or ""
    return bool(start) and start[:10] == end[:10]


# "귀국 항공" 포함 필수 — 합성기가 귀국편을 "… 귀국 항공 (항공사)"로 생성하므로
# 누락 시 날짜 연장 때 옛 귀국일이 재계획에서 빠져 스텁 일정만 남는다
_TRANSPORT_KEYWORDS = frozenset({"항공 이동", "기내 (비행 중)", "귀국 항공"})

# 숙소 예약 URL을 붙일 실제 '숙박 성격' 항목을 가리는 키워드.
# 이동 경로·식사 항목이 place에 호텔명을 우연히 포함해도 예약 링크가 붙지 않도록 한다.
_LODGING_URL_WORDS = ("체크인", "체크아웃", "숙박", "귀환", "휴식", "입실", "퇴실")


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
        result = await run_with_retry(
            preprocessor_agent,
            "아래 도시명들을 영문으로만 답해줘. 번호 그대로 줄바꿈으로 구분하여 반환.\n"
            "영문 도시명 외 다른 텍스트는 출력하지 마.\n"
            "Island, City, Province 같은 지역 접미사 없이 도시명만 짧게 반환.\n"
            "예) '서울' → Seoul | '도쿄' → Tokyo | '제주도' → Jeju | '방콕' → Bangkok\n\n"
            f"{city_list}",
            role="preprocessor",
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


# ── 여행지 안전 검사 (파이프라인 진입 게이트) ────────────────────────────

class SafetyVerdict(BaseModel):
    unsafe: bool            # 여행 자체가 부적절한 수준의 위험
    risk_summary: str       # 한국어 이유 요약 — 경고 메시지에 그대로 노출
    user_consented: bool    # 이미 경고했고 사용자가 진행 의사를 밝혔는지


safety_agent = Agent(
    model=_build_model("preprocessor"),
    output_type=SafetyVerdict,
    system_prompt=(
        "당신은 여행지 안전 판별기입니다. 검색 결과와 대화 맥락만 보고 SafetyVerdict를 반환하라.\n"
        "unsafe=true 기준 (아래에 해당할 때만):\n"
        "- 전쟁·내전·무력분쟁 진행 중\n"
        "- 정부의 여행금지·즉시철수 권고 수준 (한국 외교부 여행경보 3~4단계 상당)\n"
        "- 일반 관광객 입국이 사실상 불가능한 국가 (제재·미수교 등, 예: 북한)\n"
        "- 대규모 재난·전염병으로 여행 자체가 제한되는 상황\n"
        "단순 치안 주의(소매치기·시위 등)는 unsafe=false.\n"
        "검색 결과에는 과거 시점의 기사(예: 코로나19 팬데믹 시기 경보)가 섞여 있을 수 있다. "
        "오늘 날짜 기준으로 현재 유효한 경보만 근거로 삼고, 이미 해제됐거나 시점이 오래된 경보는 무시하라.\n"
        "user_consented=true 기준: 이전 대화 요약에 이 여행지의 위험 경고 기록이 있고, "
        "사용자 메시지가 그럼에도 진행하겠다는 의사(예: '그래도 짜줘', '응 진행해')인 경우."
    ),
)


async def _check_safety(
    destinations: list[dict], user_message: str, ai_summary: str | None,
    today: str | None = None,
) -> SafetyVerdict | None:
    """도시별 안전 정보를 검색한 뒤 LLM 1회로 판정. 실패 시 None (fail-open)."""
    try:
        cities = [d["city"] for d in destinations]
        raw = await asyncio.gather(*[
            _service.process_task("tavily_search", "search", {
                "query": f"{c} travel advisory safety warning 여행경보 위험",
                "search_depth": "basic",
                "max_results": 5,
            }) for c in cities
        ], return_exceptions=True)

        sections = []
        for city, r in zip(cities, raw):
            if isinstance(r, Exception) or r.get("status") != "success":
                continue
            snippets = "\n".join(
                f"- {item['title']}: {item['content'][:300]}"
                for item in r.get("data", [])[:5]
            )
            sections.append(f"## {city} 검색 결과\n{snippets}")

        if not sections:
            return None  # 검색 결과 없음 — 판단 근거 없이 차단하지 않는다

        prompt = "\n\n".join([
            *sections,
            f"## 오늘 날짜\n{today or date.today().isoformat()}",
            f"## 이전 대화 요약\n{ai_summary or '없음'}",
            f"## 사용자 메시지\n{user_message}",
        ])
        result = await run_with_retry(safety_agent, prompt, role="safety")
        return result.output
    except Exception:
        return None


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
    is_day_trip: bool = False    # 시작일==종료일(0박) 당일치기 여부
    origin: str | None = None    # 출발지 (한국어 원본, 미입력 시 None)


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

    origin_ctx = _origin_ctx(d.origin)
    lines = [
        "당신은 여행 일정 플래너입니다.",
        f"여행 경로: {origin_ctx['word']} → {dest_str} → {origin_ctx['word']} | 기간: {start} ~ {end} ({total_days}일) | 오늘: {d.today}",
        f"인원: 성인 {adults}명, 어린이 {child_str} | 총 예산: {budget_str}",
        f"출발지: {origin_ctx['full']} — {origin_ctx['airport_note']}",
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

    lines += (
        ["2. selected_hotels: 각 도시별 숙소 1개씩 선택"] if not d.is_day_trip else
        ["2. selected_hotels: 당일치기(숙박 없음)이므로 빈 배열로 반환"]
    )
    lines += [
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
        "- 단, 새벽(00:00~06:00) 출발 귀국편은 전날 심야 체크아웃과 심야 공항 이동이 필요해 일정 부담이 크다.",
        "  마지막 날 도착 제약을 지키는 오전~저녁 출발 귀국편이 있으면 그쪽을 우선하고,",
        "  새벽 편은 대안이 없거나 가격 차이가 매우 클 때만 선택한다. (장거리 노선은 예외 — 위 규칙대로 시간대 무관)",
        f"- ⚠️ 귀국편(return) 필수 제약: {origin_ctx['return_note']} 도착 일자가 반드시 여행 마지막 날({end})이어야 한다.",
        f"  arriving_at 날짜가 {end}을 초과하는 귀국편은 절대 선택 금지.",
        f"  귀국편 데이터에는 {end} 출발편과 {end} 하루 전 출발편이 모두 포함되어 있다. {end} 당일 도착 가능한 편을 우선 선택.",
        "- ⚠️ '실시간 항공편 없음' 표시된 구간은 selected_flights에 절대 포함하지 말 것. 항공편을 임의로 만들거나 추측하지 말 것.",
    ]

    if d.is_day_trip:
        lines += [
            "",
            "## 당일치기 시간 범위 제약 (위 항공편 선택 규칙보다 우선)",
            "⚠️ 당일치기이므로 왕복 모두 당일 안에 끝나야 한다 — 숙박 없음.",
            "- 출발편: 오전 중(00:00~11:00) 도착하는 편을 우선 선택해 현지 활동 시간을 최대한 확보한다.",
            "- 귀환편: 저녁(18:00~22:00) 이내 출발지 도착하는 편을 우선 선택한다. 22:00 이후 도착 편은 피한다.",
            "- 위 시간대에 맞는 편이 없으면 가장 근접한 시간대의 편을 선택하고 그 사유를 밝힌다.",
        ]

    lines += [
        "",
        "## ordered_queries 작성 규칙",
        "- 방문 순서 그대로: 아침식사 → 관광지 → 이동 → 점심 → 관광지 → 저녁 순",
        (
            "- 하루 총 4~6개 항목 (관광지 2~3개 + 식사 2~3개) — 당일치기이므로 왕복 이동시간을 고려해 무리하지 않게 구성"
            if d.is_day_trip else
            "- 하루 총 7~10개 항목 (관광지 3~5개 + 식사 3개)"
        ),
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
            if offers:
                for f in offers[:6]:
                    lines.append(
                        f"  - {f.get('airline')} | {f.get('origin')}→{f.get('destination')} | "
                        f"{f.get('departing_at','')} ~ {f.get('arriving_at','')} | "
                        f"비행시간 {f.get('duration','?')} | "
                        f"{f.get('price_krw',0):,}원 | {f.get('stops',0)}회 경유"
                    )
            else:
                lines.append("  - ⚠️ 실시간 항공편 없음 — 이 구간은 selected_flights에 절대 포함하지 말 것. 임의로 항공편을 만들어 넣지 말 것.")
        else:
            lines.append("  - 검색 실패 — 이 구간은 selected_flights에 절대 포함하지 말 것.")

    if not d.is_day_trip:
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
    is_day_trip: bool = False           # 시작일==종료일(0박) 당일치기 여부
    origin: str | None = None           # 출발지 (한국어 원본, 미입력 시 None)


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

    origin_ctx = _origin_ctx(d.origin)
    lines = [
        "당신은 여행 일정 완성 전문가입니다.",
        "플래너가 확정한 항공·숙소·방문 순서와 장소 검색·동선 데이터를 바탕으로",
        "완전한 OrchestratorResult를 작성하라.",
        "",
        "## 여행 기본 전제 (항상 준수)",
        f"- 이 여행의 출발지는 {origin_ctx['full']}이다. {origin_ctx['airport_note']}",
        f"- 여행 경로: {origin_ctx['word']} → {dest_str} → {origin_ctx['word']}",
        f"- 1일차 첫 항목: {origin_ctx['word']} 출발 항공 이동 (leg_index=0 depart 편 사용). cost는 '선택된 항공편' price_original·currency 그대로 사용. cost=null 절대 금지.",
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
        f"- 마지막날 마지막 항목: {origin_ctx['word']} 귀국 항공 이동 (return 편 사용). cost는 '선택된 항공편' price_original·currency 그대로 사용. cost=null 절대 금지.",
        "  예) {\"plan_name\": \"로마 피우미치노(FCO) → 인천국제공항(ICN) 귀국 항공 (항공사명)\", \"time\": \"HH:MM ~ HH:MM\", \"cost\": {\"amount\": price_original, \"currency\": currency}}",
        f"  ⚠️ 귀국편 도착 날짜는 반드시 여행 마지막 날({destinations[-1]['end_date'][:10]})이어야 한다.",
        f"  {origin_ctx['word']} 도착이 {destinations[-1]['end_date'][:10]} 이후로 넘어가는 일정은 절대 금지.",
        f"  day_plans에 {destinations[-1]['end_date'][:10]} 이후 날짜 키가 생기면 안 된다.",
        "- 각 항공 이동 항목 직전: 공항 이동 항목 삽입 (출발 2~3시간 전 기준, 새벽 출발이면 심야 이동도 그대로 기재)",
        "  ⚠️ 공항 이동 항목의 plan_name에는 반드시 공항명과 IATA 코드를 함께 표기한다.",
        "  예) plan_name='숙소 → 인천국제공항(ICN) 이동 (공항버스/택시)', time='01:30 ~ 03:00'",
        "  예) plan_name='호텔 → 파리 샤를드골공항(CDG) 이동 (택시)', time='06:00 ~ 07:30'",
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
        f"   {origin_ctx['word']} 내 활동(출발 준비, 공항 이동, 귀국 후): 출발지 현지 시각",
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
        "⚠️ 자정 이후(00:00~)에 이어지는 모든 일정은 반드시 다음 날짜의 day_plans bucket에 넣는다.",
        "   예) 2026-05-29 '23:45 ~ 23:59 저녁 식사' 후 이어지는 '00:00 ~ 00:45 저녁 식사', '00:50 ~ 01:00 숙소 귀환'은 모두 2026-05-30 bucket에 작성.",
        "⚠️ 같은 활동을 자정 기준으로 분리한 경우 cost는 첫 번째 조각에만 작성하고, 다음 날짜 continuation 조각의 cost는 반드시 null로 둔다.",
        "   예) 식사 cost는 23:45~23:59 항목에만 작성, 00:00~00:45 continuation 항목은 cost=null.",
        "올바른 예) '09:00 ~ 10:30', '20:07 ~ 23:59', '00:00 ~ 21:22', '23:30 ~ 23:59', '00:00 ~ 02:15'",
    ]

    if not d.is_day_trip:
        lines += [
            "",
            "## 숙소 귀환·휴식 배치 규칙",
            "⚠️ 숙박이 있는 날짜에는 하루 마지막 외부 일정 후 반드시 숙소/호텔 귀환 및 휴식 항목을 넣는다.",
            "   예) '관광지 → 숙소 이동 (버스/택시)' 후 '숙소 귀환 및 휴식' 또는 하나로 합쳐 '숙소 귀환 및 휴식'.",
            "⚠️ 당일 마지막 외부 일정이 23:59에 끝나거나 자정을 넘기면, 귀환/휴식 항목은 다음 날짜 첫 항목으로 작성한다.",
            "   예) 1일차 '23:45 ~ 23:59 저녁 식사' 다음에는 2일차 '00:00 ~ 07:30 숙소 귀환 및 휴식'을 반드시 넣는다.",
            "⚠️ 늦은 시간에 식당·카페를 무리하게 추가하지 말고, 숙소 귀환 및 휴식을 우선 배치한다.",
            "⚠️ 체크인 후 외부 일정이 없으면 '호텔 체크인 및 휴식'으로 하루를 마무리한다.",
            "⚠️ 심야~새벽 시간대(00:00~05:00 시작)에 걸치는 체크인·휴식 블록의 plan_name은 반드시",
            "   '호텔 체크인 및 휴식' 또는 '숙소 귀환 및 휴식'으로 표기한다. '호텔 체크인' 단독 명칭으로",
            "   여러 시간짜리 항목을 만들지 않는다 (체크인 행위 자체는 30분을 넘지 않는다).",
            "   단, 귀국 항공 이동 이후나 체크아웃 후 바로 공항으로 이동하는 마지막 날에는 숙소 귀환 항목을 만들지 않는다.",
        ]

    if d.is_day_trip:
        lines += [
            "",
            "## 당일치기 시작/종료 시간 범위",
            "⚠️ 당일치기이므로 숙박이 없다 — 오전 출발 ~ 저녁 귀가 기준으로 하루를 구성한다.",
            "- 첫 항목: 오전 중 출발 (예: 08:00~10:00대 출발).",
            "- 마지막 항목: 저녁 귀가/귀환 이동 (예: 20:00~22:00대 도착). 심야(22:00 이후) 일정으로 늘어지지 않게 한다.",
            "- 숙소 체크인·귀환·휴식 항목은 작성하지 않는다 (당일 귀가이므로 불필요).",
        ]

    lines += [
        "",
        "## 식사 배치 규칙",
        "- 일반 날: 아침(08:00~09:00), 점심(12:00~13:30), 저녁(18:30~20:00) 3회 포함",
        "- 항공 도착 날: 도착 시간 + 시내 이동(약 1.5h) 이후부터 가능한 첫 식사부터 시작",
        "- 항공 출발 날: 아침 09:00부터 시작. 탑승 시간 2~3시간 전 공항 이동 전까지 관광·식사 일정 진행.",
        "  ⚠️ 출발 전 빈 시간이 생기면 '자유 시간 (산책·쇼핑 등)' 항목으로 반드시 채울 것. 공항 이동 항목만 덜렁 있으면 절대 안 됨.",
        "⚠️ 식당·카페 영업시간을 실시간으로 확실히 확인할 수 없으므로 21:30 이후 식당·카페 방문 일정을 만들지 않는다.",
        (
            "   21:30 이후 도착하거나 식사 시간이 부족하면 외부 식당·카페 대신 '편의점 간단 식사 후 귀가 이동 준비'로 대체한다."
            if d.is_day_trip else
            "   21:30 이후 도착하거나 식사 시간이 부족하면 외부 식당·카페 대신 '숙소 귀환 및 휴식', '호텔 체크인 및 휴식', '편의점 간단 식사 후 휴식'으로 대체한다."
        ),
        "   '심야 맛집', '늦게 운영', '야간 카페' 같은 근거 없는 표현을 note에 쓰지 않는다.",
        "   불가피하게 22:00 이후 외부 장소를 넣어야 할 때는 note에 '운영 시간 확인 필요'라고 쓴다.",
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
        "⚠️ 식사·교통·입장료 등 현지 추정 비용의 currency는 반드시 그 활동이 일어나는 "
        "**도시의 현지 통화**로 통일한다. 임의로 USD를 쓰지 말 것(미국 외 목적지에 USD 금지). "
        "예) 시드니·호주=AUD, 도쿄=JPY, 파리·유럽=EUR, 방콕=THB, 토론토·캐나다=CAD, 국내=KRW. "
        "같은 도시 안의 항목들은 통화가 서로 달라선 안 된다.",
        "",
        "- 항공 이동 항목: API 검색 결과의 price_original·currency 그대로 사용. cost=null 절대 금지.",
        "  ⚠️ '선택된 항공편' 섹션에서 해당 leg_index의 price_original·currency·price_krw를 반드시 찾아 기재.",
        "  (API가 성인/아이 요금을 구분해 이미 합산한 금액을 반환함 — 인원 수 곱하기 금지)",
        "  price_krw가 있고 currency != 'KRW'이면 amount_krw에 기입. currency='KRW'이면 amount_krw 생략.",
        '  예) currency="JPY", price_original=85000, price_krw=780000',
        '      → {"amount": 85000, "currency": "JPY", "amount_krw": 780000}',
        '  예) currency="KRW", price_original=775570',
        '      → {"amount": 775570, "currency": "KRW"}  ← amount_krw 없음',
    ]

    if not d.is_day_trip:
        lines += [
            "",
            "- 숙소 체크인 항목: API 검색 결과의 price_original·currency 그대로 사용.",
            "  (API가 전체 숙박 기간 합산 금액을 반환함 — 박 수 곱하기 금지)",
            "  currency != 'KRW'이고 price_krw 있으면 amount_krw에 기입. price_krw 없으면 cost=null.",
            '  예) currency="EUR", price_original=300, price_krw=453000',
            '      → {"amount": 300, "currency": "EUR", "amount_krw": 453000}',
        ]

    lines += [
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

    if not d.is_day_trip:
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
    if len(combined) <= settings.PREPROCESSOR_SKIP_MAX_LEN:
        return combined
    pref_hint = ""
    if preferences:
        pref_hint = f"\n\n사용자 취향: {json.dumps(preferences, ensure_ascii=False)}\n위 취향을 참고하여 관련 정보를 일부 포함하되, 현지 대표 음식·명소 등 다양한 여행 정보의 균형을 유지해줘."
    result = await run_with_retry(
        preprocessor_agent,
        f"아래 검색 결과를 여행 계획에 유용한 핵심 정보 위주로 간결하게 요약해줘.{pref_hint}\n\n{combined}",
        role="preprocessor",
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


def _fmt_flight_duration(total_sec: int | None) -> str:
    """초 → 'Hh MMm' 표시 문자열."""
    if not total_sec:
        return "?"
    h, m = divmod(int(total_sec) // 60, 60)
    return f"{h}h {m:02d}m"


def _normalize_booking_flights(raw: Any) -> dict:
    """Booking search_flights 응답을 기존 파이프라인 평면 shape로 정규화한다.

    Booking offer(segments 구조) → {airline, origin, destination, departing_at,
    arriving_at, price_krw, price_original, currency, stops, duration}
    + image_url(대표 항공사 로고) · url(검색 리스트 URL) · token 추가.
    """
    if isinstance(raw, Exception) or not isinstance(raw, dict) or raw.get("status") != "success":
        msg = str(raw) if isinstance(raw, Exception) else (raw.get("message") if isinstance(raw, dict) else "검색 실패")
        return {"status": "error", "data": [], "count": 0, "message": msg}

    data = raw.get("data") or {}
    list_url = data.get("booking_list_url")
    flat: list[dict] = []
    for o in (data.get("flights") or []):
        segs = o.get("segments") or []
        if not segs:
            continue
        first_seg, last_seg = segs[0], segs[-1]
        first_leg = (first_seg.get("legs") or [{}])[0] or {}
        carriers = first_leg.get("carriers") or []
        total_legs = sum(len(s.get("legs") or []) for s in segs)
        total_sec = sum((s.get("total_time_sec") or 0) for s in segs)
        price = o.get("price")
        flat.append({
            "token": o.get("token"),
            "price_original": price,           # Booking은 KRW 통화 고정
            "currency": o.get("currency") or "KRW",
            "price_krw": price,
            "airline": carriers[0] if carriers else None,
            "origin": first_seg.get("from"),
            "destination": last_seg.get("to"),
            "departing_at": first_seg.get("departure_time"),
            "arriving_at": last_seg.get("arrival_time"),
            "stops": max(total_legs - 1, 0),
            "duration": _fmt_flight_duration(total_sec),
            "image_url": first_leg.get("logo"),   # 대표 항공사 로고
            "url": list_url,                       # 검색 리스트 URL (편 무관)
        })
    # 경유 적고 저렴한 순 정렬
    flat.sort(key=lambda x: (x["stops"], x["price_krw"] if x["price_krw"] is not None else float("inf")))
    return {
        "status": "success" if flat else "error",
        "data": flat,
        "count": len(flat),
        "booking_list_url": list_url,
    }


async def _fetch_flight_legs(
    destinations: list[dict],
    cities_en: list[str],
    adults: int,
    children: int,
    child_ages: list,
    origin_en: str = _DEFAULT_ORIGIN,
) -> list[dict]:
    """모든 항공 구간을 Booking으로 병렬 검색한다.

    구간 구성 (N개 도시):
      leg 0       : origin_en → cities_en[0]          depart
      leg 1..N-1  : cities_en[i-1] → cities_en[i]     connect
      leg N       : cities_en[-1] → origin_en          return

    Booking 항공은 2단계: search_flight_location(도시→공항ID) → search_flights.
    """
    children_csv = ",".join(str(a) for a in (child_ages or []))

    def _is_korea(country: str | None) -> bool:
        c = (country or "")
        return "korea" in c.lower() or "대한민국" in c

    # 1) 등장하는 모든 도시의 공항 정보를 한 번씩만 병렬 해석 (중복 호출 방지)
    #    selected(기본 공항) + is_korea(국가) + gmp_id(김포 후보, 사실상 서울만 보유)
    unique_cities = list(dict.fromkeys([origin_en, *cities_en]))
    loc_results = await asyncio.gather(
        *[_service.process_task("booking", "search_flight_location", {"query": c}) for c in unique_cities],
        return_exceptions=True,
    )
    airport_info: dict[str, dict] = {}
    for city, r in zip(unique_cities, loc_results):
        if not isinstance(r, Exception) and isinstance(r, dict) and r.get("status") == "success":
            data = r.get("data") or {}
            sel = data.get("selected") or {}
            cands = data.get("candidates") or []
            gmp_id = next((c.get("id") for c in cands if c.get("code") == "GMP"), None)
            airport_info[city] = {
                "id": sel.get("id"),
                "is_korea": _is_korea(sel.get("country")),
                "gmp_id": gmp_id,
            }

    def _pick_airport(city: str, other_is_korea: bool) -> str | None:
        """국내 노선(양쪽 다 한국)이면 서울쪽은 김포(GMP) 우선 — ICN 국제선 오선택 방지."""
        info = airport_info.get(city) or {}
        if other_is_korea and info.get("is_korea") and info.get("gmp_id"):
            return info["gmp_id"]
        return info.get("id")

    async def _search(from_city: str, to_city: str, depart_date: str) -> dict:
        from_korea = (airport_info.get(from_city) or {}).get("is_korea", False)
        to_korea = (airport_info.get(to_city) or {}).get("is_korea", False)
        from_id = _pick_airport(from_city, to_korea)
        to_id = _pick_airport(to_city, from_korea)
        if not from_id or not to_id:
            return {"status": "error", "data": [], "count": 0,
                    "message": f"공항 ID 해석 실패: {from_city}→{to_city}"}
        query = {"fromId": from_id, "toId": to_id, "departDate": depart_date, "adults": adults}
        if children_csv:
            query["children"] = children_csv
        raw = await _service.process_task("booking", "search_flights", query)
        return _normalize_booking_flights(raw)

    leg_info: list[dict] = []
    tasks = []

    # Depart
    tasks.append(_search(origin_en, cities_en[0], destinations[0]["start_date"][:10]))
    leg_info.append({"leg_index": 0, "direction": "depart", "from": origin_en, "to": cities_en[0]})

    # Connect legs
    for i in range(1, len(destinations)):
        tasks.append(_search(cities_en[i - 1], cities_en[i], destinations[i]["start_date"][:10]))
        leg_info.append({"leg_index": i, "direction": "connect", "from": cities_en[i - 1], "to": cities_en[i]})

    # Return — 귀국편 검색 날짜 결정
    #  국제/장거리: end_date 당일 편이 없거나 익일 도착일 수 있어 end_date와 전날 양쪽 검색.
    #  국내 단거리(양쪽 다 한국): 당일 왕복이 기본이므로 end_date 하루만 검색
    #    (마지막 날을 통째로 날리는 전날 귀국편이 후보에 섞이는 것을 방지).
    end_dt = datetime.strptime(destinations[-1]["end_date"][:10], "%Y-%m-%d").date()
    return_from_korea = (airport_info.get(cities_en[-1]) or {}).get("is_korea", False)
    return_to_korea = (airport_info.get(origin_en) or {}).get("is_korea", False)
    is_domestic_return = return_from_korea and return_to_korea
    return_dates = [str(end_dt)] if is_domestic_return else [str(end_dt), str(end_dt - timedelta(days=1))]
    n_return = len(return_dates)
    for d in return_dates:
        tasks.append(_search(cities_en[-1], origin_en, d))
    return_leg_info = {
        "leg_index": len(destinations), "direction": "return",
        "from": cities_en[-1], "to": origin_en,
    }

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 귀국편 결과 병합 (국내선 1개 날짜 / 국제선 2개 날짜)
    non_return = results[:-n_return]
    merged_data: list = []
    for r in results[-n_return:]:
        if not isinstance(r, Exception) and isinstance(r, dict) and r.get("status") == "success":
            merged_data += r.get("data", [])

    # end_date 이내 도착편만 남김 (arriving_at[:10] <= end_date)
    end_date_str = str(end_dt)
    valid_data = [f for f in merged_data if (f.get("arriving_at") or "")[:10] <= end_date_str]
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


def _normalize_booking_hotels(raw: Any) -> dict:
    """Booking search_hotels 응답을 기존 파이프라인 평면 shape로 정규화한다.

    {name, address, price_krw, rating} + image_url(photo)·hotel_id·star 추가.
    url(booking_url)은 선택 호텔에 대해서만 join 단계에서 get_hotel_details로 채운다.
    """
    if isinstance(raw, Exception) or not isinstance(raw, dict) or raw.get("status") != "success":
        msg = str(raw) if isinstance(raw, Exception) else (raw.get("message") if isinstance(raw, dict) else "검색 실패")
        return {"status": "error", "data": [], "count": 0, "message": msg}

    hotels = ((raw.get("data") or {}).get("hotels")) or []
    flat = [{
        "hotel_id": h.get("hotel_id"),
        "name": h.get("name"),
        "address": h.get("summary") or "",   # 검색 응답엔 정식 주소 없음 → 요약(위치 라벨) 사용
        "price_original": h.get("price"),    # Booking은 KRW 통화 고정
        "currency": "KRW",
        "price_krw": h.get("price"),
        "rating": h.get("review_score"),
        "star": h.get("star"),
        "image_url": h.get("photo"),
        "url": None,
    } for h in hotels]
    return {"status": "success" if flat else "error", "data": flat, "count": len(flat)}


async def _fetch_hotels(
    city_en: str,
    start_date: str,
    end_date: str,
    adults: int,
    children: int,
    child_ages: list,
) -> dict:
    """Booking 2단계: search_destination(도시→dest_id) → search_hotels."""
    try:
        loc = await _service.process_task("booking", "search_destination", {"query": city_en})
        if not isinstance(loc, dict) or loc.get("status") != "success":
            return {"status": "error", "data": [],
                    "message": (loc.get("message") if isinstance(loc, dict) else f"{city_en} 지역 검색 실패")}
        sel = (loc.get("data") or {}).get("selected") or {}
        dest_id, search_type = sel.get("dest_id"), sel.get("search_type")
        if not dest_id or not search_type:
            return {"status": "error", "data": [], "message": f"{city_en} dest_id 해석 실패"}

        query = {
            "dest_id": dest_id,
            "search_type": search_type,
            "arrival_date": start_date[:10],
            "departure_date": end_date[:10],
            "adults": adults,
        }
        if child_ages:
            query["children_age"] = ",".join(str(a) for a in child_ages)
        raw = await _service.process_task("booking", "search_hotels", query)
        return _normalize_booking_hotels(raw)
    except Exception as e:
        return {"status": "error", "data": [], "message": str(e)}


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


async def _no_hotels(destinations: list[dict]) -> dict[str, dict]:
    """당일치기(숙박 없음)일 때 숙소 검색을 생략하고 빈 결과를 반환한다."""
    return {d["city"]: {"status": "skipped", "message": "당일치기로 숙소 검색 생략"} for d in destinations}


# ── Phase 3 헬퍼 함수들 ──────────────────────────────────────────────────

def _norm_place(s: str | None) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


_KOREA_SKIP_WORDS = ("식사", "맛집", "근처", "이동", "점심", "저녁", "아침", "브런치", "야식", "야시장", "카페")


def _korea_keyword(query: str, city_kr: str | None) -> str | None:
    """Google용 ordered_query('장소명 도시명 (영문)')에서 한국관광공사 검색용 장소명만 추출.

    - 끝의 '(영문)' 괄호와 도시명(제주도/제주 등)을 제거
    - 식사·이동 등 서술형 검색어는 한국관광공사 대상이 아니므로 None (스킵)
    예) '비자림 제주 (Jeju)' → '비자림' / '저녁식사 흑돼지 제주 (Jeju)' → None
    """
    q = re.sub(r"\s*\([^)]*\)\s*$", "", query or "").strip()
    variants = set()
    if city_kr:
        variants.add(city_kr)
        variants.add(re.sub(r"(특별자치도|광역시|특별시|도|시)$", "", city_kr))
    for v in sorted((v for v in variants if v), key=len, reverse=True):
        q = q.replace(v, " ")
    q = re.sub(r"\s+", " ", q).strip()
    if not q or any(w in q for w in _KOREA_SKIP_WORDS):
        return None
    return q


def _korea_pick_image(raw: Any, query: str) -> tuple[str | None, str | None]:
    """한국관광공사 검색 결과에서 query와 확신 매칭되는 항목의 (image_url, contentid).

    image_url = firstimage 우선 → 없으면 firstimage2 → 둘 다 없으면 None.
    매칭 실패 시 (None, None) — 해외 장소는 결과가 비어 자연히 매칭 안 됨.
    """
    if not isinstance(raw, dict) or raw.get("status") != "success":
        return (None, None)
    items = ((raw.get("data") or {}).get("items")) or []
    nq = _norm_place(query)
    for it in items:
        nt = _norm_place(it.get("title"))
        if nt and (nt in nq or nq in nt):
            img = it.get("firstimage") or it.get("firstimage2")
            return (img or None, it.get("contentid"))
    if len(items) == 1:  # 결과가 하나뿐이면 그 항목으로 간주
        it = items[0]
        return (it.get("firstimage") or it.get("firstimage2") or None, it.get("contentid"))
    return (None, None)


async def _fetch_places(planner_output: PlannerOutput) -> dict[str, dict]:
    queries: list[str] = []
    query_city: dict[str, str] = {}
    for day in planner_output.days:
        for q in day.ordered_queries:
            query_city.setdefault(q, day.city)
        queries.extend(day.ordered_queries)
    queries = list(dict.fromkeys(queries))  # 순서 유지 중복 제거

    # ordered_query는 Google용('장소명 도시명 (영문)') → 한국관광공사는 장소명만 추출해 검색
    korea_keywords = [_korea_keyword(q, query_city.get(q)) for q in queries]

    async def _korea_call(kw: str | None):
        if not kw:  # 식사·이동 등은 한국관광공사 대상 아님 → 스킵
            return {"status": "skip"}
        return await _service.process_task("korea_tourism", "search_keyword", {"keyword": kw, "numOfRows": 5})

    # google_maps(장소·평점)와 korea_tourism(국내 이미지)을 병렬 호출 — 해외는 korea가 빈 결과
    google_res, korea_res = await asyncio.gather(
        asyncio.gather(
            *[_service.process_task("google_maps", "search_place", {"query": q}) for q in queries],
            return_exceptions=True,
        ),
        asyncio.gather(
            *[_korea_call(kw) for kw in korea_keywords],
            return_exceptions=True,
        ),
    )

    result: dict[str, dict] = {}
    for q, kw, g, k in zip(queries, korea_keywords, google_res, korea_res):
        entry = dict(g) if not isinstance(g, Exception) and isinstance(g, dict) else {"status": "error"}
        image_url, contentid = _korea_pick_image(k, kw or q)   # 정제된 장소명으로 매칭
        entry["image_url"] = image_url      # 한국관광공사 firstimage (없으면 None)
        entry["contentid"] = contentid
        result[q] = entry
    return result


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


_TIME_RANGE_RE = re.compile(r"^\s*(\d{2}):(\d{2})\s*~\s*(\d{2}):(\d{2})\s*$")


def _parse_time_range(value: str) -> tuple[int, int] | None:
    match = _TIME_RANGE_RE.match(value or "")
    if not match:
        return None
    sh, sm, eh, em = (int(part) for part in match.groups())
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        return None
    return sh * 60 + sm, eh * 60 + em


def _format_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _copy_item_with(item: Any, **updates: Any) -> Any:
    if hasattr(item, "model_copy"):
        return item.model_copy(update=updates)
    copied = dict(item)
    copied.update(updates)
    return copied


_DATE_KEY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _normalize_overnight_day_plans(day_plans: dict[str, list]) -> dict[str, list]:
    """
    프론트/API contract가 date bucket + "HH:MM ~ HH:MM" 문자열이므로,
    자정을 넘는 항목은 저장 가능한 두 항목으로 분할한다.

    원본 ID가 없는 현재 구조에서는 두 번째 조각에 비용을 붙이면 합산이 중복되므로
    비용은 첫 번째 조각에만 남긴다.
    """
    # LLM이 잘못된 키(예: 'krw_2026_06_25')를 생성하는 경우 방어
    valid_plans = {k: v for k, v in day_plans.items() if _DATE_KEY_RE.match(k)}
    normalized: dict[str, list] = {date_key: [] for date_key in sorted(valid_plans.keys())}

    for date_key in sorted(valid_plans.keys()):
        source_items = valid_plans.get(date_key, [])
        parsed_ranges = []
        overnight_ends = []
        has_late_night_item = False
        for item in source_items:
            time_value = item.time if hasattr(item, "time") else item.get("time", "")
            parsed = _parse_time_range(time_value)
            parsed_ranges.append(parsed)
            if parsed:
                start, end = parsed
                if start >= 18 * 60:
                    has_late_night_item = True
                if start >= 18 * 60 and end <= start:
                    overnight_ends.append(end)

        next_date = str(date.fromisoformat(date_key) + timedelta(days=1))
        early_next_day_items: list[Any] = []
        early_cutoff = max([4 * 60, *[end + 120 for end in overnight_ends]]) if overnight_ends else 0

        for idx, item in enumerate(source_items):
            parsed = parsed_ranges[idx]
            if not parsed:
                normalized.setdefault(date_key, []).append(item)
                continue

            start, end = parsed
            name = item.plan_name if hasattr(item, "plan_name") else item.get("plan_name", "")
            note = item.note if hasattr(item, "note") else item.get("note", "")
            text = f"{name} {note}"
            is_rest_continuation = any(token in text for token in ("휴식", "취침", "숙소 귀환", "호텔 귀환"))
            if (
                overnight_ends
                and start <= early_cutoff
                and (end <= max(early_cutoff, start) or is_rest_continuation)
            ):
                early_next_day_items.append(item)
                continue

            if has_late_night_item and start < 4 * 60 and is_rest_continuation:
                early_next_day_items.append(item)
                continue

            if end > start:
                normalized.setdefault(date_key, []).append(item)
                continue

            normalized.setdefault(date_key, []).append(
                _copy_item_with(item, time=f"{_format_minutes(start)} ~ 23:59")
            )
            normalized.setdefault(next_date, []).append(
                _copy_item_with(item, time=f"00:00 ~ {_format_minutes(end)}", cost=None)
            )

        if early_next_day_items:
            normalized.setdefault(next_date, []).extend(early_next_day_items)

    return {date_key: items for date_key, items in sorted(normalized.items())}


def _item_get(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def _item_set(item: Any, key: str, value: Any) -> None:
    if isinstance(item, dict):
        item[key] = value
    else:
        setattr(item, key, value)


async def _attach_media(
    day_plans: dict[str, list],
    planner_output: PlannerOutput,
    place_results: dict[str, dict],
    flight_legs: list[dict],
    hotels_by_city: dict,
    adults: int,
    child_ages: list,
) -> dict[str, list]:
    """day_plans 아이템에 image_url·url을 파이썬 후처리로 주입한다 (LLM 미개입).

    - 항공: selected_flights를 flight_legs 원본과 대조해 로고(image_url)·검색 리스트 URL(url) 역참조
    - 숙소: selected_hotels를 hotels_by_city 원본과 대조해 사진(image_url), 선택 호텔만 get_hotel_details로 booking_url(url)
    - 장소: place_results의 한국관광공사 firstimage를 이름 매칭으로 부여
    매칭은 이름 기반 best-effort — 실패 시 image_url·url = None 유지.
    """
    # 1) 항공: (norm_airline, origin_code, dest_code, image_url, url)
    #    같은 항공사가 여러 구간(출발·귀국)에 쓰이면 항공사명만으로는 방향 구분이 안 되므로
    #    출발/도착 공항코드(GMP·CJU 등)의 등장 순서로 leg를 특정한다.
    flight_media: list[tuple[str, str, str, str | None, str | None]] = []
    for fl in planner_output.selected_flights:
        offers = next(
            (leg.get("data", {}).get("data") or [] for leg in flight_legs if leg.get("leg_index") == fl.leg_index),
            [],
        )
        src = next(
            (o for o in offers if o.get("airline") == fl.airline and o.get("departing_at") == fl.departing_at),
            (offers[0] if offers else None),
        )
        if fl.airline and src:
            flight_media.append((
                _norm_place(fl.airline),
                _norm_place(fl.origin),
                _norm_place(fl.destination),
                src.get("image_url"),
                src.get("url"),
            ))

    # 2) 숙소: booking_url은 선택된 호텔에 대해서만 상세 호출 (비용 최소화)
    hotel_media: list[tuple[str, str | None, str | None]] = []
    detail_tasks, detail_meta = [], []
    for h in planner_output.selected_hotels:
        src = next(
            (s for s in (hotels_by_city.get(h.city, {}).get("data") or [])
             if _norm_place(s.get("name")) == _norm_place(h.name)),
            None,
        )
        image_url = src.get("image_url") if src else None
        hotel_id = src.get("hotel_id") if src else None
        if hotel_id:
            query = {"hotel_id": hotel_id, "arrival_date": h.check_in[:10],
                     "departure_date": h.check_out[:10], "adults": adults}
            if child_ages:
                query["children_age"] = ",".join(str(a) for a in child_ages)
            detail_tasks.append(_service.process_task("booking", "get_hotel_details", query))
            detail_meta.append((h.name, image_url))
        else:
            hotel_media.append((_norm_place(h.name), image_url, None))
    if detail_tasks:
        for (name, image_url), d in zip(detail_meta, await asyncio.gather(*detail_tasks, return_exceptions=True)):
            url = (d.get("data") or {}).get("booking_url") if isinstance(d, dict) and d.get("status") == "success" else None
            hotel_media.append((_norm_place(name), image_url, url))

    # 3) 장소: norm(name) → image_url (한국관광공사 firstimage)
    place_map: dict[str, str] = {}
    for query, res in place_results.items():
        img = res.get("image_url")
        if not img:
            continue
        place_map.setdefault(_norm_place(query), img)
        for p in ((res.get("data") or {}).get("places") or []):
            nm = _norm_place(p.get("name"))
            if nm:
                place_map.setdefault(nm, img)

    def _match(item: Any) -> tuple[str | None, str | None]:
        plan_name = _item_get(item, "plan_name") or ""
        hay = _norm_place(plan_name) + "|" + _norm_place(_item_get(item, "place"))
        # 예약 URL은 실제 숙박 성격 항목(체크인·체크아웃·숙박·귀환·휴식 등)에만 붙인다.
        # '이동' 경로 항목이나 place에 호텔명이 우연히 들어간 식사 항목엔 붙이지 않는다.
        # 사진(image)은 목적지 미리보기로 유용하므로 매칭되면 항상 유지한다.
        is_lodging = "이동" not in plan_name and any(w in plan_name for w in _LODGING_URL_WORDS)
        for nm, img, url in hotel_media:      # 숙소 우선 (가장 구체적)
            if nm and nm in hay:
                return img, (url if is_lodging else None)
        # 항공: 항공사명이 일치하는 후보 중 아래 우선순위로 leg를 특정한다.
        #   1) 출발→도착 공항코드가 hay에 순서대로 등장(방향 완전 일치)
        #   2) 도착 공항코드만 hay에 등장 — '기내 (비행 중)'·'~ 도착' continuation 카드는
        #      출발 코드가 없어 1)이 실패하므로, 도착지 코드로 해당 leg를 특정
        #   3) 항공사명만 일치(코드 정보 없음) — 최후 폴백
        # 같은 항공사가 여러 leg(출발·경유·귀국)에 쓰일 때 1)/2) 없이 항공사명만으로
        # 매칭하면 항상 첫 leg로 쏠리므로, 2) 계층이 continuation 카드의 오매칭을 막는다.
        dest_match: tuple[str | None, str | None] | None = None
        airline_fallback: tuple[str | None, str | None] | None = None
        for nm, frm, to, img, url in flight_media:
            if not (nm and nm in hay):
                continue
            if frm and to and frm in hay and to in hay and hay.find(frm) < hay.find(to):
                return img, url
            if to and to in hay and dest_match is None:
                dest_match = (img, url)
            if airline_fallback is None:
                airline_fallback = (img, url)
        if dest_match is not None:
            return dest_match
        if airline_fallback is not None:
            return airline_fallback
        np = _norm_place(_item_get(item, "place"))  # 장소
        if np and np in place_map:
            return place_map[np], None
        for key, img in place_map.items():
            if key and (key in np or np in key):
                return img, None
        return None, None

    for items in day_plans.values():
        for item in items:
            img, url = _match(item)
            if img and not _item_get(item, "image_url"):
                _item_set(item, "image_url", img)
            if url and not _item_get(item, "url"):
                _item_set(item, "url", url)
    return day_plans


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

    # ── 안전 게이트: 위험 지역이면 경고 후 사용자 동의를 먼저 받는다 ────
    verdict = await _check_safety(destinations, user_message, deps.ai_summary, deps.today)
    if verdict and verdict.unsafe and not verdict.user_consented:
        warning = (
            f"⚠️ 요청하신 여행지 관련 안내드립니다.\n\n{verdict.risk_summary}\n\n"
            "안전상의 이유로 현재 이 지역 여행은 추천드리지 않습니다. "
            "그럼에도 일정을 만들어드릴까요?"
        )
        cities_str = ", ".join(d["city"] for d in destinations)
        yield warning
        yield OrchestratorResult(
            message=warning,
            ai_summary=(
                (deps.ai_summary + "\n" if deps.ai_summary else "")
                + f"{cities_str} 여행 위험 경고 안내함 — 사용자 진행 여부 확인 대기"
            ),
        )
        return

    adults = itinerary.get("adult_count") or 1
    children = itinerary.get("child_count") or 0
    child_ages = itinerary.get("child_ages") or []
    budget = itinerary.get("budget")
    is_day_trip = _is_day_trip(itinerary)
    origin_raw = itinerary.get("origin")  # None이면 미입력 → 기존 서울/한국 기준 동작

    # 날짜 변경 감지: 기존 day_plans가 있으면 교통 이동일 제거 + 재계획 날짜 산출
    replan_dates: list[str] = []
    if itinerary.get("day_plans"):
        adjusted_day_plans, replan_dates = _get_replan_dates_for_date_change(itinerary)
        if replan_dates:
            itinerary = {**itinerary, "day_plans": adjusted_day_plans}

    # 도시명 영문 변환 (출발지 + 전체 목적지 일괄 처리 — 단일 LLM 호출)
    cities_kr = [d["city"] for d in destinations]
    translated = await _extract_english_cities([origin_raw or _DEFAULT_ORIGIN, *cities_kr])
    origin_en, cities_en = translated[0], translated[1:]

    print(
        f"\n[run_itinerary_pipeline] 파이프라인 시작"
        f"\n  origin       : {origin_raw} (en={origin_en})"
        f"\n  destinations : {cities_kr}"
        f"\n  cities_en    : {cities_en}"
        f"\n  start={destinations[0]['start_date']}, end={destinations[-1]['end_date']}"
        f"\n  adults={adults}, children={children}",
        flush=True,
    )

    # ── Phase 1: 병렬 데이터 수집 ──────────────────────────────────────
    hotels_coro = (
        _no_hotels(destinations) if is_day_trip
        else _fetch_hotels_all(cities_en, destinations, adults, children, child_ages)
    )
    web_summaries, weather_by_city, flight_legs, hotels_by_city = await asyncio.gather(
        _fetch_web_summaries(destinations, deps.preferences),
        _fetch_weather_all(destinations, deps.today),
        _fetch_flight_legs(destinations, cities_en, adults, children, child_ages, origin_en),
        hotels_coro,
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
        is_day_trip=is_day_trip,
        origin=origin_raw,
    )
    planner_context = _build_planner_prompt(planner_deps)
    planner_result = await run_with_retry(
        planner_agent,
        f"{planner_context}\n\n---\n\n사용자 메시지: {user_message}",
        role="planner",
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
        is_day_trip=is_day_trip,
        origin=origin_raw,
    )
    synth_context = _build_synthesizer_prompt(synth_deps)
    await acquire_llm_slot("synthesizer")  # run_stream은 run_with_retry를 안 타므로 직접 슬롯 확보
    for attempt in range(4):
        yielded_any = False
        try:
            prev_msg = ""
            async with synthesizer_agent.run_stream(
                f"{synth_context}\n\n---\n\n사용자 메시지: {user_message}",
                deps=synth_deps,
                message_history=history,
            ) as stream:
                async for partial in stream.stream_output():
                    msg = getattr(partial, "message", None) or ""
                    if len(msg) > len(prev_msg):
                        yield msg[len(prev_msg):]
                        prev_msg = msg
                        yielded_any = True
                result = await stream.get_output()
            break
        except Exception as e:
            if _is_rate_limit_error(e) and attempt < 3 and not yielded_any:
                wait = _retry_wait(attempt)
                print(f"[synthesizer] 429 재시도 {attempt + 1}/3, {wait:.1f}s 대기", flush=True)
                await asyncio.sleep(wait)
            else:
                raise

    if result.day_plans:
        result = result.model_copy(update={
            "day_plans": _normalize_overnight_day_plans(result.day_plans)
        })
        # image_url·url 후처리 주입 (Booking 사진·booking_url / 한국관광공사 firstimage / 항공사 로고)
        result = result.model_copy(update={
            "day_plans": await _attach_media(
                result.day_plans, planner_output, place_results,
                flight_legs, hotels_by_city, adults, child_ages,
            )
        })

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
