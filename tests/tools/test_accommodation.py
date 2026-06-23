from datetime import date, timedelta

import pytest
from app.services.adapters.accommodation_api import AccommodationAdapter
from app.services.travel_agent_service import TravelAgentService


def _future_date(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()

def _print_hotel_results(test_name, result):
    print("\n" + "="*65)
    print(f"[{test_name}] - RESULT STATUS: {result['status']}")
    print("-" * 65)
    
    if result["status"] == "success":
        print(f"FOUND: {result.get('count', 0)} hotels")
        print("-" * 65)
        # 헤더: 번호 | 호텔명 | 가격 | 별점 | 주소
        print(f"{'No':<3} | {'Hotel Name':<25} | {'Price':<15} | {'Star':<5} | {'Address'}")
        print("-" * 65)
        
        for i, hotel in enumerate(result.get("data", []), 1):
            name = hotel.get('name', 'N/A')[:23]
            orig = hotel.get('price_original')
            cur  = hotel.get('currency', '')
            krw  = hotel.get('price_krw')
            if orig is not None:
                price = f"{orig:.0f} {cur}"
                if krw:
                    price += f" / ₩{int(krw):,}"
            else:
                price = 'N/A'
            star = hotel.get('rating', '-')
            addr = hotel.get('address', 'N/A')[:40]

            print(f"{i:<3} | {name:<25} | {price:<25} | {star:<5} | {addr}")
    
    else:
        # 에러 메시지가 리스트일 경우를 대비해 상세히 출력
        msg = result.get('message')
        print(f"ERROR DETAIL: {msg}")
    
    print("="*65 + "\n")

@pytest.mark.asyncio
async def test_hotel_search_with_child():
    """[search_hotels] 아이 포함 숙소 검색 테스트 (도시명 사용)"""
    adapter = AccommodationAdapter()
    service = TravelAgentService({"duffel_accommodation": adapter})

    params = {
        "city_name": "Seoul",
        "check_in": _future_date(30),
        "check_out": _future_date(33),
        "rooms": 1,
        "adults": 2,
        "children": 1,
        "child_ages": [7]
    }

    print(f"\n[Test 1] Searching Hotels in {params['city_name']} for {params['adults']} Adults & {params['children']} Child")

    result = await service.process_task("duffel_accommodation", "search_hotels", params)
    
    # 결과 출력
    _print_hotel_results("DUFFEL HOTEL SEARCH", result)
    
    # 검증
    assert result["status"] == "success"
    assert isinstance(result["data"], list)
    assert len(result["data"]) <= 10 # 어댑터에서 10개로 제한했으므로


@pytest.mark.asyncio
async def test_hotel_validation_error():
    """[search_hotels] 아이 인원수 불일치 에러 테스트"""
    adapter = AccommodationAdapter()
    service = TravelAgentService({"duffel_accommodation": adapter})

    children_count = 2
    child_ages_list = [10]

    # 아이는 1명인데 나이 정보를 안 보냈을 때
    invalid_params = {
        "city_name": "Tokyo",
        "check_in": _future_date(30),
        "check_out": _future_date(33),
        "children": children_count,
        "child_ages": child_ages_list
    }

    result = await service.process_task("duffel_accommodation", "search_hotels", invalid_params)
    expected_message = f"아이 인원({children_count}명)과 나이 정보({len(child_ages_list)}개)의 개수가 일치하지 않습니다."
    
    assert result["status"] == "error"
    assert result["message"] == expected_message


@pytest.mark.asyncio
async def test_hotel_search_by_city_name():
    """[search_hotels] 도시명(osaka) 기반 좌표 추출 및 숙소 검색 테스트"""
    adapter = AccommodationAdapter()
    service = TravelAgentService({"duffel_accommodation": adapter})

    params = {
        "city_name": "osaka",
        "check_in": _future_date(30),
        "check_out": _future_date(33),
        "rooms": 1,
        "adults": 1
    }

    print(f"\n[Test 3] Searching Hotels in {params['city_name']} using coordinate extraction")

    result = await service.process_task("duffel_accommodation", "search_hotels", params)

    # 결과 출력
    _print_hotel_results("DUFFEL CITY NAME SEARCH", result)

    # 검증
    assert result["status"] == "success"
    if result["count"] > 0:
        assert isinstance(result["data"], list)
        assert len(result["data"]) <= 10
    else:
        # 검색 결과가 없는 경우도 정상적인 성공 응답으로 처리됨 (Count 0)
        assert result.get("message") == "검색 결과가 없습니다."
