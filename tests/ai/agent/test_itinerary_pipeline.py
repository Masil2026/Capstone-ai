"""
itinerary_pipeline.py 순수 함수 및 Mock 단위 테스트

LLM 호출 없음. 외부 API는 Mock.
검증 항목:
  - _all_dates: 날짜 범위 계산
  - _is_transport_day: 교통 이동일 판별
  - _get_replan_dates_for_date_change: 날짜 변경 시나리오별 재계획 날짜 산출
    · 말단 연장 (m=1, m=2)
    · 시작 연장 (m=1, m=2)
    · 말단 단축
    · 시작 단축
    · 날짜 변경 없음
    · 경계에 교통일 없음
  - _fetch_flight_legs: 귀국편 arriving_at 필터링

실행:
  pytest tests/ai/agent/test_itinerary_pipeline.py -s
"""
import pytest
from datetime import date
from unittest.mock import AsyncMock, patch

from app.services.agents.itinerary_pipeline import (
    _all_dates,
    _is_day_trip,
    _is_transport_day,
    _get_replan_dates_for_date_change,
    _normalize_overnight_day_plans,
    _fetch_flight_legs,
    _fetch_hotels,
    _normalize_booking_hotels,
    _korea_pick_image,
    _korea_keyword,
    _attach_media,
    _no_hotels,
    _build_planner_prompt,
    _build_synthesizer_prompt,
    _origin_ctx,
    PlannerDeps,
    SynthesizerDeps,
    PlannerOutput,
    SelectedFlight,
    SelectedHotel,
    _service,
)
from app.schemas.ai_message import DayPlanItem


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _dest(city: str, start: str, end: str) -> dict:
    return {"city": city, "start_date": start, "end_date": end}


def _plan(plan_name: str) -> dict:
    return {"plan_name": plan_name, "time": "10:00 ~ 11:00", "place": "", "note": "", "cost": None}


def _flight_plan() -> dict:
    return _plan("인천국제공항(ICN) → 도쿄 나리타(NRT) 항공 이동 (대한항공)")


def _return_plan() -> dict:
    return _plan("도쿄 나리타(NRT) → 인천국제공항(ICN) 귀국 항공 이동 (대한항공)")


def _inflight_plan() -> dict:
    return _plan("대한항공 기내 (비행 중) → ICN 도착")


def _content_plan(name: str = "아사쿠사 관광") -> dict:
    return _plan(name)


# ── _all_dates ────────────────────────────────────────────────────────────────

class TestAllDates:
    def test_single_destination(self):
        dests = [_dest("도쿄", "2026-05-15", "2026-05-18")]
        result = _all_dates(dests)
        assert result == ["2026-05-15", "2026-05-16", "2026-05-17", "2026-05-18"]

    def test_multi_destination(self):
        dests = [
            _dest("파리", "2026-12-20", "2026-12-23"),
            _dest("로마", "2026-12-23", "2026-12-26"),
        ]
        result = _all_dates(dests)
        assert result[0] == "2026-12-20"
        assert result[-1] == "2026-12-26"
        assert len(result) == 7

    def test_same_start_end(self):
        dests = [_dest("서울", "2026-06-01", "2026-06-01")]
        assert _all_dates(dests) == ["2026-06-01"]


# ── _is_transport_day ─────────────────────────────────────────────────────────

class TestIsTransportDay:
    def test_flight_item_detected(self):
        assert _is_transport_day([_flight_plan()]) is True

    def test_return_flight_detected(self):
        assert _is_transport_day([_return_plan()]) is True

    def test_inflight_item_detected(self):
        assert _is_transport_day([_inflight_plan()]) is True

    def test_content_items_not_transport(self):
        items = [_content_plan("아사쿠사 관광"), _content_plan("라멘 점심")]
        assert _is_transport_day(items) is False

    def test_empty_list(self):
        assert _is_transport_day([]) is False

    def test_mixed_items_one_transport(self):
        items = [_content_plan("저녁 식사"), _flight_plan()]
        assert _is_transport_day(items) is True

    def test_airport_transfer_alone_not_transport(self):
        """'공항 이동'만으로는 교통 이동일 판별 안 됨 (항공 이동·기내가 기준)"""
        item = _plan("숙소 → 인천국제공항 이동 (공항버스)")
        assert _is_transport_day([item]) is False


# ── _normalize_overnight_day_plans ────────────────────────────────────────────

