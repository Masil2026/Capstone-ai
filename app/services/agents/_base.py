# app/services/agents/_base.py
from app.core.config import settings

_PROVIDER_DEFAULTS = {
    "openai":  {"orchestrator": "gpt-4.1",        "preprocessor": "gpt-4o-mini"},
    "gemini":  {"orchestrator": "gemini-2.5-pro", "preprocessor": "gemini-2.0-flash"},
}


def _build_model(role: str):
    """role: 'orchestrator' | 'preprocessor'"""
    provider = settings.LLM_PROVIDER
    defaults = _PROVIDER_DEFAULTS.get(provider)
    if defaults is None:
        raise ValueError(f"지원하지 않는 LLM_PROVIDER: {provider!r}. 'openai' 또는 'gemini'를 사용하세요.")

    if role == "orchestrator":
        model_name = settings.ORCHESTRATOR_MODEL or defaults["orchestrator"]
    elif role == "preprocessor":
        model_name = settings.PREPROCESSOR_MODEL or defaults["preprocessor"]
    else:
        raise ValueError(f"알 수 없는 role: {role!r}")

    if provider == "openai":
        from pydantic_ai.models.openai import OpenAIModel
        from pydantic_ai.providers.openai import OpenAIProvider
        return OpenAIModel(model_name, provider=OpenAIProvider(api_key=settings.GPT_API_KEY))

    from pydantic_ai.models.gemini import GeminiModel
    from pydantic_ai.providers.google_gla import GoogleGLAProvider
    return GeminiModel(model_name, provider=GoogleGLAProvider(api_key=settings.GOOGLE_API_KEY))


from pydantic_ai import Agent

# 전처리 에이전트 — Tavily 비정형 결과 요약 전용 (search_web 도구 내부에서 호출)
preprocessor_agent = Agent(model=_build_model("preprocessor"))
