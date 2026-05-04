# AI Agent Flow

## 전체 흐름 요약

| 단계 | 주요 컴포넌트 | 역할 |
|------|-------------|------|
| 0. 인증 | X-Internal-Token | Spring Boot → FastAPI 서버 간 인증 |
| 1. 메모리 동기화 | Redis | 요청 memory와 Redis 비교·갱신 |
| 2. 장기 기억 로드 | OpenAI Embeddings + pgvector | 사용자 메시지와 유사한 과거 대화 검색 |
| 3. 의도 파악 + 스트리밍 | Orchestrator (GPT-4.1) | 도구 호출·외부 API 수집·텍스트 스트리밍 |
| 4. 데이터 전처리 | preprocessor_agent (GPT-4o-mini) | Tavily 비정형 결과 요약 (도구 함수 내부) |
| 5. 타입 판별 | classification_agent (GPT-4o-mini) | 스트리밍 완료 후 응답 의도 분류 |
| 6. 임베딩 생성 | OpenAI Embeddings | AI 응답 벡터화 |
| 7. done 이벤트 전송 | FastAPI SSE | 최종 구조화 페이로드 전달 |

---

## 0. 인증 — X-Internal-Token

Spring Boot → FastAPI 호출은 Clerk JWT가 아닌 **내부 공유 시크릿**으로 인증합니다.

| 항목 | 내용 |
|------|------|
| 헤더명 | `X-Internal-Token` |
| 검증 방식 | `secrets.compare_digest(요청값, settings.INTERNAL_TOKEN)` |
| 토큰 생성 | 팀 내부에서 임의 생성 (예: `openssl rand -hex 32`). `.env`에 `INTERNAL_TOKEN=<값>` 저장 |
| Spring Boot 설정 | 동일 값을 `${ai.internal-token}` 환경변수에 저장 후 모든 AI 서버 요청 헤더에 포함 |

검증 실패 시 `403 Forbidden`을 반환합니다.

---

## 1. 메모리 동기화 — Redis

매 요청마다 Spring Boot가 전달한 `memory`(body)와 Redis에 저장된 `memory:{roomId}`를 비교합니다.

```
요청 body.memory  |  Redis memory:{roomId}  →  결과
─────────────────────────────────────────────────────────────
있음              |  없음                   →  Redis에 저장
있음              |  있음, 값이 다름        →  요청 기준으로 Redis 업데이트
있음              |  있음, 값이 같음        →  유지
null              |  있음                   →  Redis 값 유지
null              |  없음                   →  memory 없이 대화 진행
```

동기화 후 `chat_history:{roomId}`에서 대화 이력을 로드합니다.

---

## 2. 장기 기억 로드 — pgvector 유사 메시지 검색

단기 기억(Redis 최근 20개)으로 커버되지 않는 과거 대화를 의미 기반으로 검색하여 orchestrator 컨텍스트에 추가합니다.

### 처리 흐름

```
사용자 메시지
  ↓
OpenAI text-embedding-3-small → 1536차원 벡터 생성
  ↓
pgvector 코사인 유사도 검색 (read-only)
  SELECT content, role
  FROM chat_messages
  WHERE room_id = :room_id
  ORDER BY embedding <=> :user_embedding
  LIMIT 5
  ↓
유사도 상위 5개 과거 메시지 → orchestrator 컨텍스트에 포함
```

### 컨텍스트 구성

orchestrator가 응답 생성 시 활용하는 컨텍스트:

| 소스 | 내용 | 저장소 |
|------|------|--------|
| ai_summary | 전체 대화 요약 | Redis memory |
| preferences | 사용자 취향 JSON | Redis memory |
| chat_history | 최근 20개 메시지 | Redis |
| 유사 과거 메시지 | 의미적으로 유사한 과거 대화 최대 5개 | pgvector (read-only) |

> FastAPI의 DB 접근은 이 유사도 검색에 한정된 read-only입니다. 모든 DB 쓰기는 Spring Boot가 담당합니다.

---

## 3. 스트리밍 응답 생성 — Orchestrator (GPT-4.1)

### 2-1. 동적 시스템 프롬프트 (OrchestratorDeps)

오케스트레이터는 매 요청마다 Redis에서 로드한 컨텍스트를 시스템 프롬프트에 주입합니다.

```python
@dataclass
class OrchestratorDeps:
    ai_summary: str | None    # 이전 대화 전체 요약
    preferences: dict | None  # 사용자 취향 (예: {"style": "adventure"})
    today: str                # YYYY-MM-DD — 날짜 계산 기준
```

`@orchestrator_agent.system_prompt` 함수가 위 값을 읽어 자연어 프롬프트로 조합합니다.
어댑터·도구 함수는 deps를 직접 참조하지 않습니다.

### 2-2. 도구 호출 및 스트리밍

```
orchestrator_agent.run_stream(user_input, deps=OrchestratorDeps(...), message_history=history)
  ↓
필요시 도구 호출 (search_flights, search_web 등)
  ↓
텍스트 토큰 생성 → SSE event: chunk 실시간 전송
```

등록된 도구 7개의 입력/출력 명세는 **[docs/agent_tools.md](agent_tools.md)** 참조.

---

## 4. 비정형 데이터 전처리 — preprocessor_agent

`search_web` 도구 함수 내부에서 Tavily 결과를 GPT-4o-mini로 요약합니다.
Elasticsearch는 사용하지 않습니다.

```
Tavily 원본 결과 (최대 15개)
  ↓
score ≥ 0.5 필터링 → 상위 10개 선택
  ↓
preprocessor_agent (GPT-4o-mini) → 핵심 정보 요약
  ↓
오케스트레이터에게 요약본 반환
```

