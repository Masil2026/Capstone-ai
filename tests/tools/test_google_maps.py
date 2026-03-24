import pytest
from unittest.mock import patch, Mock

from app.services.adapters.google_maps import GoogleMapsAdapter
from app.services.travel_agent_service import TravelAgentService


@pytest.mark.asyncio
async def test_google_maps_find_route():
    adapter = GoogleMapsAdapter()
    service = TravelAgentService(adapter)

    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": "OK",
        "routes": [
            {
                "summary": "경부고속도로",
                "legs": [
                    {
                        "start_address": "Seoul, South Korea",
                        "end_address": "Busan, South Korea",
                        "distance": {"text": "325 km"},
                        "duration": {"text": "4시간 10분"},
                        "steps": [
                            {
                                "html_instructions": "서울에서 출발",
                                "distance": {"text": "10 km"},
                                "duration": {"text": "15분"},
                                "travel_mode": "DRIVING"
                            }
                        ]
                    }
                ]
            }
        ]
    }

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await service.process_task(
            action="find_route",
            params={"origin": "Seoul", "dest": "Busan"}
        )

    assert result["status"] == "success"
    assert result["data"]["type"] == "구글맵 경로 데이터"
    assert result["data"]["count"] == 1
    assert result["data"]["routes"][0]["start_address"] == "Seoul, South Korea"
    assert result["data"]["routes"][0]["end_address"] == "Busan, South Korea"


@pytest.mark.asyncio
async def test_google_maps_search_place():
    adapter = GoogleMapsAdapter()
    service = TravelAgentService(adapter)

    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": "OK",
        "results": [
            {
                "name": "스타벅스 강남점",
                "formatted_address": "서울특별시 강남구 ...",
                "place_id": "mock_place_id_123",
                "geometry": {
                    "location": {
                        "lat": 37.4979,
                        "lng": 127.0276
                    }
                },
                "rating": 4.2,
                "user_ratings_total": 150,
                "types": ["cafe", "food", "point_of_interest", "establishment"]
            }
        ]
    }

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await service.process_task(
            action="search_place",
            params={"query": "스타벅스 강남"}
        )

    assert result["status"] == "success"
    assert result["data"]["type"] == "구글맵 장소 검색 데이터"
    assert result["data"]["count"] == 1
    assert result["data"]["places"][0]["name"] == "스타벅스 강남점"


@pytest.mark.asyncio
async def test_google_maps_invalid_action():
    adapter = GoogleMapsAdapter()
    service = TravelAgentService(adapter)

    result = await service.process_task(action="invalid", params={})

    assert result["status"] == "error"
    assert "지원하지 않는 액션" in result["message"]


@pytest.mark.asyncio
async def test_google_maps_find_route_missing_params():
    adapter = GoogleMapsAdapter()
    service = TravelAgentService(adapter)

    result = await service.process_task(
        action="find_route",
        params={"origin": "Seoul"}
    )

    assert result["status"] == "error"
    assert "origin과 dest는 필수입니다." in result["message"]


@pytest.mark.asyncio
async def test_google_maps_search_place_missing_query():
    adapter = GoogleMapsAdapter()
    service = TravelAgentService(adapter)

    result = await service.process_task(
        action="search_place",
        params={}
    )

    assert result["status"] == "error"
    assert "query는 필수입니다." in result["message"]
    