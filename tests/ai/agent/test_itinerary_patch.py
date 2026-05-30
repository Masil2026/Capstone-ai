from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.agents import itinerary_patch
from app.services.agents.itinerary_patch import try_patch_itinerary_item


def _deps(current_itinerary):
    return SimpleNamespace(
        current_itinerary=current_itinerary,
        ai_summary="1. 부산 2박 3일 일정 생성",
        preferences={},
    )


@pytest.mark.asyncio
async def test_transport_patch_only_calls_route_lookup():
    itinerary = {
        "adult_count": 2,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-18": [
                {
                    "plan_name": "숙소 → 인천국제공항 이동 (택시)",
                    "time": "17:30 ~ 18:40",
                    "place": "숙소 → 인천국제공항",
                    "note": "",
                    "cost": None,
                },
                {
                    "plan_name": "인천국제공항(ICN) → 김해공항(PUS) 항공 이동",
                    "time": "20:00 ~ 21:00",
                    "place": "ICN → PUS",
                    "note": "",
                    "cost": {"amount": 80000, "currency": "KRW"},
                },
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        assert tool_name == "google_maps"
        assert action == "find_route"
        return {
            "status": "success",
            "data": {
                "routes": [
                    {
                        "duration_text": "1시간 30분",
                        "distance_text": "65 km",
                        "fare": {"currency": "KRW", "text": "₩17,000", "value": 17000},
                    }
                ]
            },
        }

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)) as mocked:
        result = await try_patch_itinerary_item(_deps(itinerary), "인천공항 갈때 택시말고 버스타고 갈래")

    assert result is not None
    mocked.assert_awaited_once()
    patched_items = result.day_plans["2026-05-18"]
    assert patched_items[0].plan_name == "숙소 → 인천국제공항 이동 (버스)"
    assert patched_items[0].cost.amount == 34000
    assert patched_items[1].plan_name == "인천국제공항(ICN) → 김해공항(PUS) 항공 이동"


@pytest.mark.asyncio
async def test_transport_patch_handles_bus_to_private_car_without_route_fare():
    itinerary = {
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-18": [
                {
                    "plan_name": "숙소 → 인천국제공항 이동 (버스)",
                    "time": "17:30 ~ 18:40",
                    "place": "숙소 → 인천국제공항",
                    "note": "",
                    "cost": {"amount": 17000, "currency": "KRW"},
                }
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        assert tool_name == "google_maps"
        assert action == "find_route"
        assert params["mode"] == "driving"
        return {
            "status": "success",
            "data": {
                "routes": [
                    {
                        "duration_text": "1시간 5분",
                        "distance_text": "58 km",
                        "fare": None,
                    }
                ]
            },
        }

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)) as mocked:
        result = await try_patch_itinerary_item(_deps(itinerary), "인천공항 갈때 버스말고 자차타고 갈래")

    assert result is not None
    mocked.assert_awaited_once()
    item = result.day_plans["2026-05-18"][0]
    assert item.plan_name == "숙소 → 인천국제공항 이동 (자차)"
    assert item.cost is None
    assert "예상 소요시간: 1시간 5분" in item.note


@pytest.mark.asyncio
async def test_flight_patch_only_calls_duffel_flight_lookup():
    itinerary = {
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-18": [
                {
                    "plan_name": "인천국제공항(ICN) → 김해공항(PUS) 항공 이동 (기존항공)",
                    "time": "20:00 ~ 21:00",
                    "place": "ICN → PUS",
                    "note": "",
                    "cost": {"amount": 80000, "currency": "KRW"},
                }
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        assert tool_name == "duffel_flight"
        assert action == "search_flights"
        return {
            "status": "success",
            "data": [
                {
                    "price_original": 91000,
                    "currency": "KRW",
                    "price_krw": 91000,
                    "airline": "대한항공",
                    "origin": "ICN",
                    "destination": "PUS",
                    "departing_at": "2026-05-18T19:20:00",
                    "arriving_at": "2026-05-18T20:25:00",
                    "stops": 0,
                    "duration": "1h 5m",
                }
            ],
        }

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)) as mocked:
        result = await try_patch_itinerary_item(_deps(itinerary), "항공편 다른 걸로 바꿔줘")

    assert result is not None
    mocked.assert_awaited_once()
    item = result.day_plans["2026-05-18"][0]
    assert item.plan_name == "ICN → PUS 항공 이동 (대한항공)"
    assert item.cost.amount == 91000


