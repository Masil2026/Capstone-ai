import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel


async def test_agent_basic_run():
    """TestModel로 pydantic-ai Agent 기본 동작 확인 (API 호출 없음)"""
    model = TestModel()
    agent = Agent(model=model)

    result = await agent.run("안녕!")

    assert result.output is not None
    print(f"\n[TestModel 응답]: {result.output}")


async def test_agent_message_history():
    """대화 히스토리가 올바르게 쌓이는지 확인"""
    model = TestModel()
    agent = Agent(model=model)

    result1 = await agent.run("첫 번째 메시지")
    result2 = await agent.run("두 번째 메시지", message_history=result1.all_messages())

    assert len(result2.all_messages()) > len(result1.all_messages())
    print(f"\n[1차 메시지 수]: {len(result1.all_messages())}")
    print(f"[2차 메시지 수]: {len(result2.all_messages())}")


async def test_agent_with_system_prompt():
    """시스템 프롬프트가 적용되는지 확인"""
    model = TestModel()
    agent = Agent(model=model, system_prompt="여행 일정 전문가입니다.")

    result = await agent.run("도쿄 여행 일정 짜줘")

    assert result.output is not None
    print(f"\n[시스템 프롬프트 적용 응답]: {result.output}")
