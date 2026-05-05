from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


# ── 요청 ──────────────────────────────────────────────────────────────────────

class MemoryInput(BaseModel):
    aiSummary: str | None = None
    preferences: dict[str, Any] | None = None


class AiMessageRequest(BaseModel):
    roomId: str
    content: str
    memory: MemoryInput | None = None


# ── classification_agent 출력 (내부 구조체) ───────────────────────────────────

class DayPlanItem(BaseModel):
    plan_name: str
    time: str                   # "HH:MM ~ HH:MM"
    place: str
    note: str = ""


class ResponseClassification(BaseModel):
    type: Literal["chat", "itinerary", "change", "reservation", "cancel"]

    # change — dayPlans는 submit_itinerary 도구로 별도 캡처됨
    startDate: str | None = None
    endDate: str | None = None
    budget: float | None = None
    adultCount: int | None = None
    childCount: int | None = None
    childAges: list[int] | None = None

    # reservation
    reservation: dict[str, Any] | None = None

    # cancel
    reservationId: str | None = None
    cancelledAt: str | None = None

    # 메모리 갱신 (변경 없으면 None)
    ai_summary: str | None = None
    preferences: dict[str, Any] | None = None


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