class TestNormalizeOvernightDayPlans:
    def test_split_cross_midnight_item_into_next_date(self):
        day_plans = {
            "2026-05-29": [
                {
                    "plan_name": "삼성혈해물탕 본점 저녁 식사",
                    "time": "23:45 ~ 00:45",
                    "place": "삼성혈해물탕 본점",
                    "note": "",
                    "cost": {"amount": 18000, "currency": "KRW", "amount_krw": None},
                }
            ]
        }

        result = _normalize_overnight_day_plans(day_plans)

        assert result["2026-05-29"][0]["time"] == "23:45 ~ 23:59"
        assert result["2026-05-29"][0]["cost"]["amount"] == 18000
        assert result["2026-05-30"][0]["time"] == "00:00 ~ 00:45"
        assert result["2026-05-30"][0]["cost"] is None

    def test_keep_same_day_item_unchanged(self):
        item = _plan("성산일출봉 등반")
        item["time"] = "14:40 ~ 16:10"

        result = _normalize_overnight_day_plans({"2026-05-30": [item]})

        assert result == {"2026-05-30": [item]}

    def test_move_early_continuation_item_when_same_bucket_has_overnight_item(self):
        early_return = _plan("숙소 귀환 및 휴식")
        early_return["time"] = "00:50 ~ 01:00"
        dinner = _plan("삼성혈해물탕 본점 저녁 식사")
        dinner["time"] = "23:45 ~ 00:45"

        result = _normalize_overnight_day_plans({"2026-05-29": [early_return, dinner]})

        assert [item["time"] for item in result["2026-05-29"]] == ["23:45 ~ 23:59"]
        assert [item["time"] for item in result["2026-05-30"]] == ["00:00 ~ 00:45", "00:50 ~ 01:00"]

    def test_move_early_rest_until_morning_when_same_bucket_has_late_night_items(self):
        sleep = _plan("숙소 귀환 및 휴식")
        sleep["time"] = "01:55 ~ 07:30"
        sleep["note"] = "숙소에서 취침 및 휴식"
        airport = _plan("숙소 → 인천국제공항(ICN) 이동 (택시)")
        airport["time"] = "19:00 ~ 20:30"
        dinner = _plan("작제도 흑돼지 장작구이 저녁 식사")
        dinner["time"] = "23:45 ~ 00:45"

        result = _normalize_overnight_day_plans({"2026-05-29": [sleep, airport, dinner]})

        assert [item["time"] for item in result["2026-05-29"]] == ["19:00 ~ 20:30", "23:45 ~ 23:59"]
        assert [item["time"] for item in result["2026-05-30"]] == ["00:00 ~ 00:45", "01:55 ~ 07:30"]


# ── _get_replan_dates_for_date_change ─────────────────────────────────────────

