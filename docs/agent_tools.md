# Agent Tools 명세

오케스트레이터 에이전트(`orchestrator_agent`, 기본 모델 `gemini-2.5-pro` — env `ORCHESTRATOR_MODEL`로 변경 가능)에
등록된 도구 함수와 각 도구의 입출력 규격을 정의합니다.
도구는 `app/services/agents/orchestrator.py`에서 `@orchestrator_agent.tool_plain`으로 등록됩니다.

> **아키텍처 요점**
> - 오케스트레이터가 직접 호출하는 도구는 **5개**(웹·날씨·경로·장소)뿐입니다.
> - 일정 생성(`itinerary` 타입)은 오케스트레이터가 아니라 **별도 파이프라인**(`run_itinerary_pipeline`)이
>   담당하며, 항공·숙소·장소·경로·날씨 API는 그 파이프라인 내부에서 호출됩니다.
>   → [항공·숙소 등 파이프라인 내부 API](#항공숙소-등--파이프라인-내부-api) 참고.
> - `dayPlans`·변경값·예약·취소 등 구조화 데이터는 별도 도구가 아니라
>   **에이전트의 구조화 출력(`OrchestratorResult`)** 으로 반환됩니다. (submit_* 도구는 존재하지 않음)

---

## 등록 구조 개요

```
orchestrator_agent (gemini-2.5-pro, output_type=OrchestratorResult)
  └─ @tool_plain 도구 5개 (외부 API)
       ├─ search_web             → TavilySearchAdapter  (tavily_search) + preprocessor_agent 요약
       ├─ get_weather            → WeatherAdapter       (weather)
       ├─ get_historical_weather → WeatherAdapter       (weather)
       ├─ find_route             → GoogleMapsAdapter    (google_maps)
       └─ search_place           → GoogleMapsAdapter    (google_maps)

  ※ 구조화 출력(OrchestratorResult)으로 반환 — 별도 도구 아님:
       message / ai_summary / preferences / day_plans / change / reservation / cancel
```

모든 어댑터는 `TravelAgentService`를 통해 호출됩니다 (`_service.process_task(tool_name, action, params)`).

---

## 동적 컨텍스트 주입 (OrchestratorDeps)

오케스트레이터는 `deps_type=OrchestratorDeps`로 선언되며, 매 요청마다 아래 값이 주입됩니다.
동적 컨텍스트는 `@system_prompt`가 아니라 **`build_context_prompt(deps)`** 로 조립되어
user 메시지 앞에 붙습니다(프로젝트 컨벤션).

```python
@dataclass
class OrchestratorDeps:
    ai_summary: str | None           # 대화 요약 (DB)
    preferences: dict | None         # 사용자 여행 취향 (DB)
    today: str                       # YYYY-MM-DD
    similar_messages: list[dict]     # pgvector 유사 과거 메시지 (최대 5개)
    current_itinerary: dict | None   # 현재 여행 일정 (destinations·day_plans 등, DB read-only)
    request_type: str                # classification 결과: chat|itinerary|change|reservation|cancel
    reservations: list[dict]         # 채팅방의 활성 예약 목록 (DB read-only)
```

---

## 도구 목록 (오케스트레이터 직접 호출 5개)

### 1. `search_web` — 웹 검색 (비정형 정보)

| 항목 | 내용 |
|------|------|
| 어댑터 | `TavilySearchAdapter` (Tavily Search API) |
| 액션 | `search` |
| 사용 시점 | 여행지 정보·현지 팁·뉴스·트렌드 등 비정형 정보 수집이 필요할 때 |

#### 입력 파라미터

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|----------|------|------|--------|------|
| `query` | `str` | Y | — | 검색어 |
| `search_depth` | `str` | N | `"basic"` | `"basic"`(크레딧 1) / `"advanced"`(크레딧 2) |
| `max_results` | `int` | N | `15` | 최대 결과 수 |

#### 전처리 흐름 (도구 내부)

```
Tavily 결과 (최대 15개)
  ↓  score ≥ 0.5 필터 → 상위 10개
  ↓  preprocessor_agent(gemini-2.5-flash)로 요약
오케스트레이터에게 정제된 요약 반환
```

#### 반환 형식

```json
{ "status": "success", "summary": "핵심 정보 요약 텍스트 ...", "source_count": 7 }
```

---

### 2. `get_weather` — 날씨 예보

| 항목 | 내용 |
|------|------|
| 어댑터 | `WeatherAdapter` (Open-Meteo API) |
| 액션 | `get_weather` |
| 사용 시점 | 여행 기간 날씨 예보 조회 (최대 16일 선행 예보) |

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|----------|------|------|--------|------|
| `city` | `str` | Y | — | 영문 도시명 |
| `forecast_days` | `int` | N | `7` | 예보 기간 (1~16) |

```json
{
  "status": "success",
  "city": "Tokyo",
  "data": {
    "2026-05-01": {"max_temp": 22.0, "min_temp": 15.0, "weather_code": 0}
  }
}
```

---

### 3. `get_historical_weather` — 과거 날씨 조회

| 항목 | 내용 |
|------|------|
| 어댑터 | `WeatherAdapter` (Open-Meteo Historical API) |
| 액션 | `get_historical_weather` |
| 사용 시점 | 여행일이 16일 초과일 때 작년 동기 날씨를 참고 자료로 조회 |

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `city` | `str` | Y | 영문 도시명 |
| `start_date` | `str` | Y | 조회 시작일 `YYYY-MM-DD` |
| `end_date` | `str` | Y | 조회 종료일 `YYYY-MM-DD` |

반환 형식은 `get_weather`와 동일합니다.

---

### 4. `find_route` — 경로 조회

| 항목 | 내용 |
|------|------|
| 어댑터 | `GoogleMapsAdapter` (Google Maps Directions API) |
| 액션 | `find_route` |
| 사용 시점 | 두 장소 간 이동 경로·소요 시간·대중교통 요금 조회 |

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|----------|------|------|--------|------|
| `origin` | `str` | Y | — | 출발지. 장소명 또는 주소 |
| `dest` | `str` | Y | — | 목적지. 장소명 또는 주소 |
| `mode` | `str` | N | `"transit"` | `transit` / `driving` / `walking` / `bicycling` |

```json
{
  "status": "success",
  "data": {
    "type": "구글맵 경로 데이터",
    "count": 1,
    "routes": [
      {
        "summary": "...",
        "start_address": "...",
        "end_address": "...",
        "distance_text": "32.1 km",
        "duration_text": "45분",
        "fare": {"currency": "KRW", "text": "₩2,300", "value": 2300.0},
        "steps": [...]
      }
    ]
  }
}
```
> `fare`는 대중교통 일부 노선에서만 제공되며, 없으면 `null`입니다.

---

### 5. `search_place` — 장소 검색

| 항목 | 내용 |
|------|------|
| 어댑터 | `GoogleMapsAdapter` (Google Maps Places Text Search) |
| 액션 | `search_place` |
| 사용 시점 | 식당·관광지·숙소 등 특정 장소 정보 조회 |

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `query` | `str` | Y | 검색어. 예) `"도쿄 스카이트리"`, `"신주쿠 맛집"` |

