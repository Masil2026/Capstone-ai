from fastapi import HTTPException, Request
from jose import jwt, JWTError
import httpx
from app.core.config import settings


async def get_current_user(request: Request) -> dict:
    """
    Authorization: Bearer <token> 헤더를 검증하고 JWT 클레임을 반환합니다.
    FastAPI Depends()로 사용하세요.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization 헤더가 없거나 형식이 잘못되었습니다.")

    token = auth_header.split(" ", 1)[1]

    async with httpx.AsyncClient() as client:
        response = await client.get(settings.CLERK_JWKS_URL)
        jwks = response.json()

    try:
        claims = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"토큰 검증 실패: {str(e)}")

    return claims
