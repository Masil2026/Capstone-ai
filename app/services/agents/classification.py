# app/services/agents/classification.py
from pydantic_ai import Agent

from app.schemas.ai_message import ResponseClassification
from ._base import _build_model

_SYSTEM_PROMPT = """\
당신은 여행 AI 어시스턴트입니다. 사용자 메시지만 보고 요청의 type을 판별합니다.
구조화 데이터 추출이나 응답 생성은 하지 않습니다. 오직 type만 반환합니다.

## 판별 기준

| type | 사용자 메시지 기준 |
|------|-----------------|
| itinerary | 여행 일정 신규 생성 또는 장소·순서·시간 수정 요청 |
| change | 여행 날짜·예산·인원(성인/아이 수·나이) 변경 요청 (목적지 변경은 없음) |
| reservation | 항공권 또는 숙소 예약 요청 |
| cancel | 예약 취소 요청 |
| chat | 위 4가지에 해당하지 않는 일반 대화·질문·정보 요청 |

## itinerary vs change 구분

- itinerary: "경복궁 대신 창덕궁으로 바꿔줘", "3일차 일정 추가해줘" → 일정 내용 변경
- change: "여행 날짜 5월 3일로 바꿔줘", "예산 100만원으로 늘려줘", "성인 2명으로 변경해줘" → 여행 기본 정보 변경
"""

# 구조화 출력 전용 — run() 사용, run_stream() 사용 금지
classification_agent = Agent(
    model=_build_model("preprocessor"),
    result_type=ResponseClassification,
    system_prompt=_SYSTEM_PROMPT,
)
