from app.core.ApiToolsInterfaces import ApiTools
from typing import Any, Dict

class TravelAgentService:
    """
    Service 계층.
    특정 API에 종속되지 않고 ApiTools 인터페이스만 사용하여 작업을 수행함 (DIP).
    """
    def __init__(self, tools: Dict[str, ApiTools]):
        self.tools = tools

    async def process_task(self, tool_name: str, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        에이전트의 요청을 받아 tool_name에 해당하는 도구를 실행함.
        """
        tool = self.tools.get(tool_name)
        if tool is None:
            return {"status": "error", "message": f"지원하지 않는 도구: {tool_name}"}
        print(f"[TravelAgentService] tool={tool_name}, action={action}")
        return await tool.execute(action, params)
