from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


# ── 요청 ──────────────────────────────────────────────────────────────────────

class AiMessageRequest(BaseModel):
    roomId: str
    content: str


# ── classification_agent 출력 (내부 구조체) ───────────────────────────────────

class ItemCost(BaseModel):
    amount: float               # 현지 통화 금액 (1인 기준). 예) 1500.0, 280.0
    currency: str               # ISO 4217 현지 통화 코드. 예) "JPY", "USD", "KRW"
    amount_krw: int | None = None  # 한화 환산 금액. currency == "KRW"이면 null


class DayPlanItem(BaseModel):
    plan_name: str
    time: str                   # "HH:MM ~ HH:MM"
    place: str
    note: str = ""
    cost: ItemCost | None = None  # 예상 비용 (1인 기준). 무료이면 null


class ResponseClassification(BaseModel):
    type: Literal["chat", "itinerary", "change", "reservation", "cancel"]


# ── 오케스트레이터 result_type DTO ────────────────────────────────────────────

class ChangeFields(BaseModel):
    start_date: str | None = None   # YYYY-MM-DD
    end_date: str | None = None
    budget: float | None = None
    adult_count: int | None = None
    child_count: int | None = None
    child_ages: list[int] | None = None


class CancelFields(BaseModel):
    reservation_id: str
    cancelled_at: str               # ISO 8601


class ReservationFields(BaseModel):
    reservation_type: str           # "flight" | "hotel"
    detail: dict[str, Any]
    booking_url: str | None = None
    external_ref_id: str | None = None
    total_price: float | None = None
    currency: str | None = None
    reserved_at: str | None = None


class OrchestratorResult(BaseModel):
    """오케스트레이터 구조화 출력. message가 첫 번째 필드여야 스트리밍이 가능하다."""
    message: str                                        # 항상 필수 — 자연어 응답
    ai_summary: str | None = None                      # itinerary·change 후 항상 작성
    preferences: dict[str, Any] | None = None          # 취향 업데이트 시만
    day_plans: dict[str, list[DayPlanItem]] | None = None   # itinerary 타입
    change: ChangeFields | None = None                 # change 타입
    reservation: ReservationFields | None = None       # reservation 타입
    cancel: CancelFields | None = None                 # cancel 타입


# ── done 이벤트 페이로드 ──────────────────────────────────────────────────────

class MessageWithEmbedding(BaseModel):
    content: str
    embedding: list[float] | None = None


class MemoryOutput(BaseModel):
    aiSummary: str | None = None
    preferences: dict[str, Any] | None = None


class ItineraryPayload(BaseModel):
    dayPlans: dict[str, list[DayPlanItem]]


class ChangePayload(BaseModel):
    startDate: str | None = None
    endDate: str | None = None
    budget: float | None = None
    adultCount: int | None = None
    childCount: int | None = None
    childAges: list[int] | None = None

    def model_dump_exclude_none(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class CancelPayload(BaseModel):
    reservationId: str
    cancelledAt: str


class DoneEvent(BaseModel):
    type: Literal["chat", "itinerary", "change", "reservation", "cancel"]
    userMessage: MessageWithEmbedding
    assistantMessage: MessageWithEmbedding
    memory: MemoryOutput | None = None

    # 타입별 조건부 필드
    itinerary: ItineraryPayload | None = None
    change: ChangePayload | None = None
    reservation: dict[str, Any] | None = None
    cancel: CancelPayload | None = None

    model_config = {"populate_by_name": True}
