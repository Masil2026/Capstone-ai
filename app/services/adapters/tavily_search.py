import httpx
from app.core.ApiToolsInterfaces import ApiTools
from app.core.config import settings
from typing import Any, Dict


class TavilySearchAdapter(ApiTools):
    def __init__(self):
        self.api_key = settings.TAVILY_API_KEY
        self.base_url = "https://api.tavily.com/search"

    @property
    def tool_name(self) -> str:
        return "tavily_search"

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:

        # 1. 웹 검색 (Search)
        if action == "search":
            query = params.get("query")

            # 검증 로직
            if not query:
                return {"status": "error", "message": "query는 필수입니다."}

            # 2. 요청 페이로드 구성
            payload = {
                "api_key": self.api_key,
                "query": query,
                "search_depth": params.get("search_depth", "basic"),  # basic(크레딧 1) / advanced(크레딧 2)
                "max_results": int(params.get("max_results", 15)),  # ES 필터링 풀 확보를 위해 15개
                # topic: "general"(기본) / "news" — 뉴스 특화 검색 시 사용
                # include_domains: ["site.com"] — 특정 도메인만 검색 (신뢰 사이트 필터링)
                # exclude_domains: ["site.com"] — 특정 도메인 제외
                # include_images: 이미지는 Gemini 내장 Google Search 활용 예정으로 미사용
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    response = await client.post(self.base_url, json=payload)
                except httpx.TimeoutException:
                    # 네트워크 타임아웃 (30초 초과)
                    return {"status": "error", "message": "Tavily API 타임아웃 (30초 초과)"}

                # JSONDecodeError 방지를 위한 예외 처리 (서버 장애 등)
                try:
                    data = response.json()
                except Exception:
                    return {
                        "status": "error",
                        "message": f"API 응답이 JSON 형식이 아닙니다: {response.text[:100]}"
                    }

                # HTTP 오류 (4xx, 5xx)
                if response.status_code != 200:
                    return {"status": "error", "message": data.get("message", data.get("errors"))}

                # 3. 결과 데이터 정제
                # results는 ES 필터링 → Gemini Flash 전처리를 거쳐 정형화되므로 Tavily answer 미사용
                results = [
                    {
                        "url": r["url"],
                        "title": r["title"],
                        "content": r["content"],  # Tavily가 자동 truncate한 본문 요약
                        "score": r.get("score"),  # 검색 관련도 점수 (0~1)
                    }
                    for r in data.get("results", [])
                ]

                return {
                    "status": "success",
                    "count": len(results),
                    "data": results,
                }

        return {"status": "error", "message": f"지원하지 않는 액션: {action}"}