class TestGetReplanDates:

    # ── 날짜 변경 없음 ────────────────────────────────────────────────────────

    def test_no_date_change_returns_empty(self):
        itinerary = {
            "destinations": [_dest("도쿄", "2026-05-15", "2026-05-18")],
            "day_plans": {
                "2026-05-15": [_flight_plan()],
                "2026-05-16": [_content_plan()],
                "2026-05-17": [_content_plan()],
                "2026-05-18": [_return_plan()],
            },
        }
        adjusted, replan = _get_replan_dates_for_date_change(itinerary)
        assert replan == []

    def test_no_day_plans_returns_empty(self):
        itinerary = {
            "destinations": [_dest("도쿄", "2026-05-15", "2026-05-18")],
        }
        adjusted, replan = _get_replan_dates_for_date_change(itinerary)
        assert replan == []

    # ── 말단 연장 ─────────────────────────────────────────────────────────────

    def test_end_extension_same_day_return_flight(self):
        """마지막에 2일 연장 + 기존 마지막날 당일 귀국편(m=1) → 3일 재계획"""
        itinerary = {
            "destinations": [_dest("도쿄", "2026-05-15", "2026-05-20")],  # 15→18에서 15→20으로 연장
            "day_plans": {
                "2026-05-15": [_flight_plan()],
                "2026-05-16": [_content_plan()],
                "2026-05-17": [_content_plan()],
                "2026-05-18": [_return_plan()],  # ← 기존 마지막날 귀국편 (m=1)
            },
        }
        adjusted, replan = _get_replan_dates_for_date_change(itinerary)

        # 기존 귀국편 날짜 제거됐는지
        assert "2026-05-18" not in adjusted
        # 재계획 = 기존 귀국일(18) + 새 2일(19, 20)
        assert "2026-05-18" in replan
        assert "2026-05-19" in replan
        assert "2026-05-20" in replan
        # 변경 없는 날짜는 adjusted에 유지
        assert "2026-05-16" in adjusted
        assert "2026-05-17" in adjusted

    def test_end_extension_overnight_return_flight(self):
        """마지막에 2일 연장 + 야간 귀국편(m=2, 출발일+도착일) → 4일 재계획"""
        itinerary = {
            "destinations": [_dest("파리", "2026-12-20", "2026-12-26")],  # 24→26 연장
            "day_plans": {
                "2026-12-20": [_flight_plan()],
                "2026-12-21": [_content_plan()],
                "2026-12-22": [_content_plan()],
                "2026-12-23": [_return_plan()],  # 야간 출발 (m=2 중 첫날)
                "2026-12-24": [_inflight_plan()],  # 기내 연속 (m=2 중 둘째날)
            },
        }
        adjusted, replan = _get_replan_dates_for_date_change(itinerary)

        assert "2026-12-23" not in adjusted
        assert "2026-12-24" not in adjusted
        assert "2026-12-23" in replan
        assert "2026-12-24" in replan
        assert "2026-12-25" in replan
        assert "2026-12-26" in replan
        assert "2026-12-21" in adjusted
        assert "2026-12-22" in adjusted

    # ── 시작 연장 ─────────────────────────────────────────────────────────────

    def test_start_extension_same_day_departure(self):
        """시작에 2일 연장 + 기존 첫날 당일 출발편(m=1) → 3일 재계획"""
        itinerary = {
            "destinations": [_dest("도쿄", "2026-05-13", "2026-05-18")],  # 15→13으로 연장
            "day_plans": {
                "2026-05-15": [_flight_plan()],  # ← 기존 첫날 출발편 (m=1)
                "2026-05-16": [_content_plan()],
                "2026-05-17": [_content_plan()],
                "2026-05-18": [_return_plan()],
            },
        }
        adjusted, replan = _get_replan_dates_for_date_change(itinerary)

        assert "2026-05-15" not in adjusted
        assert "2026-05-13" in replan
        assert "2026-05-14" in replan
        assert "2026-05-15" in replan
        assert "2026-05-16" in adjusted
        assert "2026-05-17" in adjusted

    def test_start_extension_overnight_arrival(self):
        """시작에 2일 연장 + 야간 출발편(m=2, 출발일+도착일) → 4일 재계획"""
        itinerary = {
            "destinations": [_dest("런던", "2026-12-18", "2026-12-26")],  # 20→18 연장
            "day_plans": {
                "2026-12-20": [_flight_plan()],   # 야간 출발 (m=2 중 첫날)
                "2026-12-21": [_inflight_plan()], # 기내 연속 (m=2 중 둘째날)
                "2026-12-22": [_content_plan()],
                "2026-12-23": [_content_plan()],
                "2026-12-26": [_return_plan()],
            },
        }
        adjusted, replan = _get_replan_dates_for_date_change(itinerary)

        assert "2026-12-20" not in adjusted
        assert "2026-12-21" not in adjusted
        assert "2026-12-18" in replan
        assert "2026-12-19" in replan
        assert "2026-12-20" in replan
        assert "2026-12-21" in replan
        assert "2026-12-22" in adjusted

    # ── 말단 단축 ─────────────────────────────────────────────────────────────

    def test_end_shortening(self):
        """마지막 2일 단축 (20→18) → 범위 밖 제거 + 새 마지막 2일 재계획"""
        itinerary = {
            "destinations": [_dest("도쿄", "2026-05-15", "2026-05-18")],  # 20→18 단축
            "day_plans": {
                "2026-05-15": [_flight_plan()],
                "2026-05-16": [_content_plan()],
                "2026-05-17": [_content_plan()],
                "2026-05-18": [_content_plan()],  # 기존엔 일반 일정 (귀국편은 19-20에 있었음)
                "2026-05-19": [_return_plan()],   # 범위 밖 (제거)
                "2026-05-20": [_inflight_plan()], # 범위 밖 (제거)
            },
        }
        adjusted, replan = _get_replan_dates_for_date_change(itinerary)

        # 범위 밖 날짜 제거
        assert "2026-05-19" not in adjusted
        assert "2026-05-20" not in adjusted
        # 새 마지막날(18)과 그 전날(17)은 재계획 (귀국편 배치용)
        assert "2026-05-18" in replan
        assert "2026-05-17" in replan
        # 재계획 날짜는 new_end(18) 이하만 포함
        assert all(d <= "2026-05-18" for d in replan)

    # ── 시작 단축 ─────────────────────────────────────────────────────────────

    def test_start_shortening(self):
        """시작 2일 단축 (15→17) → 범위 밖 제거 + 새 첫날 2일 재계획"""
        itinerary = {
            "destinations": [_dest("도쿄", "2026-05-17", "2026-05-20")],  # 15→17 단축
            "day_plans": {
                "2026-05-15": [_flight_plan()],   # 범위 밖 (제거)
                "2026-05-16": [_inflight_plan()], # 범위 밖 (제거)
                "2026-05-17": [_content_plan()],  # 기존엔 일반 일정
                "2026-05-18": [_content_plan()],
                "2026-05-19": [_content_plan()],
                "2026-05-20": [_return_plan()],
            },
        }
        adjusted, replan = _get_replan_dates_for_date_change(itinerary)

        assert "2026-05-15" not in adjusted
        assert "2026-05-16" not in adjusted
        # 새 첫날(17)과 그 다음날(18) 재계획 (출발편 배치용)
        assert "2026-05-17" in replan
        assert "2026-05-18" in replan
        assert all(d >= "2026-05-17" for d in replan)

    # ── 경계에 교통일 없음 ────────────────────────────────────────────────────

    def test_end_extension_no_transport_at_boundary(self):
        """기존 마지막날이 일반 일정 → 교통일 제거 없이 신규 날짜만 재계획"""
        itinerary = {
            "destinations": [_dest("도쿄", "2026-05-15", "2026-05-20")],
            "day_plans": {
                "2026-05-15": [_content_plan()],
                "2026-05-16": [_content_plan()],
                "2026-05-17": [_content_plan()],
                "2026-05-18": [_content_plan()],  # 교통일 없음
            },
        }
        adjusted, replan = _get_replan_dates_for_date_change(itinerary)

        # 기존 날짜 유지
        assert "2026-05-18" in adjusted
        # 신규 날짜만 재계획
        assert "2026-05-19" in replan
        assert "2026-05-20" in replan
        assert "2026-05-18" not in replan


# ── _fetch_flight_legs: 귀국편 arriving_at 필터링 ─────────────────────────────

