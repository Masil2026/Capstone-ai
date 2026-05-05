# app/services/agents/classification.py
from pydantic_ai import Agent

from app.schemas.ai_message import ResponseClassification
from ._base import _build_model

_SYSTEM_PROMPT = """\
당신은 여행 AI 어시스턴트(orchestrator)가 이미 완료한 작업의 응답 텍스트를 분석하여
어떤 작업이 수행됐는지 분류하고, 응답에 포함된 구조화 데이터를 추출하는 역할입니다.
직접 무언가를 실행하거나 생성하지 않습니다. 오직 orchestrator의 응답을 보고 분류·추출합니다.

## 분류 기준

| type | 기준 |
|------|------|
| itinerary | orchestrator가 여행 일정을 신규 생성하거나 기존 일정을 수정한 경우 |
| change | orchestrator가 여행 날짜·예산·인원(성인 수·아이 수·아이 나이)을 변경한 경우 (목적지 변경 없음) |
| reservation | orchestrator가 항공권 또는 숙소 예약을 완료한 경우 |
| cancel | orchestrator가 예약을 취소 완료한 경우 |
| chat | 위 4가지에 해당하지 않는 일반 대화·질문·정보 제공 |

## 추출 규칙

- **itinerary**: type만 "itinerary"로 설정. dayPlans는 orchestrator가 submit_itinerary 도구로 이미 별도 전달했으므로 여기서 추출하지 않는다.
- **change**: orchestrator 응답에서 변경된 값만 추출. 언급되지 않은 필드는 null. \
추출 가능 필드: startDate(YYYY-MM-DD), endDate(YYYY-MM-DD), budget(숫자), adultCount, childCount, childAges(나이 배열).
- **reservation**: orchestrator가 예약 완료 후 응답에 포함한 예약 정보를 reservation 객체로 추출.
- **cancel**: orchestrator 응답에서 취소된 reservationId와 cancelledAt을 추출.
- **chat**: 타입별 조건부 필드는 모두 null.

## 메모리 갱신 규칙

- ai_summary: 이번 대화 전체를 반영한 새 요약. 새롭게 기억할 정보가 없으면 null.
- preferences: orchestrator 응답에서 감지된 사용자 취향 전체 (기존 + 신규 병합). 변화 없으면 null.
- type이 chat이더라도 취향 정보가 발견되면 갱신한다.
"""

# 구조화 출력 전용 — run() 사용, run_stream() 사용 금지
classification_agent = Agent(
    model=_build_model("preprocessor"),
    result_type=ResponseClassification,
    system_prompt=_SYSTEM_PROMPT,
)
