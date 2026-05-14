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

### **2.1.1 Query Parameters (선택)**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| hide_embedding | Boolean | `false` | **개발/Swagger 테스트 전용.** `true`이면 `done` 이벤트의 `userMessage.embedding`, `assistantMessage.embedding` 필드를 생략합니다. Spring Boot는 이 파라미터를 사용하지 않습니다. |

> ⚠️ Spring Boot에서 `?hide_embedding=true`로 호출하면 embedding이 누락되어 pgvector 저장이 불가합니다.

### **2.2 Body**

```json
{
  "roomId":"a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "content":"경복궁 대신 창덕궁으로 바꿔줘"
}
```

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| roomId | Y | UUID | 채팅방 고유 ID |
| content | Y | String | 사용자 메시지 원문 |

> `ai_summary`와 `preferences`는 FastAPI가 `roomId`로 `chat_rooms` 테이블을 직접 조회하여 로드합니다. Spring Boot는 이 값을 요청 body에 포함하지 않습니다.

---

## **3. 응답 (Response)**

FastAPI는 세 종류의 SSE 이벤트를 순서대로 전송합니다.

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

### **3.3 error 이벤트 (오류 발생 시 1회)**

- **Description**: 컨텍스트 로드 또는 에이전트 처리 중 오류가 발생하면 chunk·done 대신 error 이벤트가 전송됩니다. Spring Boot는 수신 후 `log.error` 처리합니다.

**SSE Event:** `error`

```json
{
  "message": "에이전트 오류: ..."
}
```

| Field | Type | Description |
| --- | --- | --- |
| message | String | 오류 원인 메시지 |

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
  "aiSummary": "도쿄 3박 여행. 예산 50만원. 혼자 여행 선호. 맛집 위주 일정으로 수정 요청.",
  "preferences": {
    "budget": "economy",
    "style": "adventure",
    "food": "local_restaurant"
  }
}
```

| 필드 | 설명 |
|------|------|
| `aiSummary` | **이전 ai_summary + 현재 대화를 합산한 전체 누적 재요약.** 현재 턴만의 요약이 아니라 대화 전체 흐름을 한 번에 담습니다. |
| `preferences` | **이전 preferences 내용을 포함한 전체 dict.** 새 항목만이 아니라 기존 항목을 유지하면서 업데이트된 전체 값입니다. |

> Spring Boot는 수신 후 `chat_rooms.ai_summary`, `chat_rooms.preferences`에 저장합니다.
> 저장된 값은 다음 요청 시 FastAPI가 DB에서 직접 읽어 LLM 컨텍스트에 주입합니다.
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
          "plan_name": "인천공항 → 나리타공항 (대한항공 KE705)",
          "time": "10:00 ~ 12:30",
          "place": "Narita International Airport",
          "note": "대한항공 KE705, 직항 2시간 30분",
          "cost": { "amount": 450000, "currency": "KRW", "amount_krw": null }
        },
        {
          "plan_name": "나리타공항 → 신주쿠역 이동 (나리타 익스프레스)",
          "time": "13:30 ~ 15:00",
          "place": "Narita Express (NEX)",
          "note": "신주쿠 직행, 약 90분",
          "cost": { "amount": 3070, "currency": "JPY", "amount_krw": 28549 }
        },
        {
          "plan_name": "저녁식사 — 신주쿠 라멘",
          "time": "18:30 ~ 19:30",
          "place": "Fuunji Ramen, Shinjuku",
          "note": "",
          "cost": { "amount": 1200, "currency": "JPY", "amount_krw": 11160 }
        }
      ],
      "2026-05-02": [
        {
          "plan_name": "아침식사 — 숙소 조식",
          "time": "08:00 ~ 09:00",
          "place": "Shinjuku Grand Hotel",
          "note": "조식 포함 요금",
          "cost": null
        },
        {
          "plan_name": "신주쿠역 → 아사쿠사역 이동 (지하철 오에도선)",
          "time": "09:00 ~ 09:35",
          "place": "Tokyo Metro 오에도선",
          "note": "타쿠타마 방향 탑승, 아사쿠사역 하차",
          "cost": { "amount": 280, "currency": "JPY", "amount_krw": 2604 }
        },
        {
          "plan_name": "센소지 참배 및 나카미세 거리 쇼핑",
          "time": "09:35 ~ 11:30",
          "place": "Senso-ji Temple, Asakusa",
          "note": "맑음 22°C, 선크림 필수",
          "cost": null
        }
      ]
    }
  }
}
```

