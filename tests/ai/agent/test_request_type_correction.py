from app.controller.aiMessageController import (
    _correct_request_type,
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
