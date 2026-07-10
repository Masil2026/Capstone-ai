# app/services/agents/orchestrator.py
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from pydantic_ai import Agent

_log = logging.getLogger(__name__)

from app.core.config import settings
from app.schemas.ai_message import OrchestratorResult
from app.services.adapters.tavily_search import TavilySearchAdapter
from app.services.adapters.weather_api import WeatherAdapter
from app.services.adapters.google_maps import GoogleMapsAdapter
from app.services.travel_agent_service import TravelAgentService
from ._base import _build_model, preprocessor_agent, run_with_retry

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
    reservations: list[dict]        # 채팅방의 활성 예약 목록 (DB read-only)

# ---------------------------------------------------------------------------
# 오케스트레이터 에이전트
# ---------------------------------------------------------------------------

orchestrator_agent = Agent(
    model=_build_model("orchestrator"),
    deps_type=OrchestratorDeps,
    output_type=OrchestratorResult,
    system_prompt=(
        "당신은 여행 계획 전문 AI 어시스턴트입니다.\n"
        "사용자 요청에 따라 적절한 도구를 활용하고, 구조화된 JSON(OrchestratorResult 형식)으로 응답합니다.\n"
        "⚠️ message 필드는 사용자에게 직접 노출되는 자연스러운 한국어 안내문이다. "
        "day_plans, change, reservation, cancel 같은 JSON 필드명·내부 키·기술 용어를 절대 포함하지 않는다. "
        "시스템 내부 처리 과정(데이터 반환 방식, JSON 구조 등)을 설명하는 문장도 절대 쓰지 않는다."
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
- `day_plans`: 날짜별 일정 (키='YYYY-MM-DD').
  - **신규 생성** (기존 일정 없음): 모든 날짜 포함.
  - **수정** (기존 일정 있음): **사용자가 요청한 날짜만** 반환. 나머지 날짜는 포함하지 않는다.
- `message`: 아래 기준으로 작성한다.
  - **신규 생성**: 날짜별 주요 코스를 간략히 소개한다.
    예) "1일차는 아사쿠사 → 센소지 → 나카미세 거리 코스로, 저녁에는 원하신 참치회 식당을 배치했습니다. 2일차는 신주쿠 → 하라주쿠 쇼핑 코스로 구성했습니다."
  - **수정**: 반영한 요청과 변경 결과를 구체적으로 설명한다.
    예) "해산물 요청을 반영해 1일차 저녁을 해산물 식당으로 변경했습니다. 3일차에는 시장 방문 코스를 새로 추가했습니다."
  - **정보 부족**: 누락된 정보를 구체적으로 명시하며 질문한다.
    예) "여행지가 등록되어 있지 않아요. 어디로 여행을 가실 예정인가요?"
- `ai_summary`: 번호 목록 형식. 아래 [메모리 업데이트] 참고.

처리:
1. current_itinerary(여행 기본 정보)가 있으면 반드시 참고한다.
2. **일정 생성 전 필수 정보 검증 — 아래 항목 중 하나라도 없으면 일정을 생성하지 말고 되물어본다. (day_plans = null)**
   - destinations 배열이 비어있거나 없음 → "여행지를 알려주세요."
   - start_date 또는 end_date 없음 → "여행 날짜를 알려주세요."
   - adult_count가 0 또는 없음 → "여행 인원을 알려주세요."
   - destinations 내 각 도시의 start_date/end_date 누락 → "각 도시의 체류 날짜를 알려주세요."
3. 필수 정보가 모두 있으면 get_weather, search_web, search_place, find_route 도구를 활용해 일정을 구성한다.
4. 기존 day_plans가 있으면 사용자 요청에 해당하는 날짜 일정만 새로 작성하여 반환한다.""",

    "change": """\
## 이번 요청: 여행 기본 정보 변경 (change)

**[응답 형식 — 반드시 준수]**
반환 JSON의 필드를 아래와 같이 채워야 한다:
- `change`: 변경된 필드만 포함 (변경하지 않은 필드는 null)
  가능한 필드: destinations, start_date, end_date, budget, adult_count, child_count, child_ages, origin
- `message`: 무엇이 어떻게 변경되었는지 구체적으로 안내한다.
  예) "여행 기간을 5월 3일~7일로 변경하고, 예산을 50만원으로 조정했습니다."
  정보 부족 시: 누락된 정보를 구체적으로 명시하며 질문한다.
  예) "추가하시는 아이의 나이를 알려주시겠어요?"
- `ai_summary`: 번호 목록 형식. 아래 [메모리 업데이트] 참고.

