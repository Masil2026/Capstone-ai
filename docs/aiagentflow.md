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

## 1. 컨텍스트 로드 — DB read + Redis 동기화

매 요청마다 `roomId`를 기반으로 DB에서 최신 값을 직접 조회하고, 조회 결과를 Redis에 동기화(덮어씀)합니다.

```
요청 시작
  ↓
DB chat_rooms 직접 조회 (매 요청마다)
  → ai_summary, preferences 로드
  ↓
조회 결과로 Redis memory:{chat_room_id} 덮어쓰기 (fire-and-forget)
  → Java 백엔드가 직전 done 이벤트를 보고 DB에 저장한 최신 값이 Redis에 반영됨
```

> **Redis 캐시-히트 로직 없음.** 매 요청마다 무조건 DB에서 읽습니다.
> Redis 동기화는 검증 목적으로, 이후 파이프라인에서 Redis를 활용하는 경우를 위한 사전 정렬입니다.

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
| ai_summary | 전체 대화 누적 요약 | DB chat_rooms (매 요청 직접 조회) |
| preferences | 사용자 취향 JSON (전체 누적) | DB chat_rooms (매 요청 직접 조회) |
| chat_history | 최근 20개 메시지 | DB chat_messages (매 요청 직접 조회) |
| 유사 과거 메시지 | 의미적으로 유사한 과거 대화 최대 5개 | pgvector (read-only) |
| current_itinerary | 현재 여행 일정 전체 (destination·dates·budget·adults 포함) | DB itineraries (read-only) |

> FastAPI의 DB 접근은 **read-only**에 한합니다. 모든 DB 쓰기는 Java(Spring Boot)가 `done` 이벤트 수신 후 처리합니다.

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

| type | 외부 API 도구 | 구조화 출력 필드 | Spring Boot 후처리 |
|------|-------------|----------------|------------------|
| `itinerary` | search_place, search_web, get_weather, find_route 등 | `day_plans` | dayPlans로 DB 일정 교체 |
| `change` | **없음** | `change` | change 값으로 DB 직접 업데이트 |
| `reservation` | search_flights / search_hotels + 예약 API | `reservation` | reservations 테이블 저장 |
| `cancel` | 취소 API | `cancel` | reservations.status = "cancelled" |
| `chat` | search_web, get_weather 등 (필요 시) | — | 추가 처리 없음 |

모든 type에서 사용자 취향·요약 정보가 감지되면 OrchestratorResult의 `ai_summary`, `preferences` 필드에 값을 채워 반환합니다 (별도 도구 호출 없음).

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
| `itinerary.dayPlans` | OrchestratorResult.day_plans |
| `change.*` | OrchestratorResult.change |
| `reservation` | OrchestratorResult.reservation |
| `cancel.*` | OrchestratorResult.cancel |
| `memory.aiSummary` | OrchestratorResult.ai_summary (이전 ai_summary + 현재 대화 합산 전체 재요약). `None`이면 `done.memory = null` |
| `memory.preferences` | OrchestratorResult.preferences (이전 preferences 포함 전체 누적 dict). `None`이면 `done.memory = null` |
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
[1] DB에서 컨텍스트 직접 로드 (매 요청마다)
    - chat_rooms: ai_summary, preferences
    - chat_messages: 최근 20개 대화 이력
    - itineraries: 현재 여행 일정 (roomId 기준, 없으면 None)
    → 로드 직후 Redis memory 동기화 (fire-and-forget)
  ↓
[2] 사용자 메시지 임베딩 생성 → pgvector 유사 메시지 검색 (상위 5개)
  ↓
[3] OrchestratorDeps 구성 (ai_summary·preferences·history·similar_messages·current_itinerary·request_type)
  ↓
[4] classification_agent.run(user_message) → type 판별
    (빠름. orchestrator 전에 완료하여 request_type으로 전달)
  ↓
[5] type == "itinerary" → run_itinerary_pipeline (4단계 파이프라인)
    type 그 외 → orchestrator_agent.run(user_message, deps, history)
    ├─ type에 맞는 외부 API 도구 호출
    └─ OrchestratorResult 구조화 출력 반환
  ↓
[6] chunk 이벤트 전송 (full_response 한 번에)
  ↓
[7] day_plans cost.amount_krw 자동 환산 (KRW 이외 통화만)
  ↓
[8] AI 응답 임베딩 생성
  ↓
[9] merged_summary / merged_prefs 계산
    (OrchestratorResult에 값이 있으면 사용, 없으면 ctx 값 유지)
    → ai_summary 또는 preferences 변경 시 Redis memory 업데이트
  ↓
[10] done 페이로드 구성 → event: done 전송
     memory 필드: OrchestratorResult에 변경이 있을 때만 포함 (없으면 null)
```

SSE 이벤트 포맷 및 `done` 페이로드 상세 구조는 **[docs/api/POST_v1_ai-messages.md](api/POST_v1_ai-messages.md)** 참조.

---

## 8. 메모리 아키텍처

AI 에이전트는 **DB(PostgreSQL)를 진실의 원천(source of truth)**으로 사용하고, Redis는 요청 시작 시 DB 값을 동기화해두는 보조 저장소입니다. 영속성은 `chat_rooms` 테이블(Java 관리)이 담당합니다.

### 8-1. Redis 저장 구조

| Redis 키 | 타입 | 내용 |
|----------|------|------|
| `memory:{chat_room_id}` | JSON | `ai_summary`(text) + `preferences`(json) + `loaded_at`(ISO 8601) |
| `chat_history:{chat_room_id}` | bytes (JSON) | 최근 **20개** 메시지 mirror (DB chat_messages 기반) |

`memory` 키 구조:
```json
{
  "ai_summary": "지금까지의 대화 전체 누적 요약",
  "preferences": { "food": "noodle", "transport": "transit" },
  "loaded_at": "2026-04-10T12:00:00Z"
}
```

### 8-2. memory 갱신 전체 흐름

```
[요청 시작]
  DB chat_rooms에서 ai_summary / preferences 직접 조회
  → Redis memory 덮어쓰기 (fire-and-forget)
  → OrchestratorDeps에 주입

[AI 처리]
  OrchestratorResult.ai_summary / preferences 값이 있으면
  → LLM이 이전 ai_summary + 현재 대화를 합산하여 전체 재요약 작성
  → LLM이 이전 preferences에 신규 항목을 추가/수정한 전체 dict 작성

[응답 후]
  merged_summary = 새 ai_summary (있으면) else DB에서 읽은 기존 값
  merged_prefs   = 새 preferences (있으면) else DB에서 읽은 기존 값
  ↓
  변경 있음 → Redis memory 업데이트
             done.memory = { aiSummary: merged_summary, preferences: merged_prefs }
             → Java(Spring Boot)가 chat_rooms.ai_summary / preferences 갱신
  변경 없음 → Redis memory 유지
             done.memory = null
```

`memory` 갱신은 `type`과 무관합니다. `"chat"` 타입에서도 새 취향이 감지되면 갱신됩니다.

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