### **itinerary.dayPlans 아이템 필드**

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| plan_name | Y | String | 일정 이름. 이동 항목은 `"{출발} → {도착} 이동 ({수단})"` 형식 |
| time | Y | String | 시간대. `"HH:MM ~ HH:MM"` 형식 (예: `"09:00 ~ 12:00"`) |
| place | Y | String | 장소명 또는 이동 수단·노선명 |
| note | N | String | 메모. 생략 시 빈 문자열(`""`)로 처리 |
| cost | N | Object \| null | 1인 기준 예상 비용. 무료이면 `null` |

### **cost 객체 구조**

| Field | Type | Description |
| --- | --- | --- |
| amount | Number | 1인 기준 현지 통화 금액 (소수점 허용) |
| currency | String | ISO 4217 통화 코드. 예) `"KRW"`, `"JPY"`, `"USD"`, `"CNY"` |
| amount_krw | Int \| null | 한화 환산 금액. `currency == "KRW"`이면 `null` |

> `status`는 payload에 포함하지 않습니다. Spring Boot가 기존 time 값 기준으로 결정합니다 (동일 time → 기존 status 유지, 신규 아이템 → `"todo"`).
> 

> 날짜 단위 완전 교체(full replacement) 방식입니다. payload에 포함된 날짜의 아이템 배열 전체를 FastAPI 데이터로 대체하며, 아이템 단위 병합(merge)은 수행하지 않습니다. payload에 없는 날짜는 기존 상태를 유지합니다.
> 

---

### **type: "change" 일 때**

**SSE Event:** `done`

