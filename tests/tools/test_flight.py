import pytest
from app.services.adapters.flight_api import FlightAdapter
from app.services.travel_agent_service import TravelAgentService

def _print_flight_results(test_name, result):
    print("\n" + "="*65)
    print(f"[{test_name}] - RESULT STATUS: {result['status']}")
    
    if result["status"] == "success":
        print(f"TOTAL FOUND: {result.get('count', 0)} offers")
        print("-" * 65)
        print(f"{'No':<3} | {'Airline':<18} | {'Price':<12} | {'Stops':<7} | {'Departure':<20} | {'Arrival':<20}")
        print("-" * 65)
        
        for i, flight in enumerate(result.get("data", []), 1):
            stop_text = "Direct" if flight['stops'] == 0 else f"{flight['stops']} Stop"
            print(f"{i:<3} | {flight['airline']:<18} | {flight['total_amount']:<12} | {stop_text:<7} | {flight['departing_at']}")
    
    print("="*65 + "\n")


@pytest.mark.asyncio
async def test_flight_search_with_child():
    """[search_flights] 아이 포함 검색 테스트"""
    adapter = FlightAdapter()
    service = TravelAgentService(adapter)
    
    params = {
        "origin": "ICN", "destination": "NRT",
        "departure_date": "2026-05-15",
        "adults": 1, "children": 1, "child_ages": [5]
    }

    result = await service.process_task(action="search_flights", params=params)
    
    # 결과 출력
    # _print_flight_results("CHILD INCLUDED SEARCH", result)
    
    assert result["status"] == "success"
    assert isinstance(result["data"], list)
    assert len(result["data"]) > 0
    assert len(result["data"]) <= 10


@pytest.mark.asyncio
async def test_flight_search_adults_only():
    """[search_flights] 성인만으로 검색 테스트"""
    adapter = FlightAdapter()
    service = TravelAgentService(adapter)
    
    params = {
        "origin": "ICN", "destination": "NRT",
        "departure_date": "2026-06-20",
        "adults": 2
    }

    result = await service.process_task(action="search_flights", params=params)
    
    # 결과 출력
    # _print_flight_results("ADULTS ONLY SEARCH", result)
    
    assert result["status"] == "success"
    assert isinstance(result["data"], list)
    assert len(result["data"]) > 0
    assert len(result["data"]) <= 10


@pytest.mark.asyncio
async def test_flight_validation_error():
    """[search_flights] 유효성 검사 에러 테스트(아이는 2명인데, 나이를 1개만 입력할 시에 에러)"""
    adapter = FlightAdapter()
    service = TravelAgentService(adapter)
    
    children_count = 2
    child_ages_list = [10]
    
    invalid_params = {
        "origin": "ICN", 
        "destination": "NRT",
        "departure_date": "2026-06-15",
        "children": children_count, 
        "child_ages": child_ages_list
    }

    result = await service.process_task(action="search_flights", params=invalid_params)
    
    assert result["status"] == "error"
    expected_message = f"아이 인원({children_count}명)과 나이 정보({len(child_ages_list)}개)의 개수가 일치하지 않습니다."
    assert result["message"] == expected_message