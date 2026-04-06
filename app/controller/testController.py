# api/v1/test_router.py
from fastapi import APIRouter, Depends
from app.core.auth import get_current_user

router = APIRouter()

@router.get("/python-test") # 앞에 /api/test는 main.py의 prefix가 붙여줍니다.
async def python_test():
    return "연결 성공! 나는 명지대 가이드 AI 파이썬 서버야."

@router.get("/auth-test")
async def auth_test(claims: dict = Depends(get_current_user)):
    email = claims.get("email") or claims.get("email_address")
    if not email:
        # Clerk은 primary_email_address_id 등 다른 필드를 쓰기도 함. 실제 클레임 확인 후 키 수정 필요.
        return f"✅ [FastAPI 인증 성공]: 이메일 클레임 없음. 클레임 키 목록 = {list(claims.keys())}"

    return f"✅ [FastAPI 인증 성공]: 이메일 = {email}"