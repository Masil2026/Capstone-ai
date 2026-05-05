## **[POST] /api/v1/ai-messages**

Spring Boot가 AI Agent에 사용자 메시지를 전달하고 스트리밍 응답을 수신합니다.

---

## **1. 기본 정보**

| 항목 | 내용 |
| --- | --- |
| 호출 방향 | Spring Boot → FastAPI |
| Method | `POST` |
| URL | `/api/v1/ai-messages` |
| Summary | AI Agent 스트리밍 요청 |
| 인증 | 내부 서버 토큰 (`X-Internal-Token` 헤더) |
| 요청 Content-Type | `application/json` |
| 응답 Content-Type | `text/event-stream (SSE)` |

---

## **2. 요청 (Request)**

### **2.1 Headers**

| Name | Required | Example | Description |
| --- | --- | --- | --- |
| X-Internal-Token | Y | `sk-internal-abc123` | 내부 서버 간 인증 토큰. FastAPI가 환경변수와 비교하여 검증. Spring Boot는 `${ai.internal-token}` 환경변수 값을 전송 |
| Content-Type | Y | `application/json` | — |

### **2.2 Body**

```json
{
  "roomId":"a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "content":"경복궁 대신 창덕궁으로 바꿔줘",
  "memory": {
    "aiSummary":"사용자는 도쿄 여행을 준비 중이며 예산은 50만원이다.",
    "preferences": {
      "budget":"economy",
      "style":"adventure"
    }
  }
}
```

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| roomId | Y | UUID | 채팅방 고유 ID |
| content | Y | String | 사용자 메시지 원문 |
| memory | N | Object | Spring Boot의 `chat_rooms.ai_summary`, `chat_rooms.preferences` 값. 없으면 `null` 또는 미전송 |

### **2.3 memory 동기화 규칙 추가**

AI 서버는 요청을 받으면 Redis의 `chat_history:{roomId}` 또는 별도 memory key에 저장된 `aiSummary/preferences`와, Spring Boot가 전달한 `memory` 값을 비교한다.

```
- Redis에 memory가 없고 요청 memory가 있으면 Redis에 저장한다.
- Redis memory와 요청 memory가 같으면 그대로 유지한다.
- Redis memory와 요청 memory가 다르면 요청 memory 기준으로 Redis를 업데이트한다.
- 요청 memory가 null이고 Redis memory가 있으면 Redis 값을 유지한다.
- 요청 memory와 Redis memory가 모두 없으면 memory 없이 대화를 진행한다.
```

---

## **3. 응답 (Response)**

FastAPI는 두 종류의 SSE 이벤트를 순서대로 전송합니다.

### **3.1 chunk 이벤트 (0~N회)**

- **Description**: AI Agent가 생성한 텍스트를 토큰 단위로 스트리밍합니다.

**SSE Event:** `chunk`

```json
{
  "content": "창덕궁으로"
}
```

| Field | Type | Description |
| --- | --- | --- |
| content | String | 스트리밍 텍스트 일부 |

### **3.2 done 이벤트 (마지막 1회)**

- **Description**: 스트리밍이 완료되면 최종 페이로드를 포함한 done 이벤트가 전송됩니다. type 값에 따라 조건부 필드가 달라집니다.

### **공통 필드**

| Field | Type | Nullable | Description |
| --- | --- | --- | --- |
| type | String | N | `"chat"` / `"itinerary"` / `"change"` / `"reservation"` / `"cancel"` |
| userMessage | Object | N | 사용자 메시지 텍스트 및 임베딩 벡터 |
| assistantMessage | Object | N | AI 응답 전체 텍스트 및 임베딩 벡터 |
| memory | Object | Y | FastAPI가 갱신한 대화 요약 및 선호도. 이번 턴에 변경 없으면 `null` |

### **userMessage / assistantMessage 구조**

```json
{
  "content": "메시지 텍스트 전체",
  "embedding": [0.0231, -0.1234, 0.0087, ...]
}
```

> 임베딩 모델: OpenAI `text-embedding-3-small`, dim = `1536`
> Spring Boot는 수신 후 `chat_messages.embedding`(pgvector)에 저장합니다.

