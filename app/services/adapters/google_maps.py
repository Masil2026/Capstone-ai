import httpx

from typing import Any, Dict
from app.core.ApiToolsInterfaces import ApiTools
from app.core.config import settings


class GoogleMapsAdapter(ApiTools):
    def __init__(self):
        self.api_key = settings.GOOGLE_MAPS_API_KEY
        self.directions_base_url = "https://maps.googleapis.com/maps/api/directions/json"
        self.places_text_search_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"

    @property
    def tool_name(self) -> str:
        return "google_maps"

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.api_key:
            return {"status": "error", "message": "GOOGLE_MAPS_API_KEY가 설정되지 않았습니다."}

        if action == "find_route":
            return await self._find_route(params)

        if action == "search_place":
            return await self._search_place(params)

        return {"status": "error", "message": f"지원하지 않는 액션: {action}"}

    async def _find_route(self, params: Dict[str, Any]) -> Dict[str, Any]:
        origin = params.get("origin")
        dest = params.get("dest")
        mode = params.get("mode", "transit")
        language = params.get("language", "ko")

        if not origin or not dest:
            return {"status": "error", "message": "origin과 dest는 필수입니다."}

        query_params = {
            "origin": origin,
            "destination": dest,
            "mode": mode,
            "language": language,
            "key": self.api_key,
        }

        debug_params = {**query_params, "key": "***REDACTED***"}
        print("\n[GoogleMapsAdapter] find_route 실제 요청 전송")
        print(f"URL: {self.directions_base_url}")
        print(f"Params: {debug_params}")

        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                response = await client.get(self.directions_base_url, params=query_params)
            except httpx.TimeoutException:
                return {"status": "error", "message": "Google Maps Directions API 타임아웃 (20초 초과)"}
            except httpx.RequestError as e:
                return {"status": "error", "message": f"Google Maps Directions API 요청 실패: {str(e)}"}

        print(f"[GoogleMapsAdapter] HTTP Status: {response.status_code}")

        try:
            data = response.json()
        except Exception:
            return {
                "status": "error",
                "message": f"API 응답이 JSON 형식이 아닙니다: {response.text[:200]}"
            }

        print(f"[GoogleMapsAdapter] API Status: {data.get('status')}")
        print(f"[GoogleMapsAdapter] 경로 개수: {len(data.get('routes', []))}")

        if response.status_code != 200:
            return {"status": "error", "message": f"HTTP 오류: {response.status_code}"}

        api_status = data.get("status")
        if api_status != "OK":
            return {
                "status": "error",
                "message": data.get("error_message") or api_status or "UNKNOWN_ERROR"
            }

        routes = []
        for route in data.get("routes", []):
            legs = route.get("legs", [])
            if not legs:
                continue

            first_leg = legs[0]

            routes.append({
                "summary": route.get("summary"),
                "start_address": first_leg.get("start_address"),
                "end_address": first_leg.get("end_address"),
                "distance_text": first_leg.get("distance", {}).get("text"),
                "duration_text": first_leg.get("duration", {}).get("text"),
                "fare": route.get("fare"),  # {"currency":"JPY","text":"¥500","value":500.0} or None (transit 일부 노선만)
                "steps": [
                    {
                        "instruction": step.get("html_instructions"),
                        "distance_text": step.get("distance", {}).get("text"),
                        "duration_text": step.get("duration", {}).get("text"),
                        "travel_mode": step.get("travel_mode"),
                    }
                    for step in first_leg.get("steps", [])
                ]
            })

        for r in routes:
            fare = r.get("fare")
            print(f"[GoogleMapsAdapter] 교통 요금: {fare.get('text')} ({fare.get('currency')})" if fare else "[GoogleMapsAdapter] 교통 요금: 정보 없음")

        return {
            "status": "success",
            "data": {
                "type": "구글맵 경로 데이터",
                "count": len(routes),
                "routes": routes,
            }
        }

    async def _search_place(self, params: Dict[str, Any]) -> Dict[str, Any]:
        query = params.get("query")
        language = params.get("language", "ko")
        region = params.get("region", "kr")

        if not query:
            return {"status": "error", "message": "query는 필수입니다."}

        query_params = {
            "query": query,
            "language": language,
            "region": region,
            "key": self.api_key,
        }
        if params.get("location"):
            query_params["location"] = params["location"]
        if params.get("radius"):
            query_params["radius"] = params["radius"]

        debug_params = {**query_params, "key": "***REDACTED***"}
        print("\n[GoogleMapsAdapter] search_place 실제 요청 전송")
        print(f"URL: {self.places_text_search_url}")
        print(f"Params: {debug_params}")

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.get(self.places_text_search_url, params=query_params)
            except httpx.TimeoutException:
                return {"status": "error", "message": "Google Maps Places API 타임아웃 (15초 초과)"}
            except httpx.RequestError as e:
                return {"status": "error", "message": f"Google Maps Places API 요청 실패: {str(e)}"}

        print(f"[GoogleMapsAdapter] HTTP Status: {response.status_code}")

        try:
            data = response.json()
        except Exception:
            return {
                "status": "error",
                "message": f"API 응답이 JSON 형식이 아닙니다: {response.text[:200]}"
            }

        print(f"[GoogleMapsAdapter] API Status: {data.get('status')}")
        print(f"[GoogleMapsAdapter] 결과 개수: {len(data.get('results', []))}")

        if response.status_code != 200:
            return {"status": "error", "message": f"HTTP 오류: {response.status_code}"}

        api_status = data.get("status")
        if api_status != "OK":
            return {
                "status": "error",
                "message": data.get("error_message") or api_status or "UNKNOWN_ERROR"
            }

        _PRICE_LABEL = {0: "무료", 1: "저렴", 2: "보통", 3: "비쌈", 4: "매우 비쌈"}
        places = []
        for place in data.get("results", []):
            location = place.get("geometry", {}).get("location", {})
            price_level = place.get("price_level")  # 0~4 상대 척도 (지역마다 기준 다름), 없으면 None
            places.append({
                "name": place.get("name"),
                "formatted_address": place.get("formatted_address"),
                "place_id": place.get("place_id"),
                "lat": location.get("lat"),
                "lng": location.get("lng"),
                "rating": place.get("rating"),
                "user_ratings_total": place.get("user_ratings_total"),
                "types": place.get("types", []),
                "price_level": price_level,
                "price_level_label": _PRICE_LABEL.get(price_level) if price_level is not None else None,
            })

        return {
            "status": "success",
            "data": {
                "type": "구글맵 장소 검색 데이터",
                "count": len(places),
                "places": places,
            }
        }
    
