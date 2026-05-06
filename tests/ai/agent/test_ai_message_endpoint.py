"""
POST /api/v1/ai-messages 엔드포인트 통합 테스트

httpx ASGI transport으로 FastAPI 앱을 직접 호출 (별도 서버 불필요).
인증: X-Internal-Token (settings.INTERNAL_TOKEN)
roomId: .env의 TEST_ROOM_ID (본인 Clerk 계정으로 생성된 실제 채팅방 ID)

실행:
  pytest tests/ai/agent/test_ai_message_endpoint.py -s -m llm
  pytest tests/ai/agent/test_ai_message_endpoint.py::test_invalid_token -s -m llm
"""
import asyncio
import json

import httpx
import pytest

pytestmark = pytest.mark.llm

from main import app
from app.core.config import settings

_HEADERS = {
    "X-Internal-Token": settings.INTERNAL_TOKEN,
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# SSE 수신 헬퍼
# ---------------------------------------------------------------------------

async def _call_ai_messages(content: str, room_id: str | None = None) -> list[dict]:
    """SSE 스트림 전체를 수신하여 파싱된 이벤트 목록 반환.

    반환: [{"event": "chunk"|"done"|"error", "data": dict}, ...]
    """
    rid = room_id or settings.TEST_ROOM_ID
    assert rid, "TEST_ROOM_ID를 .env에 설정하세요. (본인 Clerk 계정의 채팅방 ID)"

    events: list[dict] = []
    current_event: str | None = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/api/v1/ai-messages",
            headers=_HEADERS,
            json={"roomId": rid, "content": content},
            timeout=120.0,
        ) as response:
            assert response.status_code == 200, f"HTTP {response.status_code}: {await response.aread()}"
            assert "text/event-stream" in response.headers.get("content-type", "")

            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    current_event = line[7:].strip()
                elif line.startswith("data: ") and current_event is not None:
                    try:
                        events.append({
                            "event": current_event,
                            "data": json.loads(line[6:]),
                        })
                    except json.JSONDecodeError:
                        pass
                    current_event = None

    return events


def _assert_no_error(events: list[dict]) -> None:
    """error 이벤트가 있으면 그 메시지로 즉시 실패"""
    error_events = [e for e in events if e["event"] == "error"]
    if error_events:
        pytest.fail(f"서버 오류: {error_events[0]['data'].get('message', error_events[0]['data'])}")


# ---------------------------------------------------------------------------
# 출력 헬퍼
# ---------------------------------------------------------------------------

def _print_events(test_name: str, events: list[dict]) -> None:
    SEP = "=" * 65
    print(f"\n{SEP}")
    print(f"[{test_name}] SSE 이벤트 수신 결과")
    print(SEP)

    for e in events:
        if e["event"] == "error":
            print(f"\n[ERROR] {e['data'].get('message', e['data'])}")

    chunks = [e for e in events if e["event"] == "chunk"]
    done_list = [e for e in events if e["event"] == "done"]

    full_text = "".join(e["data"].get("content", "") for e in chunks)
    print(f"\nchunk: {len(chunks)}회 | 텍스트 미리보기:")
    print(f"  {full_text[:300]}{'...' if len(full_text) > 300 else ''}")

    if done_list:
        done = done_list[0]["data"]
        print(f"\ndone 이벤트:")
        print(f"  type        : {done.get('type')}")
        print(f"  userMessage : {done.get('userMessage', {}).get('content', '')[:80]}")
        print(f"  assistMsg   : {done.get('assistantMessage', {}).get('content', '')[:80]}")
        has_embedding = done.get("userMessage", {}).get("embedding") is not None
        print(f"  embedding   : {'있음' if has_embedding else '없음'}")
        print(f"  memory      : {'있음' if done.get('memory') else 'null'}")

        if itinerary := done.get("itinerary"):
            days = list(itinerary["dayPlans"].keys())
            print(f"  dayPlans 날짜: {days}")
            for date_key, items in itinerary["dayPlans"].items():
                print(f"\n  ── {date_key} ({len(items)}개 항목)")
                for item in items:
                    cost = item.get("cost")
                    cost_str = f"{cost['currency']} {cost['amount']}" if cost else "무료"
                    print(f"    {item.get('time','?')}  {item.get('plan_name','?')}  [{cost_str}]")

        if change := done.get("change"):
            print(f"  change      : {json.dumps(change, ensure_ascii=False)}")

    print(f"\n{SEP}\n")


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def rate_limit_guard():
    yield
    await asyncio.sleep(20)


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_token():
    """잘못된 X-Internal-Token → 403"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/ai-messages",
            headers={"X-Internal-Token": "wrong-token"},
            json={"roomId": "any-room", "content": "테스트"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_chat_type():
    """chat 타입: 일반 질문 → chunk + done(type=chat) 확인"""
    events = await _call_ai_messages("5월 날씨 어때?")
    _print_events("chat_type", events)
    _assert_no_error(events)

    chunks = [e for e in events if e["event"] == "chunk"]
    done_list = [e for e in events if e["event"] == "done"]

    assert len(chunks) > 0, "chunk 이벤트 없음"
    assert len(done_list) == 1, "done 이벤트가 1개여야 함"

    done = done_list[0]["data"]
    assert done["type"] == "chat"
    assert done["userMessage"]["content"] == "5월 날씨 어때?"
    assert done["assistantMessage"]["content"]
    assert done["userMessage"]["embedding"] is not None
    assert done["assistantMessage"]["embedding"] is not None


@pytest.mark.asyncio
async def test_itinerary_type():
    """itinerary 타입: 일정 생성 → done(type=itinerary) + dayPlans 날짜 키 형식 확인"""
    events = await _call_ai_messages(
        "여행 일정 짜줘."
    )
    _print_events("itinerary_type", events)
    _assert_no_error(events)

    done_list = [e for e in events if e["event"] == "done"]
    assert len(done_list) == 1

    done = done_list[0]["data"]
    assert done["type"] == "itinerary", f"type이 itinerary가 아님: {done['type']}"
    assert done.get("itinerary") is not None, "itinerary 페이로드 없음"

    day_plans = done["itinerary"]["dayPlans"]
    assert len(day_plans) > 0, "dayPlans가 비어 있음"

    for key in day_plans:
        assert len(key) == 10 and key[4] == "-" and key[7] == "-", \
            f"dayPlans 키가 YYYY-MM-DD 형식이 아님: '{key}'"

    for date_key, items in day_plans.items():
        assert len(items) > 0, f"{date_key}에 일정 항목 없음"
        for item in items:
            assert item.get("plan_name"), f"{date_key} 항목에 plan_name 없음"
            assert item.get("time"), f"{date_key} 항목에 time 없음"
            assert item.get("place"), f"{date_key} 항목에 place 없음"


@pytest.mark.asyncio
async def test_change_type():
    """change 타입: 여행 기본 정보 변경 → done(type=change) + change 페이로드 확인"""
    events = await _call_ai_messages("여행 날짜 5월 1일부터 4일로 바꿔줘.")
    _print_events("change_type", events)
    _assert_no_error(events)

    done_list = [e for e in events if e["event"] == "done"]
    assert len(done_list) == 1

    done = done_list[0]["data"]
    assert done["type"] == "change", f"type이 change가 아님: {done['type']}"
    assert done.get("change") is not None, "change 페이로드 없음"