### **memory 구조**

```json
{
  "aiSummary": "도쿄 3박 여행. 예산 50만원. 혼자 여행 선호.",
  "preferences": {
    "budget": "economy",
    "style": "adventure"
  }
}
```

> Spring Boot는 수신 후 `chat_rooms.ai_summary`, `chat_rooms.preferences`에 저장합니다.
> 

---

### **type: "chat" 일 때**

**SSE Event:** `done`

```json
{
  "type": "chat",
  "userMessage": {
    "content": "도쿄 날씨 어때?",
    "embedding": [0.0231, -0.1234, ...]
  },
  "assistantMessage": {
    "content": "5월 도쿄는 맑고 따뜻한 날씨입니다!",
    "embedding": [0.0871, 0.0023, ...]
  },
  "memory": null
}
```

---

### **type: "itinerary" 일 때**

**SSE Event:** `done`

```json
{
  "type": "itinerary",
  "userMessage": {
    "content": "경복궁 대신 창덕궁으로 바꿔줘",
    "embedding": [0.0231, -0.1234, 0.0087, -0.0442, 0.0511]
  },
  "assistantMessage": {
    "content": "경복궁을 창덕궁으로 변경했습니다! 오전 일정은 창덕궁 방문으로 반영했어요.",
    "embedding": [0.0871, 0.0023, -0.0194, 0.0641, -0.0312]
  },
  "memory": {
    "aiSummary": "사용자는 서울 여행 중 궁궐 방문을 선호하며 기존 경복궁 일정을 창덕궁으로 변경 요청함. 전통문화 체험과 시장 방문에도 관심이 있음.",
    "preferences": {
      "preferredPlaces": [
        "궁궐",
        "전통시장"
      ],
      "travelStyle": "여유로운 일정",
      "pace": "moderate"
    }
  },
  "itinerary": {
    "dayPlans": {
      "2026-05-01": [
        {
          "plan_name": "창덕궁 방문",
          "time": "09:00 ~ 12:00",
          "place": "창덕궁",
          "note": "후원 투어 예약 필요"
        },
        {
          "plan_name": "광장시장 점심",
          "time": "12:00 ~ 14:30",
          "place": "광장시장",
          "note": ""
        }
      ],
      "2026-05-02": [
        {
          "plan_name": "남산타워 방문",
          "time": "10:00 ~ 12:00",
          "place": "남산타워",
          "note": ""
        }
      ]
    }
  }
}
```

### **itinerary.dayPlans 아이템 필드**

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| plan_name | Y | String | 일정 이름 |
| time | Y | String | 시간대. `"HH:MM ~ HH:MM"` 형식 (예: `"09:00 ~ 12:00"`) |
| place | Y | String | 장소명 |
| note | N | String | 메모. 생략 시 빈 문자열(`""`)로 처리 |

> `status`는 payload에 포함하지 않습니다. Spring Boot가 기존 time 값 기준으로 결정합니다 (동일 time → 기존 status 유지, 신규 아이템 → `"todo"`).
> 

> 날짜 단위 완전 교체(full replacement) 방식입니다. payload에 포함된 날짜의 아이템 배열 전체를 FastAPI 데이터로 대체하며, 아이템 단위 병합(merge)은 수행하지 않습니다. payload에 없는 날짜는 기존 상태를 유지합니다.
> 

---

### **type: "change" 일 때**

**SSE Event:** `done`

```json
{
  "type": "change",
  "userMessage": {
    "content": "여행 날짜 5월 3일부터 7일로 바꿔줘",
    "embedding": [0.0231, -0.1234, ...]
  },
  "assistantMessage": {
    "content": "여행 기간을 5월 3일~7일로 변경했습니다.",
    "embedding": [0.0871, 0.0023, ...]
  },
  "memory": null,
  "change": {
    "startDate": "2026-05-03",
    "endDate": "2026-05-07",
    "budget": 500000.00,
    "adultCount": 2,
    "childCount": 1,
    "childAges": [5]
  }
}
```

