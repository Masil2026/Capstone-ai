# Agent Tools 명세

오케스트레이터(GPT-4.1)에 등록된 도구 함수 목록과 각 도구의 입력/출력 규격을 정의합니다.
모든 도구는 `app/services/agent.py`에서 `@orchestrator_agent.tool_plain`으로 등록됩니다.

---

## 등록 구조 개요

```
orchestrator_agent (GPT-4.1)
  └─ @tool_plain 도구 12개
       ├─ [외부 API]
       │   ├─ search_flights         → FlightAdapter       (duffel_flight)
       │   ├─ search_hotels          → AccommodationAdapter (duffel_accommodation)
       │   ├─ search_web             → TavilySearchAdapter  (tavily_search) + preprocessor_agent
       │   ├─ get_weather            → WeatherAdapter       (weather)
       │   ├─ get_historical_weather → WeatherAdapter       (weather)
       │   ├─ find_route             → GoogleMapsAdapter    (google_maps)
       │   └─ search_place           → GoogleMapsAdapter    (google_maps)
       └─ [구조화 데이터 전달 — 외부 API 없음]
           ├─ submit_itinerary   → itinerary 타입 dayPlans 전달
           ├─ submit_change      → change 타입 변경 값 전달
           ├─ submit_reservation → reservation 타입 예약 정보 전달
           ├─ submit_cancel      → cancel 타입 취소 정보 전달
           └─ update_memory      → 모든 타입 공통, 메모리 갱신 값 전달
```

모든 어댑터는 `TravelAgentService`를 통해 호출됩니다 (`_service.process_task(tool_name, action, params)`).

---

## OrchestratorDeps — 동적 시스템 프롬프트

오케스트레이터는 `deps_type=OrchestratorDeps`로 선언되며,
매 요청마다 Redis에서 로드한 `ai_summary`, `preferences`, 오늘 날짜를 시스템 프롬프트에 주입합니다.

```python
@dataclass
class OrchestratorDeps:
    ai_summary: str | None           # Redis memory.ai_summary
    preferences: dict | None         # Redis memory.preferences
    today: str                       # YYYY-MM-DD
    similar_messages: list[dict]     # pgvector 유사 과거 메시지 최대 5개
                                     # [{"role": "user"|"assistant", "content": "..."}]
    current_itinerary: dict | None   # 현재 여행 일정 dayPlans (DB read-only, roomId 기준)
                                     # {"YYYY-MM-DD": [{"plan_name", "time", "place", "note"}]}
                                     # 일정 없으면 None
    request_type: str                # classification_agent 판별 결과
                                     # "chat"|"itinerary"|"change"|"reservation"|"cancel"
```

`@orchestrator_agent.system_prompt`로 등록된 함수가 위 값을 읽어 프롬프트를 동적으로 구성합니다.
어댑터 코드나 도구 함수는 deps를 직접 참조하지 않습니다.

---

## 도구 목록

---

### 1. `search_flights` — 항공권 검색

| 항목 | 내용 |
|------|------|
| 어댑터 | `FlightAdapter` (Duffel Air API) |
| 액션 | `search_flights` |
| 사용 시점 | 사용자가 항공권 검색 또는 예약 후보 조회를 요청할 때 |

#### 입력 파라미터

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|----------|------|------|--------|------|
| `origin` | `str` | Y | — | 출발지. 도시명 또는 IATA 코드 모두 허용 |
| `destination` | `str` | Y | — | 도착지. 도시명 또는 IATA 코드 모두 허용 |
| `departure_date` | `str` | Y | — | 출발일. `YYYY-MM-DD` 형식 |
| `adults` | `int` | N | `1` | 성인 탑승객 수 |
| `children` | `int` | N | `0` | 소아 탑승객 수 |
| `child_ages` | `list[int]` | N | `[]` | 소아 각각의 나이 배열. `children > 0`이면 길이가 `children`과 일치해야 함 |

#### 반환 형식 (성공)