```json
{
  "status": "success",
  "data": {
    "type": "구글맵 장소 검색 데이터",
    "count": 3,
    "places": [
      {
        "name": "Tokyo Skytree",
        "formatted_address": "1 Chome-1-2 Oshiage, Sumida City, Tokyo",
        "place_id": "ChIJ...",
        "lat": 35.71, "lng": 139.81,
        "rating": 4.5,
        "user_ratings_total": 12345,
        "types": ["tourist_attraction", ...],
        "price_level": 2,
        "price_level_label": "보통"
      }
    ]
  }
}
```

---

## 구조화 출력 (OrchestratorResult)

`dayPlans`·변경값·예약·취소·메모리는 **별도 도구가 아니라 에이전트의 구조화 출력**으로 반환됩니다.
`orchestrator_agent`(및 파이프라인의 `synthesizer_agent`)는 `output_type=OrchestratorResult`로 선언되어,
LLM이 아래 필드를 직접 채웁니다.

| 필드 | 타입 | 채워지는 시점 |
|------|------|---------------|
| `message` | `str` | 항상 (자연어 응답) |
| `ai_summary` | `str \| list[str] \| None` | itinerary·change 후 |
| `preferences` | `dict \| None` | 취향 감지 시 |
| `day_plans` | `dict[str, list[DayPlanItem]] \| None` | itinerary 타입 |
| `change` | `ChangeFields \| None` | change 타입 |
| `reservation` | `ReservationFields \| None` | (현재 사용 안 함 — 항상 `null`) |
| `cancel` | `CancelFields \| None` | (현재 사용 안 함 — 항상 `null`) |

