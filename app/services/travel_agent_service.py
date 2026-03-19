from app.core.ApiToolsInterfaces import ApiTools
from typing import Any, Dict

class TravelAgentService:
    """
    Service 계층.
    특정 API에 종속되지 않고 ApiTools 인터페이스만 사용하여 작업을 수행함 (DIP).
    """
    def __init__(self, tool: ApiTools):
        self.tool = tool

    async def process_task(self, action: str, **kwargs) -> Dict[str, Any]:
        """
        에이전트의 요청을 받아 적절한 도구를 실행함.
        """
        print(f"[TravelAgentService] Processing task: {action} with tool: {self.tool.tool_name}")
        return await self.tool.execute(action, kwargs)
