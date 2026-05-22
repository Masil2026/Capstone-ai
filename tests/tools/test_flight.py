from datetime import date, timedelta

import pytest
from app.services.adapters.flight_api import FlightAdapter
from app.services.travel_agent_service import TravelAgentService


def _future_departure_date(days: int = 30) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _print_flight_results(test_name, result):
    print("\n" + "="*105)
    print(f"[{test_name}] - RESULT STATUS: {result['status']}")
    if result.get("is_duffel_fallback"):
        print("⚠️  DUFFEL FALLBACK: 실제 항공사 없음 — Duffel Airways(테스트용) 결과 사용 중")

    if result["status"] == "success":
        print(f"TOTAL FOUND: {result.get('count', 0)} offers")
        print("-" * 105)
        print(f"{'No':<3} | {'Airline':<18} | {'Route':<11} | {'Price':<30} | {'Stops':<7} | {'Departure':<22} | {'Arrival':<22} | {'Duration'}")
        print("-" * 105)

        for i, flight in enumerate(result.get("data", []), 1):
            stop_text = "Direct" if flight['stops'] == 0 else f"{flight['stops']} Stop"
            route = f"{flight['origin']}->{flight['destination']}"
            price_str = f"{flight['currency']} {flight['price_original']} ({flight.get('price_krw', '?'):,}원)"
            duration = flight.get('duration', '?')
            print(f"{i:<3} | {flight['airline']:<18} | {route:<11} | {price_str:<30} | {stop_text:<7} | {flight['departing_at']:<22} | {flight['arriving_at']:<22} | {duration}")

    print("="*105 + "\n")


@pytest.mark.asyncio
async def test_flight_search_with_child():
    """[search_flights] 아이 포함 검색 테스트"""
    adapter = FlightAdapter()
    service = TravelAgentService({"duffel_flight": adapter})

    params = {
        "origin": "london", "destination": "zurich",
        "departure_date": "2026-12-24",
        "adults": 2, "children": 1, "child_ages": [7]
    }

    result = await service.process_task("duffel_flight", "search_flights", params)
    
    # 결과 출력
    _print_flight_results("CHILD INCLUDED SEARCH", result)
    
    assert result["status"] == "success"
    assert isinstance(result["data"], list)
    assert len(result["data"]) > 0


@pytest.mark.asyncio
async def test_flight_search_adults_only():
    """[search_flights] 성인만으로 검색 테스트"""
    adapter = FlightAdapter()
    service = TravelAgentService({"duffel_flight": adapter})

    params = {
        "origin": "incheon", "destination": "canada",
        "departure_date": _future_departure_date(),
        "adults": 1
    }

    result = await service.process_task("duffel_flight", "search_flights", params)
    
    # 결과 출력
    _print_flight_results("ADULTS ONLY SEARCH", result)
    
    assert result["status"] == "success"
    assert isinstance(result["data"], list)
    assert len(result["data"]) > 0


@pytest.mark.asyncio
async def test_flight_validation_error():
    """[search_flights] 유효성 검사 에러 테스트(아이는 2명인데, 나이를 1개만 입력할 시에 에러)"""
    adapter = FlightAdapter()
    service = TravelAgentService({"duffel_flight": adapter})

    children_count = 2
    child_ages_list = [10]

    invalid_params = {
        "origin": "seoul",
        "destination": "tokyo",
        "departure_date": "2026-06-15",
        "children": children_count,
        "child_ages": child_ages_list
    }

    result = await service.process_task("duffel_flight", "search_flights", invalid_params)
    expected_message = f"아이 인원({children_count}명)과 나이 정보({len(child_ages_list)}개)의 개수가 일치하지 않습니다."
    
    assert result["status"] == "error"
    assert result["message"] == expected_message


@pytest.mark.asyncio
async def test_flight_search_with_city_names():
    """[search_flights] 도시명(seoul, osaka)을 입력했을 때 IATA 코드로 자동 변환되어 검색되는지 테스트"""
    adapter = FlightAdapter()
    service = TravelAgentService({"duffel_flight": adapter})

    # IATA 코드 대신 도시명을 입력
    params = {
        "origin": "seoul",
        "destination": "osaka",
        "departure_date": "2026-06-25",
        "adults": 1
    }

    result = await service.process_task("duffel_flight", "search_flights", params)
    
    # 결과 출력 (필요 시 주석 해제)
    _print_flight_results("CITY NAME SEARCH (seoul -> osaka)", result)
    
    assert result["status"] == "success"
    assert isinstance(result["data"], list)
    assert len(result["data"]) > 0