**DayPlanItem**: `plan_name`, `time("HH:MM ~ HH:MM")`, `place`, `note`, `cost`, `image_url`, `url`
(`image_url`·`url`은 파이프라인 후처리로 주입 — 관광공사 이미지/Booking 사진·예약링크/항공사 로고).
**ItemCost**: `amount`, `currency`(ISO 4217), `amount_krw`(비KRW일 때만).

> 액션 타입인데 해당 필드가 비어 있으면 컨트롤러(`_resolve_done_type`)가 `chat` 타입으로 강등합니다.

### 예약(reservation) · 취소(cancel) — 링크 안내 방식

이 서비스는 항공·숙소를 **직접 예약/취소하지 않습니다.** (검색 provider인 Booking은 조회·딥링크 전용)

- **reservation**: `reservation` 필드를 채우지 않고(`null`), 일정 항목에 주입된 **예약 딥링크(`url`)** 를
  안내합니다. → done 타입은 `chat`으로 강등되어 backend 예약 저장이 발생하지 않습니다.
- **cancel**: 컨트롤러가 선처리로 가로채 **"예약처에서 직접 취소" 안내**를 반환합니다(오케스트레이터 미호출).

`ReservationFields`/`CancelFields` 스키마는 방어적으로 남아 있으나 현재 흐름에서 채워지지 않습니다.

---

## 항공·숙소 등 — 파이프라인 내부 API

`itinerary` 타입 요청은 오케스트레이터 도구가 아니라 **`run_itinerary_pipeline`** 이 처리합니다.
이 파이프라인은 `planner_agent`(일정 골격) → 데이터 수집 → `synthesizer_agent`(최종 일정) 순서로 동작하며,
아래 API를 `TravelAgentService`로 직접 호출합니다. (오케스트레이터에 도구로 노출되지 않음)

| 용도 | 어댑터 (`tool_name`) | 주요 액션 |
|------|----------------------|-----------|
| 항공권 검색 | `BookingAdapter` (`booking`) | `search_flight_location`, `search_flights`, `get_flight_details` |
| 숙소 검색·상세 | `BookingAdapter` (`booking`) | `search_destination`, `search_hotels`, `get_hotel_details` |
| 장소·경로 | `GoogleMapsAdapter` (`google_maps`) | `search_place`, `find_route` |
| 국내 이미지 | `KoreaTourismAdapter` (`korea_tourism`) | `search_keyword` |
| 웹 요약 | `TavilySearchAdapter` (`tavily_search`) | `search` |
| 날씨 | `WeatherAdapter` (`weather`) | `get_historical_weather` |

> `FlightAdapter`(`duffel_flight`)·`AccommodationAdapter`(`duffel_accommodation`)는 **참고용(legacy)** 으로만
> 남아 있고 파이프라인에 등록되지 않습니다. 항공·숙소는 전부 `BookingAdapter`가 담당합니다.

일정 수정 요청(부분 변경)은 `app/services/agents/itinerary_patch.py`가 담당하며 동일하게 `BookingAdapter`·`GoogleMapsAdapter`를 사용합니다.

---

## 에러 반환 형식

모든 어댑터/도구는 예외를 raise하지 않고 실패 시 아래 형식으로 반환합니다.

```json
{ "status": "error", "message": "오류 설명 문자열" }
```

오케스트레이터는 `status: "error"` 결과를 받으면 사용자에게 자연어로 대신 설명합니다.

---

## 어댑터 파일 위치

| 어댑터 | 파일 |
|--------|------|
| `BookingAdapter` (항공·숙소, 현역) | `app/services/adapters/booking_api.py` |
| `TavilySearchAdapter` | `app/services/adapters/tavily_search.py` |
| `WeatherAdapter` | `app/services/adapters/weather_api.py` |
| `GoogleMapsAdapter` | `app/services/adapters/google_maps.py` |
| `KoreaTourismAdapter` | `app/services/adapters/korea_tourism_api.py` |
| `FlightAdapter` / `AccommodationAdapter` (Duffel, 참고용) | `app/services/adapters/flight_api.py` / `accommodation_api.py` |
