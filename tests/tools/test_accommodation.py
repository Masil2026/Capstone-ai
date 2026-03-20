import pytest
import json
from app.services.adapters.accommodation_api import AccommodationAdapter
from app.services.travel_agent_service import TravelAgentService

@pytest.mark.asyncio
async def test_accommodation_search():
    """
    search_hotels 액션 테스트 (children : 0)
    """

    # Given
    adapter = AccommodationAdapter()
    service = TravelAgentService(adapter)
    
    # 실제 어댑터의 파라미터 규격에 맞게 구성
    task_params = {
        "city_code": "TYO",        # 도쿄
        "check_in": "2026-04-01",
        "check_out": "2026-04-05",
        "rooms": 1,
        "adults": 2,
        "children": 0,
        "sort_by": "price"         # 최저가순 정렬 테스트
    }
    
    # When
    result = await service.process_task(
        action="search_hotels",
        params=task_params
    )

    # --- [응답 찍어보기] ---
    print("\n" + "="*50)
    print("API RESPONSE STATUS:", result.get("status"))
    print("TOTAL COUNT:", result.get("count"))

    if result.get("status") == "success" and result.get("data"):
        # 첫 번째 호텔 데이터만 샘플로 출력 (너무 길면 보기 힘드니까요)
        sample_hotel = result["data"][0]
        print("\n[SAMPLE HOTEL DATA]:")
        print(json.dumps(sample_hotel, indent=2, ensure_ascii=False))
    else:
        print("\n[ERROR/EMPTY DATA]:", result.get("message"))
    print("="*50 + "\n")
    
    # Then
    assert result["status"] == "success"
    # 'data'가 리스트인지 확인하는 것이 더 정확합니다.
    assert isinstance(result["data"], list)
    
    # Then
    # assert result["status"] == "success"
    # assert "숙소" in result["data"]