@pytest.mark.asyncio
async def test_flight_patch_ignores_airport_transfer_items():
    itinerary = {
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-18": [
                {
                    "plan_name": "숙소 → 인천국제공항 이동 (택시)",
                    "time": "17:30 ~ 18:40",
                    "place": "인천국제공항",
                    "note": "",
                    "cost": None,
                },
                {
                    "plan_name": "인천국제공항(ICN) → 제주국제공항(CJU) 항공 이동 (기존항공)",
                    "time": "20:54 ~ 22:04",
                    "place": "인천국제공항(ICN) → 제주국제공항(CJU)",
                    "note": "",
                    "cost": {"amount": 40586, "currency": "KRW"},
                },
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        assert tool_name == "duffel_flight"
        assert action == "search_flights"
        return {
            "status": "success",
            "data": [
                {
                    "price_original": 43000,
                    "currency": "KRW",
                    "price_krw": 43000,
                    "airline": "대한항공",
                    "origin": "ICN",
                    "destination": "CJU",
                    "departing_at": "2026-05-18T19:40:00",
                    "arriving_at": "2026-05-18T20:50:00",
                    "stops": 0,
                    "duration": "1h 10m",
                }
            ],
        }

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)) as mocked:
        result = await try_patch_itinerary_item(_deps(itinerary), "인천으로 오는 항공 저녁시간으로 바꿔줘")

    assert result is not None
    mocked.assert_awaited_once()
    assert result.day_plans["2026-05-18"][0].plan_name == "숙소 → 인천국제공항 이동 (택시)"
    assert result.day_plans["2026-05-18"][1].plan_name == "ICN → CJU 항공 이동 (대한항공)"


@pytest.mark.asyncio
async def test_return_flight_patch_syncs_next_day_arrival_and_transfer():
    itinerary = {
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-31": [
                {
                    "plan_name": "신라스테이 제주 체크아웃 및 제주국제공항 이동 (택시)",
                    "time": "08:00 ~ 08:20",
                    "place": "제주국제공항",
                    "note": "공항까지 택시 약 12,000원.",
                    "cost": {"amount": 12000, "currency": "KRW"},
                },
                {
                    "plan_name": "제주국제공항(CJU) → 인천국제공항(ICN) 귀국 항공 (Duffel Airways)",
                    "time": "10:00 ~ 11:10",
                    "place": "제주국제공항(CJU) → 인천국제공항(ICN)",
                    "note": "",
                    "cost": {"amount": 40005, "currency": "KRW"},
                },
                {
                    "plan_name": "인천국제공항 → 자택 이동 (지하철)",
                    "time": "11:30 ~ 12:30",
                    "place": "인천국제공항에서 자택",
                    "note": "여행 종료 및 귀가.",
                    "cost": {"amount": 5000, "currency": "KRW"},
                },
            ]
        },
    }

    async def mock_task(tool_name, action, params):
        assert tool_name == "duffel_flight"
        assert action == "search_flights"
        return {
            "status": "success",
            "data": [
                {
                    "price_original": 44630,
                    "currency": "AUD",
                    "price_krw": 39274,
                    "airline": "Duffel Airways",
                    "origin": "CJU",
                    "destination": "ICN",
                    "departing_at": "2026-05-31T23:13:00",
                    "arriving_at": "2026-06-01T00:23:00",
                    "stops": 0,
                    "duration": "1h 10m",
                }
            ],
        }

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "인천으로 오는 항공 저녁시간으로 바꿔줘")

    assert result is not None
    assert result.day_plans["2026-05-31"][0].time.startswith("20:")
    assert result.day_plans["2026-05-31"][1].time == "23:13 ~ 23:59"
    assert result.day_plans["2026-06-01"][0].plan_name == "Duffel Airways 기내 (비행 중) → ICN 도착"
    assert result.day_plans["2026-06-01"][0].time == "00:00 ~ 00:23"
    assert result.day_plans["2026-06-01"][1].time == "00:53 ~ 01:53"


