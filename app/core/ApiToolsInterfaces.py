from abc import ABC, abstractmethod
from typing import Any, Dict

class ApiTools(ABC):
    """
    interface ApiTools와 동일한 역할.
    모든 외부 API 어댑터는 이 규격을 반드시 준수해야 함.
    """
    
    @abstractmethod
    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        에이전트가 명령을 내리는 공통 실행 창구.
        :param action: 실행할 기능 명칭 (예: 'search_place', 'get_weather')
        :param params: 해당 기능에 필요한 파라미터 딕셔너리
        """
        pass

    @property
    @abstractmethod
    def tool_name(self) -> str:
        """도구 식별 이름 (예: 'google_maps')"""
        pass