```json
{
  "status": "success",
  "count": 5,
  "data": [
    {
      "offer_id": "off_...",
      "price": "350.00",
      "currency": "USD",
      "departure": "ICN",
      "arrival": "NRT",
      "departure_time": "2026-05-01T09:00:00",
      "arrival_time": "2026-05-01T11:30:00",
      "airline": "Korean Air",
      "flight_no": "KE705",
      "duration": "2h30m"
    }
  ]
}
```

---

### 2. `search_hotels` — 숙소 검색

| 항목 | 내용 |
|------|------|
| 어댑터 | `AccommodationAdapter` (Duffel Stays API) |
| 액션 | `search_hotels` |
| 사용 시점 | 사용자가 숙소 검색 또는 예약 후보 조회를 요청할 때 |

#### 입력 파라미터

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|----------|------|------|--------|------|
| `city_name` | `str` | Y | — | 숙소 검색 도시명 |
| `check_in` | `str` | Y | — | 체크인 날짜. `YYYY-MM-DD` 형식 |
| `check_out` | `str` | Y | — | 체크아웃 날짜. `YYYY-MM-DD` 형식 |
| `adults` | `int` | N | `1` | 성인 투숙객 수 |
| `rooms` | `int` | N | `1` | 객실 수 |
| `children` | `int` | N | `0` | 소아 투숙객 수 |
| `child_ages` | `list[int]` | N | `[]` | 소아 각각의 나이 배열 |

#### 반환 형식 (성공)

```json
{
  "status": "success",
  "count": 3,
  "data": [
    {
      "rate_id": "rat_...",
      "hotel_name": "Shinjuku Grand Hotel",
      "total_price": "450.00",
      "currency": "USD",
      "check_in": "2026-05-01",
      "check_out": "2026-05-03",
      "room_type": "Deluxe Twin"
    }
  ]
}
```

---

### 3. `search_web` — 웹 검색 (비정형 정보)

| 항목 | 내용 |
|------|------|
| 어댑터 | `TavilySearchAdapter` (Tavily Search API) |
| 액션 | `search` |
| 사용 시점 | 여행지 정보, 뉴스, 트렌드, 관광지 추천 등 비정형 정보 수집이 필요할 때 |

#### 입력 파라미터

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|----------|------|------|--------|------|
| `query` | `str` | Y | — | 검색어 |
| `search_depth` | `str` | N | `"basic"` | `"basic"` / `"advanced"` |
| `max_results` | `int` | N | `15` | 최대 결과 수 |

#### 전처리 흐름 (도구 내부)

Tavily 원본 결과는 `search_web` 도구 함수 내에서 전처리됩니다.

```
Tavily 결과 (최대 15개)
  ↓
score ≥ 0.5 필터링 → 상위 10개
  ↓
preprocessor_agent (GPT-4o-mini) 요약
  ↓
오케스트레이터에게 정제된 요약 반환
```

Elasticsearch는 사용하지 않습니다. Tavily `score` 기준으로만 필터링합니다.

#### 반환 형식 (오케스트레이터 수신 기준)

```json
{
  "status": "success",
  "summary": "도쿄 5월은 봄 날씨로 따뜻하며 아사쿠사, 우에노 공원이 인기 관광지입니다. ...",
  "source_count": 7
}
```

---

### 4. `get_weather` — 날씨 예보

| 항목 | 내용 |
|------|------|
| 어댑터 | `WeatherAdapter` (Open-Meteo API) |
| 액션 | `get_weather` |
| 사용 시점 | 여행 기간 날씨 예보 조회 (최대 16일 선행 예보) |

#### 입력 파라미터

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|----------|------|------|--------|------|
| `city` | `str` | Y | — | 영문 도시명 |
| `forecast_days` | `int` | N | `7` | 예보 기간. 1~16 |

#### 반환 형식 (성공)

```json
{
  "status": "success",
  "city": "Tokyo",
  "data": {
    "2026-05-01": {"max_temp": 22.0, "min_temp": 15.0, "weather_code": 0},
    "2026-05-02": {"max_temp": 20.5, "min_temp": 14.0, "weather_code": 61}
  }
}
```

---

### 5. `get_historical_weather` — 과거 날씨 조회