### **change 필드**

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| startDate | N | `DATE (YYYY-MM-DD)` | 변경할 여행 시작일 |
| endDate | N | `DATE (YYYY-MM-DD)` | 변경할 여행 종료일 |
| budget | N | `Decimal` | 변경할 예산 |
| adultCount | N | `Int` | 변경할 어른 수 |
| childCount | N | `Int` | 변경할 아이 수 |
| childAges | N | `Int[]` | 변경할 아이 나이 배열 |

> `destination`은 payload에 포함하지 않습니다. 목적지는 수정 불가입니다.
> 

> 변경하지 않는 필드는 payload에 포함하지 않습니다 (`null` 미전송).
> 

---

### **type: "reservation" 일 때**

**SSE Event:** `done`

```json
{
  "type": "reservation",
  "userMessage": {
    "content": "항공권 예약해줘",
    "embedding": [0.0231, -0.1234, ...]
  },
  "assistantMessage": {
    "content": "대한항공 KE123편을 예약했습니다.",
    "embedding": [0.0871, 0.0023, ...]
  },
  "memory": null,
  "reservation": {
    "type": "flight",
    "bookingUrl": "https://booking.example.com/flight/123",
    "externalRefId": "KE12345678",
    "detail": {
      "airline": "대한항공",
      "flight_no": "KE123",
      "departure": {
        "airport": "ICN",
        "datetime": "2026-05-01T09:00:00"
      },
      "arrival": {
        "airport": "NRT",
        "datetime": "2026-05-01T11:30:00"
      },
      "seat_class": "economy",
      "passengers": [
        {
          "name": "홍길동",
          "passport": "M12345678"
        }
      ]
    },
    "totalPrice": 320000.00,
    "currency": "KRW",
    "reservedAt": "2026-05-01T09:00:00+09:00"
  }
}
```

### **reservation 필드**

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| type | Y | `String` | `"flight"` / `"accommodation"` |
| bookingUrl | N | `String` | AI가 제공한 예약 링크 |
| externalRefId | N | `String` | 외부 예약 번호 |
| detail | Y | `Object` | 예약 유형별 상세. `POST /api/v1/reservations` 명세 §4.2 구조와 동일 |
| totalPrice | N | `Decimal` | 총 결제 금액 |
| currency | N | `String` | 통화 코드. 미전송 시 `"KRW"` 적용 |
| reservedAt | N | `ISO-8601 + offset` | 예약 완료 일시 |

> `itineraryId`는 payload에 포함하지 않습니다. Spring Boot가 `roomId`로 조회합니다.
> 

> `bookedBy`와 `status`는 Spring Boot에서 각각 `"ai"`, `"confirmed"`로 고정합니다.
> 

---

### **type: "cancel" 일 때**

**SSE Event:** `done`

```json
{
  "type": "cancel",
  "userMessage": {
    "content": "항공권 예약 취소해줘",
    "embedding": [0.0231, -0.1234, ...]
  },
  "assistantMessage": {
    "content": "KE123편 예약을 취소했습니다.",
    "embedding": [0.0871, 0.0023, ...]
  },
  "memory": null,
  "cancel": {
    "reservationId": "c3a7db7a-3b93-4b50-a667-4ac922e2ff11",
    "cancelledAt": "2026-04-10T10:00:00+09:00"
  }
}
```

### **cancel 필드**

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| reservationId | Y | `UUID` | 취소할 예약 고유 ID |
| cancelledAt | Y | `ISO-8601 + offset` | 취소 일시 |

---

## **4. 비즈니스 로직**

### **4.1 FastAPI 처리 흐름 (Sequence)**

1. **Token Validation**: 요청 헤더 `X-Internal-Token` 값을 환경변수와 비교하여 검증합니다.
2. **Memory Sync**: 요청 Body의 `memory`와 Redis에 저장된 roomId별 memory를 비교합니다.
    - Redis에 memory가 없고 요청 `memory`가 있으면 요청 `memory`를 Redis에 저장합니다.
    - Redis memory와 요청 `memory`가 같으면 Redis 값을 그대로 유지합니다.
    - Redis memory와 요청 `memory`가 다르면 요청 `memory` 기준으로 Redis를 업데이트합니다.
    - 요청 `memory`가 `null`이고 Redis memory가 있으면 Redis 값을 유지합니다.
    - 요청 `memory`와 Redis memory가 모두 없으면 memory 없이 대화를 진행합니다.
