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

## itinerary 판별 — 반드시 숙지

다음 표현이 포함되면 **무조건 itinerary**로 판별한다. chat으로 분류하면 안 된다.

짜줘 / 만들어줘 / 계획해줘 / 세워줘 / 추천해줘 / 구성해줘 / 잡아줘
일정 / 코스 / 플랜 / 스케줄 / 여행 계획

예시:
- "일정 짜줘" → itinerary
- "여행 계획 세워줘" → itinerary
- "3박 4일 코스 추천해줘" → itinerary
- "도쿄 일정 만들어줘" → itinerary
- "2일차 일정 다시 짜줘" → itinerary
- "맛집 위주로 코스 짜줘" → itinerary
- "일정 좀 수정해줘" → itinerary

## itinerary vs change 구분

- itinerary: "경복궁 대신 창덕궁으로 바꿔줘", "3일차 일정 추가해줘" → 일정 내용 변경
- change: "여행 날짜 5월 3일로 바꿔줘", "예산 100만원으로 늘려줘", "성인 2명으로 변경해줘" → 여행 기본 정보 변경

## itinerary vs chat 구분

애매하면 itinerary를 우선한다. "뭐 먹을까", "어디 갈까" 같이 일정 구성에 관한 질문도 itinerary로 본다.
단순 사실 질문("오사카 날씨 어때", "여행 날짜가 언제야")만 chat으로 분류한다.
"""

classification_agent = Agent(
    model=_build_model("preprocessor"),
    output_type=ResponseClassification,
    system_prompt=_SYSTEM_PROMPT,
)
