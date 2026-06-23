# AI Agent Flow

## 전체 흐름 요약

| 단계 | 주요 컴포넌트 | 역할 |
|------|-------------|------|
| 0. 인증 | X-Internal-Token | Spring Boot → FastAPI 서버 간 인증 |
| 1. 메모리 동기화 | Redis | 요청 memory와 Redis 비교·갱신 |
| 2. 장기 기억 로드 | Vertex AI Embeddings + pgvector | 사용자 메시지와 유사한 과거 대화 검색 |
| 3. 의도 파악 + 스트리밍 | Orchestrator (gemini-3.1-pro-preview) | 도구 호출·외부 API 수집·텍스트 스트리밍 |
| 4. 데이터 전처리 | preprocessor_agent (gemini-3.5-flash) | Tavily 비정형 결과 요약 (도구 함수 내부) |
| 5. 타입 판별 | classification_agent (gemini-3.5-flash) | 스트리밍 완료 후 응답 의도 분류 |
| 6. 임베딩 생성 | Vertex AI Embeddings | AI 응답 벡터화 |
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

동기화와 별개로, 대화 이력은 매 요청마다 **DB `chat_messages` 직접 조회**로 로드합니다 (Redis 캐시 미사용).

---

## 2. 장기 기억 로드 — pgvector 유사 메시지 검색

단기 기억(Redis 최근 20개)으로 커버되지 않는 과거 대화를 의미 기반으로 검색하여 orchestrator 컨텍스트에 추가합니다.

### 처리 흐름

```
사용자 메시지
  ↓
Vertex AI text-embedding-004 → 768차원 벡터 생성
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
  SELECT destinations, start_date, end_date, total_days,
         budget, adult_count, child_count, child_ages, day_plans
  FROM itineraries WHERE room_id = :room_id
  ↓
current_itinerary → orchestrator 시스템 프롬프트에 주입
  없으면 None (신규 일정 생성으로 처리)
```

`destinations`는 도시별 날짜 배열입니다. 단일 목적지도 길이 1 배열로 저장됩니다.

```json
[
  { "city": "Paris",     "start_date": "2025-06-01", "end_date": "2025-06-04" },
  { "city": "Rome",      "start_date": "2025-06-04", "end_date": "2025-06-07" },
  { "city": "Barcelona", "start_date": "2025-06-07", "end_date": "2025-06-10" }
]
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
| current_itinerary | 현재 여행 일정 전체 (destinations 배열·dates·budget·adults 포함) | DB itineraries (read-only) |

> FastAPI의 DB 접근은 **read-only**에 한합니다. 모든 DB 쓰기는 Java(Spring Boot)가 `done` 이벤트 수신 후 처리합니다.

---

## 3. 스트리밍 응답 생성 — Orchestrator (gemini-3.1-pro-preview)

### 3-1. 동적 컨텍스트 주입 (OrchestratorDeps)

오케스트레이터는 매 요청마다 DB + pgvector에서 로드한 컨텍스트를 **user_message 앞에 직접 붙여** LLM에 전달합니다.

```python
@dataclass
class OrchestratorDeps:
    ai_summary: str | None                      # 이전 대화 전체 요약 (DB chat_rooms)
    preferences: dict | None                    # 사용자 취향 JSON (DB chat_rooms)
    today: str                                  # YYYY-MM-DD — 날짜 계산 기준
    similar_messages: list[dict]                # pgvector 유사 과거 메시지 (최대 5개)
                                                # [{"role": "user"|"assistant", "content": "..."}]
    current_itinerary: dict | None              # 현재 여행 일정 전체 (DB itineraries, read-only)
                                                # {"destinations": [...], "start_date", "end_date", "budget", ...}
                                                # destinations: [{"city", "start_date", "end_date"}, ...]
                                                # 단일 목적지도 길이 1 배열. 일정 없으면 None
    request_type: str                           # classification_agent 판별 결과
