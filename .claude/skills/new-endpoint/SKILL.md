---
name: new-endpoint
description: 새 FastAPI 엔드포인트 추가 — controller 파일 생성, main.py 등록, docs/api 명세 작성
---

사용자가 요청한 새 FastAPI 엔드포인트를 추가한다. 아래 순서를 그대로 따른다.

## 사용자에게 먼저 확인할 것

아직 제공되지 않은 정보가 있으면 물어본다:
- **도메인명**: 라우터 파일명과 URL prefix에 사용 (예: `chat`, `schedule`, `place`)
- **엔드포인트 목록**: HTTP 메서드, 경로, 인증 필요 여부, 요청/응답 구조

## Step 1 — 라우터 파일 생성

`app/controller/{도메인명}Controller.py`를 생성한다.

반드시 지킬 것:
- 파일 상단 경로 주석 표기
- `router = APIRouter()` 변수명 고정
- 인증 필요 시 `claims: dict = Depends(get_current_user)` 파라미터 추가
- 라우터 함수 안에 비즈니스 로직 작성 금지 — 서비스 호출만
- 반환 포맷: `{"status": "success"|"error", ...}`

```python
# app/controller/{도메인명}Controller.py
from fastapi import APIRouter, Depends
from app.core.auth import get_current_user

router = APIRouter()

@router.{메서드}("/{경로}")
async def {함수명}(claims: dict = Depends(get_current_user)):  # 인증 불필요시 제거
    # TODO: 서비스 로직 연결
    return {"status": "success", "data": None}
```

## Step 2 — main.py에 라우터 등록

`main.py`의 기존 `include_router` 블록 바로 아래에 추가한다.

```python
from app.controller.{도메인명}Controller import router as {도메인명}_router
app.include_router({도메인명}_router, prefix="/api/{도메인명}", tags=["{도메인명}"])
```

## Step 3 — API 명세 파일 생성

`docs/api/{도메인명}.md`를 생성한다.

```markdown
# {도메인명} API

Base URL: `/api/{도메인명}`

## {HTTP메서드} /{경로}

**설명**: ...

**인증**: Bearer Token (Clerk JWT) 필요 / 불필요

**요청 파라미터**:

| 이름 | 위치 | 타입 | 필수 | 설명 |
|------|------|------|------|------|
| ...  | query \| body | string | Y \| N | ... |

**응답 예시 (200)**:
```json
{
  "status": "success",
  "data": {}
}
```

**에러 응답**:
```json
{
  "status": "error",
  "message": "에러 설명"
}
```
```

## Step 4 — 완료 확인

생성/수정된 파일을 나열하고, 사용자에게 아래를 직접 확인하도록 안내한다:

```bash
uvicorn main:app --reload --port 8000
# 브라우저에서 http://localhost:8000/docs 열어 새 엔드포인트 확인
```
