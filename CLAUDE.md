# Capstone AI — Claude Code Instructions

## 이 프로젝트에서 코드를 작성할 때 반드시 지켜야 할 규칙

---

## 기술 스택 (선택 기준 포함)

| 영역 | 사용 | 이유 |
|------|------|------|
| 웹 프레임워크 | FastAPI + uvicorn | 비동기 지원, 자동 Swagger |
| AI 에이전트 | **pydantic-ai** | LangChain 대신 타입 안전성 확보 |
| LLM | Gemini 3 Pro (오케스트레이터·최종 응답) + Gemini Flash (비정형 전처리) | 역할별 모델 분리 |
| DB | SQLAlchemy (동기) → Supabase PostgreSQL | read-only, MCP로 조회 가능 |
| 캐시 | Upstash Redis (`redis.asyncio`) | 대화 세션 저장 |
| 인증 | Clerk JWT (`python-jose`) | `Depends(get_current_user)` |
| HTTP | `httpx` (비동기) | 모든 외부 API 호출에 사용 |
| 설정 | pydantic-settings | `app/core/config.py`의 `settings` 싱글톤 |
| 테스트 | pytest + pytest-asyncio | `asyncio_mode = auto` |

**LangChain은 사용하지 않는다.** 새 코드에 LangChain을 쓰면 안 된다.

---

## 프로젝트 구조 — 파일을 어디에 만들지 판단 기준

```
main.py                          # FastAPI 앱, CORS, include_router만 있음. 로직 추가 금지
app/
  controller/                    # 라우터 파일만. 비즈니스 로직 작성 금지
  core/
    config.py                    # Settings 싱글톤. 여기서만 환경변수 읽음
    auth.py                      # Clerk JWT. 수정 시 get_current_user 시그니처 유지
    database.py                  # SQLAlchemy 동기 세션. 비동기 엔진 추가 금지
    ApiToolsInterfaces.py        # ApiTools ABC. 어댑터는 반드시 이걸 구현
  services/
    adapters/                    # 외부 API 어댑터. 파일당 하나의 서비스
    agent.py                     # pydantic-ai 에이전트 설정
    travel_agent_service.py      # 서비스 계층. 어댑터를 DIP로 주입
  schemas/                       # Pydantic 요청/응답 모델
tests/
  ai/                            # LLM 연결 테스트
  db/                            # DB / Redis 연결 테스트
  tools/                         # 어댑터 통합 & Mock 테스트
docs/
  api/                           # 엔드포인트별 API 명세 (new-endpoint 스킬이 자동 생성)
  conventions.md                 # 코딩 패턴 상세 — 코드 작성 전 반드시 확인
  aiagentflow.md                 # AI 에이전트 전체 흐름 — 에이전트 관련 작업 시 확인
```

---

## 코드 작성 규칙 (어기면 안 됨)

### 새 엔드포인트 추가 시
→ `/new-endpoint` 스킬을 사용한다.

### 새 외부 API 어댑터 추가 시
1. `app/services/adapters/{서비스명}_api.py` 생성
2. `ApiTools` ABC 구현 필수 (`execute`, `tool_name`)
3. 반환값은 항상 `{"status": "success"|"error", ...}` — 예외 raise 금지
4. 외부 HTTP 호출은 `httpx.AsyncClient`에 timeout 명시 필수
5. 패턴은 @docs/conventions.md 참고

### 설정값 사용 시
`settings.변수명` 형태로만 읽는다. `.env`를 직접 읽거나 `os.getenv`를 쓰지 않는다.

### 인증이 필요한 엔드포인트
`claims: dict = Depends(get_current_user)` 파라미터를 추가한다.

### DB 스키마 확인 필요 시
MCP로 직접 조회한다:
```sql
-- 테이블 목록
SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';

-- 특정 테이블 컬럼
SELECT column_name, data_type FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = '테이블명';
```

---

## 테스트 작성 규칙

- 테스트 파일 위치: `tests/{ai|db|tools}/test_{대상}.py`
- 외부 API 비용이 낮으면 → 실제 호출 통합 테스트
- 외부 API 비용이 높거나 안정성이 불확실하면 → `unittest.mock.patch`로 httpx 모킹
- 자세한 패턴은 @docs/conventions.md 참고

```bash
pytest                                          # 전체
pytest tests/tools/test_flight.py -s            # 특정 파일 (print 출력 포함)
pytest tests/tools/test_google_maps.py::test_google_maps_find_route -s  # 특정 테스트
```

