# tests/eval/test_eval_change_extraction.py
"""L2 평가: change payload 추출 정확도 (change_extractor_agent).

실행: pytest tests/eval/test_eval_change_extraction.py -s -m llm
비용: flash 6회 (pro 0회)
"""
import asyncio
import json

import pytest

pytestmark = pytest.mark.llm

from app.services.agents._base import run_with_retry
from app.services.agents.orchestrator import change_extractor_agent
from tests.eval.golden import CHANGE_EXTRACTION_CASES, SAMPLE_ITINERARY
from tests.eval import report

ACCURACY_THRESHOLD = 0.8


def _build_prompt(user_message: str, ai_message: str) -> str:
    itinerary = {k: v for k, v in SAMPLE_ITINERARY.items() if k != "day_plans"}
    return (
        f"## 현재 여행 기본 정보\n{json.dumps(itinerary, ensure_ascii=False, default=str)}\n\n"
        f"## 사용자 요청\n{user_message}\n\n"
        f"## AI 안내문 (변경 결과 설명)\n{ai_message}\n\n"
        "위에서 변경된 여행 기본 정보 필드만 추출하라."
    )


def _field_matches(actual: dict, key: str, expected_value) -> bool:
    got = actual.get(key)
    if key == "destinations":
        if not isinstance(got, list) or len(got) != len(expected_value):
            return False
        return all(g.get("city") == e["city"] for g, e in zip(got, expected_value))
    if key == "budget":
        return got is not None and float(got) == float(expected_value)
    return got == expected_value


async def test_change_extraction_accuracy():
    results = await asyncio.gather(
        *[
            run_with_retry(change_extractor_agent, _build_prompt(um, am), role="change_extractor")
            for um, am, _ in CHANGE_EXTRACTION_CASES
        ],
        return_exceptions=True,
    )

    passed = 0
    fails: list[str] = []
    print("\n" + "=" * 88)
    for (um, _, expected), r in zip(CHANGE_EXTRACTION_CASES, results):
        if isinstance(r, Exception):
            print(f"ERR  {um}  ({r})")
            fails.append(f"ERR  {um} ({r})")
            continue
        actual = r.output.model_dump(exclude_none=True)
        misses = [k for k, v in expected.items() if not _field_matches(actual, k, v)]
        ok = not misses
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'} [{um}]")
        print(f"     기대: {json.dumps(expected, ensure_ascii=False)}")
        print(f"     실제: {json.dumps(actual, ensure_ascii=False)}")
        if misses:
            print(f"     불일치 필드: {misses}")
            fails.append(f"FAIL {um} (불일치 필드: {misses})")

    accuracy = passed / len(CHANGE_EXTRACTION_CASES)
    print("-" * 88)
    print(f"change 추출 정확도: {passed}/{len(CHANGE_EXTRACTION_CASES)} = {accuracy:.1%} (기준 {ACCURACY_THRESHOLD:.0%})")
    print("=" * 88)

    report.add("L2 change 추출 정확도", [
        f"정확도: {passed}/{len(CHANGE_EXTRACTION_CASES)} = {accuracy:.1%} (기준 {ACCURACY_THRESHOLD:.0%})",
        *fails,
    ])
    assert accuracy >= ACCURACY_THRESHOLD, f"추출 정확도 {accuracy:.1%} < 기준 {ACCURACY_THRESHOLD:.0%}"
