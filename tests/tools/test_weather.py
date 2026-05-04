"""
Open-Meteo API 반환값 확인 테스트
- API 키 불필요
- geocoding → 날씨 2단계 호출 구조 검증
- 어댑터 연결 테스트 포함
"""
import httpx
import json
import pytest
from app.services.adapters.weather_api import WeatherAdapter


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
HISTORICAL_URL = "https://archive-api.open-meteo.com/v1/archive"

# WMO weathercode → 텍스트 변환표
WEATHERCODE_MAP = {
    0: "맑음",
    1: "대체로 맑음", 2: "부분적으로 흐림", 3: "흐림",
    45: "안개", 48: "안개 (착빙)",
    51: "이슬비 (약)", 53: "이슬비 (보통)", 55: "이슬비 (강)",
    61: "비 (약)", 63: "비 (보통)", 65: "비 (강)",
    71: "눈 (약)", 73: "눈 (보통)", 75: "눈 (강)",
    77: "싸락눈",
    80: "소나기 (약)", 81: "소나기 (보통)", 82: "소나기 (강)",
    85: "눈 소나기 (약)", 86: "눈 소나기 (강)",
    95: "뇌우", 96: "뇌우+우박 (약)", 99: "뇌우+우박 (강)",
}

def decode_weathercode(code: int) -> str:
    return WEATHERCODE_MAP.get(code, f"알 수 없음 ({code})")


def get_coordinates(city: str) -> tuple[float, float]:
    """도시명 → 위경도 변환

    주의: 한국어 도시명 미지원 — GPT-4o가 영어로 변환하여 전달해야 함
    예) "서울" X → "Seoul" O, "오사카" X → "Osaka" O
    """
    response = httpx.get(GEOCODING_URL, params={"name": city, "count": 1})
    response.raise_for_status()
    data = response.json()
    assert "results" in data and len(data["results"]) > 0, f"도시를 찾을 수 없음: {city}"
    result = data["results"][0]
    return result["latitude"], result["longitude"]


# ───────────────────────────────────────────
# 1단계: Geocoding API
# ───────────────────────────────────────────

def test_geocoding_seoul():
    """서울 geocoding 반환값 구조 확인"""
    response = httpx.get(GEOCODING_URL, params={"name": "Seoul", "count": 1})
    assert response.status_code == 200
    data = response.json()

    print("\n[Geocoding 반환값]")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    assert "results" in data
    result = data["results"][0]
    assert "latitude" in result
    assert "longitude" in result
    assert "name" in result

    print(f"\n도시명: {result['name']}, 위도: {result['latitude']}, 경도: {result['longitude']}")


# ───────────────────────────────────────────
# 2단계: 날씨 API - Hourly (케이스 1: 4일 이내)
# ───────────────────────────────────────────

def test_weather_hourly_seoul():
    """서울 hourly 날씨 반환값 구조 확인 (4일 이내 케이스)"""
    lat, lon = get_coordinates("Seoul")

    response = httpx.get(WEATHER_URL, params={
        "latitude": lat,           # geocoding에서 받아온 위도
        "longitude": lon,          # geocoding에서 받아온 경도
        "hourly": ",".join([
            "temperature_2m",              # 지상 2m 기온 (°C)
            "apparent_temperature",        # 체감온도 (바람/습도 반영)
            "precipitation_probability",   # 강수확률 (%)
            "weathercode",                 # 날씨 상태 코드 (맑음/비/눈 등)
            "windspeed_10m",               # 지상 10m 풍속 (km/h)
        ]),
        "forecast_days": 4,        # 오늘 포함 4일치 예보
        "timezone": "Asia/Seoul"   # 미설정 시 UTC 기준으로 반환됨
    })
    assert response.status_code == 200
    data = response.json()

    print("\n[Hourly 날씨 반환값 - Raw JSON]")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print("\n시간대별 날씨 (3시간 간격):")
    for i in range(0, len(data["hourly"]["time"]), 3):
        code = data["hourly"]["weathercode"][i]
        print(
            f"  {data['hourly']['time'][i]} | "
            f"{data['hourly']['temperature_2m'][i]}°C (체감 {data['hourly']['apparent_temperature'][i]}°C) | "
            f"강수확률 {data['hourly']['precipitation_probability'][i]}% | "
            f"{decode_weathercode(code)} | "
            f"풍속 {data['hourly']['windspeed_10m'][i]}km/h"
        )

    assert "hourly" in data
    assert "apparent_temperature" in data["hourly"]
    assert "temperature_2m" in data["hourly"]
    assert "time" in data["hourly"]
    assert len(data["hourly"]["time"]) > 0


