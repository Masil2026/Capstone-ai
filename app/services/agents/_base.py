# app/services/agents/_base.py
import asyncio
import random
import time

from app.core.config import settings

_PROVIDER_DEFAULTS = {
    "vertexai": {"orchestrator": "gemini-2.5-pro", "preprocessor": "gemini-2.5-flash"},
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


# ── Token Bucket ──────────────────────────────────────────────────────────────

class _TokenBucket:
    """속도 제한기. 초당 rate개 토큰을 보충하고 capacity를 초과하지 않는다.

    acquire()가 호출될 때 토큰이 없으면 보충될 때까지 대기한다.
    Semaphore(동시 호출 수 제한)와 달리, 장기 평균 속도만 조절하고 버스트는 허용한다.
    """

    def __init__(self, rate: float, capacity: float):
        self._rate = rate          # 초당 토큰 보충 속도 (VERTEX_AI_RPM / 60)
        self._capacity = capacity  # 최대 버스트 허용 토큰 수
        self._tokens = capacity    # 시작 시 꽉 채움
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._last) * self._rate,
                )
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate
            await asyncio.sleep(wait)


def _make_bucket(rpm: int) -> _TokenBucket:
    """RPM 기준: rate = 초당 처리량, capacity = RPM의 10% (최소 2 — 저쿼터 모델 버스트 방지)"""
    return _TokenBucket(rate=rpm / 60, capacity=max(2.0, rpm / 10))


# Vertex 쿼터는 모델별이므로 버킷을 분리한다: orchestrator(pro) 계열 vs 나머지(flash) 계열
_pro_bucket = _make_bucket(settings.VERTEX_AI_PRO_RPM)
_flash_bucket = _make_bucket(settings.VERTEX_AI_RPM)

# pro 모델(_build_model("orchestrator"))을 쓰는 role — 나머지는 전부 flash
_PRO_ROLES = frozenset({"orchestrator", "planner", "synthesizer"})


async def acquire_llm_slot(role: str) -> None:
    """role에 해당하는 모델 버킷에서 슬롯 확보.

    run_with_retry가 자동으로 호출한다. run_stream처럼 run_with_retry를 타지 않는
    호출은 스트림 시작 전에 직접 호출할 것.
    """
    bucket = _pro_bucket if role in _PRO_ROLES else _flash_bucket
    await bucket.acquire()


def _is_rate_limit_error(e: Exception) -> bool:
    msg = str(e)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg


def _retry_wait(attempt: int) -> float:
    """지수 백오프 + jitter 대기 시간 계산. run_stream 재시도 루프에서 사용."""
    base = 2 ** attempt
    return base + random.uniform(0, base * 0.3)


async def run_with_retry(agent: Agent, prompt: str, *, role: str, max_retries: int = 4, **kwargs):
    """Token Bucket으로 속도를 조절한 뒤, 429 발생 시 지수 백오프 + jitter로 재시도.

    - acquire(): 첫 호출 전 1회만 실행 — 재시도는 백오프가 간격을 보장하므로 제외
    - max_retries=4 → 최대 대기 합계 약 1+2+4+8 = 15초
    - jitter: 동시 요청들이 같은 타이밍에 재시도하는 Thundering Herd 방지
    """
    await acquire_llm_slot(role)
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
