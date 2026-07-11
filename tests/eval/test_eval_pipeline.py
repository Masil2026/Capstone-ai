# tests/eval/test_eval_pipeline.py
"""L3 평가: 전체 일정 파이프라인 (실제 LLM + 외부 API).

시나리오당 pro 2회 + flash 다수 + 외부 API 호출 — 비용이 커서 3개만 유지한다.
구조 검증(날짜 커버리지·시간 형식·정렬 등)은 코드로, 일정 품질은 DeepEval GEval
(judge=Gemini Flash)로 평가한다.

실행 (시나리오별 개별 실행 권장 — pro 쿼터 배려):
  pytest tests/eval/test_eval_pipeline.py -s -m llm -k domestic
  pytest tests/eval/test_eval_pipeline.py -s -m llm -k overseas
  pytest tests/eval/test_eval_pipeline.py -s -m llm -k datechange
"""
import json
import re
from datetime import date

import pytest

pytestmark = pytest.mark.llm

from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from app.services.agents.itinerary_pipeline import run_itinerary_pipeline
from app.services.agents.orchestrator import OrchestratorDeps
from tests.eval.golden import PIPELINE_SCENARIOS
from tests.eval.judge import GeminiFlashJudge
from tests.eval import report

_TIME_RE = re.compile(r"^\d{2}:\d{2} ~ \d{2}:\d{2}$")
_INTERNAL_TERMS = ("day_plans", "change", "reservation", "JSON", "json")


def _structural_scorecard(result, scenario) -> dict:
    day_plans = result.day_plans or {}
    keys = sorted(day_plans.keys())
    card: dict[str, object] = {}

    # 1. 날짜 커버리지
    if scenario.get("expected_dates") is not None:
        card["날짜 커버리지"] = "PASS" if keys == scenario["expected_dates"] else f"FAIL (반환: {keys})"
    else:
        lo, hi = scenario["expected_within"]
        within = all(lo <= k <= hi for k in keys)
        contains = all(d in keys for d in scenario.get("expected_contains", []))
        card["날짜 커버리지"] = "PASS" if (within and contains and keys) else f"FAIL (반환: {keys})"

    # 2~4. 항목 구조
    bad_time, unsorted_days, empty_days = [], [], []
    flights, flights_with_cost = 0, 0
    for dkey, items in day_plans.items():
        if not items:
            empty_days.append(dkey)
            continue
        starts = []
        for item in items:
            if not _TIME_RE.match(item.time or ""):
                bad_time.append(f"{dkey} {item.plan_name}: {item.time!r}")
            else:
                starts.append(item.time[:5])
            if "항공 이동" in item.plan_name:
                flights += 1
                flights_with_cost += item.cost is not None
        if starts != sorted(starts):
            unsorted_days.append(dkey)

    card["time 형식"] = "PASS" if not bad_time else f"FAIL {bad_time[:3]}"
    card["시간 정렬"] = "PASS" if not unsorted_days else f"FAIL {unsorted_days}"
    card["빈 날짜 없음"] = "PASS" if not empty_days else f"FAIL {empty_days}"
    card["항공 cost"] = (
        f"{flights_with_cost}/{flights}" if flights else "해당 없음"
    )

    # 5. message에 내부 용어 노출 없음
    leaked = [t for t in _INTERNAL_TERMS if t in result.message]
    card["message 청결"] = "PASS" if not leaked else f"FAIL (노출: {leaked})"
    return card


def _condense_for_judge(result) -> str:
    lines = [f"[안내문] {result.message}", ""]
    for dkey in sorted((result.day_plans or {}).keys()):
        lines.append(f"## {dkey}")
        for item in result.day_plans[dkey]:
            cost = f" | 비용 {item.cost.amount} {item.cost.currency}" if item.cost else ""
            lines.append(f"- {item.time} {item.plan_name} ({item.place}){cost}")
    return "\n".join(lines)[:8000]


_QUALITY_METRIC_KWARGS = dict(
    name="일정 품질",
    criteria=(
        "AI가 생성한 여행 일정이 요청 조건(목적지·기간·인원·예산)을 충실히 반영하는지, "
        "하루 구성(이동 동선, 식사 3회 내외, 관광·휴식 배분, 시간대의 현실성)이 "
        "실제로 실행 가능한 수준인지 평가하라. 안내문이 자연스러운 한국어인지도 본다. "
        "예산 관련: 예상 비용이 예산을 초과하더라도 안내문에서 초과 사실을 알리고 "
        "예산 업데이트를 제안했다면 감점하지 않는다 (의도된 제품 동작). "
        "식사 관련: '식사 3회 내외'는 종일 일정인 날에만 적용한다. 여행 첫날과 마지막 날은 "
        "출발·도착 시각에 따라 일정 범위 밖 시간대의 식사(예: 오후 출발일의 아침·점심, "
        "오후 도착 귀가일의 저녁)가 없는 것이 정상이며 감점하지 않는다."
    ),
    evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
    threshold=0.5,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    PIPELINE_SCENARIOS,
    ids=["domestic", "overseas", "datechange"],
)
async def test_pipeline_scenario(scenario):
    deps = OrchestratorDeps(
        ai_summary=None,
        preferences=None,
        today=date.today().isoformat(),
        similar_messages=[],
        current_itinerary=scenario["itinerary"],
        request_type="itinerary",
        reservations=[],
    )

    result = None
    async for item in run_itinerary_pipeline(deps, scenario["user_message"], []):
        if not isinstance(item, str):
            result = item

    assert result is not None, "파이프라인이 OrchestratorResult를 반환하지 않음"
    if not result.day_plans:
        report.add(f"L3 파이프라인 — {scenario['name']}",
                   [f"FAIL day_plans 비어 있음. message={result.message[:200]!r}"])
    assert result.day_plans, f"day_plans 비어 있음. message={result.message[:200]!r}"

    card = _structural_scorecard(result, scenario)

    # DeepEval GEval — judge: Gemini Flash
    it = scenario["itinerary"]
    request_summary = (
        f"{scenario['user_message']} | 목적지 {[d['city'] for d in it['destinations']]} "
        f"{it['start_date']}~{it['end_date']} | 성인 {it['adult_count']} 아이 {it['child_count']} "
        f"| 예산 {it['budget'] or '제한 없음'}"
    )
    metric = GEval(model=GeminiFlashJudge(), async_mode=False, **_QUALITY_METRIC_KWARGS)
    geval_line = "측정 실패"
    try:
        metric.measure(LLMTestCase(input=request_summary, actual_output=_condense_for_judge(result)))
        geval_line = f"{metric.score:.2f} — {metric.reason}"
    except Exception as e:
        geval_line = f"측정 실패: {e}"

    print("\n" + "=" * 80)
    print(f"[{scenario['name']}] 평가 결과")
    print("-" * 80)
    for k, v in card.items():
        print(f"  {k:<12}: {v}")
    print(f"  GEval 품질   : {geval_line}")
    print("-" * 80)
    print("[생성된 일정 전문]")
    print(_condense_for_judge(result))
    print("=" * 80)

    report.add(f"L3 파이프라인 — {scenario['name']}", [
        *[f"{k}: {v}" for k, v in card.items()],
        f"GEval 품질: {geval_line}",
    ])
    assert "FAIL" not in str(card["날짜 커버리지"]), card["날짜 커버리지"]