@pytest.mark.asyncio
async def test_return_flight_patch_searches_both_dates_and_filters_by_end_date():
    """귀국편 변경: end_date + end_date-1 양쪽 검색, end_date 초과 도착편 필터링"""
    itinerary = {
        "start_date": "2026-05-27",
        "end_date": "2026-05-30",
        "destinations": [{"city": "부산", "start_date": "2026-05-27", "end_date": "2026-05-30"}],
        "adult_count": 2,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-30": [
                {
                    "plan_name": "김해공항(PUS) → 인천국제공항(ICN) 귀국 항공 (기존항공)",
                    "time": "08:00 ~ 09:10",
                    "place": "PUS → ICN",
                    "note": "",
                    "cost": {"amount": 130000, "currency": "KRW"},
                }
            ],
        },
    }
    call_dates = []

    async def mock_task(tool_name, action, params):
        call_dates.append(params["departure_date"])
        if params["departure_date"] == "2026-05-30":
            return {
                "status": "success",
                "data": [
                    {
                        "airline": "제주항공",
                        "origin": "PUS",
                        "destination": "ICN",
                        "departing_at": "2026-05-30T23:00:00",
                        "arriving_at": "2026-05-31T00:10:00",  # end_date 초과 → 필터링
                        "stops": 0,
                        "duration": "1h 10m",
                        "price_original": 90000,
                        "currency": "KRW",
                        "price_krw": 90000,
                    }
                ],
            }
        else:  # 2026-05-29
            return {
                "status": "success",
                "data": [
                    {
                        "airline": "대한항공",
                        "origin": "PUS",
                        "destination": "ICN",
                        "departing_at": "2026-05-29T07:00:00",
                        "arriving_at": "2026-05-29T08:10:00",  # end_date 이내, 당일 도착 → 유효
                        "stops": 0,
                        "duration": "1h 10m",
                        "price_original": 120000,
                        "currency": "KRW",
                        "price_krw": 120000,
                    }
                ],
            }

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)) as mocked:
        result = await try_patch_itinerary_item(_deps(itinerary), "귀국 항공 바꿔줘")

    assert result is not None
    assert mocked.await_count == 2
    assert sorted(call_dates) == ["2026-05-29", "2026-05-30"]
    # 제주항공(arriving_at=2026-05-31, end_date 초과)은 필터링되고 대한항공이 선택됨
    all_items = [item for items in result.day_plans.values() for item in items]
    assert any("대한항공" in item.plan_name for item in all_items)
    assert not any("제주항공" in item.plan_name for item in all_items)


@pytest.mark.asyncio
async def test_non_return_flight_patch_searches_single_date():
    """출발/연결편은 단일 날짜만 검색 (귀국편 이중 검색 로직 미적용)"""
    itinerary = {
        "start_date": "2026-05-27",
        "end_date": "2026-05-30",
        "destinations": [{"city": "부산", "start_date": "2026-05-27", "end_date": "2026-05-30"}],
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-27": [
                {
                    "plan_name": "인천국제공항(ICN) → 김해공항(PUS) 항공 이동",
                    "time": "09:00 ~ 10:10",
                    "place": "ICN → PUS",
                    "note": "",
                    "cost": {"amount": 90000, "currency": "KRW"},
                }
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        return {
            "status": "success",
            "data": [
                {
                    "airline": "진에어",
                    "origin": "ICN",
                    "destination": "PUS",
                    "departing_at": "2026-05-27T11:00:00",
                    "arriving_at": "2026-05-27T12:10:00",
                    "stops": 0,
                    "duration": "1h 10m",
                    "price_original": 75000,
                    "currency": "KRW",
                    "price_krw": 75000,
                }
            ],
        }

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)) as mocked:
        result = await try_patch_itinerary_item(_deps(itinerary), "출발 항공편 바꿔줘")

    assert result is not None
    assert mocked.await_count == 1  # 단일 날짜만 검색
    item = result.day_plans["2026-05-27"][0]
    assert item.plan_name == "ICN → PUS 항공 이동 (진에어)"


@pytest.mark.asyncio
async def test_named_flight_patch_asks_confirmation_when_candidate_unmatched():
    itinerary = {
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-18": [
                {
                    "plan_name": "인천국제공항(ICN) → 김해공항(PUS) 항공 이동 (기존항공)",
                    "time": "20:00 ~ 21:00",
                    "place": "ICN → PUS",
                    "note": "",
                    "cost": {"amount": 80000, "currency": "KRW"},
                }
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        return {
            "status": "success",
            "data": [
                {
                    "price_original": 91000,
                    "currency": "KRW",
                    "price_krw": 91000,
                    "airline": "Duffel Airways",
                    "origin": "ICN",
                    "destination": "PUS",
                    "departing_at": "2026-05-18T19:20:00",
                    "arriving_at": "2026-05-18T20:25:00",
                    "stops": 0,
                    "duration": "1h 5m",
                }
            ],
        }

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "대한항공으로 바꿔줘")

    assert result is not None
    assert result.day_plans is None
    assert "대한항공 항공편을 찾지 못했습니다" in result.message
    assert "Duffel Airways" in result.message


