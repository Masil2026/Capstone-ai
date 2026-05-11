# 코딩 컨벤션 — Claude 작업 지침

코드를 작성하기 전에 이 문서를 확인한다. 모든 패턴은 실제 코드에서 추출한 것이다.

---

## 라우터 (Controller)

**규칙:**
- 파일 위치: `app/controller/{도메인명}Controller.py` (camelCase)
- 파일 상단에 경로 주석 표기
- `router = APIRouter()` — 변수명 고정
- prefix와 tags는 `main.py`의 `include_router`에서만 지정
- 라우터 함수 내에 비즈니스 로직 작성 금지 — 서비스로 위임

**템플릿:**
```python
# app/controller/{도메인명}Controller.py
from fastapi import APIRouter, Depends
from app.core.auth import get_current_user

router = APIRouter()

# 인증 불필요
@router.get("/경로")
async def 함수명():
    return {"status": "success", "data": None}

# 인증 필요
@router.get("/protected-경로")
async def 함수명(claims: dict = Depends(get_current_user)):
    email = claims.get("email") or claims.get("email_address")
    return {"status": "success", "data": email}
```

**main.py 등록:**
```python
from app.controller.{도메인명}Controller import router as {도메인명}_router
app.include_router({도메인명}_router, prefix="/api/{도메인명}", tags=["{도메인명}"])
```

---

## 외부 API 어댑터 (Adapter)

**규칙:**
- 파일 위치: `app/services/adapters/{서비스명}_api.py`
- 반드시 `ApiTools` ABC 구현 (`app/core/ApiToolsInterfaces.py`)
- `execute(action, params)` — 단일 진입점, action 문자열로 기능 분기
- 반환값은 항상 `{"status": "success"|"error"|"todo", ...}` — 예외 raise 금지
- API 키가 필요 없는 어댑터는 `__init__` 생략 가능 (예: `WeatherAdapter`)
- 미구현 action은 `"status": "todo"`로 명확히 표시

**템플릿:**
```python
import httpx
from app.core.ApiToolsInterfaces import ApiTools
from app.core.config import settings
from typing import Any, Dict

class {서비스명}Adapter(ApiTools):
    def __init__(self):
        self.api_key = settings.{API_KEY_변수명}.strip()
        self.base_url = "https://..."

    @property
    def tool_name(self) -> str:
        return "{서비스_식별자}"  # 예: "duffel_flight", "google_maps"

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if action == "{액션명}":
            # 1. 파라미터 검증
            required = params.get("필수_파라미터")
            if not required:
                return {"status": "error", "message": "필수_파라미터는 필수입니다."}

            # 2. 외부 API 호출
            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    response = await client.post(self.base_url, json=payload)
                except httpx.TimeoutException:
                    return {"status": "error", "message": "API 타임아웃 (30초 초과)"}
                except httpx.RequestError as e:
                    return {"status": "error", "message": f"요청 실패: {str(e)}"}

            try:
                data = response.json()
            except Exception:
                return {"status": "error", "message": f"JSON 파싱 실패: {response.text[:200]}"}

            if response.status_code != 200:
                return {"status": "error", "message": data.get("errors")}

            # 3. 데이터 정제 후 반환
            return {"status": "success", "count": len(results), "data": results}

        # 미구현 액션
        elif action in ["미구현_액션1", "미구현_액션2"]:
            return {"status": "todo", "message": f"'{action}' 기능은 현재 개발 중입니다."}

        return {"status": "error", "message": f"지원하지 않는 액션: {action}"}
```

**httpx timeout 기준:**
| 용도 | timeout |
|------|---------|
| 간단한 조회 (geocoding, IATA 변환 등) | `10.0` |
| 장소 검색, 경로 조회 | `15.0` ~ `20.0` |
| 항공/숙소 검색, 웹 크롤링 | `30.0` |

**디버그 로그:** API 키는 반드시 마스킹
```python
debug_params = {**query_params, "key": "***REDACTED***"}
print(f"[{클래스명}] 요청: {debug_params}")
print(f"[{클래스명}] HTTP Status: {response.status_code}")
```

---

## 서비스 계층

`TravelAgentService`는 `ApiTools` 인터페이스만 의존한다. 어댑터를 직접 import하지 않는다.
생성자는 `tool_name → 어댑터` 딕셔너리를 받고, `process_task`는 `tool_name`을 첫 번째 인자로 요구한다.

```python
service = TravelAgentService({
    "duffel_flight": FlightAdapter(),
    "duffel_accommodation": AccommodationAdapter(),
})
result = await service.process_task("duffel_flight", "search_flights", params={...})
```

`agent.py`의 `_service` 싱글턴이 모든 어댑터를 보유하며, `@orchestrator_agent.tool_plain`으로 등록된 도구 함수들이 이를 통해 API를 호출한다.

---

## pydantic-ai Agent 결과값 (v1.x 기준)

`agent.run()` 결과는 `result.output`으로 참조한다. v0.6.0에서 `result.data`가 제거되었다.

```python
result = await agent.run("입력")
answer = result.output        # ✅ 올바름 (v0.6.0+)
answer = result.data          # ❌ AttributeError (v0.6.0+에서 제거됨)

# 대화 히스토리 누적
history = result.all_messages()
result2 = await agent.run("다음 입력", message_history=history)
```

Agent 생성자도 v0.6.0부터 `result_type` → `output_type`으로 변경되었다.

