from app.controller.aiMessageController import (
    _correct_request_type,
    _has_date_change_signal,
    _is_itinerary_item_change_request,
    _should_confirm_reservation_change,
)


def test_airport_transport_change_is_itinerary_request():
    message = "인천공항 갈때 택시말고 버스타고 갈래"

    assert _is_itinerary_item_change_request(message)
    assert _correct_request_type("chat", message, {"day_plans": {"2026-05-18": []}}) == "itinerary"


def test_train_transport_change_is_itinerary_request():
    message = "부산 갈때 비행기 말고 KTX 타고 갈래"

    assert _is_itinerary_item_change_request(message)
    assert _correct_request_type("chat", message, {"day_plans": {"2026-05-18": []}}) == "itinerary"


def test_private_car_transport_change_is_itinerary_request():
    message = "렌터카 말고 자차로 이동할게"

    assert _is_itinerary_item_change_request(message)
    assert _correct_request_type("chat", message, {"day_plans": {"2026-05-18": []}}) == "itinerary"


def test_airport_transport_change_does_not_override_without_current_itinerary():
    message = "인천공항 갈때 택시말고 버스타고 갈래"

    assert _correct_request_type("chat", message, None) == "chat"


def test_airport_transport_change_does_not_trigger_reservation_change_confirmation():
    message = "인천공항 갈때 택시말고 버스타고 갈래"
    reservations = [{"id": "reservation-1", "type": "flight", "detail": {"airline": "대한항공"}}]

    assert not _should_confirm_reservation_change(message, reservations)


def test_departure_date_change_keeps_change_type():
    """'출발'+'바꿔'가 있어도 날짜 신호가 있으면 change 분류를 유지한다."""
    message = "출발 날짜를 7월 20일로 바꿔줘"

    assert _has_date_change_signal(message)
    assert _correct_request_type("change", message, {"day_plans": {"2026-05-18": []}}) == "change"


def test_arrival_date_change_keeps_change_type():
    """'도착'+'수정'이 있어도 월/일 표기가 있으면 change 분류를 유지한다."""
    message = "도착일을 8월 5일로 수정해줘"

    assert _has_date_change_signal(message)
    assert _correct_request_type("change", message, {"day_plans": {"2026-05-18": []}}) == "change"


def test_period_change_keeps_change_type():
    message = "여행 기간을 3박 4일로 변경해서 하루 일찍 출발할래"

    assert _has_date_change_signal(message)
    assert _correct_request_type("change", message, {"day_plans": {"2026-05-18": []}}) == "change"


def test_transport_change_without_date_signal_still_overrides_to_itinerary():
    """날짜 신호가 없는 항목 변경은 기존대로 itinerary로 강제 보정된다."""
    message = "공항 이동은 리무진버스로 바꿔줘"

    assert not _has_date_change_signal(message)
    assert _correct_request_type("change", message, {"day_plans": {"2026-05-18": []}}) == "itinerary"