@pytest.mark.asyncio
async def test_hotel_patch_only_calls_duffel_accommodation_lookup():
    itinerary = {
        "destination": "부산",
        "start_date": "2026-05-18",
        "end_date": "2026-05-20",
        "adult_count": 2,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-18": [
                {
                    "plan_name": "기존 호텔 체크인",
                    "time": "15:00 ~ 16:00",
                    "place": "기존 호텔",
                    "note": "",
                    "cost": {"amount": 180000, "currency": "KRW"},
                }
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        assert tool_name == "duffel_accommodation"
        assert action == "search_hotels"
        return {
            "status": "success",
            "data": [
                {
                    "name": "부산 새 호텔",
                    "address": "부산 해운대구",
                    "price_original": 220000,
                    "currency": "KRW",
                    "price_krw": 220000,
                    "rating": 4.4,
                }
            ],
        }

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)) as mocked:
        result = await try_patch_itinerary_item(_deps(itinerary), "호텔 다른 곳으로 바꿔줘")

    assert result is not None
    mocked.assert_awaited_once()
    item = result.day_plans["2026-05-18"][0]
    assert item.plan_name == "부산 새 호텔 체크인"
    assert item.place == "부산 해운대구"


@pytest.mark.asyncio
async def test_other_hotel_request_is_not_treated_as_named_hotel():
    itinerary = {
        "destination": "제주",
        "start_date": "2026-05-28",
        "end_date": "2026-05-31",
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-28": [
                {
                    "plan_name": "기존 호텔 체크인",
                    "time": "22:25 ~ 22:45",
                    "place": "기존 호텔",
                    "note": "",
                    "cost": None,
                }
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        return {
            "status": "success",
            "data": [
                {"name": "Hotel Leo", "address": "14, Sammu-ro", "price_original": 120000, "currency": "KRW"},
                {"name": "Grand Hyatt Jeju", "address": "12 Noyeon-ro", "price_original": 240000, "currency": "KRW"},
            ],
        }

    with patch.object(itinerary_patch, "_extract_english_city", new=AsyncMock(return_value="Jeju")), \
         patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "다른 숙소로 바꿔줘")

    assert result is not None
    assert result.day_plans["2026-05-28"][0].plan_name == "Hotel Leo 체크인"
    assert "다른을(를) 찾지 못했습니다" not in result.message


@pytest.mark.asyncio
async def test_flight_patch_filters_afternoon_offers():
    """'오후 시간' 요청 시 12:00-18:00 출발편만 선택된다."""
    itinerary = {
        "start_date": "2026-05-27",
        "end_date": "2026-05-31",
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-31": [
                {
                    "plan_name": "CJU → ICN 항공 이동 (구항공)",
                    "time": "08:00 ~ 09:10",
                    "place": "CJU → ICN",
                    "note": "비행시간 1h 10m | 직항",
                    "cost": None,
                }
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        return {
            "status": "success",
            "data": [
                {
                    "price_original": 80000,
                    "currency": "KRW",
                    "price_krw": 80000,
                    "airline": "이른아침항공",
                    "origin": "CJU",
                    "destination": "ICN",
                    "departing_at": "2026-05-31T07:30:00",
                    "arriving_at": "2026-05-31T08:40:00",
                    "stops": 0,
                    "duration": "1h 10m",
                },
                {
                    "price_original": 90000,
                    "currency": "KRW",
                    "price_krw": 90000,
                    "airline": "오후항공",
                    "origin": "CJU",
                    "destination": "ICN",
                    "departing_at": "2026-05-31T14:20:00",
                    "arriving_at": "2026-05-31T15:30:00",
                    "stops": 0,
                    "duration": "1h 10m",
                },
                {
                    "price_original": 95000,
                    "currency": "KRW",
                    "price_krw": 95000,
                    "airline": "저녁항공",
                    "origin": "CJU",
                    "destination": "ICN",
                    "departing_at": "2026-05-31T22:00:00",
                    "arriving_at": "2026-05-31T23:10:00",
                    "stops": 0,
                    "duration": "1h 10m",
                },
            ],
        }

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "인천으로 오는 항공 오후 시간으로 바꿔줘")

    assert result is not None
    items = result.day_plans["2026-05-31"]
    flight = next(it for it in items if "항공 이동" in it.plan_name)
    assert "오후항공" in flight.plan_name
    assert flight.time.startswith("14:")


