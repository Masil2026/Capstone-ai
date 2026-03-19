from app.core.ApiToolsInterfaces import ApiTools
from typing import Any, Dict

class FlightAdapter(ApiTools):
    @property
    def tool_name(self) -> str:
        return "flight"

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        # TODO: Amadeus/Skyscanner 등 API 키 발급 및 인증 로직 구현
        # 예시: params = {"origin": "ICN", "destination": "NRT", "departureDate": "2024-12-01"}
        
        if action == "search_flight":
            # origin = params.get("origin")
            # dest = params.get("destination")
            # response = await self.client.get_flights(origin, dest, date)
            return {"status": "success", "data": "항공권 검색 데이터 (Mock)"}
            
        return {"status": "error", "message": f"지원하지 않는 액션: {action}"}
