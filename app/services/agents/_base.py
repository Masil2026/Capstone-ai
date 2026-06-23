# app/services/agents/_base.py
from app.core.config import settings

_PROVIDER_DEFAULTS = {
    "vertexai": {"orchestrator": "gemini-2.5-pro", "preprocessor": "gemini-2.0-flash"},
}


def _build_model(role: str):
    """role: 'orchestrator' | 'preprocessor'"""
    provider = settings.LLM_PROVIDER
    defaults = _PROVIDER_DEFAULTS.get(provider)
    if defaults is None:
        raise ValueError(f"지원하지 않는 LLM_PROVIDER: {provider!r}. 'vertexai'를 사용하세요.")

    if role == "orchestrator":
        model_name = settings.ORCHESTRATOR_MODEL or defaults["orchestrator"]
    elif role == "preprocessor":
        model_name = settings.PREPROCESSOR_MODEL or defaults["preprocessor"]
    else:
        raise ValueError(f"알 수 없는 role: {role!r}")

    from pydantic_ai.models.vertexai import VertexAIModel
    from pydantic_ai.providers.google_vertex import GoogleVertexProvider
    return VertexAIModel(
        model_name,
        provider=GoogleVertexProvider(
            project_id=settings.GOOGLE_CLOUD_PROJECT,
            region=settings.GOOGLE_CLOUD_REGION,
        ),
    )


from pydantic_ai import Agent

# 전처리 에이전트 — Tavily 비정형 결과 요약 전용 (search_web 도구 내부에서 호출)
preprocessor_agent = Agent(model=_build_model("preprocessor"))
