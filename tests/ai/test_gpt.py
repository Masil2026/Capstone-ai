# import pytest
# from pydantic_ai import Agent
# from app.services.agent import _build_model


# @pytest.mark.asyncio
# async def test_gpt_preprocessor():
#     """gpt-4o-mini (preprocessor) 연결 테스트"""
#     agent = Agent(model=_build_model("preprocessor"))
#     result = await agent.run("안녕! 한 문장으로 짧게 대답해줘.")
#     print(f"\n[Preprocessor({agent.model})] {result.data}")
#     assert result.data is not None


# @pytest.mark.asyncio
# async def test_gpt_orchestrator():
#     """gpt-4.1 (orchestrator) 연결 테스트"""
#     agent = Agent(model=_build_model("orchestrator"))
#     result = await agent.run("안녕! 한 문장으로 짧게 대답해줘.")
#     print(f"\n[Orchestrator({agent.model})] {result.data}")
#     assert result.data is not None
