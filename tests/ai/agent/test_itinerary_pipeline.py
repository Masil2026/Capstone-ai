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
    _no_hotels,
    _build_planner_prompt,
    _build_synthesizer_prompt,
    PlannerDeps,
    SynthesizerDeps,
    PlannerOutput,
    _service,
)


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
        return {
            "airline": "Test Air",
            "origin": "PRG",
            "destination": "ICN",
            "departing_at": "2026-12-29T22:00:00+01:00",
            "arriving_at": arriving_at,
            "price_original": 800.0,
            "currency": "EUR",
            "price_krw": 1200000,
            "stops": 0,
        }

    @pytest.mark.asyncio
    async def test_filters_offers_arriving_after_end_date(self):
        """end_date 이후 도착 귀국편은 결과에서 제외"""
        end_date = "2026-12-30"
        valid_offer = self._make_offer("2026-12-30T18:00:00+09:00")    # 당일 도착 ✓
        invalid_offer = self._make_offer("2026-12-31T18:00:00+09:00")  # 익일 도착 ✗

        def _mock_task(tool, action, params):
            departure_date = params.get("departure_date", "")
            if departure_date == end_date:
                return {"status": "success", "data": [invalid_offer], "count": 1}
            else:  # end_date - 1
                return {"status": "success", "data": [valid_offer], "count": 1}

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
        # end_date 출발편 → 당일 도착 (유효)
        offer_same_day = self._make_offer("2026-05-18T16:00:00+09:00")
        # end_date-1 출발편 → 당일 도착 (유효)
        offer_prev_day = self._make_offer("2026-05-18T10:00:00+09:00")

        def _mock_task(tool, action, params):
            departure_date = params.get("departure_date", "")
            if departure_date == end_date:
                return {"status": "success", "data": [offer_same_day], "count": 1}
            else:
                return {"status": "success", "data": [offer_prev_day], "count": 1}

        destinations = [{"city": "도쿄", "start_date": "2026-05-15", "end_date": end_date}]
        with patch.object(_service, "process_task", side_effect=AsyncMock(side_effect=_mock_task)):
            legs = await _fetch_flight_legs(
                destinations=destinations,
                cities_en=["Tokyo"],
                adults=2, children=0, child_ages=[],
            )

        return_leg = next(l for l in legs if l["direction"] == "return")
        offers = return_leg["data"]["data"]
        # 두 날짜 모두 end_date 도착이므로 2개 모두 유지
        assert len(offers) == 2
        # 모든 offer가 end_date 이내 도착임을 확인
        assert all(o["arriving_at"][:10] <= end_date for o in offers)

    @pytest.mark.asyncio
    async def test_fallback_to_all_when_no_valid_offers(self):
        """end_date 이내 도착편이 없으면 전체 결과를 fallback으로 제공"""
        end_date = "2026-12-30"
        only_late_offer = self._make_offer("2026-12-31T18:00:00+09:00")

        def _mock_task(tool, action, params):
            return {"status": "success", "data": [only_late_offer], "count": 1}

        destinations = [{"city": "런던", "start_date": "2026-12-20", "end_date": end_date}]
        with patch.object(_service, "process_task", side_effect=AsyncMock(side_effect=_mock_task)):
            legs = await _fetch_flight_legs(
                destinations=destinations,
                cities_en=["London"],
                adults=1, children=0, child_ages=[],
            )

        return_leg = next(l for l in legs if l["direction"] == "return")
        # 유효 편 없으면 전체 결과 유지 (fallback)
        assert return_leg["data"]["status"] == "success"
        assert len(return_leg["data"]["data"]) >= 1

    @pytest.mark.asyncio
    async def test_depart_and_connect_legs_unaffected(self):
        """출발·경유 구간은 필터링 영향 없음"""
        end_date = "2026-12-30"

        def _mock_task(tool, action, params):
            return {"status": "success", "data": [], "count": 0}

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

def _make_planner_deps(is_day_trip: bool) -> PlannerDeps:
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
    )


def _make_synth_deps(is_day_trip: bool) -> SynthesizerDeps:
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
