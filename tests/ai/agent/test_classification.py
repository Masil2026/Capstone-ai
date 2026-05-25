import pytest
from app.services.agents.classification import classification_agent
from app.schemas.ai_message import ResponseClassification

pytestmark = pytest.mark.llm


def _print_result(test_name: str, result: ResponseClassification) -> None:
    print("\n" + "=" * 55)
    print(f"[{test_name}] type: {result.type}")
    print("=" * 55 + "\n")


# ---------------------------------------------------------------------------
# itinerary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_classification_itinerary_new():
    """신규 일정 생성 요청 → itinerary"""
    r = await classification_agent.run("도쿄 3박 4일 여행 일정 만들어줘. 5월 1일 출발이야.")
    result: ResponseClassification = r.output
    _print_result("itinerary_new", result)
    assert result.type == "itinerary"


@pytest.mark.asyncio
async def test_classification_itinerary_modify():
    """기존 일정 수정 요청 → itinerary"""
    r = await classification_agent.run("1일차 경복궁을 창덕궁으로 바꿔줘.")
    result: ResponseClassification = r.output
    _print_result("itinerary_modify", result)
    assert result.type == "itinerary"


@pytest.mark.asyncio
async def test_classification_itinerary_add():
    """일정 추가 요청 → itinerary"""
    r = await classification_agent.run("3일차에 오사카 도톤보리 저녁 일정 추가해줘.")
    result: ResponseClassification = r.output
    _print_result("itinerary_add", result)
    assert result.type == "itinerary"


# ---------------------------------------------------------------------------
# change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_classification_change_dates():
    """날짜 변경 요청 → change"""
    r = await classification_agent.run("여행 날짜 5월 10일부터 15일로 바꿔줘.")
    result: ResponseClassification = r.output
    _print_result("change_dates", result)
    assert result.type == "change"


@pytest.mark.asyncio
async def test_classification_change_budget():
    """예산 변경 요청 → change"""
    r = await classification_agent.run("예산 100만원으로 늘려줘.")
    result: ResponseClassification = r.output
    _print_result("change_budget", result)
    assert result.type == "change"


@pytest.mark.asyncio
async def test_classification_change_travelers():
    """인원 변경 요청 → change"""
    r = await classification_agent.run("성인 2명, 아이 1명(7살)으로 변경해줘.")
    result: ResponseClassification = r.output
    _print_result("change_travelers", result)
    assert result.type == "change"


# ---------------------------------------------------------------------------
# itinerary vs change 경계 케이스
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_classification_boundary_place_vs_date():
    """장소 변경(itinerary) vs 날짜 변경(change) 구분"""
    r_place = await classification_agent.run("2일차 남산타워 대신 한강공원으로 바꿔줘.")
    r_date = await classification_agent.run("출발일을 5월 5일로 바꿔줘.")
    _print_result("boundary_place", r_place.output)
    _print_result("boundary_date", r_date.output)
    assert r_place.output.type == "itinerary"
    assert r_date.output.type == "change"


@pytest.mark.asyncio
async def test_classification_hotel_change_is_itinerary():
    """항공/숙소 항목 변경 요청 → itinerary"""
    r = await classification_agent.run("호텔 바꿔줘")
    result: ResponseClassification = r.output
    _print_result("hotel_change", result)
    assert result.type == "itinerary"


@pytest.mark.asyncio
async def test_classification_transport_change_is_itinerary():
    """일정 내 이동수단 변경 요청 → itinerary"""
    r = await classification_agent.run("인천공항 갈때 택시말고 버스타고 갈래")
    result: ResponseClassification = r.output
    _print_result("transport_change", result)
    assert result.type == "itinerary"


@pytest.mark.asyncio
async def test_classification_train_transport_change_is_itinerary():
    """KTX 등 이동수단 변경 요청 → itinerary"""
    r = await classification_agent.run("부산 갈때 비행기 말고 KTX 타고 갈래")
    result: ResponseClassification = r.output
    _print_result("train_transport_change", result)
    assert result.type == "itinerary"


# ---------------------------------------------------------------------------
# reservation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_classification_reservation_flight():
    """항공권 예약 요청 → reservation"""
    r = await classification_agent.run("대한항공 KE705편으로 예약해줘.")
    result: ResponseClassification = r.output
    _print_result("reservation_flight", result)
    assert result.type == "reservation"


@pytest.mark.asyncio
async def test_classification_reservation_hotel():
    """숙소 예약 요청 → reservation"""
    r = await classification_agent.run("신주쿠 그랜드 호텔 3박으로 예약해줘.")
    result: ResponseClassification = r.output
    _print_result("reservation_hotel", result)
    assert result.type == "reservation"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_classification_cancel():
    """예약 취소 요청 → cancel"""
    r = await classification_agent.run("항공권 예약 취소해줘.")
    result: ResponseClassification = r.output
    _print_result("cancel", result)
    assert result.type == "cancel"


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_classification_chat_weather():
    """날씨 정보 요청 → chat"""
    r = await classification_agent.run("5월 도쿄 날씨 어때?")
    result: ResponseClassification = r.output
    _print_result("chat_weather", result)
    assert result.type == "chat"


@pytest.mark.asyncio
async def test_classification_chat_question():
    """일반 질문 → chat"""
    r = await classification_agent.run("도쿄에서 오사카 이동할 때 신칸센이랑 비행기 중 뭐가 나아?")
    result: ResponseClassification = r.output
    _print_result("chat_question", result)
    assert result.type == "chat"


@pytest.mark.asyncio
async def test_classification_chat_greeting():
    """일반 대화 → chat"""
    r = await classification_agent.run("고마워! 여행 기대된다.")
    result: ResponseClassification = r.output
    _print_result("chat_greeting", result)
    assert result.type == "chat"
