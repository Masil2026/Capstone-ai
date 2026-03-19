from app.core.ApiToolsInterfaces import ApiTools
from typing import Any, Dict

class WeatherAdapter(ApiTools):
    @property
    def tool_name(self) -> str:
        return "weather"

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        # TODO: OpenWeatherMap 등 API 키 발급 및 인증 로직 구현
        # 예시: params = {"city": "Seoul", "units": "metric"}
        
        if action == "get_weather":
            # city = params.get("city")
            # url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={self.api_key}"
            return {"status": "success", "data": "OpenWeatherMap 날씨 데이터 (Mock)"}
            
        return {"status": "error", "message": f"지원하지 않는 액션: {action}"}
