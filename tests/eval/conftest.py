# tests/eval/conftest.py
import asyncio

import pytest

from tests.eval.report import RESULTS

_COOLDOWN_SECONDS = 30  # Vertex 분당 쿼터 창 회복용 — 테스트 사이에만 대기
_first_test_done = False


@pytest.fixture(autouse=True)
async def llm_quota_cooldown():
    global _first_test_done
    if _first_test_done:
        print(f"\n[eval] Vertex 쿼터 회복 대기 {_COOLDOWN_SECONDS}s...", flush=True)
        await asyncio.sleep(_COOLDOWN_SECONDS)
    _first_test_done = True
    yield


def pytest_terminal_summary(terminalreporter):
    if not RESULTS:
        return
    terminalreporter.section("AI 평가 결과 요약", sep="=")
    for title, lines in RESULTS:
        terminalreporter.write_line(f"\n■ {title}")
        for line in lines:
            terminalreporter.write_line(f"   {line}")
    terminalreporter.write_line("")
