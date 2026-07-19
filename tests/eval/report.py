# tests/eval/report.py
"""eval 테스트 결과 수집 — 테스트 종료 후 conftest의 terminal summary가 한번에 출력."""

RESULTS: list[tuple[str, list[str]]] = []


def add(title: str, lines: list[str]) -> None:
    RESULTS.append((title, lines))