@pytest.mark.asyncio
async def test_flight_patch_asks_confirmation_when_no_offers_match_time_preference():
    """요청 시간대 항공편이 없으면 후보 목록을 안내하는 confirmation을 반환한다."""
    itinerary = {
        "start_date": "2026-05-27",
        "end_date": "2026-05-31",
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-31": [
                {
                    "plan_name": "CJU → ICN 항공 이동 (구항공)",
                    "time": "22:00 ~ 23:10",
                    "place": "CJU → ICN",
                    "note": "",
                    "cost": None,
                }
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        return {
            "status": "success",
            "data": [
                {
                    "price_original": 95000,
                    "currency": "KRW",
                    "price_krw": 95000,
                    "airline": "저녁항공A",
                    "origin": "CJU",
                    "destination": "ICN",
                    "departing_at": "2026-05-31T20:30:00",
                    "arriving_at": "2026-05-31T21:40:00",
                    "stops": 0,
                    "duration": "1h 10m",
                },
                {
                    "price_original": 88000,
                    "currency": "KRW",
                    "price_krw": 88000,
                    "airline": "저녁항공B",
                    "origin": "CJU",
                    "destination": "ICN",
                    "departing_at": "2026-05-31T22:10:00",
                    "arriving_at": "2026-05-31T23:20:00",
                    "stops": 0,
                    "duration": "1h 10m",
                },
            ],
        }

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "항공 오전 시간으로 바꿔줘")

    assert result is not None
    assert result.day_plans is None  # 일정 변경 없음
    assert "오전 시간대 항공편이 없습니다" in result.message
    assert "저녁항공A" in result.message or "저녁항공B" in result.message
    assert "바꿀까요" in result.message


@pytest.mark.asyncio
async def test_flight_patch_removes_orphaned_pre_departure_arrival_airport_item():
    """출발 전 시각에 도착지 공항을 출발지로 갖는 이동 항목(고아 항목)이 제거된다."""
    itinerary = {
        "start_date": "2026-05-27",
        "end_date": "2026-05-31",
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-31": [
                {
                    "plan_name": "인천국제공항(ICN) → 자택 이동 (지하철)",
                    "time": "11:30 ~ 12:30",
                    "place": "인천국제공항(ICN) → 자택",
                    "note": "",
                    "cost": None,
                },
                {
                    "plan_name": "신라스테이 제주 체크아웃 및 제주국제공항 이동 (택시)",
                    "time": "19:57 ~ 20:17",
                    "place": "제주국제공항",
                    "note": "",
                    "cost": None,
                },
                {
                    "plan_name": "CJU → ICN 항공 이동 (구항공)",
                    "time": "08:00 ~ 09:10",
                    "place": "CJU → ICN",
                    "note": "비행시간 1h 10m | 직항",
                    "cost": None,
                },
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        return {
            "status": "success",
            "data": [
                {
                    "price_original": 95000,
                    "currency": "KRW",
                    "price_krw": 95000,
                    "airline": "신항공",
                    "origin": "CJU",
                    "destination": "ICN",
                    "departing_at": "2026-05-31T20:30:00",
                    "arriving_at": "2026-05-31T21:40:00",
                    "stops": 0,
                    "duration": "1h 10m",
                },
            ],
        }

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "항공 바꿔줘")

    assert result is not None
    items_31 = result.day_plans["2026-05-31"]
    plan_names = [it.plan_name for it in items_31]
    # 고아 항목(인천국제공항 → 자택 이동)은 제거되어야 함
    assert not any("인천국제공항(ICN) → 자택" in name for name in plan_names), (
        f"고아 도착후 항목이 남아있음: {plan_names}"
    )
    # 출발전 이동(체크아웃) 및 항공 이동은 남아있어야 함
    assert any("제주국제공항" in name for name in plan_names)
    assert any("항공 이동" in name for name in plan_names)


@pytest.mark.asyncio
async def test_other_hotel_request_excludes_current_named_hotel():
    itinerary = {
        "destination": "제주",
        "start_date": "2026-05-28",
        "end_date": "2026-05-31",
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-28": [
                {
                    "plan_name": "Hotel Leo 체크인",
                    "time": "22:25 ~ 22:45",
                    "place": "14, Sammu-ro",
                    "note": "",
                    "cost": None,
                }
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        return {
            "status": "success",
            "data": [
                {"name": "Hotel Leo", "address": "14, Sammu-ro", "price_original": 120000, "currency": "KRW"},
                {"name": "Grand Hyatt Jeju", "address": "12 Noyeon-ro", "price_original": 240000, "currency": "KRW"},
            ],
        }

    with patch.object(itinerary_patch, "_extract_english_city", new=AsyncMock(return_value="Jeju")), \
         patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "Hotel Leo 말고 다른 숙소로 바꿔줘")

    assert result is not None
    assert result.day_plans["2026-05-28"][0].plan_name == "Grand Hyatt Jeju 체크인"


