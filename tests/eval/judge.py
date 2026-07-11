# tests/eval/judge.py
"""DeepEval judge — Vertex AI Gemini Flash 래퍼.

DeepEval 기본 judge는 OpenAI라서 Vertex Gemini용 커스텀 모델을 구현한다.
judge는 flash를 사용해 pro 쿼터를 소모하지 않는다.
"""
import os

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")

from deepeval.models import DeepEvalBaseLLM
from google import genai
from google.oauth2 import service_account

from app.core.config import settings


class GeminiFlashJudge(DeepEvalBaseLLM):
    def __init__(self):
        creds = None
        if settings.GOOGLE_APPLICATION_CREDENTIALS:
            creds = service_account.Credentials.from_service_account_file(
                settings.GOOGLE_APPLICATION_CREDENTIALS,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        self._client = genai.Client(
            vertexai=True,
            project=settings.GOOGLE_CLOUD_PROJECT,
            location=settings.GOOGLE_CLOUD_REGION,
            credentials=creds,
        )
        self._model = settings.PREPROCESSOR_MODEL or "gemini-2.5-flash"

    def load_model(self):
        return self._client

    def get_model_name(self) -> str:
        return f"vertex:{self._model}"

    @staticmethod
    def _config(schema):
        if schema is None:
            return None
        return {"response_mime_type": "application/json", "response_schema": schema}

    @staticmethod
    def _parse(resp, schema):
        if schema is None:
            return resp.text
        return resp.parsed if resp.parsed is not None else schema.model_validate_json(resp.text)

    def generate(self, prompt: str, schema=None):
        resp = self._client.models.generate_content(
            model=self._model, contents=prompt, config=self._config(schema),
        )
        return self._parse(resp, schema)

    async def a_generate(self, prompt: str, schema=None):
        resp = await self._client.aio.models.generate_content(
            model=self._model, contents=prompt, config=self._config(schema),
        )
        return self._parse(resp, schema)
