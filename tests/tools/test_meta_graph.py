import json
import pytest
from app.services.adapters.meta_instagram import MetaGraphAdapter
from app.services.travel_agent_service import TravelAgentService

"""
Meta Graph API (Instagram) 반환값 확인 테스트
- 현재 Mock 모드 (USE_MOCK = True) 기준으로 작성
- 실제 API 연동 시 USE_MOCK = False 후 어댑터 테스트만 재실행
"""

@pytest.mark.asyncio
async def test_meta_graph_get_posts():
    # Given
    adapter = MetaGraphAdapter()
    service = TravelAgentService(adapter)
    
    # When
    result = await service.process_task(
        action="get_user_posts",
        params={"user_id": "test_user"}
    )
    
    # Then
    assert result["status"] == "success"
    assert "인스타그램" in result["data"]


@pytest.mark.asyncio
async def test_meta_graph_invalid_action():
    """에러 처리 — 지원하지 않는 액션 확인"""
    adapter = MetaGraphAdapter()
    service = TravelAgentService(adapter)

    result = await service.process_task(action="invalid", params={})

    assert result["status"] == "error"
    assert "지원하지 않는 액션" in result["message"]


@pytest.mark.asyncio
async def test_adapter_search_by_hashtag_basic():
    """해시태그 검색 기본 동작 및 반환값 구조 확인"""
    adapter = MetaGraphAdapter()
    result = await adapter.execute("search_by_hashtag", {
        "query": "오사카 여행",
        "max_results": 10,
    })

    print("\n[어댑터 해시태그 검색 - 정제된 결과]")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["status"] == "success"
    assert result["count"] > 0
    item = result["data"][0]
    assert "caption" in item
    assert "like_count" in item
    assert "timestamp" in item
    assert "permalink" in item


@pytest.mark.asyncio
async def test_adapter_search_by_hashtag_max_results():
    """max_results 파라미터 동작 확인"""
    adapter = MetaGraphAdapter()
    result = await adapter.execute("search_by_hashtag", {
        "query": "여행",
        "max_results": 2,
    })

    assert result["status"] == "success"
    assert result["count"] <= 2
    assert len(result["data"]) <= 2


@pytest.mark.asyncio
async def test_adapter_min_likes_filter():
    """인기도 기반 필터링 — min_likes 이상인 게시물만 반환 확인"""
    adapter = MetaGraphAdapter()
    result = await adapter.execute("search_by_hashtag", {
        "query": "오사카",
        "min_likes": 500, # 임시 설정값
    })

    assert result["status"] == "success"
    for post in result["data"]:
        assert post["like_count"] >= 500  # 500 미만은 없어야 함


@pytest.mark.asyncio
async def test_adapter_sorted_by_latest():
    """최신순 정렬 — timestamp 내림차순 확인"""
    adapter = MetaGraphAdapter()
    result = await adapter.execute("search_by_hashtag", {
        "query": "오사카",
    })

    assert result["status"] == "success"
    timestamps = [post["timestamp"] for post in result["data"]]
    assert timestamps == sorted(timestamps, reverse=True)  # 최신이 위에 있어야 함


@pytest.mark.asyncio
async def test_adapter_missing_query():
    """에러 처리 — query 누락 시 에러 반환 확인"""
    adapter = MetaGraphAdapter()
    result = await adapter.execute("search_by_hashtag", {})

    print("\n[query 누락 에러]")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["status"] == "error"
    assert "query" in result["message"]


@pytest.mark.asyncio
async def test_adapter_unsupported_action():
    """에러 처리 — 지원하지 않는 액션 확인"""
    adapter = MetaGraphAdapter()
    result = await adapter.execute("unknown_action", {})

    assert result["status"] == "error"
    assert "지원하지 않는 액션" in result["message"]