import httpx
from app.core.ApiToolsInterfaces import ApiTools
from app.core.config import settings
from app.services.adapters.currency_converter import to_krw
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

        print(f"\n[FlightAdapter] Places Suggestions 요청: query='{query}'")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers, params=params)
        except httpx.TimeoutException:
            print(f"[FlightAdapter] Places Suggestions 타임아웃: query='{query}'")
            return None
        except httpx.RequestError as e:
            print(f"[FlightAdapter] Places Suggestions 요청 실패: {e}")
            return None

        print(f"[FlightAdapter] Places Suggestions HTTP Status: {response.status_code}")
        try:
            data = response.json()
        except Exception:
            print(f"[FlightAdapter] Places Suggestions JSON 파싱 실패: {response.text[:200]}")
            return None

        if response.status_code != 200 or not data.get("data"):
            print(f"[FlightAdapter] Places Suggestions 결과 없음: {data}")
            return None

        suggestions = data["data"]

        # 1. 공항 타입이면 바로 반환
        for s in suggestions:
            if s.get("type") == "airport":
                iata = s.get("iata_code")
                if iata:
                    print(f"[FlightAdapter] Places Suggestions 결과: '{query}' → IATA={iata} (airport)")
                    return iata

        # 2. 도시 타입이면 포함된 공항 중 도시명 일치 공항을 우선 반환
        for s in suggestions:
            if s.get("type") == "city":
                airports = s.get("airports", [])
                if airports:
                    target_city = s.get("name")
                    matching = [a for a in airports if a.get("city_name") == target_city]
                    best = matching[0] if matching else airports[0]
                    iata = best.get("iata_code")
                    if iata:
                        print(f"[FlightAdapter] Places Suggestions 결과: '{query}' → IATA={iata} (city→airport {best.get('name')})")
                        return iata

        # 3. fallback: 첫 번째 결과
        iata = suggestions[0].get("iata_code")
        print(f"[FlightAdapter] Places Suggestions 결과: '{query}' → IATA={iata} (fallback)")
        return iata

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

            print(f"\n[FlightAdapter] search_flights 요청")
            print(f"  origin_query={origin_query!r} → IATA={origin}")
            print(f"  dest_query={dest_query!r}   → IATA={destination}")
            print(f"  departure_date={departure_date}, passengers={passengers}")
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=self._get_headers(), json=payload)
                print(f"[FlightAdapter] search_flights HTTP Status: {response.status_code}")
                data = response.json()

                if response.status_code != 201:
                    print(f"[FlightAdapter] search_flights 오류: {data.get('errors')}")
                    return {"status": "error", "message": data.get("errors")}

                offers = data.get("data", {}).get("offers", [])
                print(f"[FlightAdapter] search_flights 결과: {len(offers)}개 offers")
                # Duffel Airways는 테스트용 가상 항공사이므로 제외, 결과 없으면 fallback으로 포함
                real_offers = [o for o in offers if o.get("owner", {}).get("name") != "Duffel Airways"]
                print(f"[FlightAdapter] Duffel Airways 제외 후: {len(real_offers)}개 offers")
                is_duffel_fallback = False
                if not real_offers and offers:
                    real_offers = offers
                    is_duffel_fallback = True
                    print(f"[FlightAdapter] 실제 항공사 결과 없음 → Duffel Airways fallback 사용 ({len(real_offers)}개)")
                processed_results = []
                for offer in real_offers[:10]:
                    segments = offer["slices"][0]["segments"]
                    orig_amount = float(offer["total_amount"])
                    orig_currency = offer["total_currency"]
                    price_krw = await to_krw(orig_amount, orig_currency)
                    processed_results.append({
                        "offer_id": offer["id"],
                        "price_original": orig_amount,   # 현지 통화 금액
                        "currency": orig_currency,        # 현지 통화 코드
                        "price_krw": price_krw,           # 한화 환산 금액
                        "airline": offer["owner"]["name"],
                        "origin": segments[0]["origin"]["iata_code"],
                        "destination": segments[-1]["destination"]["iata_code"],
                        "departing_at": segments[0]["departing_at"],
                        "arriving_at": segments[-1]["arriving_at"],
                        "stops": len(segments) - 1, # 경유 횟수 (0개면 직항, 1개면 1회 경유)
                    })
                processed_results.sort(key=lambda x: (x["stops"], x["price_krw"]))

                return {
                    "status": "success",
                    "count": len(processed_results),
                    "is_duffel_fallback": is_duffel_fallback,
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