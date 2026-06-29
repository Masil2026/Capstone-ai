# app/services/agents/_base.py
import asyncio
import random

from app.core.config import settings

_PROVIDER_DEFAULTS = {
    "vertexai": {"orchestrator": "gemini-3.1-pro-preview", "preprocessor": "gemini-3.5-flash"},
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

    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider
    from google.oauth2 import service_account

    creds = None
    if settings.GOOGLE_APPLICATION_CREDENTIALS:
        creds = service_account.Credentials.from_service_account_file(
            settings.GOOGLE_APPLICATION_CREDENTIALS,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    return GoogleModel(
        model_name,
        provider=GoogleProvider(
            project=settings.GOOGLE_CLOUD_PROJECT,
            location=settings.GOOGLE_CLOUD_REGION,
            credentials=creds,
        ),
    )


from pydantic_ai import Agent


def _is_rate_limit_error(e: Exception) -> bool:
    msg = str(e)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg


def _retry_wait(attempt: int) -> float:
    """지수 백오프 + jitter 대기 시간 계산. run_stream 재시도 루프에서 사용."""
    base = 2 ** attempt
    return base + random.uniform(0, base * 0.3)


async def run_with_retry(agent: Agent, prompt: str, *, role: str, max_retries: int = 4, **kwargs):
    """429 발생 시 지수 백오프 + jitter로 재시도.

    - max_retries=4 → 최대 대기 합계 약 1+2+4+8 = 15초
    - jitter: 동시 요청들이 같은 타이밍에 재시도하는 Thundering Herd 방지
    """
    for attempt in range(max_retries):
        try:
            return await agent.run(prompt, **kwargs)
        except Exception as e:
            if _is_rate_limit_error(e) and attempt < max_retries - 1:
                base = 2 ** attempt                      # 1s → 2s → 4s → 8s
                jitter = random.uniform(0, base * 0.3)   # ±30% 무작위
                wait = base + jitter
                print(f"[{role}] 429 재시도 {attempt + 1}/{max_retries - 1}, {wait:.1f}s 대기", flush=True)
                await asyncio.sleep(wait)
                continue
            raise


# 전처리 에이전트 — Tavily 비정형 결과 요약 전용 (search_web 도구 내부에서 호출)
preprocessor_agent = Agent(model=_build_model("preprocessor"))
