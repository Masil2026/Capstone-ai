# tests/eval/conftest.py
from tests.eval.report import RESULTS


def pytest_terminal_summary(terminalreporter):
    if not RESULTS:
        return
    terminalreporter.section("AI 평가 결과 요약", sep="=")
    for title, lines in RESULTS:
        terminalreporter.write_line(f"\n■ {title}")
        for line in lines:
            terminalreporter.write_line(f"   {line}")
    terminalreporter.write_line("")
