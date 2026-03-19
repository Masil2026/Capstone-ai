import pytest
from app.services.adapters.accommodation_api import AccommodationAdapter
from app.services.travel_agent_service import TravelAgentService

@pytest.mark.asyncio
async def test_accommodation_search():
    # Given
    adapter = AccommodationAdapter()
    service = TravelAgentService(adapter)
    
    # When
    result = await service.process_task(
        action="search_hotel",
        city="Tokyo"
    )
    
    # Then
    assert result["status"] == "success"
    assert "숙소" in result["data"]