**날짜·예산·인원 변경 예시**

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
    "endDate": "2026-05-07"
  }
}
```

**destinations 변경 예시** (destinations 변경 시 startDate·endDate 항상 함께 포함)

```json
{
  "type": "change",
  "userMessage": {
    "content": "로마 다음에 바르셀로나 추가해줘",
    "embedding": [0.0231, -0.1234, ...]
  },
  "assistantMessage": {
    "content": "파리→로마→바르셀로나 순서로 여행지를 업데이트했습니다.",
    "embedding": [0.0871, 0.0023, ...]
  },
  "memory": null,
  "change": {
    "destinations": [
      { "city": "Paris",     "start_date": "2025-06-01", "end_date": "2025-06-04" },
      { "city": "Rome",      "start_date": "2025-06-04", "end_date": "2025-06-07" },
      { "city": "Barcelona", "start_date": "2025-06-07", "end_date": "2025-06-10" }
    ],
    "startDate": "2025-06-01",
    "endDate": "2025-06-10"
  }
}
```

### **change 필드**

**기본 원칙: 변경된 필드만 전송합니다. 변경하지 않은 필드는 payload에 포함하지 않습니다 (`null` 미전송).**

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| destinations | N | `Object[]` | 여행지 변경 시 포함. 배열 전체를 항상 교체 — 부분 수정 없음. `[{"city":"Paris","start_date":"YYYY-MM-DD","end_date":"YYYY-MM-DD"}, ...]` |
| startDate | N | `DATE (YYYY-MM-DD)` | 여행 시작일 변경 시 포함. destinations 변경 시 `destinations[0].start_date`와 반드시 일치 |
| endDate | N | `DATE (YYYY-MM-DD)` | 여행 종료일 변경 시 포함. destinations 변경 시 `destinations[-1].end_date`와 반드시 일치 |
| budget | N | `Decimal` | 예산 변경 시 포함 |
| adultCount | N | `Int` | 성인 수 변경 시 포함 |
| childCount | N | `Int` | 아이 수 변경 시 포함 |
| childAges | N | `Int[]` | 아이 나이 변경 시 포함 |

**필드별 전송 규칙 요약**

| 사용자 요청 | 전송 필드 |
|------------|----------|
| "날짜 5월 3일~7일로 바꿔줘" | `startDate`, `endDate` |
| "예산 100만원으로 늘려줘" | `budget` |
| "성인 3명으로 변경해줘" | `adultCount` |
| "아이 추가, 7살" | `childCount`, `childAges` |
| "파리 대신 암스테르담으로 바꿔줘" | `destinations`(전체 배열), `startDate`, `endDate` |
| "날짜 바꾸고 예산도 늘려줘" | `startDate`, `endDate`, `budget` |

> Spring Boot는 수신한 필드만 `itineraries` 테이블에 업데이트합니다. 누락된 필드는 기존 DB 값을 유지합니다.
> `total_days`는 payload에 없으며, Spring Boot가 `(endDate - startDate + 1)`로 자동 재계산합니다.
> 

---

### **type: "reservation" 일 때**

**SSE Event:** `done`

**항공권 예시**

```json
{
  "type": "reservation",
  "userMessage": {
    "content": "항공권 예약해줘",
    "embedding": [0.0231, -0.1234, ...]
  },
  "assistantMessage": {
    "content": "대한항공편을 예약했습니다.",
    "embedding": [0.0871, 0.0023, ...]
  },
  "memory": null,
  "reservation": {
    "type": "flight",
    "bookingUrl": "https://booking.tripai.app/flights/FLT-20260501-B7XM2R",
    "externalRefId": "FLT-20260501-B7XM2R",
    "detail": {
      "airline": "대한항공",
      "departure": "ICN",
      "arrival": "NRT",
      "departing_at": "2026-05-01T09:00:00",
      "arriving_at": "2026-05-01T11:30:00",
      "stops": 0
    },
    "totalPrice": 320000.00,
    "currency": "KRW",
    "reservedAt": "2026-05-01T09:00:00+09:00"
  }
}
```

**숙소 예시**

```json
{
  "type": "reservation",
  "userMessage": {
    "content": "숙소 예약해줘",
    "embedding": [0.0231, -0.1234, ...]
  },
  "assistantMessage": {
    "content": "롯데호텔 도쿄 예약이 완료되었습니다.",
    "embedding": [0.0871, 0.0023, ...]
  },
  "memory": null,
  "reservation": {
    "type": "accommodation",
    "bookingUrl": "https://booking.tripai.app/stays/HTL-20260501-A3K9PQ",
    "externalRefId": "HTL-20260501-A3K9PQ",
    "detail": {
      "name": "롯데호텔 도쿄",
      "check_in": "2026-05-01",
      "check_out": "2026-05-03",
      "rooms": 1,
      "guests": 2
    },
    "totalPrice": 450000.00,
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
| detail | Y | `Object` | 예약 유형별 상세. 아래 `detail 구조` 참고 |
| totalPrice | N | `Decimal` | 총 결제 금액 |
| currency | N | `String` | 통화 코드. 미전송 시 `"KRW"` 적용 |
| reservedAt | N | `ISO-8601 + offset` | 예약 완료 일시 |

### **detail 구조 (type별)**

**type: `"flight"`**

| Field | Type | Description |
| --- | --- | --- |
| `airline` | `String` | 항공사명 |
| `departure` | `String` | 출발 공항 IATA 코드 (예: `"ICN"`) |
| `arrival` | `String` | 도착 공항 IATA 코드 (예: `"NRT"`) |
| `departing_at` | `String` | 출발 일시. ISO 8601 (예: `"2026-05-01T09:00:00"`) |
| `arriving_at` | `String` | 도착 일시. ISO 8601 (예: `"2026-05-01T11:30:00"`) |
| `stops` | `Int` | 경유 횟수. 직항이면 `0` |

**type: `"accommodation"`**

| Field | Type | Description |
| --- | --- | --- |
| `name` | `String` | 숙소명 |
| `check_in` | `String` | 체크인 날짜. `YYYY-MM-DD` |
| `check_out` | `String` | 체크아웃 날짜. `YYYY-MM-DD` |
| `rooms` | `Int` | 객실 수 |
| `guests` | `Int` | 투숙 인원 수 |

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
2. **Context Load**: `roomId`를 기준으로 필요한 컨텍스트를 조회합니다. **매 요청마다 DB에서 직접 조회합니다 (Redis 캐시 히트 로직 없음).**
    - DB `chat_rooms`: `ai_summary`, `preferences` 직접 조회 → 조회 후 Redis `memory:{roomId}` 동기화 (fire-and-forget)
    - DB `chat_messages`: 최근 20개 대화 이력 (시간 역순 조회 후 pydantic-ai ModelMessage 변환)
    - DB `chat_messages` (pgvector): 유사 과거 메시지 최대 5개 (코사인 유사도 기준)
    - DB `itineraries`: 현재 여행 일정 전체 (destinations·start_date·end_date·budget·adult_count·child_count·child_ages·day_plans 포함)
3. **Agent 처리 및 type 판별**: 대화 이력, memory, 사용자 메시지를 기반으로 Agent가 요청 의도를 분석하고 `type`을 판별합니다.
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
9. **Memory 갱신 판단**: OrchestratorResult에 `ai_summary` 또는 `preferences` 값이 있으면 Redis memory를 업데이트합니다.
    - `aiSummary`: **이전 ai_summary + 현재 대화를 합산한 전체 누적 재요약** (현재 턴만의 요약이 아님)
    - `preferences`: **이전 preferences를 유지하면서 신규 항목을 추가/수정한 전체 dict** (delta가 아닌 전체 값)
    - 변경 사항이 있으면 `done.memory`에 merged 값 포함, 없으면 `null` 전송
    - `memory`는 `type`과 무관합니다. `"chat"` 타입에서도 새 취향이 감지되면 포함될 수 있습니다.
10. **done 전송**: `type`, 메시지 임베딩, 갱신된 `memory`, type별 조건부 페이로드를 포함한 `done` 이벤트를 전송합니다.

> FastAPI는 DB에 직접 쓰지 않습니다. 모든 DB 저장은 Spring Boot가 `done` 이벤트 수신 후 처리합니다. FastAPI의 DB 접근은 벡터 유사도 검색 등 read-only에 한합니다.
> 


---

---

## **4.2 Spring Boot 처리 흐름**

1. **ChatRoom 조회**: 사용자 메시지 수신 시 `roomId`로 `chat_rooms`를 조회합니다.
    - `chat_rooms.ai_summary`
    - `chat_rooms.preferences`
2. **AI 서버 요청 생성**: Spring Boot는 FastAPI에 사용자 메시지를 전달합니다.
    - `roomId`
    - `content`
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
        - `destinations` 포함 시 배열 전체 교체, `start_date`/`end_date` 함께 갱신, `total_days`는 Spring Boot에서 `(end_date - start_date + 1)`로 재계산
        - 날짜 범위 변경 시 `day_plans` 키 조정
    - `"reservation"` → `itineraryRepository.findByRoomId(roomId)`로 `itineraryId` 조회 → `reservations` 저장
        - `bookedBy = "ai"`
        - `status = "confirmed"`
    - `"cancel"` → `reservations.status = "cancelled"` 갱신, `cancelled_at` 저장
    - `null` → 하위 호환. `log.warn` 후 `"chat"`과 동일 처리
    - unknown type → `log.error` 후 `502 Bad Gateway` 반환

---

## **4.3 Memory 흐름**

```
대화 시작 (roomId + content)
↓
DB chat_rooms에서 ai_summary / preferences 직접 조회 (매 요청마다)
↓
조회 결과로 Redis memory:{roomId} 동기화 (fire-and-forget)
↓
DB 값을 OrchestratorDeps에 주입 → LLM 처리
  - ai_summary: 이전 요약 전체를 LLM에게 제공
  - preferences: 이전 취향 전체를 LLM에게 제공
↓
LLM이 OrchestratorResult.ai_summary 생성 시
  → 이전 ai_summary + 현재 대화를 합산한 전체 누적 재요약 작성
LLM이 OrchestratorResult.preferences 생성 시
  → 이전 preferences 유지 + 신규 항목 추가/수정한 전체 dict 작성
↓
merged_summary / merged_prefs 계산
  (LLM 생성 값이 있으면 사용, 없으면 DB에서 읽은 기존 값 유지)
↓
변경 있음 → Redis memory 갱신 → done.memory 포함
변경 없음 → done.memory = null
↓
Spring Boot가 done.memory를 받아 DB chat_rooms 저장
  → 다음 요청 시 FastAPI가 이 값을 DB에서 직접 읽음
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
| 180초 내 스트림 미완료 | `504 Gateway Timeout` | `TimeoutException` |
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