# ───────────────────────────────────────────
# 3단계: 날씨 API - Daily (케이스 2: 5~16일)
# ───────────────────────────────────────────

def test_weather_daily_seoul():
    """서울 daily 날씨 반환값 구조 확인 (5~16일 케이스)"""
    lat, lon = get_coordinates("Seoul")

    response = httpx.get(WEATHER_URL, params={
        "latitude": lat,           # geocoding에서 받아온 위도
        "longitude": lon,          # geocoding에서 받아온 경도
        "daily": ",".join([
            "temperature_2m_max",              # 일 최고기온 (°C)
            "temperature_2m_min",              # 일 최저기온 (°C)
            "apparent_temperature_max",        # 일 최고 체감온도
            "apparent_temperature_min",        # 일 최저 체감온도
            "precipitation_probability_max",   # 일 최대 강수확률 (%)
            "weathercode",                     # 날씨 상태 코드
            "uv_index_max",                    # 일 최대 자외선 지수
        ]),
        "forecast_days": 16,       # 오늘 포함 최대 16일치 예보
        "timezone": "Asia/Seoul"   # 미설정 시 UTC 기준으로 반환됨
    })
    assert response.status_code == 200
    data = response.json()

    print("\n[Daily 날씨 반환값 - Raw JSON]")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print("\n첫 5일:")
    for i in range(5):
        code = data["daily"]["weathercode"][i]
        print(
            f"  {data['daily']['time'][i]} | "
            f"최고 {data['daily']['temperature_2m_max'][i]}°C (체감 {data['daily']['apparent_temperature_max'][i]}°C) | "
            f"최저 {data['daily']['temperature_2m_min'][i]}°C (체감 {data['daily']['apparent_temperature_min'][i]}°C) | "
            f"강수확률 {data['daily']['precipitation_probability_max'][i]}% | "
            f"{decode_weathercode(code)} | "
            f"UV {data['daily']['uv_index_max'][i]}"
        )

    assert "daily" in data
    assert "apparent_temperature_max" in data["daily"]
    assert "uv_index_max" in data["daily"]
    assert len(data["daily"]["time"]) == 16


# ───────────────────────────────────────────
# 해외 도시 테스트
# ───────────────────────────────────────────

def test_weather_daily_osaka():
    """오사카 daily 날씨 반환값 확인"""
    lat, lon = get_coordinates("Osaka")

    response = httpx.get(WEATHER_URL, params={
        "latitude": lat,           # geocoding에서 받아온 위도
        "longitude": lon,          # geocoding에서 받아온 경도
        "daily": ",".join([
            "temperature_2m_max",              # 일 최고기온 (°C)
            "temperature_2m_min",              # 일 최저기온 (°C)
            "apparent_temperature_max",        # 일 최고 체감온도
            "precipitation_probability_max",   # 일 최대 강수확률 (%)
            "weathercode",                     # 날씨 상태 코드
            "uv_index_max",                    # 일 최대 자외선 지수
        ]),
        "forecast_days": 7,        # 오늘 포함 7일치 예보
        "timezone": "Asia/Tokyo"   # 미설정 시 UTC 기준으로 반환됨
    })
    assert response.status_code == 200
    data = response.json()

    print("\n[오사카 Daily 날씨 반환값 - Raw JSON]")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print("\n첫 3일:")
    for i in range(3):
        code = data["daily"]["weathercode"][i]
        print(
            f"  {data['daily']['time'][i]} | "
            f"최고 {data['daily']['temperature_2m_max'][i]}°C (체감 {data['daily']['apparent_temperature_max'][i]}°C) | "
            f"최저 {data['daily']['temperature_2m_min'][i]}°C | "
            f"강수확률 {data['daily']['precipitation_probability_max'][i]}% | "
            f"{decode_weathercode(code)}"
        )

    assert "daily" in data


