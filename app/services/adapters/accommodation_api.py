import httpx
from app.core.ApiToolsInterfaces import ApiTools
from app.core.config import settings
from typing import Any, Dict, Optional, Tuple

class AccommodationAdapter(ApiTools):
    def __init__(self):
        self.api_key = settings.DUFFEL_API_KEY.strip()
        self.base_url = "https://api.duffel.com/stays"

    @property
    def tool_name(self) -> str:
        return "duffel_accommodation"

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Duffel-Version": "v2",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _get_coordinates(self, query: str) -> Optional[Tuple[float, float]]:
        """Duffel Places Suggestions API를 사용하여 도시명을 위도/경도로 변환합니다."""
        url = "https://api.duffel.com/places/suggestions"
        headers = self._get_headers()
        params = {"query": query}

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers, params=params)
            
            if response.status_code != 200:
                return None
            
            data = response.json()
            suggestions = data.get("data", [])
            
            if not suggestions:
                return None
            
            # 첫 번째 결과의 위도, 경도 추출
            first_suggestion = suggestions[0]
            lat = first_suggestion.get("latitude")
            lon = first_suggestion.get("longitude")
            
            if lat is not None and lon is not None:
                return lat, lon
            return None

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        
        # 1. 숙소 검색 (Search)
        if action == "search_hotels":
            # 공식 문서 엔드포인트: /search
            url = f"{self.base_url}/search"
            
            check_in = params.get("check_in")
            check_out = params.get("check_out")
            city_query = params.get("city_name") or params.get("city_code") # 도시명 또는 코드 사용
            adults = int(params.get("adults", 1))
            children = int(params.get("children", 0))
            child_ages = params.get("child_ages", [])

            # 검증 로직
            if not all([check_in, check_out, city_query]):
                return {"status": "error", "message": "check_in, check_out, city_name(또는 city_code)는 필수입니다."}
            
            # 위도/경도 좌표 추출
            coords = await self._get_coordinates(city_query)
            if not coords:
                return {"status": "error", "message": f"'{city_query}'에 해당하는 위치 정보를 찾을 수 없습니다."}
            
            lat, lon = coords

            if children > 0 and len(child_ages) != children:
                return {
                    "status": "error", 
                    "message": f"아이 인원({children}명)과 나이 정보({len(child_ages)}개)의 개수가 일치하지 않습니다."
                }

            # 2. Guests 리스트 구성 (type과 age를 가진 객체 리스트)
            guests = []
            for _ in range(adults):
                guests.append({"type": "adult"})
            for age in child_ages:
                guests.append({"type": "child", "age": int(age)})

            # 3. 페이로드 구성 (위도/경도 기반 검색)
            # radius는 5000(5km)으로 설정
            payload = {
                "data": {
                    "location": {
                        "latitude": lat,
                        "longitude": lon,
                        "radius": 5000
                    },
                    "check_in_date": check_in,
                    "check_out_date": check_out,
                    "guests": guests,
                    "rooms": int(params.get("rooms", 1))
                }
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=self._get_headers(), json=payload)
                
                # JSONDecodeError 방지를 위한 예외 처리
                try:
                    data = response.json()
                except Exception:
                    return {
                        "status": "error", 
                        "message": f"API 응답이 JSON 형식이 아닙니다: {response.text[:100]}"
                    }
                
                # 성공 응답 코드는 200 또는 201
                if response.status_code not in [200, 201]:
                    return {"status": "error", "message": data.get("errors")}

                # 4. 결과 데이터 정제
                raw_results = data.get("data", {}).get("results", [])
                
                if not raw_results:
                    return {
                        "status": "success",
                        "count": 0,
                        "data": [],
                        "message": "검색 결과가 없습니다."
                    }

                processed_hotels = []
                for res in raw_results[:10]: # 상위 10개만 추천
                    hotel = res.get("accommodation", {})
                    # 주소 정보 가공 (주소가 객체인 경우 line_one 추출)
                    location_info = hotel.get("location", {})
                    address_data = location_info.get("address", {})
                    
                    if isinstance(address_data, dict):
                        address_str = address_data.get("line_one", "Address Not Provided")
                    else:
                        address_str = str(address_data)

                    processed_hotels.append({
                        "hotel_id": hotel.get("id"),
                        "name": hotel.get("name"),
                        "price": f"{res.get('cheapest_rate_total_amount')} {res.get('cheapest_rate_currency')}",
                        "rating": hotel.get("rating"),
                        "address": address_str,
                        "chain": hotel.get("chain", {}).get("name", "Independent")
                    })

                return {
                    "status": "success",
                    "count": len(processed_hotels),
                    "data": processed_hotels
                }

        # --- [TODO: 나중에 구현할 액션들] ---
        elif action in ["get_hotel_details", "create_booking", "cancel_booking"]:
            return {
                "status": "todo",
                "message": f"[Duffel Stays] '{action}' 기능은 현재 개발 중(TODO)입니다."
            }

        return {"status": "error", "message": f"지원하지 않는 액션: {action}"}