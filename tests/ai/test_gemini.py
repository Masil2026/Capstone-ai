import pytest
from pydantic_ai import Agent
from app.services.agents._base import _build_model

pytestmark = pytest.mark.llm


@pytest.mark.asyncio
async def test_vertexai_connection():
    """Vertex AI gemini-3.1-pro-preview 연결 테스트 — ADC 인증 확인"""
    agent = Agent(model=_build_model("orchestrator"))

    try:
        result = await agent.run("안녕! 잘 연결되어 있어?")
        print(f"\n[Vertex AI 응답]: {result.output}")
        assert result.output is not None

    except Exception as e:
        print(f"\n에러 발생 상세: {e}")
        pytest.fail(f"연결 실패: {e}")
