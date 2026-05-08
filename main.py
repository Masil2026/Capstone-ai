import logging
import sys

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from app.controller.aiMessageController import router as ai_message_router

_app_logger = logging.getLogger("app")
_app_logger.setLevel(logging.INFO)
if not _app_logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
    _app_logger.addHandler(_h)

app = FastAPI(
    title="MJU Capstone AI AGENTI",
    description="명지대학교 자연캠퍼스 가이드 및 여행 일정을 짜주는 AI 에이전트 서버입니다.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["InternalToken"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-Internal-Token",
    }
    for path in schema.get("paths", {}).values():
        for operation in path.values():
            operation.setdefault("security", [{"InternalToken": []}])
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi

app.include_router(ai_message_router, prefix="/api/v1", tags=["AI Messages"])

@app.get("/", tags=["Health Check"])
async def root():
    return {"message": "Capstone AI API is running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