처리:
1. 외부 API 도구는 호출하지 않는다.
2. 사용자 메시지에서 변경된 필드만 추출하여 change 필드에 작성한다.
3. **변경 전 정보 부족 검증 — 아래 경우 change = null로 두고 message에서 되물어본다.**
   - child_count를 늘리는데 추가되는 아이 나이 정보가 없음
     → "추가하시는 아이의 나이를 알려주시겠어요?"
   - destinations를 변경하는데 각 도시의 체류 날짜가 명확하지 않음
     → "각 도시의 체류 날짜를 알려주세요. 예) 파리 3박, 로마 4박"
   - start_date만 있고 end_date(또는 총 여행 기간)를 알 수 없음
     → "여행 종료일 또는 총 여행 기간을 알려주세요."
4. child_ages 배열 길이는 최종 child_count와 반드시 일치해야 한다.""",

    "reservation": """\
## 이번 요청: 예약 (reservation)

⚠️ 이 서비스는 항공·숙소를 **직접 예약하지 않는다.** 예약은 사용자가 제공된 예약 링크에서 직접 완료한다.
- `reservation` 필드는 **반드시 null**로 둔다. 예약번호·예약완료 문구·예약 시각을 **절대 지어내지 말 것.**
- "예약이 완료되었습니다" 같은 표현을 **절대 쓰지 않는다.** (실제로 예약되지 않음)

**[응답 방법]**
`## 예약 가능한 항목` 섹션의 **예약 링크(url)를 그대로** 사용자에게 안내한다.

- 사용자가 예약할 항목(항공/숙소)을 명확히 지정한 경우:
  해당 항목의 예약 링크를 제시하고, "아래 링크에서 직접 예약을 완료해 주세요" 형식으로 안내한다.
  예) "대한항공 ICN→NRT (5/1) 편은 아래 링크에서 예약하실 수 있어요.\n예약하기: <예약 링크>"
- 사용자가 "예약해줘"처럼 항목을 특정하지 않은 경우:
  `## 예약 가능한 항목`의 항목들을 번호 목록(항목명 + 예약 링크)으로 보여주고,
  "어떤 항목을 예약하시겠어요? 링크를 통해 직접 예약을 완료하시면 됩니다 😊"로 마무리한다.
- 예약 링크가 있는 항목이 없으면, 아직 예약 가능한 항공/숙소 정보가 없다고 안내한다.""",

    "cancel": """\
## 이번 요청: 예약 취소 (cancel)

⚠️ 이 서비스는 예약을 **직접 취소하지 않는다.** 취소는 사용자가 실제로 예약한 곳에서 직접 진행해야 한다.
- `cancel` 필드는 **반드시 null**로 둔다. 취소 완료 문구·취소 시각을 **절대 지어내지 말 것.**
- "취소되었습니다/취소 처리되었습니다" 같은 표현을 **절대 쓰지 않는다.** (실제로 취소되지 않음)

