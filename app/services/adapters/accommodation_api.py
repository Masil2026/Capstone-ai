import os
import time
import hashlib
import httpx
from app.core.ApiToolsInterfaces import ApiTools
from app.core.config import settings
from typing import Any, Dict

class AccommodationAdapter(ApiTools):
    def __init__(self):
        self.api_key = settings.HOTELBEDS_API_KEY
        self.secret = settings.HOTELBEDS_SECRET
        self.base_url = settings.HOTELBEDS_BASE_URL

    @property
    def tool_name(self) -> str:
        return "hotelbeds_accommodation"
    
    # sha256 서명 생성
    def _generate_signature(self) -> str:
        timestamp = int(time.time())
        payload = f"{self.api_key}{self.secret}{timestamp}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Api-Key": self.api_key,
            "X-Signature": self._generate_signature(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        
        # --- [액션 1: 도시별 숙소 검색 및 정렬(가격 기준 or 평점 기준)] ---
        if action == "search_hotels":
            url = f"{self.base_url}/hotel-api/1.0/hotels"

            # 파라미터 목록
            check_in = params.get("check_in")
            check_out = params.get("check_out")
            city_code = params.get("city_code")
            rooms = params.get("rooms")
            adults = params.get("adults")
            children = params.get("children", 0)
            child_ages = params.get("child_ages", [])
            sort_by = params.get("sort_by") # price 또는 rating (정렬 기준)

            if not all([check_in, check_out, city_code, rooms, adults]):
                return {
                    "status": "error", 
                    "message": "[HotelBeds API] check_in, check_out, city_code, rooms, adults 정보가 모두 필요합니다."
                }
            
            if children > 0 and len(child_ages) != children:
                return {"status": "error", "message": f"아이 인원({children}명)과 나이 정보({len(child_ages)}개)의 개수가 일치하지 않습니다."}
            
            occupancy = {
                "rooms": int(rooms),
                "adults": int(adults),
                "children": children
            }

            # 아이가 있다면 나이(paxes) 정보 반드시 포함
            if children > 0:
                occupancy["paxes"] = [{"type": "CH", "age": int(age)} for age in child_ages]
            
            # 사용자로부터 입력받는 동적 파라미터 구성
            payload = {
                "stay": {"checkIn": check_in, "checkOut": check_out},
                "occupancies": [occupancy],
                "destination": {"code": city_code},
                "filter": {"maxHotels": params.get("max_hotels", 50)} # 호텔 50개를 뽑아서, 처리함
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=self._get_headers(), json=payload)
                data = response.json()
                print(f"RAW DATA: {data}")
                all_hotels = data.get("hotels", {}).get("hotels", [])

                if sort_by == "price":
                    # 최저가 순 정렬
                    sorted_hotels = sorted(all_hotels, key=lambda x: float(x.get('minRate', 999999)))
                elif sort_by == "rating":
                    # 평점 순 정렬 (Hotelbeds는 1-5점 star rating 제공)
                    # 데이터가 문자열일 수 있어 float 변환 필요
                    sorted_hotels = sorted(all_hotels, key=lambda x: float(x.get('categoryCode', '0').replace('EST', '')), reverse=True)
                else:
                    sorted_hotels = all_hotels

                return {
                    "status": "success",
                    "count": len(sorted_hotels[:50]),
                    "data": sorted_hotels[:50] # 상위 50개만 반환
                }

        # --- [TODO: 나중에 구현할 액션들] ---
        elif action in ["create_booking", "get_booking_details", "cancel_booking"]:
            return {
                "status": "todo",
                "message": f"'{action}' 기능은 현재 개발 중(TODO)입니다. DB 연동 후 업데이트 예정입니다."
            }
            
        return {"status": "error", "message": f"지원하지 않는 액션: {action}"}
