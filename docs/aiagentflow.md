# AI Agent Flow

## 전체 흐름 요약

| 단계 | 주요 컴포넌트 | 역할 |
|------|-------------|------|
| 1. 의도 파악 | Orchestrator (Gemini 3 Pro) | 사용자 입력 분석 및 도구 선택 |
| 2. 데이터 수집 | Info Sources API | 실시간 외부 데이터 (날씨, 지도, 뉴스 등) 확보 |
| 3. 데이터 분류 | FastAPI 전처리 레이어 | 데이터를 정형 / 비정형으로 분리 |
| 4. 데이터 정제 | Elasticsearch & Gemini Flash | 비정형 데이터 필터링 및 요약 전처리 |
| 5. 최종 생성 | Gemini 3 Pro | 컨텍스트를 반영한 최종 답변 / 일정 구성 |

---

## 1. 입력 및 의도 파악 — Orchestrator (Gemini 3 Pro)

사용자가 채팅 UI를 통해 텍스트나 이미지를 입력하면 **Gemini 3 Pro** 기반의 AI 에이전트가 가동됩니다.

- **입력 처리**: 사용자의 텍스트와 이미지를 분리하여 수용
- **이미지 분석**: 이미지 입력 시 Gemini 내장 Google Search를 활용해 장소를 파악
- **오케스트레이션**: 파악된 정보를 바탕으로 어떤 도구(API)를 호출할지 결정

---

## 2. 정보 소스 호출 및 데이터 수집 — Info Sources

에이전트의 결정에 따라 외부 및 내부 API로부터 필요한 데이터를 실시간으로 가져옵니다.

**정형 데이터 소스**
- 숙소 API (Duffel Stays)
- 항공권 API (Duffel Air)
- 렌트카 API
- 날씨 API (Open-Meteo)
- 지도 API (Google Maps)

**비정형 데이터 소스**
- 웹 검색 (Tavily)
- 트렌드 정보 (Instagram — Meta Graph API)

---

## 3. AI 백엔드 전처리 워크플로우 — FastAPI

수집된 파편화된 데이터는 **FastAPI** 기반의 AI 백엔드로 전달되어 정제 과정을 거칩니다.

### 3-1. 데이터 분류

들어온 데이터를 **정형(Structured)** 과 **비정형(Unstructured)** 으로 분류합니다.

### 3-2. 유형별 처리

**정형 처리**
- 항공, 숙소, 날씨, 렌트카 등 수치화된 데이터를 규격에 맞게 정리

**비정형 처리**
- **Elasticsearch**: 비정형 데이터에서 필요한 정보를 필터링
- **Gemini Flash**: 필터링된 비정형 데이터를 요약 / 전처리하여 메인 모델이 이해하기 쉬운 형태로 변환

---

## 4. 최종 응답 생성 및 저장 — Gemini 3 Pro

전처리가 완료된 정형 + 비정형 데이터가 메인 모델로 전달됩니다.

- **최종 일정 생성**: Gemini 3 Pro가 전처리된 데이터 + Redis 단기 히스토리 + 장기 요약(ai_summary) + 사용자 취향(preferences)을 결합하여 최종 응답(일정)을 생성
- **데이터 피드백**: 일정 수립 완료 응답 시 Java 백엔드로 함께 반환 → Java가 `chat_rooms` 테이블의 `ai_summary`, `preferences` 갱신

---

## 5. 메모리 아키텍처

AI 에이전트는 Redis를 단일 메모리 저장소로 사용하고, 영속성은 `chat_rooms` 테이블(Java 관리)이 담당합니다.

### 5-1. Redis 저장 구조

세션(`chat_room_id`) 키 단위로 두 가지를 관리합니다.

| Redis 키 | 타입 | 내용 |
|----------|------|------|
| `memory:{chat_room_id}` | JSON | `ai_summary`(text) + `preferences`(json) + `loaded_at`(ISO 8601) |
| `chat_history:{chat_room_id}` | bytes (JSON) | 최근 **20개** 메시지 — 초과 시 오래된 것부터 제거 |

`memory` 키 구조:
```json
{
  "ai_summary": "지금까지의 대화 전체 요약본",
  "preferences": { "preference_food": "noodle" },
  "loaded_at": "2026-04-10T12:00:00Z"
}
```

### 5-2. 요청 시작 시 메모리 로딩 로직

매 요청 시작 시 `chat_rooms.updated_at`을 기준으로 Redis가 최신인지 판단합니다.

```
요청 수신
    ↓
Redis에 해당 chat_room_id 데이터 있는가?
    ├─ 없음 ──────────────────────────────────────────────┐
    └─ 있음 → chat_rooms.updated_at > memory.loaded_at?  │
                  ├─ 최신 아님 (DB가 더 새로움) ──────────┤
                  └─ 최신 → Redis 그대로 사용             │
                                                          ↓
                              chat_rooms에서 ai_summary + preferences 조회
                                          ↓
                              Redis memory:{chat_room_id} 저장
                              { ai_summary, preferences, loaded_at: now() }
                              (chat_history는 비어있으므로 빈 배열로 시작)
```

### 5-3. 일정 수립 완료 시 저장 흐름

일정 수립이 완료된 응답 시점에 Gemini가 갱신된 요약·취향을 생성하고 Java로 함께 반환합니다.

```
Gemini Pro 최종 응답 (일정)
    + 갱신된 ai_summary
    + 갱신된 preferences
         ↓
         ├─→ Redis 갱신 (ai_summary, preferences, chat_history)
         └─→ Java 백엔드 반환
                  ↓
         chat_rooms 테이블 UPDATE (ai_summary, preferences, updated_at)