```python
agent = Agent(
    model=...,
    deps_type=MyDeps,
    output_type=MyOutput,    # ✅ v0.6.0+ (result_type은 deprecated)
    system_prompt="...",
)
```

## pydantic-ai 동적 컨텍스트 주입

**`@agent.system_prompt` + `RunContext` 패턴은 사용하지 않는다.**

동적 컨텍스트는 일반 함수로 생성하고 `agent.run()` 직전에 user_message 앞에 붙인다.

```python
# ✅ 올바른 패턴
agent = Agent(
    model=...,
    deps_type=MyDeps,
    output_type=MyOutput,
    system_prompt="역할 설명 (정적 문자열만)",
)

def build_context_prompt(deps: MyDeps) -> str:
    sections = []
    if deps.current_itinerary:
        sections.append(f"## 현재 여행 정보\n- 여행지: {deps.current_itinerary['destination']}")
    if deps.ai_summary:
        sections.append(f"## 이전 대화 요약\n{deps.ai_summary}")
    return "\n\n".join(sections)

context_block = build_context_prompt(deps)
result = await agent.run(
    f"{context_block}\n\n---\n\n사용자 메시지: {user_message}",
    deps=deps,
    message_history=history,
)
```

## pydantic-ai 스트리밍 (SSE용)

### 구조화 출력 실시간 스트리밍 (output_type 지정 에이전트)

`run_stream()` + `stream_output()`으로 partial 객체를 토큰 단위로 수신한다.
`message` 필드가 `OrchestratorResult`의 첫 번째 필드여야 가장 먼저 스트리밍된다.

```python
async with agent.run_stream(prompt, deps=deps, message_history=history) as stream_result:
    prev_msg = ""
    async for partial in stream_result.stream_output():
        msg = getattr(partial, "message", None) or ""
        if len(msg) > len(prev_msg):
            yield _sse("chunk", {"content": msg[len(prev_msg):]})
            prev_msg = msg
    orch_result = await stream_result.get_output()  # 완성된 전체 객체
```

### 텍스트 출력 스트리밍 (output_type 없는 에이전트)

```python
async with agent.run_stream("입력", message_history=history) as result:
    async for chunk in result.stream_text(delta=True):
        yield f"event: chunk\ndata: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"
    full_response = await result.get_output()
```

---

## 설정값

`settings` 싱글톤에서만 읽는다. `os.getenv`나 `.env` 직접 읽기 금지.

```python
from app.core.config import settings

self.api_key = settings.DUFFEL_API_KEY.strip()
```

---

## 테스트 패턴

### 파일 구조

```
tests/
  ai/    → LLM 연결 테스트 (실제 API 호출)
  db/    → DB / Redis 연결 테스트 (실제 연결)
  tools/ → 어댑터 테스트 (통합 or Mock)
```

### 어떤 방식을 쓸지 판단 기준

| 상황 | 방식 |
|------|------|
| 외부 API 비용이 낮음 (Duffel, Tavily, Open-Meteo) | 실제 API 호출 통합 테스트 |
| 외부 API 비용이 높거나 키 없이도 로직 검증 가능 | `unittest.mock.patch`로 httpx 모킹 |
| DB 연결 확인 | `SessionLocal` 직접 생성, fixture 없음 |

### 통합 테스트 템플릿

```python
import pytest
from app.services.adapters.{어댑터} import {어댑터클래스}
from app.services.travel_agent_service import TravelAgentService

def _print_{도메인}_results(test_name, result):
    print("\n" + "="*65)
    print(f"[{test_name}] STATUS: {result['status']}")
    if result["status"] == "success":
        for i, item in enumerate(result.get("data", []), 1):
            print(f"{i}. {item}")
    print("="*65 + "\n")

@pytest.mark.asyncio
async def test_{어댑터}_{액션}_{시나리오}():
    """{테스트 목적 한 줄 설명}"""
    adapter = {어댑터클래스}()
    service = TravelAgentService(adapter)

    result = await service.process_task(action="{액션}", params={...})

    _print_{도메인}_results("{테스트명}", result)

    assert result["status"] == "success"
    assert isinstance(result["data"], list)
```

### Mock 테스트 템플릿

```python
from unittest.mock import patch, Mock

@pytest.mark.asyncio
async def test_{어댑터}_{액션}_mock():
    adapter = {어댑터클래스}()
    service = TravelAgentService(adapter)

    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": "OK",
        "results": [...]   # 실제 API 응답 구조 그대로
    }

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await service.process_task(action="{액션}", params={...})

    assert result["status"] == "success"
    assert result["data"]["count"] == 1
```

### 유효성 검사 에러 테스트 — 반드시 포함

새 어댑터를 만들면 파라미터 검증 실패 케이스를 테스트해야 한다.

```python
@pytest.mark.asyncio
async def test_{어댑터}_validation_error():
    """{어떤 검증이 실패하는지 설명}"""
    result = await service.process_task(action="{액션}", params={잘못된_params})

    assert result["status"] == "error"
    assert result["message"] == "정확한 에러 메시지"  # 하드코딩, 메시지 변경 추적 가능하게
```

### 테스트 네이밍

```
test_{어댑터명}_{액션명}_{시나리오}

예:
test_flight_search_with_child       ← 정상 케이스 (조건 명시)
test_flight_search_adults_only      ← 정상 케이스 (다른 조건)
test_flight_validation_error        ← 검증 실패
test_google_maps_find_route         ← 정상 케이스
test_google_maps_find_route_missing_params  ← 파라미터 누락
test_google_maps_invalid_action     ← 지원하지 않는 액션
```
