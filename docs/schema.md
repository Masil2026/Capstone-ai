# 여행 비서 AI Agent — DB 테이블 명세서

> **기술 스택**: PostgreSQL 16 + pgvector  
> **인증**: Clerk (외부) → users 테이블에 미러링  
> **총 테이블 수**: 6개

---

## 목차
1. [users](#1-users)
2. [chat_rooms](#2-chat_rooms)
3. [chat_messages](#3-chat_messages)
4. [itineraries](#4-itineraries)
5. [itinerary_logs](#5-itinerary_logs)
6. [reservations](#6-reservations)
7. [관계 요약](#관계-요약)
8. [설계 주요 결정 사항](#설계-주요-결정-사항)

---

## 1. users
Clerk에서 관리하는 사용자 정보를 로컬 DB에 미러링하는 테이블입니다.

| 컬럼명 | 타입 | 제약 | 설명 |
|---|---|---|---|
| `clerk_id` | VARCHAR(255) | PK | Clerk 발급 고유 ID (`user_2N...` 형태) |
| `created_at` | TIMESTAMP | NOT NULL, DEFAULT now() | 로컬 DB 최초 등록 일시 |

---

## 2. chat_rooms
사용자 1명이 여러 여행 계획을 가질 수 있으므로, 1개의 채팅방 = 1개의 여행 계획 컨텍스트로 설계합니다.

| 컬럼명 | 타입 | 제약 | 설명 |
|---|---|---|---|
| `id` | UUID | PK | 채팅방 고유 ID |
| `clerk_id` | VARCHAR(255) | NOT NULL, FK → users ON DELETE CASCADE | 소유자 |
| `name` | VARCHAR(100) | NOT NULL | 채팅방 이름 |
| `ai_summary` | TEXT | | AI가 생성한 대화 요약 |
| `preferences` | JSONB | | 사용자 여행 선호도 `{"budget":"economy","style":"adventure"}` |
| `created_at` | TIMESTAMP | NOT NULL, DEFAULT now() | 생성 일시 |
| `updated_at` | TIMESTAMP | NOT NULL, DEFAULT now() | 마지막 수정 일시 |

**인덱스**: `clerk_id`

> ⚠️ 마이그레이션 파일 생성 시 `clerk_id` FK에 `ON DELETE CASCADE` 추가 필요

---

## 3. chat_messages
채팅방 내 모든 대화 메시지를 저장합니다. `embedding`은 과거 대화 문맥을 벡터 유사도 검색할 때 사용합니다.

| 컬럼명 | 타입 | 제약 | 설명 |
|---|---|---|---|
| `id` | UUID | PK | 메시지 고유 ID |
| `room_id` | UUID | NOT NULL, FK → chat_rooms ON DELETE CASCADE | 소속 채팅방 |
| `role` | VARCHAR(20) | NOT NULL, CHECK IN ('user','assistant','tool') | 발화 주체 |
| `content` | TEXT | NOT NULL | 메시지 본문 |
| `embedding` | vector(1536) | | 메시지 임베딩 벡터 (pgvector) |
| `created_at` | TIMESTAMP | NOT NULL, DEFAULT now() | 전송 일시 |

**인덱스**: `room_id`, `embedding` (IVFFlat, cosine)

> ⚠️ 마이그레이션 파일 생성 시 `room_id` FK에 `ON DELETE CASCADE` 추가 필요

---

## 4. itineraries
AI Agent가 생성·수정한 여행 일정입니다. `day_plans`는 실제 날짜를 Key로 사용하는 JSONB 구조입니다.

| 컬럼명 | 타입 | 제약 | 설명 |
|---|---|---|---|
| `id` | UUID | PK | 일정 고유 ID |
| `room_id` | UUID | NOT NULL, UNIQUE, FK → chat_rooms ON DELETE CASCADE | 연결된 채팅방 |
| `destination` | VARCHAR(255) | NOT NULL | 목적지 |
| `budget` | DECIMAL(12,2) | | 예산 |
| `adult_count` | INT | NOT NULL, DEFAULT 1, CHECK >= 1 | 어른 수 |
| `child_count` | INT | NOT NULL, DEFAULT 0, CHECK >= 0 | 아이 수 |
| `child_ages` | JSONB | DEFAULT '[]' | 아이 나이 |
| `total_days` | INT | NOT NULL, CHECK >= 1 | 총 여행 일수 |
| `start_date` | DATE | NOT NULL | 여행 시작일 |
| `end_date` | DATE | NOT NULL | 여행 종료일 |
| `status` | VARCHAR(20) | NOT NULL, DEFAULT 'draft' | 일정 상태 (`draft` / `completed`) |
| `day_plans` | JSONB | NOT NULL, DEFAULT '{}' | 날짜별 일정 상세 |
| `created_at` | TIMESTAMP | NOT NULL, DEFAULT now() | 생성 일시 |
| `updated_at` | TIMESTAMP | NOT NULL, DEFAULT now() | 마지막 수정 일시 |

**day_plans 아이템 필드**

| 필드 | 타입 | 출처 | 설명 |
|------|------|------|------|
| `plan_name` | VARCHAR | AI 서버 | 일정 이름 |
| `time` | VARCHAR | AI 서버 | `"HH:MM ~ HH:MM"` 형식 |
| `place` | VARCHAR | AI 서버 | 장소명 또는 이동 수단·노선명 |
| `note` | VARCHAR | AI 서버 | 메모. 없으면 빈 문자열 |
| `cost` | JSONB \| null | AI 서버 | 1인 기준 예상 비용. 무료이면 `null` |
| `status` | VARCHAR | **백엔드 자동 생성** | `"todo"` / `"done"`. AI 서버가 보내지 않음. Spring Boot가 신규 항목에 `"todo"` 부여, 기존 항목의 `status` 유지 |

**cost 객체 구조**

| 필드 | 타입 | 설명 |
|------|------|------|
| `amount` | Number | 1인 기준 현지 통화 금액 (소수점 허용) |
| `currency` | VARCHAR | ISO 4217 통화 코드. 예) `"KRW"`, `"JPY"`, `"USD"` |
| `amount_krw` | INT \| null | 한화 환산 금액. `currency == "KRW"`이면 `null` |

**day_plans 구조 예시** *(DB에 저장된 최종 형태 — status는 백엔드가 추가)*
```json
{
  "2026-05-01": [
    {
      "plan_name": "인천공항 → 나리타공항 이동 (대한항공 KE705)",
      "time": "10:00 ~ 12:30",
      "place": "Narita International Airport",
      "note": "직항 2시간 30분",
      "cost": {"amount": 450000, "currency": "KRW", "amount_krw": null},
      "status": "done"
    },
    {
      "plan_name": "센소지 참배 및 나카미세 거리 쇼핑",
      "time": "15:00 ~ 17:30",
      "place": "Senso-ji Temple, Asakusa",
      "note": "맑음 22°C, 선크림 필수",
      "cost": null,
      "status": "todo"
    },
    {
      "plan_name": "저녁식사 — 신주쿠 라멘",
      "time": "18:30 ~ 19:30",
      "place": "Fuunji Ramen, Shinjuku",
      "note": "",
      "cost": {"amount": 1200, "currency": "JPY", "amount_krw": 11160},
      "status": "todo"
    }
  ],
  "2026-05-02": [
    {
      "plan_name": "숙소 체크아웃",
      "time": "10:00 ~ 11:00",
      "place": "Shinjuku Grand Hotel",
      "note": "",
      "cost": null,
      "status": "todo"
    }
  ]
}
```

> `status`는 AI 서버가 반환하는 `done` 이벤트의 `itinerary.dayPlans`에 포함되지 않습니다.  
> Spring Boot가 수신 후 DB 저장 시 `time` 기준으로 기존 항목이면 기존 `status` 유지, 신규 항목이면 `"todo"`를 부여합니다.

**인덱스**: `day_plans` (GIN)

> ⚠️ 마이그레이션 파일 생성 시 `room_id` FK에 `ON DELETE CASCADE` 추가 필요

---

## 5. itinerary_logs
`itineraries`의 기본 정보 수정 또는 `day_plans` 수정 시(향후 일정 직접 편집 기능 포함) 변경 전 주요 필드의 스냅샷을 쌓는 이력 테이블입니다. `status` 변경 및 아이템 완료 상태 변경(`todo`/`done`)은 스냅샷 대상에서 제외됩니다.

| 컬럼명 | 타입 | 제약 | 설명 |
|---|---|---|---|
| `id` | UUID | PK | 로그 고유 ID |
| `itinerary_id` | UUID | NOT NULL, FK → itineraries ON DELETE CASCADE | 대상 일정 |
| `destination` | VARCHAR(255) | | 변경 전 목적지 스냅샷 |
| `budget` | DECIMAL(12,2) | | 변경 전 예산 스냅샷 |
| `adult_count` | INT | | 변경 전 어른 수 스냅샷 |
| `child_count` | INT | | 변경 전 아이 수 스냅샷 |
| `child_ages` | JSONB | | 변경 전 아이 나이 스냅샷 |
| `total_days` | INT | | 변경 전 총 여행 일수 스냅샷 |
| `start_date` | DATE | | 변경 전 여행 시작일 스냅샷 |
| `end_date` | DATE | | 변경 전 여행 종료일 스냅샷 |
| `day_plans` | JSONB | NOT NULL | 변경 전 day_plans 스냅샷 |
| `created_at` | TIMESTAMP | NOT NULL, DEFAULT now() | 변경 발생 일시 |

**인덱스**: `itinerary_id`

> ⚠️ 마이그레이션 파일 생성 시 `itinerary_id` FK에 `ON DELETE CASCADE` 추가 필요

---

## 6. reservations
예약 완료된 정보를 저장합니다. 항공·숙소·렌트카를 단일 테이블로 관리하며, 유형별 상세는 `detail` JSONB로 처리합니다.

| 컬럼명 | 타입 | 제약 | 설명 |
|---|---|---|---|
| `id` | UUID | PK | 예약 고유 ID |
| `itinerary_id` | UUID | NOT NULL, FK → itineraries ON DELETE RESTRICT | 연결된 일정 |
| `type` | VARCHAR(20) | NOT NULL, CHECK IN ('flight','accommodation','car_rental') | 예약 유형 |
| `status` | VARCHAR(20) | NOT NULL, DEFAULT 'confirmed' | 예약 상태 (`confirmed` / `changed` / `cancelled`) |
| `booked_by` | VARCHAR(10) | NOT NULL, DEFAULT 'user', CHECK IN ('user','ai') | 예약 주체 (`user`: 사용자 직접 / `ai`: AI Agent) |
| `booking_url` | TEXT | | AI가 사용자에게 제공한 예약 링크 |
| `external_ref_id` | VARCHAR(255) | | 외부 예약 번호 |
| `detail` | JSONB | NOT NULL, DEFAULT '{}' | 유형별 상세 정보 |
| `total_price` | DECIMAL(12,2) | | 총 결제 금액 |
| `currency` | VARCHAR(3) | DEFAULT 'KRW' | 통화 코드 |
| `reserved_at` | TIMESTAMP | | 예약 완료 일시 |
| `cancelled_at` | TIMESTAMP | | 취소 일시 |
| `created_at` | TIMESTAMP | NOT NULL, DEFAULT now() | 레코드 생성 일시 |
| `updated_at` | TIMESTAMP | NOT NULL, DEFAULT now() | 마지막 수정 일시 |

**detail 구조 예시**
```json
// 항공권
{
  "airline": "대한항공",
  "flight_no": "KE123",
  "departure": {"airport": "ICN", "datetime": "2026-05-01T09:00:00"},
  "arrival":   {"airport": "NRT", "datetime": "2026-05-01T11:30:00"},
  "seat_class": "economy",
  "passengers": [{"name": "홍길동", "passport": "M12345678"}]
}
// 숙소
{
  "hotel_name": "롯데호텔 도쿄",
  "room_type": "디럭스 더블",
  "check_in": "2026-05-01",
  "check_out": "2026-05-03",
  "guests": 2
}
```

**인덱스**: `itinerary_id`, `status`, `detail` (GIN)

> ⚠️ 마이그레이션 파일 생성 시 `itinerary_id` FK에 `ON DELETE RESTRICT` 추가 필요

---

## 관계 요약
```
users ──< chat_rooms ──< chat_messages
                    ──< itineraries ──< itinerary_logs
                                   ──< reservations
```

| 관계 | 카디널리티 | 설명 |
|---|---|---|
| users → chat_rooms | 1:N | 사용자는 여러 채팅방(여행 계획)을 가질 수 있음 |
| chat_rooms → chat_messages | 1:N | 채팅방에 여러 메시지 포함 |
| chat_rooms → itineraries | 1:1 | 채팅방에서 일정이 생성됨 |
| itineraries → itinerary_logs | 1:N | 일정 수정 시 변경 전 스냅샷 누적 |
| itineraries → reservations | 1:N | 하나의 일정에 여러 예약 포함 |

---

## 설계 주요 결정 사항

### 통합 예약 테이블
항공·숙소·렌트카를 각각 별도 테이블로 분리하지 않고, `type` 컬럼과 `detail` JSONB로 통합 관리합니다.

### reservations.clerk_id 제거
`itinerary_id → itineraries → room_id → chat_rooms → clerk_id` 조인으로 사용자를 특정할 수 있으므로 중복 저장을 제거했습니다.

### itinerary_logs 추가
`itineraries`의 기본 정보 수정 또는 `day_plans` 수정 시 변경 전 주요 9개 필드(`destination`, `budget`, `adult_count`, `child_count`, `child_ages`, `total_days`, `start_date`, `end_date`, `day_plans`)의 스냅샷을 쌓아 일정 히스토리를 관리합니다. `status` 변경 및 아이템 완료 상태 변경(`todo`/`done`)은 스냅샷 대상에서 제외됩니다.

### reservations.status 단순화
예약 완료된 데이터만 저장하는 테이블이므로 `confirmed`, `changed`, `cancelled`만 유지합니다.