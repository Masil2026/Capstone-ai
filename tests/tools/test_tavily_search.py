"""
Tavily Search API 반환값 확인 테스트
- 실제 API 호출로 반환값 구조 파악
- 어댑터 구현 전 raw 응답 확인 목적
- 어댑터 연결 테스트 포함
"""
import httpx
import json
import os
import pytest
from dotenv import load_dotenv
from app.services.adapters.tavily_search import TavilySearchAdapter

load_dotenv()

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
TAVILY_URL = "https://api.tavily.com/search"


# ───────────────────────────────────────────
# 기본 검색 테스트
# Credit 1 소모
# ───────────────────────────────────────────
def test_tavily_basic_search():
    """기본 검색 반환값 구조 확인"""
    response = httpx.post(TAVILY_URL, json={
        "api_key": TAVILY_API_KEY,
        "query": "오사카 여행 명소",       # 검색 쿼리
        "search_depth": "basic",           # basic(빠름) / advanced(정확, 크레딧 2배 소모)
        "max_results": 5,                  # 반환할 결과 수
        # include_raw_content: True 추가 시 content 전체 본문 반환
        # 기본값 False → content 필드는 Tavily가 자동 truncate, 결과에 [...] 표시됨
        #
        # 추가 파라미터 (어댑터 구현 시 필요에 따라 사용)
        # topic: "general"(기본) / "news" - 뉴스 특화 검색
        # include_domains: ["site.com"] - 특정 도메인만 검색 (신뢰 사이트 필터링)
        # exclude_domains: ["site.com"] - 특정 도메인 제외
        # include_images: 이미지는 GPT-4o Vision으로 처리하므로 미사용
    })
    assert response.status_code == 200
    data = response.json()

    print("\n[Tavily 기본 검색 - Raw JSON]")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    assert "results" in data
    assert len(data["results"]) > 0


# ───────────────────────────────────────────
# advanced 검색 테스트
# Credit 2 소모
# ───────────────────────────────────────────
def test_tavily_advanced_search():
    """advanced 검색 반환값 구조 확인 (크레딧 2배 소모)"""
    response = httpx.post(TAVILY_URL, json={
        "api_key": TAVILY_API_KEY,
        "query": "오사카 여행 명소",
        "search_depth": "advanced",        # 더 정확한 결과, 크레딧 2배 소모
        "max_results": 5,
    })
    assert response.status_code == 200
    data = response.json()

    print("\n[Tavily advanced 검색 - Raw JSON]")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    assert "results" in data
    assert len(data["results"]) > 0


# ───────────────────────────────────────────
# include_answer 옵션 테스트
# Credit 1 소모
# ───────────────────────────────────────────
def test_tavily_with_answer():
    """include_answer 옵션 - Tavily가 검색 결과 요약 답변 생성"""
    response = httpx.post(TAVILY_URL, json={
        "api_key": TAVILY_API_KEY,
        "query": "오사카 3박 4일 여행 일정",
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": True,            # 검색 결과를 요약한 답변 포함 여부
    })
    assert response.status_code == 200
    data = response.json()

    print("\n[Tavily include_answer - Raw JSON]")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    assert "results" in data
    assert "answer" in data


# ───────────────────────────────────────────
# 어댑터 연결 테스트
# ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_adapter_search_basic():
    """어댑터 기본 검색 — 반환값 구조 확인"""
    adapter = TavilySearchAdapter()
    result = await adapter.execute("search", {
        "query": "오사카 여행 명소",
    })

    print("\n[어댑터 기본 검색 - 정제된 결과]")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["status"] == "success"
    assert result["count"] > 0
    assert "data" in result
    item = result["data"][0]
    assert "url" in item
    assert "title" in item
    assert "content" in item
    assert "score" in item


@pytest.mark.asyncio
async def test_adapter_search_missing_query():
    """어댑터 에러 처리 — query 누락 시 에러 반환 확인"""
    adapter = TavilySearchAdapter()
    result = await adapter.execute("search", {})

    print("\n[query 누락 에러]")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_adapter_unsupported_action():
    """어댑터 에러 처리 — 지원하지 않는 액션 확인"""
    adapter = TavilySearchAdapter()
    result = await adapter.execute("unknown_action", {})

    assert result["status"] == "error"
