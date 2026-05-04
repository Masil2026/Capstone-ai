# Agent Tools 명세

오케스트레이터(GPT-4.1)에 등록된 도구 함수 목록과 각 도구의 입력/출력 규격을 정의합니다.
모든 도구는 `app/services/agent.py`에서 `@orchestrator_agent.tool_plain`으로 등록됩니다.

---

## 등록 구조 개요

```
orchestrator_agent (GPT-4.1)
  └─ @tool_plain 도구 7개
       ├─ search_flights     → FlightAdapter       (duffel_flight)
       ├─ search_hotels      → AccommodationAdapter (duffel_accommodation)
       ├─ search_web         → TavilySearchAdapter  (tavily_search) + preprocessor_agent
       ├─ get_weather        → WeatherAdapter       (weather)
       ├─ get_historical_weather → WeatherAdapter   (weather)
       ├─ find_route         → GoogleMapsAdapter    (google_maps)
       └─ search_place       → GoogleMapsAdapter    (google_maps)
```

모든 어댑터는 `TravelAgentService`를 통해 호출됩니다 (`_service.process_task(tool_name, action, params)`).

---

## OrchestratorDeps — 동적 시스템 프롬프트

오케스트레이터는 `deps_type=OrchestratorDeps`로 선언되며,
매 요청마다 Redis에서 로드한 `ai_summary`, `preferences`, 오늘 날짜를 시스템 프롬프트에 주입합니다.

```python
@dataclass
class OrchestratorDeps:
    ai_summary: str | None       # Redis memory.ai_summary
    preferences: dict | None     # Redis memory.preferences
    today: str                   # YYYY-MM-DD
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
