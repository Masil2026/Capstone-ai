import pytest
from app.services.agents.context import load_context

_ROOM_ID = "32f7e9e2-f6e6-4ead-9717-537f6768b2c1"
_USER_MESSAGE = "도쿄 3박 4일 여행 일정 만들어줘"


def _print_context(ctx: dict) -> None:
    print("\n" + "=" * 60)
    print(f"[load_context] room_id: {_ROOM_ID}")
    print("-" * 60)

    emb = ctx.get("user_embedding")
    print(f"user_embedding   : dim={len(emb)}, first3={emb[:3]}" if emb else "user_embedding   : None")

    history = ctx.get("history") or []
    print(f"history          : {len(history)}개 메시지")

    print(f"ai_summary       : {ctx.get('ai_summary')}")
    print(f"preferences      : {ctx.get('preferences')}")

    similar = ctx.get("similar_messages") or []
    print(f"similar_messages : {len(similar)}개")
    for i, m in enumerate(similar, 1):
        print(f"  [{i}] role={m['role']} | {m['content'][:60]}...")

    itinerary = ctx.get("current_itinerary")
    if itinerary:
        print(f"current_itinerary: destination={itinerary.get('destination')}, "
              f"{itinerary.get('start_date')} ~ {itinerary.get('end_date')}, "
              f"total_days={itinerary.get('total_days')}")
    else:
        print("current_itinerary: None (저장된 일정 없음)")

    print("=" * 60 + "\n")


@pytest.mark.asyncio
async def test_load_context():
    """실제 room_id로 context 전체 파이프라인 통합 테스트"""
    ctx = await load_context(_ROOM_ID, _USER_MESSAGE)

    _print_context(ctx)

    # user_embedding: 1536차원 float 리스트
    assert isinstance(ctx["user_embedding"], list)
    assert len(ctx["user_embedding"]) == 1536

    # history: 리스트 (비어있을 수 있음)
    assert isinstance(ctx["history"], list)

    # similar_messages: 리스트 (비어있을 수 있음)
    assert isinstance(ctx["similar_messages"], list)

    # current_itinerary: dict 또는 None
    assert ctx["current_itinerary"] is None or isinstance(ctx["current_itinerary"], dict)
