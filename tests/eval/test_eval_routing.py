# tests/eval/test_eval_routing.py
"""L1 평가: 요청 타입 라우팅 정확도 (classification LLM + _correct_request_type 보정).

실행: pytest tests/eval/test_eval_routing.py -s -m llm
비용: flash 24회 (pro 0회)
"""
import asyncio

import pytest

pytestmark = pytest.mark.llm

from app.controller.aiMessageController import _correct_request_type
from app.services.agents._base import run_with_retry
from app.services.agents.classification import classification_agent
from tests.eval.golden import ROUTING_CASES, SAMPLE_ITINERARY
from tests.eval import report

ACCURACY_THRESHOLD = 0.85


async def _route(message: str, has_itinerary: bool) -> tuple[str, str]:
    result = await run_with_retry(classification_agent, message, role="classification")
    raw = result.output.type
    corrected = _correct_request_type(raw, message, SAMPLE_ITINERARY if has_itinerary else None)
    return raw, corrected


async def test_routing_accuracy():
    results = await asyncio.gather(
        *[_route(msg, has_it) for msg, _, has_it in ROUTING_CASES],
        return_exceptions=True,
    )

    passed = 0
    fails: list[str] = []
    print("\n" + "=" * 88)
    print(f"{'결과':<4} {'기대':<12} {'분류':<12} {'보정':<12} 메시지")
    print("-" * 88)
    for (msg, expected, _), r in zip(ROUTING_CASES, results):
        if isinstance(r, Exception):
            print(f"ERR  {expected:<12} {'-':<12} {'-':<12} {msg}  ({r})")
            fails.append(f"ERR  {msg} ({r})")
            continue
        raw, corrected = r
        ok = corrected == expected
        passed += ok
        print(f"{'PASS' if ok else 'FAIL':<4} {expected:<12} {raw:<12} {corrected:<12} {msg}")
        if not ok:
            fails.append(f"FAIL {msg} (기대 {expected}, 분류 {raw}, 보정 {corrected})")

    accuracy = passed / len(ROUTING_CASES)
    print("-" * 88)
    print(f"라우팅 정확도: {passed}/{len(ROUTING_CASES)} = {accuracy:.1%} (기준 {ACCURACY_THRESHOLD:.0%})")
    print("=" * 88)

    report.add("L1 라우팅 정확도", [
        f"정확도: {passed}/{len(ROUTING_CASES)} = {accuracy:.1%} (기준 {ACCURACY_THRESHOLD:.0%})",
        *fails,
    ])
    assert accuracy >= ACCURACY_THRESHOLD, f"라우팅 정확도 {accuracy:.1%} < 기준 {ACCURACY_THRESHOLD:.0%}"
