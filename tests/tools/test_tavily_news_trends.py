"""
Tavily 기사 및 인스타 트렌드 검색 테스트
- topic="news" 로 기사 특화 검색 확인
- 인스타 트렌드는 topic="general" + 쿼리 조합으로 확인
  (Instagram은 로그인 벽으로 Tavily가 직접 크롤링 불가 → 블로그/미디어 집계 결과 활용)
"""
import json
import pytest
from app.services.adapters.tavily_search import TavilySearchAdapter


# ───────────────────────────────────────────
# 기사 검색 — topic: "news"
# Credit 1 소모
# ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_tavily_search_news():
    """기사 검색 — topic=news로 뉴스 특화 결과 확인"""
    adapter = TavilySearchAdapter()
    result = await adapter.execute("search", {
        "query": "오사카 여행 최신 뉴스",
        "topic": "news",
        "max_results": 5,
    })

    print("\n[기사 검색 - topic=news]")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["status"] == "success"
    assert result["count"] > 0
    item = result["data"][0]
    assert "url" in item
    assert "title" in item
    assert "content" in item
    assert "score" in item


# ───────────────────────────────────────────
# 인스타 트렌드 검색 — topic: "general" + 쿼리 조합
# Credit 1 소모
# ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_tavily_search_instagram_trends():
    """인스타 트렌드 검색 — 쿼리로 인스타 관련 콘텐츠 집계 사이트 결과 확인"""
    adapter = TavilySearchAdapter()
    result = await adapter.execute("search", {
        "query": "오사카 여행 인스타 트렌드 핫플",
        "topic": "general",
        "max_results": 5,
    })

    print("\n[인스타 트렌드 검색 - topic=general + 쿼리 조합]")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["status"] == "success"
    assert result["count"] > 0
    item = result["data"][0]
    assert "url" in item
    assert "title" in item
    assert "content" in item
    assert "score" in item