# ───────────────────────────────────────────
# 4단계: Historical API (케이스 3: 16일 초과)
# ───────────────────────────────────────────

def test_historical_weather_seoul():
    """서울 Historical API 반환값 구조 확인 (16일 초과 케이스)"""
    lat, lon = get_coordinates("Seoul")

    # 작년 같은 시기 데이터 조회 (여행일이 6개월 뒤라면 작년 같은 달 데이터 참고)
    response = httpx.get(HISTORICAL_URL, params={
        "latitude": lat,
        "longitude": lon,
        "start_date": "2025-06-15",    # 작년 여행 시작일에 해당하는 날짜
        "end_date": "2025-06-18",      # 작년 여행 종료일에 해당하는 날짜
        "daily": ",".join([
            "temperature_2m_max",              # 일 최고기온 (°C)
            "temperature_2m_min",              # 일 최저기온 (°C)
            "apparent_temperature_max",        # 일 최고 체감온도
            "apparent_temperature_min",        # 일 최저 체감온도
            "precipitation_sum",               # 일 총 강수량 (mm)
            "weathercode",                     # 날씨 상태 코드
            "uv_index_max",                    # 일 최대 자외선 지수
        ]),
        "timezone": "auto",
    })
    assert response.status_code == 200
    data = response.json()

    print("\n[Historical 날씨 반환값 - Raw JSON]")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print("\n조회된 날짜별 데이터:")
    for i in range(len(data["daily"]["time"])):
        code = data["daily"]["weathercode"][i]
        print(
            f"  {data['daily']['time'][i]} | "
            f"최고 {data['daily']['temperature_2m_max'][i]}°C (체감 {data['daily']['apparent_temperature_max'][i]}°C) | "
            f"최저 {data['daily']['temperature_2m_min'][i]}°C | "
            f"강수량 {data['daily']['precipitation_sum'][i]}mm | "
            f"{decode_weathercode(code)} | "
            f"UV {data['daily']['uv_index_max'][i]}"
        )

    assert "daily" in data
    assert "temperature_2m_max" in data["daily"]
    assert "precipitation_sum" in data["daily"]
    assert len(data["daily"]["time"]) > 0


def test_historical_weather_osaka():
    """오사카 Historical API 반환값 확인"""
    lat, lon = get_coordinates("Osaka")

    response = httpx.get(HISTORICAL_URL, params={
        "latitude": lat,
        "longitude": lon,
        "start_date": "2025-08-01",
        "end_date": "2025-08-05",
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "apparent_temperature_max",
            "precipitation_sum",
            "weathercode",
            "uv_index_max",
        ]),
        "timezone": "auto",
    })
    assert response.status_code == 200
    data = response.json()

    print("\n[오사카 Historical 날씨 반환값 - Raw JSON]")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    assert "daily" in data
    assert len(data["daily"]["time"]) > 0


# ───────────────────────────────────────────
# 어댑터 연결 테스트
# ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_adapter_get_weather_hourly():
    """어댑터 hourly 예보 — 4일 이내 케이스 반환값 구조 확인"""
    adapter = WeatherAdapter()
    result = await adapter.execute("get_weather", {
        "city": "Seoul",
        "forecast_days": 3,
    })

    print("\n[어댑터 hourly 예보 - 정제된 결과]")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["status"] == "success"
    assert result["forecast_type"] == "hourly"
    assert result["count"] > 0
    item = result["data"][0]
    assert "time" in item
    assert "temperature" in item
    assert "apparent_temperature" in item
    assert "precipitation_probability" in item
    assert "weather" in item
    assert "windspeed" in item