3. **History Load**: Redis `chat_history:{roomId}`에서 대화 이력을 조회하고, Redis에 저장된 `aiSummary`, `preferences`를 함께 로드합니다.
4. **Agent 처리 및 type 판별**: 대화 이력, memory, 사용자 메시지를 기반으로 Agent가 요청 의도를 분석하고 `type`을 판별합니다.
    - `"chat"`
    - `"itinerary"`
    - `"change"`
    - `"reservation"`
    - `"cancel"`
5. **외부 API 호출 및 전처리**: 필요한 경우 외부 API를 호출하고 서비스 payload 구조에 맞게 응답을 전처리합니다.
    - 장소 검색
    - 일정 생성/수정에 필요한 장소 정보 조회
    - 항공/숙소 예약 후보 조회
    - 날씨, 영업시간, 위치 정보 조회
    - 날짜/시간 포맷 정규화
    - 가격/통화 정규화
    - 불필요한 필드 제거
    - `dayPlans` 구조 변환
    - `reservation.detail` 구조 변환
6. **LLM 호출**: Redis의 대화 이력, memory, 사용자 메시지, 외부 API 전처리 결과를 기반으로 OpenAI Chat Completion API를 호출합니다.
7. **Chunk 전송**: 생성된 텍스트 토큰을 `chunk` 이벤트로 실시간 스트리밍합니다.
8. **Embedding 생성**: 응답 완료 후 사용자 메시지 및 AI 응답에 대한 임베딩 벡터를 생성합니다.
    - 임베딩 모델: OpenAI `text-embedding-3-small`
    - dim = `1536`
9. **Memory 갱신 판단**: LLM 결과를 기반으로 이번 턴에서 memory 갱신이 필요한지 판단합니다.
    - 사용자의 선호도, 예산, 여행 스타일, 동행 정보 등 장기 기억할 정보가 있으면 Redis memory를 업데이트합니다.
    - 변경 사항이 있으면 `done.memory`에 갱신된 `aiSummary`, `preferences`를 포함합니다.
    - 변경 사항이 없으면 `done.memory`는 `null`로 전송합니다.
    - `memory`는 `type`과 무관하게 포함될 수 있습니다. 즉 `"chat"`, `"change"`, `"reservation"`, `"cancel"` 타입에서도 memory 변경이 있으면 `null`이 아닌 값이 내려올 수 있습니다.
10. **done 전송**: `type`, 메시지 임베딩, 갱신된 `memory`, type별 조건부 페이로드를 포함한 `done` 이벤트를 전송합니다.

> FastAPI는 DB에 직접 쓰지 않습니다. 모든 DB 저장은 Spring Boot가 `done` 이벤트 수신 후 처리합니다. FastAPI의 DB 접근은 벡터 유사도 검색 등 read-only에 한합니다.
> 

> FastAPI의 Redis memory는 대화 진행을 위한 캐시/컨텍스트 저장소이며, 최종 영속 저장 기준은 Spring Boot의 `chat_rooms.ai_summary`, `chat_rooms.preferences`입니다.
> 

---

---

## **4.2 Spring Boot 처리 흐름**

1. **ChatRoom 조회**: 사용자 메시지 수신 시 `roomId`로 `chat_rooms`를 조회합니다.
    - `chat_rooms.ai_summary`
    - `chat_rooms.preferences`
2. **AI 서버 요청 생성**: Spring Boot는 FastAPI에 사용자 메시지와 현재 저장된 memory를 함께 전달합니다.
    - `roomId`
    - `content`
    - `memory.aiSummary`
    - `memory.preferences`
