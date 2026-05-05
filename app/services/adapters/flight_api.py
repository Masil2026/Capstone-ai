import httpx
from app.core.ApiToolsInterfaces import ApiTools
from app.core.config import settings
from typing import Any, Dict

class FlightAdapter(ApiTools):
    def __init__(self):
        self.api_key = settings.DUFFEL_API_KEY.strip()
        self.base_url = "https://api.duffel.com/air"

    @property
    def tool_name(self) -> str:
        return "duffel_flight"

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Duffel-Version": "v2",  # 필수 헤더
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _get_iata_code(self, query: str) -> str:
        """Duffel Places Suggestions API를 사용하여 도시명을 IATA 코드로 변환합니다."""
        url = "https://api.duffel.com/places/suggestions"
        headers = self._get_headers()
        params = {"query": query}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers, params=params)
        except (httpx.TimeoutException, httpx.RequestError):
            return None

        try:
            data = response.json()
        except Exception:
            return None

        if response.status_code != 200 or not data.get("data"):
            return None

        return data["data"][0].get("iata_code")

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:

        # 1. 항공권 검색 요청 (Offer Request)
        if action == "search_flights":
            url = f"{self.base_url}/offer_requests"
            
            # 1. 필수 파라미터 추출
            origin_query = params.get("origin")
            dest_query = params.get("destination")
            departure_date = params.get("departure_date")
            adults = int(params.get("adults", 1))
            children = int(params.get("children", 0))  # 기본값 0
            child_ages = params.get("child_ages", [])

            # 2. 검증 로직: 필수 값 체크
            if not all([origin_query, dest_query, departure_date]):
                return {
                    "status": "error",
                    "message": "[Duffel API] origin, destination, departure_date 정보가 필수입니다."
                }

            # 3. IATA 코드 변환 (도시명 -> 코드)
            # 입력값이 이미 3자리 대문자 IATA 코드 형태라면 그대로 사용하고, 아니면 검색 수행
            origin = origin_query.upper() if len(origin_query) == 3 and origin_query.isalpha() else await self._get_iata_code(origin_query)
            destination = dest_query.upper() if len(dest_query) == 3 and dest_query.isalpha() else await self._get_iata_code(dest_query)

            if not origin or not destination:
                return {
                    "status": "error",
                    "message": f"[Duffel API] IATA 코드를 찾을 수 없습니다. (출발지: {origin_query}, 도착지: {dest_query})"
                }

            # 4. 검증 로직: 아동 인원과 나이 정보 개수 일치 여부
            if children > 0 and len(child_ages) != children:
                return {
                    "status": "error", 
                    "message": f"아이 인원({children}명)과 나이 정보({len(child_ages)}개)의 개수가 일치하지 않습니다."
                }

            # 4. 승객(Passengers) 리스트 구성
            passengers = []

            # 성인은 age 없이 type만 보냅니다.
            for _ in range(adults):
                passengers.append({"type": "adult"})

            # 아동은 type 없이 age만 보냅니다. (Duffel 최신 규격 반영)
            if children > 0:
                for age in child_ages:
                    passengers.append({"age": int(age)})

            # 5. API 요청 페이로드 작성
            payload = {
                "data": {
                    "slices": [
                        {
                            "origin": origin,
                            "destination": destination,
                            "departure_date": departure_date
                        }
                    ],
                    "passengers": passengers,
                    "cabin_class": params.get("cabin_class", "economy")
                }
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=self._get_headers(), json=payload)
                data = response.json()
                
                if response.status_code != 201:
                    return {"status": "error", "message": data.get("errors")}

                offers = data.get("data", {}).get("offers", [])
                
                processed_results = []
                for offer in offers[:10]: # 항공편 중 상위 10개만 뽑음
                    segments = offer["slices"][0]["segments"]
                    processed_results.append({
                        "offer_id": offer["id"],
                        "total_amount": f"{offer['total_amount']} {offer['total_currency']}",
                        "airline": offer["owner"]["name"],
                        "origin": segments[0]["origin"]["iata_code"],
                        "destination": segments[-1]["destination"]["iata_code"],
                        "departing_at": segments[0]["departing_at"],
                        "arriving_at": segments[-1]["arriving_at"],
                        "stops": len(segments) - 1, # 경유 횟수 (0개면 직항, 1개면 1회 경유)
                    })

                return {
                    "status": "success",
                    "count": len(processed_results),
                    "data": processed_results
                }
        
        # --- [TODO: 나중에 구현할 항공 관련 액션들] ---
        # 1. get_offer_details: 선택한 항공권의 상세 정보 및 수하물 규정 확인
        # 2. create_order: 실제 항공권 예약 및 결제 요청
        # 3. get_booking_details: 예약 번호로 예약 상태 조회
        # 4. cancel_booking: 예약 취소 요청
        elif action in ["get_offer_details", "create_order", "get_booking_details", "cancel_booking"]:
            return {
                "status": "todo",
                "message": f"[Duffel Flight] '{action}' 기능은 현재 개발 중(TODO)입니다. 예약 프로세스 설계 후 업데이트 예정입니다."
            }

        return {"status": "error", "message": f"지원하지 않는 액션: {action}"}