@pytest.mark.asyncio
async def test_adapter_get_weather_daily():
    """어댑터 daily 예보 — 5일 이상 케이스 반환값 구조 확인"""
    adapter = WeatherAdapter()
    result = await adapter.execute("get_weather", {
        "city": "Osaka",
        "forecast_days": 7,
    })

    print("\n[어댑터 daily 예보 - 정제된 결과]")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["status"] == "success"
    assert result["forecast_type"] == "daily"
    assert result["count"] == 7
    item = result["data"][0]
    assert "date" in item
    assert "temperature_max" in item
    assert "temperature_min" in item
    assert "precipitation_probability_max" in item
    assert "weather" in item
    assert "uv_index_max" in item


@pytest.mark.asyncio
async def test_adapter_missing_city():
    """어댑터 에러 처리 — city 누락 시 에러 반환 확인"""
    adapter = WeatherAdapter()
    result = await adapter.execute("get_weather", {})

    print("\n[city 누락 에러]")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_adapter_korean_city():
    """어댑터 에러 처리 — 한국어 도시명 입력 시 에러 반환 확인"""
    adapter = WeatherAdapter()
    result = await adapter.execute("get_weather", {
        "city": "서울",
        "forecast_days": 3,
    })

    print("\n[한국어 도시명 에러]")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_adapter_invalid_forecast_days():
    """어댑터 에러 처리 (get_weather) — forecast_days 범위 초과 시 에러 반환 확인"""
    adapter = WeatherAdapter()
    result = await adapter.execute("get_weather", {
        "city": "Seoul",
        "forecast_days": 17,
    })

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_adapter_unsupported_action():
    """어댑터 에러 처리 — 지원하지 않는 액션 확인"""
    adapter = WeatherAdapter()
    result = await adapter.execute("unknown_action", {})

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_adapter_get_historical_weather():
    """어댑터 historical 날씨 — 반환값 구조 확인"""
    adapter = WeatherAdapter()
    result = await adapter.execute("get_historical_weather", {
        "city": "Seoul",
        "start_date": "2025-06-15",
        "end_date": "2025-06-18",
    })

    print("\n[어댑터 historical 날씨 - 정제된 결과]")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["status"] == "success"
    assert result["forecast_type"] == "historical"
    assert result["count"] == 4
    item = result["data"][0]
    assert "date" in item
    assert "temperature_max" in item
    assert "temperature_min" in item
    assert "apparent_temperature_max" in item
    assert "apparent_temperature_min" in item
    assert "precipitation_sum" in item
    assert "weather" in item


@pytest.mark.asyncio
async def test_adapter_historical_missing_city():
    """어댑터 에러 처리 (get_historical_weather) — city 누락 시 에러 반환 확인"""
    adapter = WeatherAdapter()
    result = await adapter.execute("get_historical_weather", {
        "start_date": "2025-06-15",
        "end_date": "2025-06-18",
    })

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_adapter_historical_korean_city():
    """어댑터 에러 처리 (get_historical_weather) — 한국어 도시명 입력 시 에러 반환 확인"""
    adapter = WeatherAdapter()
    result = await adapter.execute("get_historical_weather", {
        "city": "서울",
        "start_date": "2025-06-15",
        "end_date": "2025-06-18",
    })

    print("\n[한국어 도시명 에러]")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_adapter_historical_missing_dates():
    """어댑터 에러 처리 (get_historical_weather) — start_date/end_date 누락 시 에러 반환 확인"""
    adapter = WeatherAdapter()
    result = await adapter.execute("get_historical_weather", {
        "city": "Seoul",
    })

    assert result["status"] == "error"