class TestFetchFlightLegsFilter:

    def _make_offer(self, arriving_at: str) -> dict:
        """Booking search_flights offer(segments 구조) 형태."""
        return {
            "token": "TKN",
            "trip_type": "ONEWAY",
            "is_direct": True,
            "price": 1200000,
            "price_rounded": 1200000,
            "currency": "KRW",
            "segments": [{
                "from": "PRG",
                "to": "ICN",
                "departure_time": "2026-12-29T22:00:00+01:00",
                "arrival_time": arriving_at,
                "total_time_sec": 43200,
                "stops": 0,
                "legs": [{
                    "from": "PRG", "to": "ICN", "flight_number": "TA1",
                    "carriers": ["Test Air"], "logo": "http://logo/ta.png",
                    "cabin_class": "ECONOMY",
                }],
            }],
        }

    def _loc_ok(self, params):
        code = (params.get("query", "XXX")[:3] or "XXX").upper()
        return {"status": "success", "data": {"selected": {"id": f"{code}.AIRPORT"}}}

    @pytest.mark.asyncio
    async def test_filters_offers_arriving_after_end_date(self):
        """end_date 이후 도착 귀국편은 결과에서 제외"""
        end_date = "2026-12-30"
        valid_offer = self._make_offer("2026-12-30T18:00:00+09:00")    # 당일 도착 ✓
        invalid_offer = self._make_offer("2026-12-31T18:00:00+09:00")  # 익일 도착 ✗

        def _mock_task(tool, action, params):
            if action == "search_flight_location":
                return self._loc_ok(params)
            flights = [invalid_offer] if params.get("departDate") == end_date else [valid_offer]
            return {"status": "success", "data": {"flights": flights, "booking_list_url": "http://list"}}

        destinations = [{"city": "프라하", "start_date": "2026-12-20", "end_date": end_date}]
        with patch.object(_service, "process_task", side_effect=AsyncMock(side_effect=_mock_task)):
            legs = await _fetch_flight_legs(
                destinations=destinations,
                cities_en=["Prague"],
                adults=2, children=0, child_ages=[],
            )

        return_leg = next(l for l in legs if l["direction"] == "return")
        data = return_leg["data"]
        assert data["status"] == "success"
        offers = data["data"]
        # invalid_offer(31일 도착)는 필터링, valid_offer(30일 도착)만 남아야 함
        assert len(offers) == 1
        assert offers[0]["arriving_at"].startswith("2026-12-30")

    @pytest.mark.asyncio
    async def test_keeps_offers_arriving_on_end_date(self):
        """end_date 당일 도착 귀국편은 필터링되지 않고 유지"""
        end_date = "2026-05-18"
        offer_same_day = self._make_offer("2026-05-18T16:00:00+09:00")
        offer_prev_day = self._make_offer("2026-05-18T10:00:00+09:00")

        def _mock_task(tool, action, params):
            if action == "search_flight_location":
                return self._loc_ok(params)
            flights = [offer_same_day] if params.get("departDate") == end_date else [offer_prev_day]
            return {"status": "success", "data": {"flights": flights, "booking_list_url": "http://list"}}

        destinations = [{"city": "도쿄", "start_date": "2026-05-15", "end_date": end_date}]
        with patch.object(_service, "process_task", side_effect=AsyncMock(side_effect=_mock_task)):
            legs = await _fetch_flight_legs(
                destinations=destinations,
                cities_en=["Tokyo"],
                adults=2, children=0, child_ages=[],
            )

        return_leg = next(l for l in legs if l["direction"] == "return")
        offers = return_leg["data"]["data"]
        assert len(offers) == 2
        assert all(o["arriving_at"][:10] <= end_date for o in offers)

    @pytest.mark.asyncio
    async def test_fallback_to_all_when_no_valid_offers(self):
        """end_date 이내 도착편이 없으면 전체 결과를 fallback으로 제공"""
        end_date = "2026-12-30"
        only_late_offer = self._make_offer("2026-12-31T18:00:00+09:00")

        def _mock_task(tool, action, params):
            if action == "search_flight_location":
                return self._loc_ok(params)
            return {"status": "success", "data": {"flights": [only_late_offer], "booking_list_url": "http://list"}}

        destinations = [{"city": "런던", "start_date": "2026-12-20", "end_date": end_date}]
        with patch.object(_service, "process_task", side_effect=AsyncMock(side_effect=_mock_task)):
            legs = await _fetch_flight_legs(
                destinations=destinations,
                cities_en=["London"],
                adults=1, children=0, child_ages=[],
            )

        return_leg = next(l for l in legs if l["direction"] == "return")
        assert return_leg["data"]["status"] == "success"
        assert len(return_leg["data"]["data"]) >= 1

    @pytest.mark.asyncio
    async def test_depart_and_connect_legs_unaffected(self):
        """출발·경유 구간은 필터링 영향 없음"""
        end_date = "2026-12-30"

        def _mock_task(tool, action, params):
            if action == "search_flight_location":
                return self._loc_ok(params)
            return {"status": "success", "data": {"flights": [], "booking_list_url": "http://list"}}

        destinations = [
            {"city": "파리", "start_date": "2026-12-20", "end_date": "2026-12-25"},
            {"city": "로마", "start_date": "2026-12-25", "end_date": end_date},
        ]
        with patch.object(_service, "process_task", side_effect=AsyncMock(side_effect=_mock_task)):
            legs = await _fetch_flight_legs(
                destinations=destinations,
                cities_en=["Paris", "Rome"],
                adults=2, children=0, child_ages=[],
            )

        directions = [l["direction"] for l in legs]
        assert "depart" in directions
        assert "connect" in directions
        assert "return" in directions
        assert len(legs) == 3  # depart(0) + connect(1) + return(2)

    @pytest.mark.asyncio
    async def test_normalizes_booking_offer_to_flat_shape(self):
        """Booking offer가 평면 shape + image_url(로고)·url(리스트)로 정규화되는지"""
        offer = self._make_offer("2026-12-24T18:00:00+09:00")

        def _mock_task(tool, action, params):
            if action == "search_flight_location":
                return self._loc_ok(params)
            return {"status": "success", "data": {"flights": [offer], "booking_list_url": "http://list"}}

        destinations = [{"city": "프라하", "start_date": "2026-12-20", "end_date": "2026-12-25"}]
        with patch.object(_service, "process_task", side_effect=AsyncMock(side_effect=_mock_task)):
            legs = await _fetch_flight_legs(
                destinations=destinations, cities_en=["Prague"],
                adults=1, children=0, child_ages=[],
            )

        depart = next(l for l in legs if l["direction"] == "depart")
        f = depart["data"]["data"][0]
        assert f["airline"] == "Test Air"
        assert f["origin"] == "PRG" and f["destination"] == "ICN"
        assert f["price_krw"] == 1200000 and f["currency"] == "KRW"
        assert f["image_url"] == "http://logo/ta.png"   # 대표 항공사 로고
        assert f["url"] == "http://list"                # 검색 리스트 URL


