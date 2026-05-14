"""
cancel 선처리(pre-check) 테스트

순수 헬퍼 함수(단위 테스트) + 엔드포인트(Mock 통합 테스트).

검증 항목:
  - _build_cancel_list_message : 항공/숙소 포맷 확인
  - _user_targets_cancel_item  : 번호/IATA/이름/예약번호로 특정 항목 지목 감지
  - _get_cancel_intercept_message : 예약 없음 / 막연한 요청 / 특정 지목 분기
  - 엔드포인트: 예약 없음       → type=chat, "예약 내역이 없어요", 오케스트레이터 미호출
  - 엔드포인트: 막연한 취소     → type=chat, 목록 반환, 오케스트레이터 미호출
  - 엔드포인트: 특정 항목 지목  → 오케스트레이터에 위임

실행:
  pytest tests/ai/agent/test_cancel_intercept.py -s
"""
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.controller.aiMessageController import (
    _build_cancel_list_message,
    _get_cancel_intercept_message,
    _user_targets_cancel_item,
)
from app.core.config import settings
from app.schemas.ai_message import OrchestratorResult

# ── 공통 픽스처 ────────────────────────────────────────────────────────────────

_HEADERS = {
    "X-Internal-Token": settings.INTERNAL_TOKEN,
    "Content-Type": "application/json",
}
_ROOM_ID = "test-room-cancel-001"
_FIXED_EMBEDDING = [0.1] * 1536

_FLIGHT_RES = {
    "id": "res-flight-uuid-001",
    "type": "flight",
    "external_ref_id": "ABC123",
    "total_price": 450000,
    "currency": "KRW",
    "detail": {
        "airline": "대한항공",
        "departure": "ICN",
        "arrival": "NRT",
        "departing_at": "2026-06-01T09:00:00",
        "arriving_at": "2026-06-01T11:30:00",
        "stops": 0,
    },
}

_HOTEL_RES = {
    "id": "res-hotel-uuid-002",
    "type": "accommodation",
    "external_ref_id": "XYZ789",
    "total_price": 320000,
    "currency": "KRW",
    "detail": {
        "name": "롯데호텔 도쿄",
        "check_in": "2026-06-01",
        "check_out": "2026-06-03",
        "rooms": 1,
        "guests": 2,
    },
}

_RESERVATIONS = [_FLIGHT_RES, _HOTEL_RES]


def _make_ctx(*, reservations=None):
    return {
        "user_embedding": _FIXED_EMBEDDING,
        "history": [],
        "ai_summary": None,
        "preferences": None,
        "similar_messages": [],
        "current_itinerary": None,
        "reservations": reservations if reservations is not None else [],
    }


def _mock_cls(request_type: str):
    r = MagicMock()
    r.output.type = request_type
    return r


async def _call_endpoint(content: str) -> list[dict]:
    """SSE 스트림 전체 수신 → 파싱된 이벤트 목록 반환"""
    from main import app

    events: list[dict] = []
    current_event: str | None = None
    data_lines: list[str] = []

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST", "/api/v1/ai-messages",
            headers=_HEADERS,
            json={"roomId": _ROOM_ID, "content": content},
            timeout=30.0,
        ) as resp:
            assert resp.status_code == 200, f"HTTP {resp.status_code}"
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    current_event = line[7:].strip()
                    data_lines = []
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
                elif line == "" and current_event and data_lines:
                    try:
                        events.append({"event": current_event, "data": json.loads("\n".join(data_lines))})
                    except json.JSONDecodeError:
                        pass
                    current_event = None
                    data_lines = []
    return events


def _done(events: list[dict]) -> dict:
    d = next((e["data"] for e in events if e["event"] == "done"), None)
    assert d is not None, "done 이벤트 없음"
    return d


def _assert_no_error(events: list[dict]):
    errs = [e for e in events if e["event"] == "error"]
    if errs:
        pytest.fail(f"에러 이벤트 발생: {errs[0]['data']}")


def _print_done(name: str, done: dict):
    print(f"\n{'='*65}\n[{name}]")
    print(f"  type    : {done.get('type')}")
    print(f"  message : {done.get('assistantMessage', {}).get('content', '')[:120]}")
    print(f"{'='*65}\n")


# ── 단위 테스트: _build_cancel_list_message ────────────────────────────────────

class TestBuildCancelListMessage:
    def test_항공_포맷(self):
        msg = _build_cancel_list_message([_FLIGHT_RES])
        assert "1. [항공] 대한항공 ICN→NRT (2026-06-01) | 예약번호: ABC123 | 450,000 KRW" in msg

    def test_숙소_포맷(self):
        msg = _build_cancel_list_message([_HOTEL_RES])
        assert "1. [숙소] 롯데호텔 도쿄 (2026-06-01~2026-06-03) | 예약번호: XYZ789 | 320,000 KRW" in msg

    def test_항공_숙소_순서(self):
        msg = _build_cancel_list_message(_RESERVATIONS)
        assert "1. [항공]" in msg
        assert "2. [숙소]" in msg

    def test_헤더_및_질문_포함(self):
        msg = _build_cancel_list_message(_RESERVATIONS)
        assert "현재 예약 내역입니다" in msg
        assert "어떤 항목을 취소해드릴까요?" in msg
        assert "한 번에 하나씩" in msg

    def test_가격_없는_예약(self):
        res = {**_FLIGHT_RES, "total_price": None}
        msg = _build_cancel_list_message([res])
        assert "가격정보없음" in msg

    def test_예약번호_없는_예약(self):
        res = {**_HOTEL_RES, "external_ref_id": None}
        msg = _build_cancel_list_message([res])
        assert "예약번호: 없음" in msg


