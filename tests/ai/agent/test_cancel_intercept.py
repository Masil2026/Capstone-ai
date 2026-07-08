"""
cancel 선처리(pre-check) 테스트

이 서비스는 예약을 직접 취소하지 않는다(Booking은 조회·딥링크 전용).
따라서 cancel 요청은 예약 유무·지목과 무관하게 항상 '예약처에서 직접 취소' 안내로 응답하며,
오케스트레이터를 호출하지 않고 cancel payload도 생성하지 않는다(done type은 chat).

검증 항목:
  - _get_cancel_intercept_message : 입력과 무관하게 항상 동일한 안내 메시지 반환
  - 엔드포인트: 예약 없음 / 예약 있음 / 특정 항목 지목 → 모두 type=chat 안내, 오케스트레이터 미호출

실행:
  pytest tests/ai/agent/test_cancel_intercept.py -s
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.controller.aiMessageController import (
    _CANCEL_GUIDANCE_MESSAGE,
    _get_cancel_intercept_message,
)
from app.core.config import settings
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


# ── 단위 테스트: _get_cancel_intercept_message ────────────────────────────────
# 입력(예약 유무·지목 여부)과 무관하게 항상 동일한 '직접 취소' 안내를 반환해야 한다.

class TestCancelInterceptMessage:
    def test_예약_없음도_안내_메시지(self):
        assert _get_cancel_intercept_message("취소해줘", []) == _CANCEL_GUIDANCE_MESSAGE

    def test_예약_있어도_안내_메시지(self):
        assert _get_cancel_intercept_message("취소해줘", _RESERVATIONS) == _CANCEL_GUIDANCE_MESSAGE

    def test_특정_항목_지목해도_안내_메시지(self):
        assert _get_cancel_intercept_message("1번 취소해줘", _RESERVATIONS) == _CANCEL_GUIDANCE_MESSAGE
        assert _get_cancel_intercept_message("롯데호텔 도쿄 취소해줘", _RESERVATIONS) == _CANCEL_GUIDANCE_MESSAGE

    def test_안내_문구_내용(self):
        assert "직접" in _CANCEL_GUIDANCE_MESSAGE
        assert "예약처" in _CANCEL_GUIDANCE_MESSAGE


# ── 엔드포인트 통합 Mock 테스트 ───────────────────────────────────────────────
# 모든 cancel 요청은 type=chat 안내로 응답하고 오케스트레이터를 호출하지 않는다.

async def _run_cancel(content: str, reservations: list[dict]) -> dict:
    ctx = _make_ctx(reservations=reservations)
    with patch("app.controller.aiMessageController.load_context", new=AsyncMock(return_value=ctx)), \
         patch("app.controller.aiMessageController.classification_agent") as mock_cls, \
         patch("app.controller.aiMessageController.orchestrator_agent") as mock_orch, \
         patch("app.controller.aiMessageController.get_user_embedding", new=AsyncMock(return_value=_FIXED_EMBEDDING)):

        mock_cls.run = AsyncMock(return_value=_mock_cls("cancel"))
        events = await _call_endpoint(content)

    _assert_no_error(events)
    done = _done(events)
    _print_done(content, done)

    assert done["type"] == "chat"
    assert done["assistantMessage"]["content"] == _CANCEL_GUIDANCE_MESSAGE
    assert done.get("cancel") is None
    mock_orch.run_stream.assert_not_called()
    return done


@pytest.mark.asyncio
async def test_cancel_no_reservations_returns_guidance():
    """예약 없음 → type=chat 직접 취소 안내, 오케스트레이터 미호출"""
    await _run_cancel("취소해줘", [])


@pytest.mark.asyncio
async def test_cancel_with_reservations_returns_guidance():
    """예약 있어도 → type=chat 직접 취소 안내 (가짜 목록/취소 없음)"""
    await _run_cancel("취소해줘", _RESERVATIONS)


@pytest.mark.asyncio
async def test_cancel_specific_item_returns_guidance():
    """특정 항목 지목해도 → type=chat 직접 취소 안내 (cancel payload 미생성)"""
    await _run_cancel("1번 취소해줘", _RESERVATIONS)


@pytest.mark.asyncio
async def test_cancel_emits_chunk_before_done():
    """cancel → chunk 이벤트가 done 이전에 전송됨"""
    ctx = _make_ctx(reservations=_RESERVATIONS)
    with patch("app.controller.aiMessageController.load_context", new=AsyncMock(return_value=ctx)), \
         patch("app.controller.aiMessageController.classification_agent") as mock_cls, \
         patch("app.controller.aiMessageController.orchestrator_agent"), \
         patch("app.controller.aiMessageController.get_user_embedding", new=AsyncMock(return_value=_FIXED_EMBEDDING)):

        mock_cls.run = AsyncMock(return_value=_mock_cls("cancel"))
        events = await _call_endpoint("취소해줘")

    event_names = [e["event"] for e in events]
    assert "chunk" in event_names, "chunk 이벤트 없음"
    assert event_names.index("chunk") < event_names.index("done"), "chunk이 done보다 먼저여야 함"
    combined = "".join(e["data"]["content"] for e in events if e["event"] == "chunk")
    assert combined == _CANCEL_GUIDANCE_MESSAGE