# ── _is_day_trip ────────────────────────────────────────────────────────────

class TestIsDayTrip:
    def test_same_start_end_is_day_trip(self):
        assert _is_day_trip({"start_date": "2026-06-01", "end_date": "2026-06-01"}) is True

    def test_different_start_end_is_not_day_trip(self):
        assert _is_day_trip({"start_date": "2026-06-01", "end_date": "2026-06-03"}) is False

    def test_missing_start_date_is_not_day_trip(self):
        assert _is_day_trip({"end_date": "2026-06-01"}) is False

    def test_datetime_suffix_ignored(self):
        """start_date/end_date에 시간까지 포함돼도 날짜(앞 10자리)만 비교"""
        itinerary = {"start_date": "2026-06-01T00:00:00", "end_date": "2026-06-01T00:00:00"}
        assert _is_day_trip(itinerary) is True


# ── _no_hotels ────────────────────────────────────────────────────────────────

class TestNoHotels:
    @pytest.mark.asyncio
    async def test_returns_skipped_status_per_city(self):
        destinations = [_dest("서울", "2026-06-01", "2026-06-01")]
        result = await _no_hotels(destinations)
        assert result == {"서울": {"status": "skipped", "message": "당일치기로 숙소 검색 생략"}}


# ── 당일치기 프롬프트 분기 (_build_planner_prompt / _build_synthesizer_prompt) ──

def _make_planner_deps(is_day_trip: bool, origin: str | None = None) -> PlannerDeps:
    destinations = [_dest("서울", "2026-06-01", "2026-06-01")]
    itinerary_info = {
        "destinations": destinations,
        "start_date": "2026-06-01",
        "end_date": "2026-06-01",
        "total_days": 1,
        "budget": None,
        "adult_count": 2,
        "child_count": 0,
        "child_ages": [],
        "day_plans": None,
    }
    return PlannerDeps(
        itinerary_info=itinerary_info,
        web_summaries={"서울": "정보 없음"},
        weather_by_city={"서울": []},
        flight_legs=[],
        hotels_by_city={"서울": {"status": "skipped"}},
        cities_en=["Seoul"],
        preferences=None,
        ai_summary=None,
        today="2026-05-01",
        similar_messages=[],
        replan_dates=[],
        is_day_trip=is_day_trip,
        origin=origin,
    )


def _make_synth_deps(is_day_trip: bool, origin: str | None = None) -> SynthesizerDeps:
    destinations = [_dest("서울", "2026-06-01", "2026-06-01")]
    itinerary_info = {
        "destinations": destinations,
        "start_date": "2026-06-01",
        "end_date": "2026-06-01",
        "total_days": 1,
        "budget": None,
        "adult_count": 2,
        "child_count": 0,
        "child_ages": [],
        "day_plans": None,
    }
    return SynthesizerDeps(
        itinerary_info=itinerary_info,
        planner_output=PlannerOutput(days=[], selected_flights=[], selected_hotels=[]),
        place_results={},
        route_results={},
        weather_by_city={"서울": []},
        web_summaries={"서울": "정보 없음"},
        preferences=None,
        ai_summary=None,
        today="2026-05-01",
        similar_messages=[],
        attraction_prices={},
        replan_dates=[],
        is_day_trip=is_day_trip,
        origin=origin,
    )


class TestPlannerPromptDayTrip:
    def test_day_trip_hides_hotel_sections(self):
        prompt = _build_planner_prompt(_make_planner_deps(is_day_trip=True))
        assert "## 숙소 데이터 (도시별)" not in prompt
        assert "당일치기(숙박 없음)이므로 빈 배열로 반환" in prompt
        assert "## 당일치기 시간 범위 제약" in prompt
        assert "하루 총 4~6개 항목" in prompt

    def test_normal_trip_keeps_hotel_sections(self):
        prompt = _build_planner_prompt(_make_planner_deps(is_day_trip=False))
        assert "## 숙소 데이터 (도시별)" in prompt
        assert "2. selected_hotels: 각 도시별 숙소 1개씩 선택" in prompt
        assert "## 당일치기 시간 범위 제약" not in prompt
        assert "하루 총 7~10개 항목" in prompt


class TestSynthesizerPromptDayTrip:
    def test_day_trip_hides_hotel_sections(self):
        prompt = _build_synthesizer_prompt(_make_synth_deps(is_day_trip=True))
        assert "## 숙소 귀환·휴식 배치 규칙" not in prompt
        assert "## 선택된 숙소" not in prompt
        assert "- 숙소 체크인 항목" not in prompt
        assert "## 당일치기 시작/종료 시간 범위" in prompt
        assert "편의점 간단 식사 후 귀가 이동 준비" in prompt

    def test_normal_trip_keeps_hotel_sections(self):
        prompt = _build_synthesizer_prompt(_make_synth_deps(is_day_trip=False))
        assert "## 숙소 귀환·휴식 배치 규칙" in prompt
        assert "## 선택된 숙소" in prompt
        assert "- 숙소 체크인 항목" in prompt
        assert "## 당일치기 시작/종료 시간 범위" not in prompt


# ── _origin_ctx: 출발지 문구 조립 ─────────────────────────────────────────────

