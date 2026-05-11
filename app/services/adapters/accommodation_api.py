import httpx
from app.core.ApiToolsInterfaces import ApiTools
from app.core.config import settings
from app.services.adapters.currency_converter import to_krw
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

        print(f"\n[AccommodationAdapter] Places Suggestions 요청: query='{query}'")
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url, headers=headers, params=params)
                print(f"[AccommodationAdapter] Places Suggestions HTTP Status: {response.status_code}")

                if response.status_code != 200:
                    print(f"[AccommodationAdapter] Places Suggestions 실패: {response.text[:200]}")
                    return None

                data = response.json()
                suggestions = data.get("data", [])
                
                print(f"[AccommodationAdapter] Places Suggestions 결과: {len(suggestions)}건")
                if not suggestions:
                    return None

                # 1. 도시(city) 타입의 제안을 우선적으로 찾음
                for suggestion in suggestions:
                    if suggestion.get("type") == "city":
                        # 도시 자체에 좌표가 있는 경우 바로 사용
                        lat = suggestion.get("latitude")
                        lon = suggestion.get("longitude")
                        if lat is not None and lon is not None:
                            print(f"[AccommodationAdapter] 좌표 확정 (city): ({lat}, {lon})")
                            return lat, lon

                        # 도시 좌표가 없다면 포함된 공항들 중 최적의 공항 선택
                        airports = suggestion.get("airports", [])
                        if airports:
                            target_city = suggestion.get("name")
                            matching_airports = [a for a in airports if a.get("city_name") == target_city]
                            best_airport = matching_airports[0] if matching_airports else airports[0]
                            lat = best_airport.get("latitude")
                            lon = best_airport.get("longitude")
                            if lat is not None and lon is not None:
                                print(f"[AccommodationAdapter] 좌표 확정 (airport {best_airport.get('iata_code')}): ({lat}, {lon})")
                                return lat, lon

                # 2. 도시 타입이 없거나 좌표를 못 찾은 경우, 다른 제안들 중 좌표가 있는 첫 번째 항목 사용
                for suggestion in suggestions:
                    lat = suggestion.get("latitude")
                    lon = suggestion.get("longitude")
                    if lat is not None and lon is not None:
                        print(f"[AccommodationAdapter] 좌표 확정 (fallback): ({lat}, {lon})")
                        return lat, lon

                print(f"[AccommodationAdapter] 좌표 추출 실패: suggestions에 좌표 없음")
                return None
            except Exception as e:
                print(f"[AccommodationAdapter] Places Suggestions 예외: {e}")
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

            # 1. 기본적인 파라미터 검증
            if not all([check_in, check_out, city_query]):
                return {"status": "error", "message": "check_in, check_out, city_name(또는 city_code)는 필수입니다."}

            if children > 0 and len(child_ages) != children:
                return {
                    "status": "error", 
                    "message": f"아이 인원({children}명)과 나이 정보({len(child_ages)}개)의 개수가 일치하지 않습니다."
                }

            # 2. 위도/경도 좌표 추출
            coords = await self._get_coordinates(city_query)
            if not coords:
                return {"status": "error", "message": f"'{city_query}'에 해당하는 위치 정보를 찾을 수 없습니다."}
            
            lat, lon = coords

            # 3. Guests 리스트 구성
            guests = []
            for _ in range(adults):
                guests.append({"type": "adult"})
            for age in child_ages:
                guests.append({"type": "child", "age": int(age)})

            # 4. 페이로드 구성 (문서에 따른 geographic_coordinates 중첩 구조)
            # radius는 20km로 설정
            payload = {
                "data": {
                    "location": {
                        "geographic_coordinates": {
                            "latitude": lat,
                            "longitude": lon
                        },
                        "radius": 20 # 검색 반경이 20km
                    },
                    "check_in_date": check_in,
                    "check_out_date": check_out,
                    "guests": guests,
                    "rooms": int(params.get("rooms", 1))
                }
            }

            print(f"\n[AccommodationAdapter] search_hotels 요청")
            print(f"  city_query={city_query!r} → 좌표=({lat}, {lon})")
            print(f"  check_in={check_in}, check_out={check_out}, guests={guests}, rooms={params.get('rooms', 1)}")
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=self._get_headers(), json=payload)
                print(f"[AccommodationAdapter] search_hotels HTTP Status: {response.status_code}")

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
                    print(f"[AccommodationAdapter] search_hotels 오류: {data.get('errors')}")
                    return {"status": "error", "message": data.get("errors")}

                # 4. 결과 데이터 정제
                raw_results = data.get("data", {}).get("results", [])
                print(f"[AccommodationAdapter] search_hotels 결과: {len(raw_results)}개 hotels")
                
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

                    # Chain 이름 안전하게 추출
                    chain_info = hotel.get("chain")
                    chain_name = "Independent"
                    if isinstance(chain_info, dict):
                        chain_name = chain_info.get("name", "Independent")

                    raw_amount = res.get("cheapest_rate_total_amount")
                    orig_amount = float(raw_amount) if raw_amount else None
                    orig_currency = res.get("cheapest_rate_currency", "USD")
                    price_krw = await to_krw(orig_amount, orig_currency) if orig_amount else None
                    processed_hotels.append({
                        "hotel_id": hotel.get("id"),
                        "name": hotel.get("name"),
                        "price_original": orig_amount,   # 현지 통화 1박 금액 (없으면 None)
                        "currency": orig_currency,
                        "price_krw": price_krw,           # 한화 환산 1박 금액 (없으면 None)
                        "rating": hotel.get("rating"),
                        "address": address_str,
                        "chain": chain_name
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