# ── 단위 테스트: _user_targets_cancel_item ────────────────────────────────────

class TestUserTargetsCancelItem:
    def test_숫자_번호_지목(self):
        assert _user_targets_cancel_item("1번 취소해줘", _RESERVATIONS) is True

    def test_첫번째_지목(self):
        assert _user_targets_cancel_item("첫 번째 취소해줘", _RESERVATIONS) is True

    def test_두번째_지목(self):
        assert _user_targets_cancel_item("두 번째 취소해줘", _RESERVATIONS) is True

    def test_세번째_지목(self):
        assert _user_targets_cancel_item("세 번째 거 취소", _RESERVATIONS) is True

    def test_IATA_코드_지목(self):
        assert _user_targets_cancel_item("ICN 출발 항공편 취소", _RESERVATIONS) is True

    def test_항공사명_지목(self):
        assert _user_targets_cancel_item("대한항공 취소해줘", _RESERVATIONS) is True

    def test_숙소명_지목(self):
        assert _user_targets_cancel_item("롯데호텔 도쿄 취소해줘", _RESERVATIONS) is True

    def test_예약번호_지목(self):
        assert _user_targets_cancel_item("ABC123 취소해줘", _RESERVATIONS) is True

    def test_막연한_요청_False(self):
        assert _user_targets_cancel_item("취소해줘", _RESERVATIONS) is False

    def test_전부_취소_False(self):
        assert _user_targets_cancel_item("전부 취소해줘", _RESERVATIONS) is False

    def test_모두_취소_False(self):
        assert _user_targets_cancel_item("다 취소해줘", _RESERVATIONS) is False

    def test_번호_지목_예약목록_무관(self):
        # "1번" 패턴은 예약 목록이 비어 있어도 매칭됨.
        # 빈 목록 가드는 _get_cancel_intercept_message에서 먼저 처리한다.
        assert _user_targets_cancel_item("1번 취소해줘", []) is True

    def test_짧은_식별자_무시(self):
        # 2글자 이하 identifier는 매칭하지 않음
        short_res = {**_FLIGHT_RES, "external_ref_id": "AB"}
        assert _user_targets_cancel_item("AB 취소", [short_res]) is False


# ── 단위 테스트: _get_cancel_intercept_message ────────────────────────────────

class TestGetCancelInterceptMessage:
    def test_예약_없음_안내_반환(self):
        msg = _get_cancel_intercept_message("취소해줘", [])
        assert msg == "취소할 수 있는 예약 내역이 없어요."

    def test_막연한_요청_목록_반환(self):
        msg = _get_cancel_intercept_message("취소해줘", _RESERVATIONS)
        assert msg is not None
        assert "현재 예약 내역입니다" in msg

    def test_특정_번호_지목_None_반환(self):
        msg = _get_cancel_intercept_message("1번 취소해줘", _RESERVATIONS)
        assert msg is None

    def test_특정_IATA_지목_None_반환(self):
        msg = _get_cancel_intercept_message("ICN→NRT 항공편 취소해줘", _RESERVATIONS)
        assert msg is None

    def test_특정_이름_지목_None_반환(self):
        msg = _get_cancel_intercept_message("롯데호텔 도쿄 취소해줘", _RESERVATIONS)
        assert msg is None

    def test_특정_예약번호_지목_None_반환(self):
        msg = _get_cancel_intercept_message("ABC123 취소해줘", _RESERVATIONS)
        assert msg is None