```

`build_context_prompt(deps)` 함수가 위 값을 읽어 컨텍스트 블록 문자열을 생성하고, `orchestrator_agent.run()` 직전에 user_message 앞에 붙입니다.

```python
context_block = build_context_prompt(deps)
run_result = await orchestrator_agent.run(
    f"{context_block}\n\n---\n\n사용자 메시지: {user_message}",
    deps=deps,
    message_history=ctx["history"],
)
```

> **왜 `@agent.system_prompt` 데코레이터를 쓰지 않는가?**
> pydantic-ai 0.0.54에서 `@agent.system_prompt`에 `RunContext`를 받는 함수를 등록하면,
> `async def`든 `def`든 실제 `agent.run()` 시점에 **조용히 호출되지 않는** 버그가 있습니다.
> 오류도 없고 예외도 없이 시스템 프롬프트 함수가 무시되어 LLM이 컨텍스트 없이 응답합니다.
> 이 버그를 우회하기 위해 컨텍스트를 user_message에 직접 주입하는 패턴을 사용합니다.

컨텍스트 블록 구성 순서 (위에서 아래로):
1. 오늘 날짜
2. `## 현재 여행 기본 정보` (current_itinerary가 있으면 실제 값, 없으면 "등록 안 됨" 명시)
3. `## 이전 대화 요약` (ai_summary가 있을 때만)
4. `## 사용자 취향` (preferences가 있을 때만)
5. `## 참고할 과거 대화` (similar_messages가 있을 때만)
6. `## 이번 요청: {type}` — 타입별 응답 형식 지시문
7. `## 메모리 업데이트` 지시문

어댑터·도구 함수는 deps를 직접 참조하지 않습니다.

### 3-2. 타입별 orchestrator 처리 방식

classification_agent가 판별한 `type`은 `OrchestratorDeps.request_type`으로 전달됩니다.
orchestrator는 이를 참고해 적절한 도구를 선택합니다.

| type | 외부 API 도구 | 구조화 출력 필드 | Spring Boot 후처리 |
|------|-------------|----------------|------------------|
| `itinerary` | search_place, search_web, get_weather, find_route 등 | `day_plans` | dayPlans로 DB 일정 교체. `day_plans=null`이면 되묻기로 처리하여 DB 업데이트 없음 |
| `change` | **없음** | `change` | change 값으로 DB 직접 업데이트. `change=null`이면 되묻기로 처리하여 DB 업데이트 없음 |
| `reservation` | search_flights / search_hotels + 예약 API | `reservation` | reservations 테이블 저장 |
| `cancel` | 취소 API | `cancel` | reservations.status = "cancelled" |
| `chat` | search_web, get_weather 등 (필요 시) | — | 추가 처리 없음 |

#### itinerary 타입 — 필수 정보 검증 (2중 검증)

프론트엔드에서 기본 여행 정보를 입력받더라도, 오케스트레이터가 일정 생성 전에 한 번 더 검증합니다.
아래 항목 중 하나라도 누락이면 `day_plans = null`로 두고 `message`에서 되물어봅니다.

| 검증 항목 | 되묻기 예시 |
|----------|-----------|
| `destinations` 배열이 비거나 없음 | "여행지를 알려주세요." |
| `start_date` 또는 `end_date` 없음 | "여행 날짜를 알려주세요." |
| `adult_count`가 0 또는 없음 | "여행 인원을 알려주세요." |
| destinations 내 도시의 날짜 누락 | "각 도시의 체류 날짜를 알려주세요." |

#### change 타입 — 정보 부족 시 되묻기

변경에 필요한 정보가 부족하면 `change = null`로 두고 `message`에서 사용자에게 되물어봅니다. DB 업데이트는 발생하지 않습니다.

| 상황 | 되묻기 예시 |
|------|-----------|
| 아이 수를 늘리는데 나이 정보 없음 | "추가하시는 아이의 나이를 알려주시겠어요?" |
| destinations 변경인데 도시별 날짜 불명확 | "각 도시의 체류 날짜를 알려주세요. 예) 파리 3박, 로마 4박" |
| start_date만 있고 end_date 또는 기간 불명 | "여행 종료일 또는 총 여행 기간을 알려주세요." |

`child_ages` 배열 길이는 최종 `child_count`와 반드시 일치해야 합니다.

#### change 타입 — destinations 전체 교체

destinations 변경(여행지 추가·제거·순서 변경 포함)은 `change` 타입으로 처리합니다.
destinations가 바뀌면 `start_date`, `end_date`도 함께 반환합니다. `total_days`는 payload에 포함하지 않으며 Spring Boot가 `(end_date - start_date + 1)`로 재계산합니다.

```python
class ChangeFields(BaseModel):
    destinations: list[DestinationItem] | None = None  # 전체 배열 교체. 변경 없으면 None
    start_date: str | None = None   # destinations 변경 시 destinations[0].start_date와 일치
    end_date: str | None = None     # destinations 변경 시 destinations[-1].end_date와 일치
    # total_days는 payload에 포함하지 않음 — Spring Boot가 (end_date - start_date + 1)로 재계산
    budget: float | None = None
    adult_count: int | None = None
    child_count: int | None = None
    child_ages: list[int] | None = None

class DestinationItem(BaseModel):
    city: str
    start_date: str   # YYYY-MM-DD
    end_date: str     # YYYY-MM-DD
```