항공·숙소·날씨·지도 등 정형 데이터는 각 어댑터에서 직접 정제 후 반환하므로 별도 전처리가 필요하지 않습니다.

---

## 5. 타입 판별 — classification_agent

스트리밍이 완료된 후 `classification_agent`(GPT-4o-mini)가 전체 응답 텍스트를 분석하여
`type`과 타입별 구조화 데이터를 추출합니다.

```
orchestrator 전체 응답 텍스트
  ↓
classification_agent.run(응답 텍스트, result_type=ResponseClassification)
  ↓
ResponseClassification 구조체 반환
```

### 타입 판별 기준

| type | 기준 |
|------|------|
| `itinerary` | 일정 초기 생성 또는 기존 일정의 장소·순서·시간 수정 |
| `change` | 여행 기본 정보 변경 (날짜·예산·인원 등) |
| `reservation` | 항공권 또는 숙소 예약 요청 |
| `cancel` | 예약 취소 요청 |
| `chat` | 위 4가지에 해당하지 않는 일반 대화·질문 |

### itinerary vs change 구분

- **itinerary**: "경복궁 대신 창덕궁으로 바꿔줘", "3일차 일정 추가해줘" → `dayPlans` 반환
- **change**: "여행 날짜 5월 3일부터 7일로 바꿔줘", "예산 100만원으로 늘려줘" → `startDate`, `budget` 등 반환

### dayPlans 반환 단위

- **itinerary (신규 생성)**: 전체 여행 기간 모든 날짜의 `dayPlans` 반환
- **itinerary (수정)**: 수정된 날짜의 `dayPlans`만 반환. Spring Boot가 해당 날짜 단위로 전체 교체

```json
"dayPlans": {
  "2026-05-04": [
    { "plan_name": "창덕궁 방문", "time": "09:00 ~ 12:00", "place": "창덕궁", "note": "" }
  ]
}
```

### ResponseClassification 구조

```python
class DayPlanItem(BaseModel):
    plan_name: str
    time: str           # "HH:MM ~ HH:MM"
    place: str
    note: str = ""

class ResponseClassification(BaseModel):
    type: Literal["chat", "itinerary", "change", "reservation", "cancel"]
    # itinerary 타입
    dayPlans: dict[str, list[DayPlanItem]] | None = None
    # change 타입
    startDate: str | None = None
    endDate: str | None = None
    budget: float | None = None
    adultCount: int | None = None
    childCount: int | None = None
    childAges: list[int] | None = None
    # reservation 타입
    reservation: dict[str, Any] | None = None
    # cancel 타입
    reservationId: str | None = None
    cancelledAt: str | None = None
    # 메모리 갱신 (모든 타입 공통, 변경 없으면 None)
    ai_summary: str | None = None
    preferences: dict[str, Any] | None = None
```

---

## 6. 임베딩 생성

| 시점 | 대상 | 용도 |
|------|------|------|
| 요청 시작 시 | 사용자 메시지 | pgvector 유사도 검색 (단계 2) |
| 응답 완료 후 | AI 응답 전문 | Spring Boot가 `chat_messages.embedding`에 저장 |

- 모델: `text-embedding-3-small` (OpenAI), 차원 `1536`
- 요청 시작 시 생성한 사용자 메시지 임베딩도 `done` 이벤트에 포함해 Spring Boot가 함께 저장

---

## 7. SSE 이벤트 전송 흐름

```
POST /api/v1/ai-messages (Spring Boot 요청)
  ↓
[0] X-Internal-Token 검증
  ↓
[1] memory 동기화 (Redis)
  ↓
[2] 사용자 메시지 임베딩 생성 → pgvector 유사 메시지 검색 (상위 5개)
  ↓
[3] chat_history 로드 (Redis) + 컨텍스트 구성
    (ai_summary + preferences + 유사 과거 메시지 + 최근 20개)
  ↓
[4] orchestrator_agent.run_stream()
  ├─ 필요시 도구 호출 (search_web → preprocessor_agent 내부 호출)
  └─ 텍스트 토큰 → event: chunk 반복 전송
  ↓
[5] 스트리밍 완료 → 전체 응답 텍스트 확보
  ↓
[6] classification_agent.run() → ResponseClassification 추출
  ↓
[7] AI 응답 임베딩 생성
  ↓
[8] memory 갱신 판단 → Redis 업데이트 (변경 있을 때만)
  ↓
[9] chat_history 저장 (Redis, 최대 20개)
  ↓
[10] done 페이로드 구성 → event: done 전송
```

SSE 이벤트 포맷 및 `done` 페이로드 상세 구조는 **[docs/api/POST_v1_ai-messages.md](api/POST_v1_ai-messages.md)** 참조.

---

## 8. 메모리 아키텍처

AI 에이전트는 Redis를 단일 메모리 저장소로 사용하고, 영속성은 `chat_rooms` 테이블(Java 관리)이 담당합니다.

### 8-1. Redis 저장 구조

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

### 8-2. memory 갱신 흐름

```
done 이벤트 전송 시점
  ↓
classification_agent 결과에 ai_summary / preferences 포함?
  ├─ 없음 → done.memory = null, Redis memory 유지
  └─ 있음 → Redis memory 업데이트 → done.memory에 포함
               ↓
             Spring Boot가 chat_rooms.ai_summary / preferences 갱신
```

`memory` 갱신은 `type`과 무관합니다. `"chat"` 타입에서도 사용자 취향 정보가 감지되면 갱신됩니다.