| 항목 | 내용 |
|------|------|
| 어댑터 | `WeatherAdapter` (Open-Meteo Historical API) |
| 액션 | `get_historical_weather` |
| 사용 시점 | 여행일이 16일 초과일 때 작년 동기 날씨를 참고 자료로 조회 |

#### 입력 파라미터

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|----------|------|------|--------|------|
| `city` | `str` | Y | — | 영문 도시명 |
| `start_date` | `str` | Y | — | 조회 시작일. `YYYY-MM-DD` |
| `end_date` | `str` | Y | — | 조회 종료일. `YYYY-MM-DD` |

반환 형식은 `get_weather`와 동일합니다.

---

### 6. `find_route` — 경로 조회

| 항목 | 내용 |
|------|------|
| 어댑터 | `GoogleMapsAdapter` (Google Maps Directions API) |
| 액션 | `find_route` |
| 사용 시점 | 두 장소 간 이동 경로, 소요 시간 조회 |

#### 입력 파라미터

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|----------|------|------|--------|------|
| `origin` | `str` | Y | — | 출발지. 장소명 또는 주소 |
| `dest` | `str` | Y | — | 목적지. 장소명 또는 주소 |
| `mode` | `str` | N | `"transit"` | `transit` / `driving` / `walking` / `bicycling` |

#### 반환 형식 (성공)

```json
{
  "status": "success",
  "data": {
    "type": "구글맵 경로 데이터",
    "count": 1,
    "routes": [
      {
        "summary": "Express Bus Route",
        "duration": "45분",
        "distance": "32.1 km",
        "steps": [...]
      }
    ]
  }
}
```

---

### 7. `search_place` — 장소 검색

| 항목 | 내용 |
|------|------|
| 어댑터 | `GoogleMapsAdapter` (Google Maps Places API) |
| 액션 | `search_place` |
| 사용 시점 | 식당, 관광지, 숙소 등 특정 장소 정보 조회 |

#### 입력 파라미터

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|----------|------|------|--------|------|
| `query` | `str` | Y | — | 검색어. 예: `"도쿄 스카이트리"`, `"신주쿠 맛집"` |

#### 반환 형식 (성공)

```json
{
  "status": "success",
  "data": {
    "type": "구글맵 장소 검색 데이터",
    "count": 3,
    "places": [
      {
        "name": "Tokyo Skytree",
        "address": "1 Chome-1-2 Oshiage, Sumida City, Tokyo",
        "rating": 4.5,
        "place_id": "ChIJ..."
      }
    ]
  }
}
```

---

---

## 구조화 데이터 전달 도구 (외부 API 없음)

아래 도구들은 외부 API를 호출하지 않습니다. orchestrator가 작업을 완료한 후 구조화된 결과를 엔드포인트에 전달하기 위해 호출합니다. 반환값은 orchestrator 확인용이며, 실제 데이터는 엔드포인트 컨텍스트에 캡처됩니다.

---

### 8. `submit_itinerary` — 일정 구조체 전달

| 항목 | 내용 |
|------|------|
| 어댑터 | 없음 (외부 API 호출 없음) |
| 사용 시점 | 일정 생성 또는 수정 완료 시 반드시 호출. 텍스트 스트리밍과 병행 가능 |

orchestrator가 일정을 생성하거나 수정할 때 이 도구를 호출하여 구조화된 `dayPlans`를 엔드포인트에 전달합니다.
classification_agent 대신 orchestrator가 직접 구조체를 생성하므로 정확도가 높습니다.

#### 입력 파라미터

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `day_plans` | `dict[str, list[DayPlanItem]]` | Y | 날짜별 일정 목록. 신규 생성 시 전체 날짜, 수정 시 변경된 날짜만 포함 |

#### DayPlanItem 구조

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `plan_name` | `str` | Y | 일정 이름 |
| `time` | `str` | Y | `"HH:MM ~ HH:MM"` 형식 |
| `place` | `str` | Y | 장소명 |
| `note` | `str` | N | 메모. 생략 시 `""` |

#### 입력 예시