> destinations를 부분 수정하는 API는 없습니다. 항상 배열 전체를 교체합니다.

모든 type에서 사용자 취향·요약 정보가 감지되면 OrchestratorResult의 `ai_summary`, `preferences` 필드에 값을 채워 반환합니다 (별도 도구 호출 없음).

### 3-3. 도구 호출 및 응답 수신

```
build_context_prompt(deps) → context_block 생성
  ↓
orchestrator_agent.run(
    f"{context_block}\n\n---\n\n사용자 메시지: {user_input}",
    deps=OrchestratorDeps(...),
    message_history=history,
)
  ↓
type에 따라 필요한 도구 호출 (change는 도구 호출 없음)
  ↓
OrchestratorResult 구조화 출력 반환 (result.data)
  ↓
result.data.message → SSE event: chunk 한 번에 전송
result.data.day_plans / change / reservation / cancel → done 이벤트에 포함
```

> 구조화 출력(`result_type=OrchestratorResult`)이기 때문에 `run_stream`이 아닌 `run`을 사용합니다.
> chunk 이벤트는 스트리밍이 아닌 단일 전송입니다.

등록된 도구 8개의 입력/출력 명세는 **[docs/agent_tools.md](agent_tools.md)** 참조.

---

## 4. 비정형 데이터 전처리 — preprocessor_agent

`search_web` 도구 함수 내부에서 Tavily 결과를 gemini-3.5-flash로 요약합니다.
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

`classification_agent`(gemini-3.5-flash)는 **orchestrator 실행 전에** 사용자 메시지만 보고 `type`을 판별합니다.
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
| `change` | 여행 날짜·예산·인원(성인/아이 수·나이)·**여행지(destinations 전체 교체)** 변경 요청 |
| `reservation` | 항공권 또는 숙소 예약 요청 |
| `cancel` | 예약 취소 요청 |
| `chat` | 위 4가지에 해당하지 않는 일반 대화·질문·정보 요청 |

### itinerary vs change 구분

- **itinerary**: "경복궁 대신 창덕궁으로 바꿔줘", "3일차 일정 추가해줘" → 일정 내용(day_plans) 변경
- **change**: "여행 날짜 5월 3일부터 7일로 바꿔줘", "예산 100만원으로 늘려줘", "파리 대신 암스테르담으로 바꿔줘", "로마 다음에 바르셀로나 추가해줘" → 여행 기본 정보(destinations·날짜·예산·인원) 변경

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
| `change.destinations` | destinations 전체 배열 교체 시 포함. `start_date`/`end_date`/`total_days`도 함께 포함 |
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

- 모델: `text-embedding-004` (Vertex AI), 차원 `768`
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
[5] type == "itinerary" → run_itinerary_pipeline (4단계 파이프라인, 다구간 지원)
    type 그 외 → orchestrator_agent.run(user_message, deps, history)
    ├─ type에 맞는 외부 API 도구 호출
    └─ OrchestratorResult 구조화 출력 반환
    ※ day_plans 반환 정책:
       - 신규 생성: 전체 날짜 포함
       - 수정:     사용자가 요청한 날짜만 반환 (변경 없는 날짜는 포함하지 않음)
    ※ change.destinations 반환 정책:
       - destinations 변경 시 배열 전체 교체 (부분 수정 없음)
       - start_date / end_date / total_days 항상 함께 반환
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
| `memory:{chat_room_id}` | JSON | `ai_summary`(text) + `preferences`(json) |
| `chatroom_history:{room_id}` | bytes (JSON) | 최근 **20개** 메시지 raw 이력 (테스트·검증용) |
| `pgchatroom_history:{room_id}` | bytes (JSON) | pgvector 유사도 검색 결과 상위 5개 (테스트·검증용) |