@pytest.mark.asyncio
async def test_hotel_patch_normalizes_korean_country_city_before_lookup():
    itinerary = {
        "destinations": [{"city": "대한민국 부산", "start_date": "2026-05-18", "end_date": "2026-05-20"}],
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-18": [
                {
                    "plan_name": "기존 호텔 체크인",
                    "time": "15:00 ~ 16:00",
                    "place": "기존 호텔",
                    "note": "",
                    "cost": None,
                }
            ],
        },
    }
    seen_city_names = []

    async def mock_task(tool_name, action, params):
        seen_city_names.append(params["city_name"])
        assert tool_name == "duffel_accommodation"
        assert action == "search_hotels"
        return {
            "status": "success",
            "data": [
                {
                    "name": "부산 저가 호텔",
                    "address": "부산 해운대구",
                    "price_original": 90000,
                    "currency": "KRW",
                    "price_krw": 90000,
                    "rating": 4.1,
                }
            ],
        }

    with patch.object(itinerary_patch, "_extract_english_city", new=AsyncMock(return_value="Busan")), \
         patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "저렴한 숙소로 바꿔줘")

    assert result is not None
    assert seen_city_names == ["Busan"]
    assert result.day_plans["2026-05-18"][0].plan_name == "부산 저가 호텔 체크인"


@pytest.mark.asyncio
async def test_hotel_quality_request_selects_highest_rated_candidate():
    itinerary = {
        "destinations": [{"city": "대한민국 제주", "start_date": "2026-05-28", "end_date": "2026-05-31"}],
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-28": [
                {
                    "plan_name": "기존 호텔 체크인",
                    "time": "22:25 ~ 22:45",
                    "place": "기존 호텔",
                    "note": "",
                    "cost": None,
                }
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        return {
            "status": "success",
            "data": [
                {"name": "Hotel Leo", "address": "14, Sammu-ro", "price_original": 100000, "currency": "KRW", "rating": 3},
                {"name": "Grand Hyatt Jeju", "address": "12 Noyeon-ro", "price_original": 240000, "currency": "KRW", "rating": 4.7},
            ],
        }

    with patch.object(itinerary_patch, "_extract_english_city", new=AsyncMock(return_value="Jeju")), \
         patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "시설 좋은 호텔로 바꿔줘")

    assert result is not None
    assert result.day_plans["2026-05-28"][0].plan_name == "Grand Hyatt Jeju 체크인"


@pytest.mark.asyncio
async def test_hotel_patch_updates_related_airport_to_hotel_transfer():
    itinerary = {
        "destinations": [{"city": "대한민국 제주", "start_date": "2026-05-28", "end_date": "2026-05-31"}],
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-28": [
                {
                    "plan_name": "숙소 → 인천국제공항 이동 (택시)",
                    "time": "18:00 ~ 19:30",
                    "place": "인천국제공항",
                    "note": "여유 있게 공항 도착을 위해 저녁 식사 후 이동합니다.",
                    "cost": {"amount": 55000, "currency": "KRW"},
                },
                {
                    "plan_name": "인천국제공항(ICN) → 제주국제공항(CJU) 항공 이동",
                    "time": "20:54 ~ 22:04",
                    "place": "인천국제공항(ICN) → 제주국제공항(CJU)",
                    "note": "",
                    "cost": {"amount": 40586, "currency": "KRW"},
                },
                {
                    "plan_name": "제주국제공항 → 신라스테이 제주 이동 (택시)",
                    "time": "22:10 ~ 22:25",
                    "place": "신라스테이 제주",
                    "note": "공항에서 호텔까지 택시로 약 15분 소요",
                    "cost": {"amount": 12000, "currency": "KRW"},
                },
                {
                    "plan_name": "신라스테이 제주 체크인",
                    "time": "22:25 ~ 22:45",
                    "place": "신라스테이 제주",
                    "note": "2026-05-28 ~ 2026-05-31 숙박.",
                    "cost": {"amount": 420000, "currency": "KRW"},
                },
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        assert params["city_name"] == "Jeju"
        return {
            "status": "success",
            "data": [
                {
                    "name": "Hotel Leo",
                    "address": "14, Sammu-ro",
                    "price_original": 378.75,
                    "currency": "AUD",
                    "price_krw": 333300,
                    "rating": 3,
                }
            ],
        }

    with patch.object(itinerary_patch, "_extract_english_city", new=AsyncMock(return_value="Jeju")), \
         patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "저렴한 숙소로 바꿔줘")

    assert result is not None
    items = result.day_plans["2026-05-28"]
    assert items[0].plan_name == "숙소 → 인천국제공항 이동 (택시)"
    assert items[0].place == "인천국제공항"
    assert items[2].plan_name == "제주국제공항 → Hotel Leo 이동 (택시)"
    assert items[2].place == "14, Sammu-ro"
    assert "신라스테이 제주" not in items[2].plan_name
    assert items[3].plan_name == "Hotel Leo 체크인"
    assert items[3].place == "14, Sammu-ro"


