import pytest
from app.services.adapters.meta_instagram import MetaGraphAdapter
from app.services.travel_agent_service import TravelAgentService

@pytest.mark.asyncio
async def test_meta_graph_get_posts():
    # Given
    adapter = MetaGraphAdapter()
    service = TravelAgentService(adapter)
    
    # When
    result = await service.process_task(
        action="get_user_posts",
        user_id="test_user"
    )
    
    # Then
    assert result["status"] == "success"
    assert "인스타그램" in result["data"]