# ── 엔드포인트 통합 Mock 테스트 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_no_reservations_returns_chat_without_orchestrator():
    """예약 없음 → type=chat '없어요' 메시지, 오케스트레이터 미호출"""
    ctx = _make_ctx(reservations=[])

    with patch("app.controller.aiMessageController.load_context", new=AsyncMock(return_value=ctx)), \
         patch("app.controller.aiMessageController.classification_agent") as mock_cls, \
         patch("app.controller.aiMessageController.orchestrator_agent") as mock_orch, \
         patch("app.controller.aiMessageController.get_user_embedding", new=AsyncMock(return_value=_FIXED_EMBEDDING)):

        mock_cls.run = AsyncMock(return_value=_mock_cls("cancel"))
        events = await _call_endpoint("취소해줘")

    _assert_no_error(events)
    done = _done(events)
    _print_done("cancel_no_reservations", done)

    assert done["type"] == "chat"
    assert "없어요" in done["assistantMessage"]["content"]
    mock_orch.run_stream.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_vague_returns_list_without_orchestrator():
    """막연한 취소 요청 → type=chat 예약 목록 반환, 오케스트레이터 미호출"""
    ctx = _make_ctx(reservations=_RESERVATIONS)

    with patch("app.controller.aiMessageController.load_context", new=AsyncMock(return_value=ctx)), \
         patch("app.controller.aiMessageController.classification_agent") as mock_cls, \
         patch("app.controller.aiMessageController.orchestrator_agent") as mock_orch, \
         patch("app.controller.aiMessageController.get_user_embedding", new=AsyncMock(return_value=_FIXED_EMBEDDING)):

        mock_cls.run = AsyncMock(return_value=_mock_cls("cancel"))
        events = await _call_endpoint("취소해줘")

    _assert_no_error(events)
    done = _done(events)
    _print_done("cancel_vague", done)

    assert done["type"] == "chat"
    content = done["assistantMessage"]["content"]
    assert "현재 예약 내역입니다" in content
    assert "대한항공" in content
    assert "롯데호텔 도쿄" in content
    assert "어떤 항목을 취소해드릴까요?" in content
    mock_orch.run_stream.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_vague_emits_chunk_before_done():
    """막연한 취소 → chunk 이벤트가 done 이전에 전송됨"""
    ctx = _make_ctx(reservations=_RESERVATIONS)

    with patch("app.controller.aiMessageController.load_context", new=AsyncMock(return_value=ctx)), \
         patch("app.controller.aiMessageController.classification_agent") as mock_cls, \
         patch("app.controller.aiMessageController.orchestrator_agent"), \
         patch("app.controller.aiMessageController.get_user_embedding", new=AsyncMock(return_value=_FIXED_EMBEDDING)):

        mock_cls.run = AsyncMock(return_value=_mock_cls("cancel"))
        events = await _call_endpoint("전부 취소해줘")

    event_names = [e["event"] for e in events]
    assert "chunk" in event_names, "chunk 이벤트 없음"
    assert event_names.index("chunk") < event_names.index("done"), "chunk이 done보다 먼저여야 함"

    chunk_contents = [e["data"]["content"] for e in events if e["event"] == "chunk"]
    combined = "".join(chunk_contents)
    assert "현재 예약 내역입니다" in combined


@pytest.mark.asyncio
async def test_cancel_specific_item_delegates_to_orchestrator():
    """특정 항목 지목 → 오케스트레이터에 위임, type=cancel"""
    ctx = _make_ctx(reservations=_RESERVATIONS)
    mock_result = OrchestratorResult(
        message="대한항공 ICN→NRT 항공편 예약을 취소했습니다.",
    )

    async def _stream_output():
        yield mock_result

    @asynccontextmanager
    async def _mock_run_stream(*_, **__):
        m = MagicMock()
        m.stream_output = _stream_output
        m.get_output = AsyncMock(return_value=mock_result)
        yield m

    with patch("app.controller.aiMessageController.load_context", new=AsyncMock(return_value=ctx)), \
         patch("app.controller.aiMessageController.classification_agent") as mock_cls, \
         patch("app.controller.aiMessageController.orchestrator_agent") as mock_orch, \
         patch("app.controller.aiMessageController.get_user_embedding", new=AsyncMock(return_value=_FIXED_EMBEDDING)), \
         patch("app.controller.aiMessageController.save_memory"):

        mock_cls.run = AsyncMock(return_value=_mock_cls("cancel"))
        mock_orch.run_stream = _mock_run_stream

        events = await _call_endpoint("1번 취소해줘")

    _assert_no_error(events)
    done = _done(events)
    _print_done("cancel_specific_by_number", done)

    assert done["type"] == "cancel"
    assert "취소" in done["assistantMessage"]["content"]


@pytest.mark.asyncio
async def test_cancel_specific_by_name_delegates_to_orchestrator():
    """숙소명으로 특정 항목 지목 → 오케스트레이터에 위임"""
    ctx = _make_ctx(reservations=_RESERVATIONS)
    mock_result = OrchestratorResult(message="롯데호텔 도쿄 예약을 취소했습니다.")

    async def _stream_output():
        yield mock_result

    @asynccontextmanager
    async def _mock_run_stream(*_, **__):
        m = MagicMock()
        m.stream_output = _stream_output
        m.get_output = AsyncMock(return_value=mock_result)
        yield m

    with patch("app.controller.aiMessageController.load_context", new=AsyncMock(return_value=ctx)), \
         patch("app.controller.aiMessageController.classification_agent") as mock_cls, \
         patch("app.controller.aiMessageController.orchestrator_agent") as mock_orch, \
         patch("app.controller.aiMessageController.get_user_embedding", new=AsyncMock(return_value=_FIXED_EMBEDDING)), \
         patch("app.controller.aiMessageController.save_memory"):

        mock_cls.run = AsyncMock(return_value=_mock_cls("cancel"))
        mock_orch.run_stream = _mock_run_stream

        events = await _call_endpoint("롯데호텔 도쿄 취소해줘")

    _assert_no_error(events)
    done = _done(events)
    _print_done("cancel_specific_by_name", done)

    assert done["type"] == "cancel"