@pytest.mark.asyncio
async def test_named_hotel_patch_asks_confirmation_when_candidate_unmatched():
    itinerary = {
        "destinations": [{"city": "대한민국 제주", "start_date": "2026-05-28", "end_date": "2026-05-31"}],
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-28": [
                {
                    "plan_name": "제주국제공항 → 신라스테이 제주 이동 (택시)",
                    "time": "22:10 ~ 22:25",
                    "place": "신라스테이 제주",
                    "note": "공항에서 신라스테이 제주까지 택시로 약 15분 소요",
                    "cost": {"amount": 12000, "currency": "KRW"},
                },
                {
                    "plan_name": "신라스테이 제주 체크인",
                    "time": "22:25 ~ 22:45",
                    "place": "신라스테이 제주",
                    "note": "2026-05-28 ~ 2026-05-31 숙박.",
                    "cost": {"amount": 420000, "currency": "KRW"},
                },
            ],
            "2026-05-29": [
                {
                    "plan_name": "늘봄흑돼지 → 신라스테이 제주 이동 (택시)",
                    "time": "21:00 ~ 21:20",
                    "place": "신라스테이 제주",
                    "note": "택시비 약 11,000원.",
                    "cost": {"amount": 11000, "currency": "KRW"},
                }
            ],
            "2026-05-31": [
                {
                    "plan_name": "신라스테이 제주 체크아웃 및 제주국제공항 이동 (택시)",
                    "time": "08:00 ~ 08:20",
                    "place": "제주국제공항",
                    "note": "신라스테이 제주에서 공항까지 택시 약 12,000원.",
                    "cost": {"amount": 12000, "currency": "KRW"},
                }
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        return {
            "status": "success",
            "data": [
                {
                    "name": "Hotel Leo",
                    "address": "14, Sammu-ro",
                    "price_original": 378.75,
                    "currency": "AUD",
                    "price_krw": 333300,
                    "rating": 3,
                }
            ],
        }

    with patch.object(itinerary_patch, "_extract_english_city", new=AsyncMock(return_value="Jeju")), \
         patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "신라호텔로 바꿔줘")

    assert result is not None
    assert result.day_plans is None
    assert "신라호텔을(를) 찾지 못했습니다" in result.message
    assert "Hotel Leo" in result.message
    assert "이 중 하나로 바꿀까요?" in result.message


@pytest.mark.asyncio
async def test_hotel_patch_does_local_patch_when_lookup_fails():
    itinerary = {
        "destinations": [{"city": "대한민국 부산", "start_date": "2026-05-18", "end_date": "2026-05-20"}],
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-18": [
                {
                    "plan_name": "기존 호텔 체크인",
                    "time": "15:00 ~ 16:00",
                    "place": "기존 호텔",
                    "note": "",
                    "cost": None,
                }
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        return {"status": "error", "message": [{"code": "invalid_date"}]}

    with patch.object(itinerary_patch, "_extract_english_city", new=AsyncMock(return_value="Busan")), \
         patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)) as mocked:
        result = await try_patch_itinerary_item(_deps(itinerary), "저렴한 숙소로 바꿔줘")

    assert result is not None
    assert mocked.await_count >= 1
    item = result.day_plans["2026-05-18"][0]
    assert item.plan_name == "저렴한 숙소 체크인"
    assert "저렴한 숙소" in result.preferences["accommodation"]


@pytest.mark.asyncio
async def test_meal_preference_patch_does_not_call_external_services():
    itinerary = {
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-28": [
                {"plan_name": "공항 도착", "time": "17:00 ~ 18:00", "place": "제주국제공항", "note": "", "cost": None},
                {"plan_name": "호텔 체크인", "time": "19:00 ~ 19:30", "place": "제주 호텔", "note": "", "cost": None},
            ],
            "2026-05-29": [
                {"plan_name": "성산 일출봉 방문", "time": "10:00 ~ 12:00", "place": "성산 일출봉", "note": "", "cost": None},
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        if tool_name == "google_maps":
            assert action == "search_place"
            return {
                "status": "success",
                "data": {
                    "places": [
                        {
                            "name": "고등어회 맛집",
                            "formatted_address": "제주특별자치도 제주시",
                        }
                    ]
                },
            }
        if tool_name == "tavily_search":
            assert action == "search"
            return {
                "status": "success",
                "data": [
                    {"title": "고등어회 맛집", "content": "제주 현지 고등어회 맛집 소개"},
                ],
            }
        raise AssertionError(f"unexpected tool: {tool_name}.{action}")

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)) as mocked:
        result = await try_patch_itinerary_item(_deps(itinerary), "1일차 저녁에 고등어회 먹고 싶어")

    assert result is not None
    assert mocked.await_count == 2
    items = result.day_plans["2026-05-28"]
    assert any(item.plan_name == "저녁 식사 (고등어회 맛집)" for item in items)
    assert any(item.place == "제주특별자치도 제주시" for item in items)
    assert result.preferences["food"] == "고등어회"