class TestOriginCtx:
    def test_missing_origin_falls_back_to_korea_wording(self):
        """출발지 미입력 시 기존 대한민국/ICN·GMP 문구 그대로 유지 (하위 호환)"""
        ctx = _origin_ctx(None)
        assert ctx["word"] == "한국"
        assert ctx["full"] == "대한민국"
        assert "인천국제공항(ICN)" in ctx["airport_note"]
        assert "김포공항(GMP)" in ctx["airport_note"]
        assert ctx["return_note"] == "한국(ICN/GMP)"

    def test_given_origin_uses_generalized_wording(self):
        """출발지가 있으면 도시명 기반 일반화 문구를 사용하고, ICN/GMP를 못박지 않는다"""
        ctx = _origin_ctx("부산")
        assert ctx["word"] == "부산"
        assert ctx["full"] == "부산"
        assert ctx["return_note"] == "부산"
        assert "ICN" not in ctx["airport_note"]
        assert "GMP" not in ctx["airport_note"]


# ── 플래너/합성기 프롬프트 — 출발지 반영 ───────────────────────────────────────

class TestPlannerPromptOrigin:
    def test_missing_origin_keeps_existing_korea_wording(self):
        prompt = _build_planner_prompt(_make_planner_deps(is_day_trip=False, origin=None))
        assert "출발지: 대한민국 — 항공 출발 공항은 인천국제공항(ICN) 또는 김포공항(GMP)이다." in prompt
        assert "여행 경로: 한국 → 서울 → 한국" in prompt
        assert "⚠️ 귀국편(return) 필수 제약: 한국(ICN/GMP) 도착 일자가" in prompt

    def test_given_origin_replaces_korea_wording(self):
        prompt = _build_planner_prompt(_make_planner_deps(is_day_trip=False, origin="부산"))
        assert "출발지: 부산 —" in prompt
        assert "여행 경로: 부산 → 서울 → 부산" in prompt
        assert "⚠️ 귀국편(return) 필수 제약: 부산 도착 일자가" in prompt
        assert "인천국제공항(ICN) 또는 김포공항(GMP)" not in prompt


class TestSynthesizerPromptOrigin:
    def test_missing_origin_keeps_existing_korea_wording(self):
        prompt = _build_synthesizer_prompt(_make_synth_deps(is_day_trip=False, origin=None))
        assert "이 여행의 출발지는 대한민국이다. 항공 출발 공항은 인천국제공항(ICN) 또는 김포공항(GMP)이다." in prompt
        assert "여행 경로: 한국 → 서울 → 한국" in prompt
        assert "1일차 첫 항목: 한국 출발 항공 이동" in prompt
        assert "마지막날 마지막 항목: 한국 귀국 항공 이동" in prompt

    def test_given_origin_replaces_korea_wording(self):
        prompt = _build_synthesizer_prompt(_make_synth_deps(is_day_trip=False, origin="부산"))
        assert "이 여행의 출발지는 부산이다." in prompt
        assert "여행 경로: 부산 → 서울 → 부산" in prompt
        assert "1일차 첫 항목: 부산 출발 항공 이동" in prompt
        assert "마지막날 마지막 항목: 부산 귀국 항공 이동" in prompt


# ── 당일치기 + 출발지 결합 ────────────────────────────────────────────────────

class TestDayTripWithOrigin:
    """당일치기(is_day_trip=True)와 출발지 지정이 함께 와도 문구가 충돌 없이 결합되는지 확인."""

    def test_planner_prompt_combines_day_trip_and_origin(self):
        prompt = _build_planner_prompt(_make_planner_deps(is_day_trip=True, origin="부산"))
        # 당일치기 전용 섹션 유지
        assert "## 당일치기 시간 범위 제약" in prompt
        assert "하루 총 4~6개 항목" in prompt
        assert "2. selected_hotels: 당일치기(숙박 없음)이므로 빈 배열로 반환" in prompt
        # 출발지 반영 문구도 함께 적용
        assert "출발지: 부산 —" in prompt
        assert "여행 경로: 부산 → 서울 → 부산" in prompt
        assert "인천국제공항(ICN) 또는 김포공항(GMP)" not in prompt

    def test_synthesizer_prompt_combines_day_trip_and_origin(self):
        prompt = _build_synthesizer_prompt(_make_synth_deps(is_day_trip=True, origin="부산"))
        # 당일치기 전용 섹션 유지 (숙소 귀환 규칙 없음)
        assert "## 당일치기 시작/종료 시간 범위" in prompt
        assert "## 숙소 귀환·휴식 배치 규칙" not in prompt
        assert "## 선택된 숙소" not in prompt
        # 출발지 반영 문구도 함께 적용
        assert "이 여행의 출발지는 부산이다." in prompt
        assert "1일차 첫 항목: 부산 출발 항공 이동" in prompt
        assert "마지막날 마지막 항목: 부산 귀국 항공 이동" in prompt


# ── _fetch_flight_legs: 출발지 파라미터화 ─────────────────────────────────────

