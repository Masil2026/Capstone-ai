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

매 요청마다 Spring Boot가 전달한 `memory`(body)와 Redis에 저장된 `memory:{chat_room_id}`를 비교합니다.

```
요청 body.memory  |  Redis memory:{chat_room_id}  →  결과
─────────────────────────────────────────────────────────────
있음              |  없음                         →  Redis에 저장
있음              |  있음, 값이 다름              →  요청 기준으로 Redis 전체 교체 (merge 아님)
있음              |  있음, 값이 같음              →  유지
null              |  있음                         →  Redis 값 유지
null              |  없음                         →  memory 없이 대화 진행
```

> 동기화는 `aiSummary`와 `preferences` 두 필드를 하나의 단위로 취급합니다.
> 어느 한 쪽만 다르더라도 `memory` 전체를 요청 값으로 교체합니다.

동기화 후 `chat_history:{chat_room_id}`에서 대화 이력을 로드합니다.

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

### 2-2. 현재 일정 로드 — DB 조회 (read-only)

일정 생성·수정 요청 시 orchestrator가 기존 dayPlans를 참고할 수 있도록 DB에서 현재 일정을 로드합니다.

```
roomId
  ↓
PostgreSQL read-only 조회
  SELECT day_plans FROM itineraries WHERE room_id = :room_id
  ↓
current_itinerary → orchestrator 시스템 프롬프트에 주입
  없으면 None (신규 일정 생성으로 처리)
```

> 일정이 없는 경우(첫 일정 생성)는 `current_itinerary = None`으로 처리하며, orchestrator가 전체 일정을 새로 생성합니다.

### 컨텍스트 구성

orchestrator가 응답 생성 시 활용하는 컨텍스트:

| 소스 | 내용 | 저장소 |
|------|------|--------|
| ai_summary | 전체 대화 요약 | Redis memory |
| preferences | 사용자 취향 JSON | Redis memory |
| chat_history | 최근 20개 메시지 | Redis |
| 유사 과거 메시지 | 의미적으로 유사한 과거 대화 최대 5개 | pgvector (read-only) |
| current_itinerary | 현재 여행 일정 dayPlans | PostgreSQL (read-only) |

> FastAPI의 DB 접근은 유사도 검색 및 현재 일정 조회에 한정된 read-only입니다. 모든 DB 쓰기는 Spring Boot가 담당합니다.

---

## 3. 스트리밍 응답 생성 — Orchestrator (GPT-4.1)

### 3-1. 동적 시스템 프롬프트 (OrchestratorDeps)

오케스트레이터는 매 요청마다 Redis + pgvector에서 로드한 컨텍스트를 시스템 프롬프트에 주입합니다.

```python
@dataclass
class OrchestratorDeps:
    ai_summary: str | None                      # 이전 대화 전체 요약 (Redis memory)
    preferences: dict | None                    # 사용자 취향 JSON (Redis memory)
    today: str                                  # YYYY-MM-DD — 날짜 계산 기준
    similar_messages: list[dict]                # pgvector 유사 과거 메시지 (최대 5개)
                                                # [{"role": "user"|"assistant", "content": "..."}]
    current_itinerary: dict | None              # 현재 여행 일정 dayPlans (DB read-only)
                                                # {"YYYY-MM-DD": [{"plan_name", "time", "place", "note"}]}
                                                # 일정 없으면 None
```

`@orchestrator_agent.system_prompt` 함수가 위 값을 읽어 자연어 프롬프트로 조합합니다.
`similar_messages`는 "참고할 수 있는 과거 대화" 형태로 시스템 프롬프트에 삽입됩니다.
`current_itinerary`는 일정 수정 시 기존 일정 맥락으로 주입됩니다. `None`이면 신규 생성으로 처리합니다.
어댑터·도구 함수는 deps를 직접 참조하지 않습니다.

### 3-2. 타입별 orchestrator 처리 방식

