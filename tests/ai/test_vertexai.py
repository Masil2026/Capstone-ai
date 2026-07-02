import pytest
from pydantic_ai import Agent
from app.services.agents._base import _build_model

pytestmark = pytest.mark.llm


@pytest.mark.asyncio
async def test_vertexai_preprocessor():
    """Vertex AI gemini-3.5-flash (preprocessor) 연결 테스트 — ADC 인증 확인"""
    agent = Agent(model=_build_model("preprocessor"))

    try:
        result = await agent.run("안녕! 한 문장으로 짧게 대답해줘.")
        print(f"\n[Preprocessor({agent.model})] {result.output}")
        assert result.output is not None

    except Exception as e:
        print(f"\n에러 발생 상세: {e}")
        pytest.fail(f"연결 실패: {e}")


@pytest.mark.asyncio
async def test_vertexai_orchestrator():
    """Vertex AI gemini-2.5-pro (orchestrator) 연결 테스트 — ADC 인증 확인"""
    agent = Agent(model=_build_model("orchestrator"))

    try:
        result = await agent.run("안녕! 한 문장으로 짧게 대답해줘.")
        print(f"\n[Orchestrator({agent.model})] {result.output}")
        assert result.output is not None

    except Exception as e:
        print(f"\n에러 발생 상세: {e}")
        pytest.fail(f"연결 실패: {e}")