**[응답 방법]**
`message`에, 예약은 사용자가 예약을 완료한 곳(항공사·숙소 공식 사이트 또는 예약 시 사용한 예약 사이트)에서
직접 취소해야 한다고 안내한다. 각 예약처의 취소 규정·수수료는 해당 사이트에서 확인하도록 덧붙인다.
예) "예약 취소는 예약을 진행하신 항공사/숙소(또는 예약 사이트)에서 직접 진행하셔야 해요. "
    "취소 규정과 수수료는 예약처에서 확인하실 수 있습니다.\"""",

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
        f"  reservations     : {len(deps.reservations)}건\n"
        f"  current_itinerary: "
        f"{({k: v for k, v in deps.current_itinerary.items() if k != 'day_plans'} if deps.current_itinerary else None)}",
        flush=True,
    )
    sections: list[str] = []
    sections.append(f"오늘 날짜: {deps.today}")

    if deps.current_itinerary:
        it = deps.current_itinerary
        destinations = it.get("destinations") or []
        dest_str = " → ".join(d["city"] for d in destinations) if destinations else "미설정"
        child_ages = it.get("child_ages") or []
        child_str = f"{it.get('child_count')}명 (나이: {child_ages})" if it.get("child_count") else "없음"
        budget = it.get("budget")
        budget_str = f"{int(budget):,}원" if budget else "미설정"
        day_plans = it.get("day_plans")

        section_lines = [
            "## 현재 여행 기본 정보 (DB에서 조회된 실제 값 — 반드시 이 데이터를 기준으로 답변할 것)",
            f"- 출발지: {it.get('origin') or '미설정(기본값: 서울/대한민국)'}",
            f"- 여행지: {dest_str}",
            f"- 여행 기간: {it.get('start_date')} ~ {it.get('end_date')} ({it.get('total_days')}일)",
            f"- 예산: {budget_str}",
            f"- 성인: {it.get('adult_count')}명",
            f"- 어린이: {child_str}",
        ]
        if len(destinations) > 1:
            section_lines.append("- 도시별 일정:")
            for d in destinations:
                section_lines.append(f"  - {d['city']}: {d['start_date']} ~ {d['end_date']}")
        if day_plans:
            section_lines.append("")
            if deps.request_type in ("change", "chat"):
                # 활동 수정·질의응답: 날짜별 전체 상세 필요
                section_lines.append("### 기존 일정 (수정 시 반드시 이 내용을 기준으로 변경할 것)")
                for date_key, items in day_plans.items():
                    section_lines.append(f"#### {date_key}")
                    for item in items:
                        if isinstance(item, dict):
                            section_lines.append(
                                f"  - {item.get('time','')} {item.get('plan_name','')} ({item.get('place','')})"
                            )
            elif deps.request_type == "reservation":
                # 예약 가능한 항목(딥링크 url 보유 항공·숙소)만 추출해 링크와 함께 노출.
                # url은 파이프라인 후처리로 항공(검색 리스트)·숙소(예약 페이지)에만 주입됨.
                seen_urls: set[str] = set()
                resv_lines: list[str] = []
                for date_key, items in day_plans.items():
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if isinstance(item, dict) and item.get("url") and item["url"] not in seen_urls:
                            seen_urls.add(item["url"])
                            resv_lines.append(f"- {item.get('plan_name','')} | 예약 링크: {item['url']}")
                if resv_lines:
                    section_lines.append("### 예약 가능한 항목 (아래 예약 링크를 그대로 사용자에게 안내)")
                    section_lines.extend(resv_lines)
                else:
                    section_lines.append("### 예약 가능한 항목\n- 예약 링크가 있는 항목이 없습니다.")
            else:
                # cancel·itinerary: 날짜 + 활동 수 요약만
                section_lines.append("### 기존 일정 요약")
                for date_key, items in day_plans.items():
                    count = len(items) if isinstance(items, list) else 0
                    section_lines.append(f"- {date_key}: {count}개 일정")
        else:
            section_lines.append("- day_plans: 아직 없음")
        sections.append("\n".join(section_lines))
    else:
        sections.append("## 현재 여행 기본 정보\n아직 여행 일정이 등록되지 않았습니다.")

    if deps.ai_summary:
        sections.append(f"## 이전 대화 요약\n{deps.ai_summary}")

    if deps.preferences:
        sections.append(f"## 사용자 취향\n{json.dumps(deps.preferences, ensure_ascii=False, indent=2)}")

    if deps.similar_messages:
        msgs = "\n".join(f"[{m['role']}] {m['content']}" for m in deps.similar_messages)
        sections.append(f"## 참고할 과거 대화\n{msgs}")

    if deps.reservations:
        lines = ["## 활성 예약 목록 (취소 요청 시 아래 id를 그대로 사용할 것)"]
        for r in deps.reservations:
            detail = r.get("detail") or {}
            name = detail.get("name") or detail.get("airline") or "알 수 없음"
            price_str = f"{r['total_price']} {r['currency']}" if r.get("total_price") else "가격정보없음"
            lines.append(
                f"- id={r['id']} | type={r['type']} | {name} | "
                f"external_ref_id={r.get('external_ref_id') or '없음'} | {price_str}"
            )
        sections.append("\n".join(lines))
    elif deps.request_type == "cancel":
        sections.append("## 활성 예약 목록\n취소할 수 있는 예약이 없습니다.")

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
    if len(snippets) <= settings.PREPROCESSOR_SKIP_MAX_LEN:
        return {"status": "success", "summary": snippets, "source_count": len(filtered)}
    result = await run_with_retry(
        preprocessor_agent,
        f"아래 검색 결과를 여행 계획에 유용한 핵심 정보 위주로 간결하게 요약해줘.\n\n{snippets}",
        role="preprocessor",
    )
    return {"status": "success", "summary": result.output, "source_count": len(filtered)}


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
    - mode: "transit"(대중교통, 기본값) / "walking"(도보, 1km 이내) / "bicycling" — "driving" 사용 금지 (렌터카 제외)
    - 반환: {status, data: {routes: [{distance_text, duration_text, fare, steps}]}}
    - fare: {"currency":"JPY","text":"¥500","value":500.0} 또는 null (transit 일부 노선만 제공)
    - fare가 있으면 이동 항목 cost에 사용: fare.value(1인) × 인원수. amount_krw 절대 작성 금지.
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

# 예약/취소는 orchestrator 프롬프트(reservation·cancel 타입)가 구조화 출력으로 직접 처리한다.
# (Booking은 조회·딥링크 전용이라 실제 예약 실행 API가 없음 — 별도 book/cancel 도구를 두지 않는다.)


