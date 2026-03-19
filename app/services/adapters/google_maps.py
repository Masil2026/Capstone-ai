from app.core.interfaces import ApiTools
from typing import Any, Dict

class GoogleMapsAdapter(ApiTools):
    @property
    def tool_name(self) -> str:
        return "google_maps"

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        # TODO: Google Cloud Console에서 API Key 발급 및 인증 로직 구현
        # 예시: params = {"origin": "서울", "dest": "부산", "mode": "transit"}
        
        if action == "find_route":
            # origin = params.get("origin")
            # dest = params.get("dest")
            # url = f"https://maps.googleapis.com/maps/api/directions/json?origin={origin}&destination={dest}&key={self.api_key}"
            return {"status": "success", "data": "구글맵 경로 데이터 (Mock)"}
            
        elif action == "search_place":
            # query = params.get("query")
            # url = f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={query}&key={self.api_key}"
            return {"status": "success", "data": "구글맵 장소 검색 데이터 (Mock)"}

        return {"status": "error", "message": f"지원하지 않는 액션: {action}"}
