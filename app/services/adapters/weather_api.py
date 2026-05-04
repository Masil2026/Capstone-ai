import httpx
from app.core.ApiToolsInterfaces import ApiTools
from typing import Any, Dict


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
HISTORICAL_URL = "https://archive-api.open-meteo.com/v1/archive"

# WMO weathercode → 한글 변환표
# 참고: https://open-meteo.com/en/docs#weathervariables
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


class WeatherAdapter(ApiTools):
    # Open-Meteo는 API 키가 필요 없으므로 __init__ 생략

    @property
    def tool_name(self) -> str:
        return "weather"

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:

        # 1. 날씨 조회 (get_weather)
        if action == "get_weather":
            city = params.get("city")
            forecast_days = int(params.get("forecast_days", 7))

            # 검증 로직
            # 주의: Open-Meteo Geocoding API는 한국어 도시명을 지원하지 않음
            # GPT-4o가 영어 도시명으로 변환하여 전달해야 함 (예: "서울" → "Seoul")
            if not city:
                return {"status": "error", "message": "city는 필수입니다."}

            if not 1 <= forecast_days <= 16:
                return {"status": "error", "message": "forecast_days는 1~16 사이여야 합니다."}

            # timezone 미설정 시 UTC 기준으로 반환됨 — 기본값 "auto"로 위치 기반 자동 설정
            timezone = params.get("timezone", "auto")

            async with httpx.AsyncClient(timeout=30.0) as client:

                # 1. Geocoding: 도시명 → 위경도 변환
                try:
                    geo_response = await client.get(GEOCODING_URL, params={"name": city, "count": 1})
                except httpx.TimeoutException:
                    # 네트워크 타임아웃 (30초 초과)
                    return {"status": "error", "message": "Geocoding API 타임아웃 (30초 초과)"}

                # JSONDecodeError 방지를 위한 예외 처리 (서버 장애 등)
                try:
                    geo_data = geo_response.json()
                except Exception:
                    return {
                        "status": "error",
                        "message": f"Geocoding API 응답이 JSON 형식이 아닙니다: {geo_response.text[:100]}"
                    }

                # HTTP 오류 (4xx, 5xx)
                if geo_response.status_code != 200:
                    return {"status": "error", "message": f"Geocoding API 오류: {geo_response.status_code}"}

                # 도시를 찾지 못한 경우 — 한국어 도시명 입력 시에도 이 경로로 처리됨
                if not geo_data.get("results"):
                    return {"status": "error", "message": f"도시를 찾을 수 없습니다: {city}"}

                location = geo_data["results"][0]
                lat = location["latitude"]
                lon = location["longitude"]

                # 2-A. 4일 이내 → Hourly 예보 (시간대별 상세 데이터)
                if forecast_days <= 4:
                    try:
                        weather_response = await client.get(WEATHER_URL, params={
                            "latitude": lat,
                            "longitude": lon,
                            "hourly": ",".join([
                                "temperature_2m",              # 지상 2m 기온 (°C)
                                "apparent_temperature",        # 체감온도 (바람/습도 반영)
                                "precipitation_probability",   # 강수확률 (%)
                                "weathercode",                 # 날씨 상태 코드
                                "windspeed_10m",               # 지상 10m 풍속 (km/h)
                            ]),
                            "forecast_days": forecast_days,
                            "timezone": timezone,
                        })
                    except httpx.TimeoutException:
                        # 네트워크 타임아웃 (30초 초과)
                        return {"status": "error", "message": "Weather API 타임아웃 (30초 초과)"}

                    # JSONDecodeError 방지를 위한 예외 처리 (서버 장애 등)
                    try:
                        weather_data = weather_response.json()
                    except Exception:
                        return {
                            "status": "error",
                            "message": f"Weather API 응답이 JSON 형식이 아닙니다: {weather_response.text[:100]}"
                        }

                    # HTTP 오류 (4xx, 5xx)
                    if weather_response.status_code != 200:
                        return {"status": "error", "message": f"Weather API 오류: {weather_response.status_code}"}

                    # 3. 결과 데이터 정제 (hourly)
                    hourly = weather_data["hourly"]
                    processed_forecast = [
                        {
                            "time": hourly["time"][i],
                            "temperature": hourly["temperature_2m"][i],
                            "apparent_temperature": hourly["apparent_temperature"][i],
                            "precipitation_probability": hourly["precipitation_probability"][i],
                            "weather": WEATHERCODE_MAP.get(hourly["weathercode"][i], f"알 수 없음 ({hourly['weathercode'][i]})"),
                            "windspeed": hourly["windspeed_10m"][i],
                        }
                        for i in range(len(hourly["time"]))
                    ]

                    return {
                        "status": "success",
                        "city": location["name"],
                        "forecast_type": "hourly",
                        "forecast_days": forecast_days,
                        "count": len(processed_forecast),
                        "data": processed_forecast,
                    }

                # 2-B. 5~16일 → Daily 예보 (일별 요약 데이터)
                else:
                    try:
                        weather_response = await client.get(WEATHER_URL, params={
                            "latitude": lat,
                            "longitude": lon,
                            "daily": ",".join([
                                "temperature_2m_max",              # 일 최고기온 (°C)
                                "temperature_2m_min",              # 일 최저기온 (°C)
                                "apparent_temperature_max",        # 일 최고 체감온도
                                "apparent_temperature_min",        # 일 최저 체감온도
                                "precipitation_probability_max",   # 일 최대 강수확률 (%)
                                "weathercode",                     # 날씨 상태 코드
                                "uv_index_max",                    # 일 최대 자외선 지수
                            ]),
                            "forecast_days": forecast_days,
                            "timezone": timezone,
                        })
                    except httpx.TimeoutException:
                        # 네트워크 타임아웃 (30초 초과)
                        return {"status": "error", "message": "Weather API 타임아웃 (30초 초과)"}

                    try:
                        weather_data = weather_response.json()
                    except Exception:
                        # API가 JSON이 아닌 응답을 반환한 경우 (서버 장애 등)
                        return {
                            "status": "error",
                            "message": f"Weather API 응답이 JSON 형식이 아닙니다: {weather_response.text[:100]}"
                        }

                    # HTTP 오류 (4xx, 5xx)
                    if weather_response.status_code != 200:
                        return {"status": "error", "message": f"Weather API 오류: {weather_response.status_code}"}

                    # 3. 결과 데이터 정제 (daily)
                    daily = weather_data["daily"]
                    processed_forecast = [
                        {
                            "date": daily["time"][i],
                            "temperature_max": daily["temperature_2m_max"][i],
                            "temperature_min": daily["temperature_2m_min"][i],
                            "apparent_temperature_max": daily["apparent_temperature_max"][i],
                            "apparent_temperature_min": daily["apparent_temperature_min"][i],
                            "precipitation_probability_max": daily["precipitation_probability_max"][i],
                            "weather": WEATHERCODE_MAP.get(daily["weathercode"][i], f"알 수 없음 ({daily['weathercode'][i]})"),
                            "uv_index_max": daily["uv_index_max"][i],
                        }
                        for i in range(len(daily["time"]))
                    ]

                    return {
                        "status": "success",
                        "city": location["name"],
                        "forecast_type": "daily",
                        "forecast_days": forecast_days,
                        "count": len(processed_forecast),
                        "data": processed_forecast,
                    }

        # 2. 과거 날씨 조회 (get_historical_weather)
        # 여행일이 오늘로부터 16일 초과인 경우 — 작년 같은 시기 데이터를 참고용으로 조회
        # GPT-4o가 이 데이터를 바탕으로 근거 있는 날씨 유추를 수행
        elif action == "get_historical_weather":
            city = params.get("city")
            start_date = params.get("start_date")  # YYYY-MM-DD (작년 여행 시작일에 해당하는 날짜)
            end_date = params.get("end_date")       # YYYY-MM-DD (작년 여행 종료일에 해당하는 날짜)

            # 검증 로직
            if not city:
                return {"status": "error", "message": "city는 필수입니다."}

            if not all([start_date, end_date]):
                return {"status": "error", "message": "start_date, end_date는 필수입니다. (형식: YYYY-MM-DD)"}

            async with httpx.AsyncClient(timeout=30.0) as client:

                # 1. Geocoding: 도시명 → 위경도 변환
                try:
                    geo_response = await client.get(GEOCODING_URL, params={"name": city, "count": 1})
                except httpx.TimeoutException:
                    # 네트워크 타임아웃 (30초 초과)
                    return {"status": "error", "message": "Geocoding API 타임아웃 (30초 초과)"}

                # JSONDecodeError 방지를 위한 예외 처리 (서버 장애 등)
                try:
                    geo_data = geo_response.json()
                except Exception:
                    return {
                        "status": "error",
                        "message": f"Geocoding API 응답이 JSON 형식이 아닙니다: {geo_response.text[:100]}"
                    }

                # HTTP 오류 (4xx, 5xx)
                if geo_response.status_code != 200:
                    return {"status": "error", "message": f"Geocoding API 오류: {geo_response.status_code}"}

                # 도시를 찾지 못한 경우 — 한국어 도시명 입력 시에도 이 경로로 처리됨
                if not geo_data.get("results"):
                    return {"status": "error", "message": f"도시를 찾을 수 없습니다: {city}"}

                location = geo_data["results"][0]
                lat = location["latitude"]
                lon = location["longitude"]

                # 2. Historical API 호출
                try:
                    hist_response = await client.get(HISTORICAL_URL, params={
                        "latitude": lat,
                        "longitude": lon,
                        "start_date": start_date,
                        "end_date": end_date,
                        "daily": ",".join([
                            "temperature_2m_max",              # 일 최고기온 (°C)
                            "temperature_2m_min",              # 일 최저기온 (°C)
                            "apparent_temperature_max",        # 일 최고 체감온도
                            "apparent_temperature_min",        # 일 최저 체감온도
                            "precipitation_sum",               # 일 총 강수량 (mm) — 예보와 달리 확률 대신 실측값
                            "weathercode",                     # 날씨 상태 코드
                            # uv_index_max: Historical API에서 null 반환으로 제외
                        ]),
                        "timezone": "auto",
                    })
                except httpx.TimeoutException:
                    # 네트워크 타임아웃 (30초 초과)
                    return {"status": "error", "message": "Historical API 타임아웃 (30초 초과)"}

                # JSONDecodeError 방지를 위한 예외 처리 (서버 장애 등)
                try:
                    hist_data = hist_response.json()
                except Exception:
                    return {
                        "status": "error",
                        "message": f"Historical API 응답이 JSON 형식이 아닙니다: {hist_response.text[:100]}"
                    }

                # HTTP 오류 (4xx, 5xx)
                if hist_response.status_code != 200:
                    return {"status": "error", "message": f"Historical API 오류: {hist_response.status_code}"}

                # 3. 결과 데이터 정제
                daily = hist_data["daily"]
                processed_history = [
                    {
                        "date": daily["time"][i],
                        "temperature_max": daily["temperature_2m_max"][i],
                        "temperature_min": daily["temperature_2m_min"][i],
                        "apparent_temperature_max": daily["apparent_temperature_max"][i],
                        "apparent_temperature_min": daily["apparent_temperature_min"][i],
                        "precipitation_sum": daily["precipitation_sum"][i],
                        "weather": WEATHERCODE_MAP.get(daily["weathercode"][i], f"알 수 없음 ({daily['weathercode'][i]})"),
                    }
                    for i in range(len(daily["time"]))
                ]

                return {
                    "status": "success",
                    "city": location["name"],
                    "forecast_type": "historical",
                    "count": len(processed_history),
                    "data": processed_history,
                }

        return {"status": "error", "message": f"지원하지 않는 액션: {action}"}