`memory` 키 구조:
```json
{
  "ai_summary": "지금까지의 대화 전체 누적 요약",
  "preferences": { "food": "noodle", "transport": "transit" }
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

## 9. itinerary 파이프라인 — 다구간 상세

`run_itinerary_pipeline`은 `destinations` 배열을 기준으로 동작합니다. 단일 목적지(배열 길이 1)와 다구간(길이 N) 모두 동일한 코드로 처리합니다.

### LLM 호출 횟수 — 목적지 수와 무관하게 고정

| 단계 | 모델 | 호출 수 |
|------|------|---------|
| classification_agent | gemini-3.5-flash | 1 |
| _extract_english_cities (배치) | gemini-3.5-flash | 1 (N개 도시 한 번에) |
| _fetch_web_summaries (도시별 병렬 후 배치 요약) | gemini-3.5-flash | 1 |
| planner_agent | gemini-3.1-pro-preview | 1 |
| synthesizer_agent | gemini-3.1-pro-preview | 1 |
| **합계** | | **gemini-3.1-pro-preview ×2 + gemini-3.5-flash ×3** |

### Phase 1 — 병렬 데이터 수집 (LLM 0회)

N개 목적지의 모든 데이터를 `asyncio.gather`로 한꺼번에 수집합니다.

```
destinations = [Paris(06-01~04), Rome(06-04~07), Barcelona(06-07~10)]

병렬 실행:
  웹 검색   × 3 도시 (Tavily, 도시당 2쿼리)
  날씨      × 3 도시 (Open-Meteo)
  항공      × 4 구간 (Seoul→Paris, Paris→Rome, Rome→Barcelona, Barcelona→Seoul)
  숙소      × 3 도시 (Duffel, 각자 check_in/check_out 다름)
  도시명 영문 변환 (배치 1회 — GPT-4o-mini)

웹검색 결과 요약 (배치 1회 — GPT-4o-mini): 전 도시 결과 → 도시별 요약 dict
```

### Phase 2 — 플래너 (GPT-4.1 ×1)

모든 목적지 데이터를 단일 프롬프트에 담아 한 번에 처리합니다.

```python
class SelectedFlight(BaseModel):
    direction: str    # "depart" | "connect" | "return"
    leg_index: int    # 0=Seoul→D1, 1=D1→D2, 2=D2→D3, ...
    origin: str
    destination: str
    ...

class DaySchedule(BaseModel):
    date: str         # YYYY-MM-DD
    city: str         # 어느 도시의 일정인지 — 합성기가 도시 전환일 처리에 사용
    ordered_queries: list[str]

class PlannerOutput(BaseModel):
    days: list[DaySchedule]
    selected_flights: list[SelectedFlight]  # depart 1 + connect N-1 + return 1
    selected_hotels: list[SelectedHotel]    # 도시당 1개
```

플래너 프롬프트 추가 규칙:
- 도시 이동일: 오전 관광 → 공항/역 이동 → 연결편 → 다음 도시 도착 후 일정
- 연결 항공이 없는 도시 간 이동은 기차/버스 옵션 포함 (Tavily 검색 결과 활용)

### Phase 3 — 장소 검색 + 동선 병렬 (LLM 0회)

`DaySchedule.ordered_queries` 전체를 Google Maps에 병렬 호출합니다. 도시가 여러 개여도 쿼리에 도시명이 포함되어 있어 추가 처리 불필요합니다.

### Phase 4 — 합성기 (GPT-4.1 ×1)

전체 일정을 단일 호출로 작성합니다.

합성기 프롬프트 추가 규칙:
- 도시 전환일 항목: `이전 도시 체크아웃 → 공항/역 이동 → 연결편 → 다음 도시 이동`
- 연결편 cost: `SelectedFlight[direction="connect"]`의 price_original·currency·price_krw 사용
- 숙소 체크인 항목: 해당 도시 `SelectedHotel.check_in` 날짜에 삽입

---

## 10. 구현 설계 결정 사항

### 10-1. pgvector 비동기 접근

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

### 10-2. 단계별 에러 처리 정책

각 단계 실패 시 전체 요청을 중단하지 않고 폴백합니다.

| 단계 | 실패 시 처리 |
|------|------------|
| pgvector 유사 메시지 검색 | `similar_messages = []`로 폴백, 스트리밍 계속 진행 |
| classification_agent | `type = "chat"`, 타입별 페이로드 없이 done 이벤트 전송 |
| AI 응답 임베딩 생성 | `embedding = null`로 done 이벤트 전송. Spring Boot는 null embedding은 저장하지 않음 |
| orchestrator 스트리밍 중 실패 | SSE 연결 종료. Spring Boot가 `504 Gateway Timeout` 또는 `503` 반환 |

### 10-3. LLM 모델 기본값

`ORCHESTRATOR_MODEL`, `PREPROCESSOR_MODEL` 환경변수가 없으면 아래 기본값을 사용합니다.

| 역할 | `LLM_PROVIDER=vertexai` |
|------|------------------------|
| orchestrator | `gemini-3.1-pro-preview` |
| preprocessor / classification | `gemini-3.5-flash` |
