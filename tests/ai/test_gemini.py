# import pytest
# from pydantic_ai import Agent
# from pydantic_ai.models.gemini import GeminiModel
# from pydantic_ai.providers.google_gla import GoogleGLAProvider
# from app.core.config import settings

# # 일반 테스트 시에도 LLM을 호출하면 토큰을 사용하기 때문에 전체 주석처리
# async def test_gemini_check():
#     """pydantic-ai를 이용한 Gemini 연결 테스트(API 호출)"""
#     model = GeminiModel(
#         "gemini-2.0-flash",
#         provider=GoogleGLAProvider(api_key=settings.GOOGLE_API_KEY),
#     )
#     agent = Agent(model=model)

#     try:
#         result = await agent.run("안녕! 잘 연결되어 있어?")
#         print(f"\n[Gemini 응답]: {result.data}")
#         assert result.data is not None

#     except Exception as e:
#         print(f"\n에러 발생 상세: {e}")
#         pytest.fail(f"연결 실패: {e}")