classification_agent가 판별한 `type`은 `OrchestratorDeps.request_type`으로 전달됩니다.
orchestrator는 이를 참고해 적절한 도구를 선택합니다.

| type | 외부 API 도구 | 구조화 데이터 전달 도구 | Spring Boot 후처리 |
|------|-------------|----------------------|------------------|
| `itinerary` | search_place, search_web, get_weather 등 | `submit_itinerary(day_plans)` | dayPlans로 DB 일정 교체 |
| `change` | **없음** | `submit_change(startDate, budget, ...)` | change 값으로 DB 직접 업데이트 |
| `reservation` | search_flights / search_hotels + 예약 API | `submit_reservation(...)` | reservations 테이블 저장 |
| `cancel` | 취소 API | `submit_cancel(reservationId, cancelledAt)` | reservations.status = "cancelled" |
| `chat` | search_web, get_weather 등 (필요 시) | 호출 없음 | 추가 처리 없음 |

모든 type에서 사용자 취향·요약 정보가 감지되면 `update_memory(ai_summary, preferences)`를 호출합니다.

### 3-3. 도구 호출 및 스트리밍

```
orchestrator_agent.run_stream(user_input, deps=OrchestratorDeps(...), message_history=history)
  ↓
type에 따라 필요한 도구 호출 (change는 도구 호출 없음)
  ↓
itinerary 타입이면 → submit_itinerary(day_plans={...}) 호출
  (텍스트 스트리밍과 병행. dayPlans 구조체를 엔드포인트로 전달)
  ↓
텍스트 토큰 생성 → SSE event: chunk 실시간 전송
```

등록된 도구 8개의 입력/출력 명세는 **[docs/agent_tools.md](agent_tools.md)** 참조.

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

`classification_agent`(GPT-4o-mini)는 **orchestrator 실행 전에** 사용자 메시지만 보고 `type`을 판별합니다.
구조화 데이터(dayPlans, change 필드, reservation, memory 등)는 모두 orchestrator의 도구 호출이 담당합니다.

```
사용자 메시지
  ↓
classification_agent.run(user_message, result_type=ResponseClassification)
  ↓
type 반환 ("chat" | "itinerary" | "change" | "reservation" | "cancel")
```

판별된 `type`은 `OrchestratorDeps.request_type`으로 전달되어 orchestrator가 어떤 도구를 호출할지 결정하는 데 활용됩니다.

### 타입 판별 기준

| type | 사용자 메시지 기준 |
|------|-----------------|
| `itinerary` | 일정 신규 생성 또는 장소·순서·시간 수정 요청 |
| `change` | 여행 날짜·예산·인원(성인/아이 수·나이) 변경 요청 (목적지 변경 없음) |
| `reservation` | 항공권 또는 숙소 예약 요청 |
| `cancel` | 예약 취소 요청 |
| `chat` | 위 4가지에 해당하지 않는 일반 대화·질문·정보 요청 |

### itinerary vs change 구분

- **itinerary**: "경복궁 대신 창덕궁으로 바꿔줘", "3일차 일정 추가해줘" → 일정 내용 변경
- **change**: "여행 날짜 5월 3일부터 7일로 바꿔줘", "예산 100만원으로 늘려줘" → 여행 기본 정보 변경

### ResponseClassification 구조

type만 반환합니다. 구조화 데이터는 orchestrator의 submit_* 도구가 담당합니다.

```python
class ResponseClassification(BaseModel):
    type: Literal["chat", "itinerary", "change", "reservation", "cancel"]
```

### done 페이로드 데이터 출처

| done 필드 | 출처 |
|-----------|------|
| `type` | classification_agent |
| `itinerary.dayPlans` | orchestrator → `submit_itinerary` 도구 캡처 |
| `change.*` | orchestrator → `submit_change` 도구 캡처 |
| `reservation` | orchestrator → `submit_reservation` 도구 캡처 |
| `cancel.*` | orchestrator → `submit_cancel` 도구 캡처 |
| `memory` | orchestrator → `update_memory` 도구 캡처 (`None`이면 `done.memory = null`) |
| `userMessage.embedding` | 요청 시작 시 생성 |
| `assistantMessage.embedding` | 스트리밍 완료 후 생성 |

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
[3] chat_history 로드 (Redis) + 현재 일정 로드 (DB, roomId 기준) + 컨텍스트 구성
  ↓
