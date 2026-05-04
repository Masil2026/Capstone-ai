from fastapi import FastAPI
from app.controller.TestController import router as test_router

app = FastAPI(
    title="MJU Capstone AI AGENTI",
    description="명지대학교 자연캠퍼스 가이드 및 여행 일정을 짜주는 AI 에이전트 서버입니다.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.include_router(test_router, prefix="/api/test", tags=["Test"])

@app.get("/", tags=["Health Check"])
async def root():
    return {"message": "Capstone AI API is running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
