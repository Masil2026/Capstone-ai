import pytest
from app.services.adapters.flight_api import FlightAdapter
from app.services.travel_agent_service import TravelAgentService

@pytest.mark.asyncio
async def test_flight_search():
    # Given
    adapter = FlightAdapter()
    service = TravelAgentService(adapter)
    
    # When
    result = await service.process_task(
        action="search_flight",
        origin="ICN",
        destination="NRT"
    )
    
    # Then
    assert result["status"] == "success"
    assert "항공권" in result["data"]
