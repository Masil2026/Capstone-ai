import pytest
from app.services.adapters.tavily_search import TavilySearchAdapter
from app.services.travel_agent_service import TravelAgentService

@pytest.mark.asyncio
async def test_tavily_search():
    # Given
    adapter = TavilySearchAdapter()
    service = TravelAgentService(adapter)
    
    # When
    result = await service.process_task(
        action="search",
        query="MJU University"
    )
    
    # Then
    assert result["status"] == "success"
    assert "Tavily" in result["data"]
