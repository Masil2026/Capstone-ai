import pytest
from app.services.adapters.weather_api import WeatherAdapter
from app.services.travel_agent_service import TravelAgentService

@pytest.mark.asyncio
async def test_weather_get_weather():
    # Given
    adapter = WeatherAdapter()
    service = TravelAgentService(adapter)
    
    # When
    result = await service.process_task(
        action="get_weather",
        city="Seoul"
    )
    
    # Then
    assert result["status"] == "success"
    assert "OpenWeatherMap" in result["data"]