```json
{
  "day_plans": {
    "2026-05-01": [
      { "plan_name": "창덕궁 방문", "time": "09:00 ~ 12:00", "place": "창덕궁", "note": "후원 투어 예약 필요" },
      { "plan_name": "광장시장 점심", "time": "12:00 ~ 14:30", "place": "광장시장", "note": "" }
    ]
  }
}
```

#### 반환 형식

```json
{ "status": "success", "message": "일정이 저장되었습니다." }
```

> 이 도구의 반환값은 orchestrator가 확인용으로만 사용합니다.
> 실제 `dayPlans`는 엔드포인트 컨텍스트에 캡처되어 `done` 이벤트의 `itinerary.dayPlans`로 전송됩니다.

---

### 9. `submit_change` — 여행 기본 정보 변경값 전달

| 항목 | 내용 |
|------|------|
| 사용 시점 | `change` 타입. 외부 API 없이 사용자 메시지에서 변경값을 추출해 호출 |

#### 입력 파라미터 (변경된 필드만 포함, 나머지는 생략)

| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `start_date` | `str \| None` | 변경할 여행 시작일 (`YYYY-MM-DD`) |
| `end_date` | `str \| None` | 변경할 여행 종료일 (`YYYY-MM-DD`) |
| `budget` | `float \| None` | 변경할 예산 |
| `adult_count` | `int \| None` | 변경할 성인 수 |
| `child_count` | `int \| None` | 변경할 아이 수 |
| `child_ages` | `list[int] \| None` | 변경할 아이 나이 배열 |

---

### 10. `submit_reservation` — 예약 정보 전달

| 항목 | 내용 |
|------|------|
| 사용 시점 | `reservation` 타입. 예약 API 호출 완료 후 예약 정보를 전달 |

#### 입력 파라미터

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `reservation_type` | `str` | Y | `"flight"` 또는 `"accommodation"` |
| `booking_url` | `str \| None` | N | 예약 링크 |
| `external_ref_id` | `str \| None` | N | 외부 예약 번호 |
| `detail` | `dict` | Y | 예약 상세 정보 |
| `total_price` | `float \| None` | N | 총 결제 금액 |
| `currency` | `str \| None` | N | 통화 코드 (미전송 시 `"KRW"`) |
| `reserved_at` | `str \| None` | N | 예약 완료 일시 (ISO-8601 + offset) |

---

### 11. `submit_cancel` — 취소 정보 전달

| 항목 | 내용 |
|------|------|
| 사용 시점 | `cancel` 타입. 취소 API 호출 완료 후 취소 정보를 전달 |

#### 입력 파라미터

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `reservation_id` | `str` | Y | 취소된 예약 고유 ID |
| `cancelled_at` | `str` | Y | 취소 일시 (ISO-8601 + offset) |

---

### 12. `update_memory` — 메모리 갱신값 전달

| 항목 | 내용 |
|------|------|
| 사용 시점 | 모든 타입 공통. 대화 중 사용자 취향·기억할 정보가 감지될 때 호출 |

#### 입력 파라미터

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `ai_summary` | `str \| None` | N | 이번 대화를 반영한 새 요약. 변경 없으면 생략 |
| `preferences` | `dict \| None` | N | 감지된 사용자 취향 전체 (기존 + 신규 병합). 변경 없으면 생략 |

---

## 에러 반환 형식

모든 도구는 예외를 raise하지 않습니다. 실패 시 항상 아래 형식으로 반환합니다.

```json
{ "status": "error", "message": "오류 설명 문자열" }
```

오케스트레이터는 `status: "error"` 결과를 받으면 사용자에게 자연어로 대신 설명합니다.

---

## 어댑터 파일 위치

| 도구 | 어댑터 파일 |
|------|-------------|
| `search_flights` | `app/services/adapters/flight_api.py` |
| `search_hotels` | `app/services/adapters/accommodation_api.py` |
| `search_web` | `app/services/adapters/tavily_search.py` |
| `get_weather`, `get_historical_weather` | `app/services/adapters/weather_api.py` |
| `find_route`, `search_place` | `app/services/adapters/google_maps.py` |