[4] classification_agent.run(user_message) → type 판별
    (빠름. orchestrator 전에 완료하여 request_type으로 전달)
  ↓
[5] orchestrator_agent.run_stream(user_message, deps={..., request_type})
  ├─ type에 맞는 외부 API 도구 호출
  ├─ 구조화 데이터 전달 도구 호출 (submit_itinerary / submit_change / submit_reservation / submit_cancel)
  ├─ 필요시 update_memory(ai_summary, preferences) 호출
  └─ 텍스트 토큰 → event: chunk 반복 전송
  ↓
[6] 스트리밍 완료 → 도구 캡처 결과 확보
  ↓
[7] AI 응답 임베딩 생성
  ↓
[8] update_memory 캡처 결과 → Redis 업데이트 (캡처된 경우에만)
  ↓
[9] chat_history 저장 (Redis, 최대 20개)
  ↓
[10] done 페이로드 구성 (type + 캡처 결과 조합) → event: done 전송
```

SSE 이벤트 포맷 및 `done` 페이로드 상세 구조는 **[docs/api/POST_v1_ai-messages.md](api/POST_v1_ai-messages.md)** 참조.

---

## 8. 메모리 아키텍처

AI 에이전트는 Redis를 단일 메모리 저장소로 사용하고, 영속성은 `chat_rooms` 테이블(Java 관리)이 담당합니다.

### 8-1. Redis 저장 구조

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

---

## 9. 구현 설계 결정 사항

### 9-1. pgvector 비동기 접근

`database.py`는 동기 SQLAlchemy 세션을 사용합니다. FastAPI async 환경에서 pgvector 쿼리 시 블로킹을 방지하기 위해 `asyncio.get_event_loop().run_in_executor(None, sync_query_fn)`으로 스레드풀에서 실행합니다.

```python
import asyncio
from app.core.database import SessionLocal

def _query_similar_messages(room_id: str, embedding: list[float]) -> list[dict]:
    with SessionLocal() as db:
        rows = db.execute(
            "SELECT role, content FROM chat_messages "
            "WHERE room_id = :room_id ORDER BY embedding <=> :emb LIMIT 5",
            {"room_id": room_id, "emb": str(embedding)}
        ).fetchall()
    return [{"role": r.role, "content": r.content} for r in rows]

# 호출 시
loop = asyncio.get_event_loop()
similar = await loop.run_in_executor(None, _query_similar_messages, room_id, embedding)
```

### 9-2. 단계별 에러 처리 정책

각 단계 실패 시 전체 요청을 중단하지 않고 폴백합니다.

| 단계 | 실패 시 처리 |
|------|------------|
| pgvector 유사 메시지 검색 | `similar_messages = []`로 폴백, 스트리밍 계속 진행 |
| classification_agent | `type = "chat"`, 타입별 페이로드 없이 done 이벤트 전송 |
| AI 응답 임베딩 생성 | `embedding = null`로 done 이벤트 전송. Spring Boot는 null embedding은 저장하지 않음 |
| orchestrator 스트리밍 중 실패 | SSE 연결 종료. Spring Boot가 `504 Gateway Timeout` 또는 `503` 반환 |

### 9-3. LLM 모델 기본값

`ORCHESTRATOR_MODEL`, `PREPROCESSOR_MODEL` 환경변수가 없으면 아래 기본값을 사용합니다.

| 역할 | `LLM_PROVIDER=openai` | `LLM_PROVIDER=gemini` |
|------|----------------------|----------------------|
| orchestrator | `gpt-4.1` | `gemini-2.5-pro` |
| preprocessor / classification | `gpt-4o-mini` | `gemini-2.0-flash` |
