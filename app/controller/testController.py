# api/v1/test_router.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/python-test") # 앞에 /api/test는 main.py의 prefix가 붙여줍니다.
async def python_test():
    return "연결 성공! 나는 명지대 가이드 AI 파이썬 서버야."