class TestFetchFlightLegsOrigin:
    def _loc_ok(self, params):
        code = (params.get("query", "XXX")[:3] or "XXX").upper()
        return {"status": "success", "data": {"selected": {"id": f"{code}.AIRPORT"}}}

    @pytest.mark.asyncio
    async def test_defaults_to_seoul_when_origin_omitted(self):
        """origin_en 생략 시 기존 동작(Seoul 기준)과 동일 — 하위 호환"""
        def _mock_task(tool, action, params):
            if action == "search_flight_location":
                return self._loc_ok(params)
            return {"status": "success", "data": {"flights": [], "booking_list_url": "http://list"}}

        destinations = [_dest("도쿄", "2026-06-01", "2026-06-03")]
        with patch.object(_service, "process_task", side_effect=AsyncMock(side_effect=_mock_task)):
            legs = await _fetch_flight_legs(
                destinations=destinations, cities_en=["Tokyo"],
                adults=1, children=0, child_ages=[],
            )

        depart = next(l for l in legs if l["direction"] == "depart")
        return_leg = next(l for l in legs if l["direction"] == "return")
        assert depart["from"] == "Seoul"
        assert return_leg["to"] == "Seoul"

    @pytest.mark.asyncio
    async def test_uses_given_origin_for_depart_and_return(self):
        """origin_en을 지정하면 출발/귀국 구간이 해당 출발지 기준으로 검색된다"""
        def _mock_task(tool, action, params):
            if action == "search_flight_location":
                return self._loc_ok(params)
            return {"status": "success", "data": {"flights": [], "booking_list_url": "http://list"}}

        destinations = [_dest("도쿄", "2026-06-01", "2026-06-03")]
        with patch.object(_service, "process_task", side_effect=AsyncMock(side_effect=_mock_task)):
            legs = await _fetch_flight_legs(
                destinations=destinations, cities_en=["Tokyo"],
                adults=1, children=0, child_ages=[],
                origin_en="Busan",
            )

        depart = next(l for l in legs if l["direction"] == "depart")
        return_leg = next(l for l in legs if l["direction"] == "return")
        assert depart["from"] == "Busan"
        assert return_leg["to"] == "Busan"


# ── Booking 숙소 정규화 ───────────────────────────────────────────────────────

class TestNormalizeBookingHotels:
    def _raw(self):
        return {"status": "success", "data": {"hotels": [
            {"hotel_id": 111, "name": "롯데호텔", "summary": "명동 5성급",
             "price": 300000, "review_score": 8.9, "star": 5,
             "photo": "http://img/lotte.jpg"},
        ]}}

    def test_maps_to_flat_with_media(self):
        out = _normalize_booking_hotels(self._raw())
        assert out["status"] == "success"
        h = out["data"][0]
        assert h["name"] == "롯데호텔"
        assert h["price_krw"] == 300000
        assert h["rating"] == 8.9
        assert h["image_url"] == "http://img/lotte.jpg"
        assert h["hotel_id"] == 111
        assert h["url"] is None   # booking_url은 join 단계에서 채움

    def test_error_passthrough(self):
        out = _normalize_booking_hotels({"status": "error", "message": "x"})
        assert out["status"] == "error"
        assert out["data"] == []


# ── 한국관광공사 이미지 매칭 ──────────────────────────────────────────────────

class TestKoreaPickImage:
    def _raw(self, items):
        return {"status": "success", "data": {"items": items}}

    def test_firstimage_preferred(self):
        raw = self._raw([{"title": "경복궁", "firstimage": "a.jpg", "firstimage2": "b.jpg", "contentid": "1"}])
        assert _korea_pick_image(raw, "경복궁") == ("a.jpg", "1")

    def test_fallback_to_firstimage2(self):
        raw = self._raw([{"title": "경복궁", "firstimage": "", "firstimage2": "b.jpg", "contentid": "1"}])
        assert _korea_pick_image(raw, "경복궁") == ("b.jpg", "1")

    def test_no_image_returns_none_image(self):
        raw = self._raw([{"title": "경복궁", "firstimage": "", "firstimage2": "", "contentid": "1"}])
        assert _korea_pick_image(raw, "경복궁") == (None, "1")

    def test_no_match_returns_none(self):
        raw = self._raw([{"title": "다른곳", "firstimage": "a.jpg", "contentid": "1"},
                         {"title": "또다른곳", "firstimage": "c.jpg", "contentid": "2"}])
        assert _korea_pick_image(raw, "센소지") == (None, None)

    def test_error_returns_none(self):
        assert _korea_pick_image({"status": "error"}, "x") == (None, None)


# ── _attach_media: day_plans 후처리 조인 ──────────────────────────────────────

