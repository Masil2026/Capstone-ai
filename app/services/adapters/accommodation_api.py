import httpx
from app.core.ApiToolsInterfaces import ApiTools
from app.core.config import settings
from typing import Any, Dict

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

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        
        # 1. 숙소 검색 (Search)
        if action == "search_hotels":
            # 공식 문서 엔드포인트: /search
            url = f"{self.base_url}/search"
            
            check_in = params.get("check_in")
            check_out = params.get("check_out")
            city_code = params.get("city_code") 
            adults = int(params.get("adults", 1))
            children = int(params.get("children", 0))
            child_ages = params.get("child_ages", [])

            # 검증 로직
            if not all([check_in, check_out, city_code]):
                return {"status": "error", "message": "check_in, check_out, city_code는 필수입니다."}
            
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

            # 3. 페이로드 구성
            # 참고: Duffel Stays는 iata_code 검색을 지원하지 않을 수 있어, 
            # 실패 시 위도/경도 좌표 검색으로 전환이 필요할 수 있습니다.
            payload = {
                "data": {
                    "location": {"iata_code": city_code},
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
                
                processed_hotels = []
                for res in raw_results[:10]:
                    hotel = res.get("accommodation", {})
                    # 주소 정보 가공 (주소가 객체인 경우 line_one 추출)
                    location_info = hotel.get("location", {})
                    address_data = location_info.get("address", {})
                    address_str = address_data.get("line_one", "Address Not Provided") if isinstance(address_data, dict) else str(address_data)

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