3. **SSE 스트림 수신**: FastAPI 응답을 SSE로 수신합니다.
    - `chunk` 이벤트는 클라이언트로 실시간 전달합니다.
    - `done` 이벤트는 최종 저장 및 후속 처리를 위해 파싱합니다.
4. **Message 저장**: `done.userMessage`, `done.assistantMessage`를 `chat_messages` 테이블에 저장합니다.
5. **Embedding 저장**: 각 메시지의 임베딩 벡터를 native query로 업데이트합니다.
    - `CAST(:embedding AS vector)`
6. **Memory 저장**: `done.memory`가 `null`이 아닌 경우 `chat_rooms.ai_summary`, `chat_rooms.preferences`를 갱신합니다.
    - `done.memory == null`이면 기존 값을 유지합니다.
    - memory 저장 여부는 `type`이 아니라 `done.memory`의 null 여부로 판단합니다.
7. **type 분기 처리**:
    - `"chat"` → 추가 도메인 처리 없이 종료
    - `"itinerary"` → `itinerary_logs` 스냅샷 저장 → `itineraries.day_plans` 갱신
        - 동일 `time` 일정이 있으면 기존 `status` 유지
        - 신규 일정이면 `status = "todo"`
        - `time` 기준 오름차순 정렬
    - `"change"` → `itinerary_logs` 스냅샷 저장 → `itineraries` 기본 정보 갱신
        - 날짜 범위 변경 시 `day_plans` 키 조정
        - `destination`은 수정하지 않음
    - `"reservation"` → `itineraryRepository.findByRoomId(roomId)`로 `itineraryId` 조회 → `reservations` 저장
        - `bookedBy = "ai"`
        - `status = "confirmed"`
    - `"cancel"` → `reservations.status = "cancelled"` 갱신, `cancelled_at` 저장
    - `null` → 하위 호환. `log.warn` 후 `"chat"`과 동일 처리
    - unknown type → `log.error` 후 `502 Bad Gateway` 반환

---

## **4.3 Memory 동기화 흐름**

```
대화 시작
↓
Spring Boot가 roomId로 chat_rooms 조회
↓
chat_rooms.ai_summary / preferences를 AI 서버 요청 body의 memory로 전달
↓
AI 서버가 요청 memory와 Redis memory 비교
↓
다르거나 Redis에 없으면 Redis memory 업데이트
↓
Redis memory + 대화 이력 기반으로 LLM 처리
↓
LLM 결과에서 memory 갱신 필요 시 Redis memory 업데이트
↓
AI 서버가 done.memory로 갱신된 memory 반환
↓
Spring Boot가 done.memory를 chat_rooms에 저장
```

---

## **4.4 DB 저장 구조**

### **chat_messages Table**

| Column | Type | Description |
| --- | --- | --- |
| room_id | `UUID` | 소속 채팅방 ID |
| role | `VARCHAR(20)` | `user` / `assistant` |
| content | `TEXT` | 메시지 본문 |
| embedding | `vector(1536)` | 메시지 임베딩 벡터 |
| created_at | `TIMESTAMP` | 저장 일시 |

---

## **4.5 에러 처리 (Spring Boot 측 WebClient)**

| 상황 | Spring Boot 응답 | 원인 |
| --- | --- | --- |
| 60초 내 스트림 미완료 | `504 Gateway Timeout` | `TimeoutException` |
| FastAPI 서버 연결 불가 | `503 Service Unavailable` | `WebClientRequestException` |
| FastAPI 4xx / 5xx 응답 | `502 Bad Gateway` | `WebClientResponseException` |
| 알 수 없는 SSE event type | 해당 이벤트 무시 (`log.warn`) | - |
| unknown type 수신 | `502 Bad Gateway` | FastAPI ↔ Spring Boot 계약 위반 |

---

## **5. 호출 예시 (Example)**

```bash
curl -X POST https://ai-agent-server.internal/api/v1/ai-messages \
  -H "X-Internal-Token: sk-internal-abc123" \
  -H "Content-Type: application/json" \
  -d '{
    "roomId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "content": "경복궁 대신 창덕궁으로 바꿔줘"
  }'
```