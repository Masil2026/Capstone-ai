# 📍 MJU Travel AI Agent
> **사용자 맞춤형 여행 일정을 설계하는 스마트 AI 에이전트**

본 프로젝트는 **FastAPI**와 **pydantic-ai**를 기반으로 하며, 최신 **Gemini 3** 모델을 활용하여 사용자의 의도에 맞는 도구(Tool)를 스스로 선택하고 최적의 여행 경로를 제안합니다.

---

## 🛠 Tech Stack

### **Backend & Framework**
* **Language:** Python 3.12+
* **Framework:** **FastAPI** (Async support, High performance)
* **Environment:** Pydantic Settings (Type-safe config via `.env`)
* **Server:** Uvicorn

### **AI & LLM Strategy**
* **Orchestration:** **pydantic-ai** (타입 안전한 Tool 정의, Agent, RunContext)
* **Core Model:** **Google Gemini 3 Flash Preview** (`gemini-3-flash-preview`)
    * *Reason:* 에이전트의 복잡한 도구 호출(Function Calling) 시 넉넉한 무료 한도(15 RPM)와 빠른 응답 속도 확보.
* **Observability:** **LangSmith** (LLM 추론 과정 추적 및 레이턴시 모니터링)

### **Database & Infrastructure**
* **Database:** PostgreSQL (Supabase) - 사용자 정보 및 일정 저장
* **Cache/Memory:** Redis (Upstash) - 대화 맥락(Conversation Summary) 관리

---

## 🤖 LLM Information
본 서비스는 구글의 가장 최신 모델 라인업을 활용합니다.

| 모델 ID | 버전 | 주요 특징 | 권장 Temperature |
| :--- | :--- | :--- | :--- |
| **`gemini-3-flash-preview`** | v3.0 (2025) | 멀티모달 이해 및 고성능 추론, 낮은 지연 시간 | `1.0` (에이전트 최적화) |
| **`gemini-3.1-pro-preview`** | v3.1 (2025) | 복잡한 논리 추론 및 긴 문맥(1M+ tokens) 처리 | `1.0` (정밀 추론용) |

* **Function Calling:** 실시간 날씨, 장소 검색 등 외부 API와의 연동 지원.
* **System Prompt:** 명지대학교 자연캠퍼스 전문 가이드 페르소나 적용.

---

## 🚀 Quick Start (서버 실행 가이드)

프로젝트를 로컬 환경에서 실행하고 API 문서를 확인하는 단계별 안내입니다.

### **1. 가상환경 설정 및 패키지 설치**
프로젝트 루트 폴더에서 아래 명령어를 순서대로 입력하세요.

```powershell
# 1. 가상환경 생성 (최초 1회)
python -m venv venv
or
py -m venv venv

# 2. 가상환경 활성화 (Windows)
.\venv\Scripts\activate

# 3. 필수 라이브러리 설치
pip install -r requirements.txt
```

### **2. 환경 변수 설정 (.env)**
프로젝트 루트 디렉토리에 `.env` 파일을 생성하고, 발급받은 API 키들을 입력합니다. (키 유출 주의)

### **3. API 서버 실행**
`main.py`가 최상단 디렉토리에 위치하므로 아래 명령어로 실행합니다.

```powershell
uvicorn main:app --reload --port 8000
```

### **4. API Documentation (Swagger)**
서버 실행 후 브라우저에서 아래 주소에 접속하여 API 명세를 확인하고 직접 테스트할 수 있습니다.
| 기능 | 접속 주소 | 비고 |
| :--- | :--- | :--- |
| **Swagger UI** | [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) | 가장 권장되는 대화형 문서 |

---

## 🐳 Docker 빌드 및 배포

> 실제 배포는 `capstone-deploy` 레포에서 전체 서비스를 한번에 실행합니다.
> 아래는 이 서비스만 단독으로 빌드/실행할 때 사용합니다.

### 이미지 빌드

```bash
docker build -t capstone-ai .
```

### 단독 실행

```bash
docker run -p 8000:8000 --env-file .env capstone-ai
```

### 빌드 과정

1. `python:3.12-slim` 이미지에서 `requirements.txt` 설치
2. 소스 코드 복사 후 uvicorn으로 실행
3. 포트 8000에서 서비스

### 환경 변수 (`.env`)

`.env` 파일을 프로젝트 루트에 생성하고 아래 값들을 채워넣으세요.

| 변수 | 설명 |
|------|------|
| `GOOGLE_API_KEY` | Gemini API 키 |
| `LANGCHAIN_API_KEY` | LangSmith API 키 |
| `LANGCHAIN_TRACING_V2` | LangSmith 추적 활성화 (`true`) |
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_KEY` | Supabase API 키 |
| `REDIS_URL` | Upstash Redis 접속 URL |
=======

