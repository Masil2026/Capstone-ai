import pytest
from app.services.adapters.google_maps import GoogleMapsAdapter
from app.services.travel_agent_service import TravelAgentService

@pytest.mark.asyncio
async def test_google_maps_find_route():
    # Given
    adapter = GoogleMapsAdapter()
    service = TravelAgentService(adapter)
    
    # When
    result = await service.process_task(
        action="find_route",
        params={"origin": "Seoul", "dest": "Busan"}
    )
    
    # Then
    assert result["status"] == "success"
    assert "구글맵" in result["data"]

@pytest.mark.asyncio
async def test_google_maps_invalid_action():
    adapter = GoogleMapsAdapter()
    service = TravelAgentService(adapter)
    
    result = await service.process_task(action="invalid", params={})
    
    assert result["status"] == "error"
    assert "지원하지 않는 액션" in result["message"]
