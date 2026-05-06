import secrets

from fastapi import HTTPException, Request
from jose import jwt, JWTError
import httpx
from app.core.config import settings

_jwks_cache: dict | None = None


async def _get_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache is not None:
        return _jwks_cache
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(settings.CLERK_JWKS_URL)
        response.raise_for_status()
        _jwks_cache = response.json()
    return _jwks_cache


async def verify_internal_token(request: Request) -> None:
    """
    X-Internal-Token 헤더를 검증합니다. Spring Boot → FastAPI 내부 서버 간 인증에 사용.
    FastAPI Depends()로 사용하세요.
    """
    token = request.headers.get("X-Internal-Token", "")
    if not secrets.compare_digest(token, settings.INTERNAL_TOKEN):
        raise HTTPException(status_code=403, detail="유효하지 않은 내부 서버 토큰입니다.")


async def get_current_user(request: Request) -> dict:
    """
    Authorization: Bearer <token> 헤더를 검증하고 JWT 클레임을 반환합니다.
    FastAPI Depends()로 사용하세요.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization 헤더가 없거나 형식이 잘못되었습니다.")

    token = auth_header.split(" ", 1)[1]
    jwks = await _get_jwks()

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