class TestAttachMedia:
    @pytest.mark.asyncio
    async def test_injects_place_hotel_flight_media(self):
        # day_plans: 관광지 + 호텔 체크인 + 항공 이동
        day_plans = {
            "2026-05-01": [
                DayPlanItem(plan_name="대한항공 기내 (비행 중) → NRT 도착", time="00:00 ~ 12:00", place="기내"),
                DayPlanItem(plan_name="센소지 관광", time="13:00 ~ 15:00", place="센소지"),
                DayPlanItem(plan_name="롯데호텔 체크인", time="18:00 ~ 19:00", place="롯데호텔"),
            ],
        }
        planner_output = PlannerOutput(
            days=[],
            selected_flights=[SelectedFlight(
                direction="depart", leg_index=0, airline="대한항공",
                origin="ICN", destination="NRT",
                departing_at="2026-05-01T09:00:00+09:00", arriving_at="2026-05-01T12:00:00+09:00",
                price_original=500000, currency="KRW", price_krw=500000, stops=0,
            )],
            selected_hotels=[SelectedHotel(
                city="도쿄", name="롯데호텔", address="신주쿠",
                check_in="2026-05-01", check_out="2026-05-03",
                price_original=300000, currency="KRW", price_krw=300000,
            )],
        )
        place_results = {"센소지": {"status": "success", "image_url": "http://kto/senso.jpg", "contentid": "9"}}
        flight_legs = [{"leg_index": 0, "direction": "depart", "data": {"status": "success", "data": [{
            "airline": "대한항공", "departing_at": "2026-05-01T09:00:00+09:00",
            "image_url": "http://logo/ke.png", "url": "http://booking/list",
        }]}}]
        hotels_by_city = {"도쿄": {"status": "success", "data": [{
            "name": "롯데호텔", "hotel_id": 111, "image_url": "http://img/lotte.jpg",
        }]}}

        def _mock_task(tool, action, params):
            if action == "get_hotel_details":
                return {"status": "success", "data": {"booking_url": "http://booking/lotte"}}
            return {"status": "error"}

        with patch.object(_service, "process_task", side_effect=AsyncMock(side_effect=_mock_task)):
            out = await _attach_media(day_plans, planner_output, place_results,
                                      flight_legs, hotels_by_city, adults=1, child_ages=[])

        items = {it.plan_name: it for it in out["2026-05-01"]}
        # 항공: 로고 + 검색 리스트 URL
        flight = items["대한항공 기내 (비행 중) → NRT 도착"]
        assert flight.image_url == "http://logo/ke.png"
        assert flight.url == "http://booking/list"
        # 장소: 한국관광공사 firstimage
        assert items["센소지 관광"].image_url == "http://kto/senso.jpg"
        assert items["센소지 관광"].url is None
        # 숙소: 사진 + booking_url(상세 호출)
        hotel = items["롯데호텔 체크인"]
        assert hotel.image_url == "http://img/lotte.jpg"
        assert hotel.url == "http://booking/lotte"

    @pytest.mark.asyncio
    async def test_no_match_keeps_none(self):
        day_plans = {"2026-05-01": [DayPlanItem(plan_name="자유 산책", time="10:00 ~ 11:00", place="어딘가")]}
        planner_output = PlannerOutput(days=[], selected_flights=[], selected_hotels=[])
        with patch.object(_service, "process_task", side_effect=AsyncMock(return_value={"status": "error"})):
            out = await _attach_media(day_plans, planner_output, {}, [], {}, adults=1, child_ages=[])
        item = out["2026-05-01"][0]
        assert item.image_url is None and item.url is None


# ── 국내 노선 김포(GMP) 우선 ──────────────────────────────────────────────────

class TestDomesticGimpoPreference:
    def _loc(self, city):
        table = {
            "Seoul": {"selected": {"id": "ICN.AIRPORT", "code": "ICN", "country": "대한민국"},
                      "candidates": [{"id": "ICN.AIRPORT", "code": "ICN", "country": "대한민국"},
                                     {"id": "GMP.AIRPORT", "code": "GMP", "country": "대한민국"}]},
            "Jeju": {"selected": {"id": "CJU.AIRPORT", "code": "CJU", "country": "대한민국"},
                     "candidates": [{"id": "CJU.AIRPORT", "code": "CJU", "country": "대한민국"}]},
            "Tokyo": {"selected": {"id": "NRT.AIRPORT", "code": "NRT", "country": "Japan"},
                      "candidates": [{"id": "NRT.AIRPORT", "code": "NRT", "country": "Japan"}]},
        }
        return {"status": "success", "data": table[city]}

    async def _run(self, city_en, dest_city):
        calls = []

        def _mock_task(tool, action, params):
            if action == "search_flight_location":
                return self._loc(params["query"])
            if action == "search_flights":
                calls.append(params)
            return {"status": "success", "data": {"flights": [], "booking_list_url": "x"}}

        destinations = [{"city": dest_city, "start_date": "2026-06-01", "end_date": "2026-06-03"}]
        with patch.object(_service, "process_task", side_effect=AsyncMock(side_effect=_mock_task)):
            await _fetch_flight_legs(destinations=destinations, cities_en=[city_en],
                                     adults=1, children=0, child_ages=[])
        return {i for p in calls for i in (p["fromId"], p["toId"])}

    @pytest.mark.asyncio
    async def test_domestic_uses_gimpo_not_incheon(self):
        """제주(국내) 여행: 서울 출발이 ICN이 아니라 GMP로 잡혀야 함"""
        ids = await self._run("Jeju", "제주")
        assert "GMP.AIRPORT" in ids
        assert "ICN.AIRPORT" not in ids   # 인천 국제선 오선택 방지

    @pytest.mark.asyncio
    async def test_international_uses_incheon(self):
        """도쿄(국제) 여행: 서울 출발은 기존대로 ICN"""
        ids = await self._run("Tokyo", "도쿄")
        assert "ICN.AIRPORT" in ids
        assert "GMP.AIRPORT" not in ids


# ── 한국관광공사 검색 키워드 추출 (Google용 ordered_query → 장소명) ──────────────

class TestKoreaKeyword:
    def test_extracts_place_name(self):
        assert _korea_keyword("비자림 제주 (Jeju)", "제주도") == "비자림"
        assert _korea_keyword("만장굴 제주 (Jeju)", "제주도") == "만장굴"

    def test_keeps_multiword_attraction(self):
        assert _korea_keyword("오설록 티 뮤지엄 제주 (Jeju)", "제주도") == "오설록 티 뮤지엄"

    def test_skips_meal_and_transit_queries(self):
        assert _korea_keyword("저녁식사 흑돼지 구이 제주 (Jeju)", "제주도") is None
        assert _korea_keyword("제주국제공항 근처 아침식사 제주 (Jeju)", "제주도") is None

    def test_strips_city_variants(self):
        # 도시명이 '제주도'/'제주' 어느 형태로 붙어도 제거
        assert _korea_keyword("성산일출봉 제주도 (Jeju)", "제주도") == "성산일출봉"