@pytest.mark.asyncio
async def test_meal_patch_updates_adjacent_transfer_endpoints():
    itinerary = {
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-30": [
                {
                    "plan_name": "한라산 국립공원 → 미영이네 이동 (버스)",
                    "time": "18:30 ~ 19:15",
                    "place": "미영이네",
                    "note": "",
                    "cost": {"amount": 2300, "currency": "KRW"},
                },
                {
                    "plan_name": "저녁 식사 - 미영이네",
                    "time": "19:15 ~ 20:15",
                    "place": "미영이네",
                    "note": "",
                    "cost": {"amount": 17000, "currency": "KRW"},
                },
                {
                    "plan_name": "미영이네 → LOTTE CITY HOTEL JEJU AIRPORT 이동 (버스)",
                    "time": "20:15 ~ 21:30",
                    "place": "LOTTE CITY HOTEL JEJU AIRPORT",
                    "note": "",
                    "cost": {"amount": 5000, "currency": "KRW"},
                },
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        if tool_name == "google_maps":
            return {
                "status": "success",
                "data": {
                    "places": [
                        {
                            "name": "노도고등어회 제주순살갈치조림",
                            "formatted_address": "제주특별자치도 제주시 서해안로 364",
                        }
                    ]
                },
            }
        if tool_name == "tavily_search":
            return {"status": "success", "data": []}
        raise AssertionError(f"unexpected tool: {tool_name}.{action}")

    with patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "1일차 저녁 식사를 고등어회로 바꿔줘")

    assert result is not None
    items = result.day_plans["2026-05-30"]
    assert items[0].plan_name == "한라산 국립공원 → 노도고등어회 제주순살갈치조림 이동 (버스)"
    assert items[0].place == "제주특별자치도 제주시 서해안로 364"
    assert items[1].plan_name == "저녁 식사 (노도고등어회 제주순살갈치조림)"
    assert items[2].plan_name == "노도고등어회 제주순살갈치조림 → LOTTE CITY HOTEL JEJU AIRPORT 이동 (버스)"
    assert "미영이네" not in items[2].plan_name


@pytest.mark.asyncio
async def test_meal_patch_rejects_place_candidate_outside_trip_city():
    itinerary = {
        "destinations": [{"city": "제주", "start_date": "2026-05-30", "end_date": "2026-05-31"}],
        "adult_count": 1,
        "child_count": 0,
        "child_ages": [],
        "day_plans": {
            "2026-05-30": [
                {
                    "plan_name": "저녁 식사 - 미영이네",
                    "time": "19:15 ~ 20:15",
                    "place": "미영이네",
                    "note": "고등어회 요청을 반영했습니다.",
                    "cost": {"amount": 17000, "currency": "KRW"},
                },
            ],
        },
    }

    async def mock_task(tool_name, action, params):
        if tool_name == "google_maps":
            if params["query"] == "제주 Jeju":
                return {
                    "status": "success",
                    "data": {
                        "places": [
                            {
                                "name": "제주",
                                "formatted_address": "제주특별자치도",
                                "lat": 33.4996,
                                "lng": 126.5312,
                            }
                        ]
                    },
                }
            assert "제주" in params["query"]
            assert params["location"] == "33.4996,126.5312"
            return {
                "status": "success",
                "data": {
                    "places": [
                        {
                            "name": "제주은희네해장국 인천시청점",
                            "formatted_address": "인천광역시 남동구 구월동 1129-9",
                            "lat": 37.4563,
                            "lng": 126.7052,
                        },
                        {
                            "name": "제주 돼지국밥",
                            "formatted_address": "제주특별자치도 제주시 서해안로 364",
                            "lat": 33.5124,
                            "lng": 126.4920,
                        },
                    ]
                },
            }
        if tool_name == "tavily_search":
            return {"status": "success", "data": []}
        raise AssertionError(f"unexpected tool: {tool_name}.{action}")

    with patch.object(itinerary_patch, "_extract_english_city", new=AsyncMock(return_value="Jeju")), \
         patch.object(itinerary_patch._service, "process_task", new=AsyncMock(side_effect=mock_task)):
        result = await try_patch_itinerary_item(_deps(itinerary), "1일차 저녁 식사를 돼지국밥으로 바꿔줘")

    assert result is not None
    item = result.day_plans["2026-05-30"][0]
    assert item.plan_name == "저녁 식사 (제주 돼지국밥)"
    assert item.place == "제주특별자치도 제주시 서해안로 364"
    assert "인천시청점" not in item.plan_name
    assert "고등어회" not in item.note
