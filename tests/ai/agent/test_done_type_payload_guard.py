from app.controller.aiMessageController import _build_done_event
from app.schemas.ai_message import (
    CancelFields,
    ChangeFields,
    DestinationItem,
    OrchestratorResult,
    ReservationFields,
)


_EMBEDDING = [0.1] * 1536


def _done(request_type: str, result: OrchestratorResult):
    return _build_done_event(
        request_type=request_type,
        user_message="test user message",
        user_embedding=_EMBEDDING,
        full_response=result.message,
        assistant_embedding=_EMBEDDING,
        orch_result=result,
        merged_summary=None,
        merged_prefs=None,
    )


def test_empty_reservation_payload_downgrades_to_chat():
    done = _done("reservation", OrchestratorResult(message="예약할 항목을 찾지 못했습니다."))

    assert done.type == "chat"
    assert done.reservation is None


def test_present_reservation_payload_keeps_reservation_type():
    done = _done(
        "reservation",
        OrchestratorResult(
            message="예약했습니다.",
            reservation=ReservationFields(
                reservation_type="flight",
                detail={"airline": "Korean Air", "departure": "ICN", "arrival": "NRT"},
                external_ref_id="FLT-20260519-ABC123",
            ),
        ),
    )

    assert done.type == "reservation"
    assert done.reservation["detail"]["airline"] == "Korean Air"


def test_empty_change_payload_downgrades_to_chat():
    done = _done("change", OrchestratorResult(message="변경할 기본 정보를 찾지 못했습니다."))

    assert done.type == "chat"
    assert done.change is None


def test_present_change_payload_keeps_change_type():
    done = _done(
        "change",
        OrchestratorResult(
            message="여행 날짜를 변경했습니다.",
            change=ChangeFields(
                destinations=[DestinationItem(city="Tokyo", start_date="2026-06-01", end_date="2026-06-03")]
            ),
        ),
    )

    assert done.type == "change"
    assert done.change.destinations == [{"city": "Tokyo", "start_date": "2026-06-01", "end_date": "2026-06-03"}]


def test_empty_cancel_payload_downgrades_to_chat():
    done = _done("cancel", OrchestratorResult(message="취소할 예약을 찾지 못했습니다."))

    assert done.type == "chat"
    assert done.cancel is None


def test_present_cancel_payload_keeps_cancel_type():
    done = _done(
        "cancel",
        OrchestratorResult(
            message="예약을 취소했습니다.",
            cancel=CancelFields(reservation_id="reservation-1", cancelled_at="2026-05-19T12:00:00+09:00"),
        ),
    )

    assert done.type == "cancel"
    assert done.cancel.reservationId == "reservation-1"
