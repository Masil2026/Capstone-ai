import pytest
from unittest.mock import patch, Mock, AsyncMock

from app.services.adapters.booking_api import BookingAdapter
from app.services.travel_agent_service import TravelAgentService


def _status(code, text="rate limited"):
    mock = Mock()
    mock.status_code = code
    mock.text = text
    mock.json.return_value = {"status": True, "message": "Success", "data": []}
    return mock


def _service():
    """키 없는 테스트 환경에서도 로직 검증되도록 api_key 주입."""
    adapter = BookingAdapter()
    adapter.api_key = "test-key"
    return TravelAgentService({"booking": adapter})


def _ok(payload):
    mock = Mock()
    mock.status_code = 200
    mock.json.return_value = {"status": True, "message": "Success", **payload}
    return mock


# ============================== Hotels ============================== #
@pytest.mark.asyncio
async def test_booking_search_destination_selects_best():
    """hotel 타입 제외 + query 이름 매칭으로 city 자동 선택"""
    service = _service()
    data = {"data": [
        {"dest_id": "-716583", "search_type": "city", "dest_type": "city",
         "name": "서울", "label": "Seoul, South Korea", "nr_hotels": 5171},
        {"dest_id": "gangnam", "search_type": "district", "dest_type": "district",
         "name": "강남구", "label": "...", "nr_hotels": 157},
        {"dest_id": "h1", "search_type": "hotel", "dest_type": "hotel",
         "name": "어떤호텔", "nr_hotels": 1},
    ]}

    with patch("httpx.AsyncClient.get", return_value=_ok(data)):
        result = await service.process_task("booking", "search_destination", {"query": "서울"})

    assert result["status"] == "success"
    assert result["data"]["selected"]["dest_id"] == "-716583"
    # hotel 타입은 후보에서 제외
    assert all(c["dest_type"] != "hotel" for c in result["data"]["candidates"])


@pytest.mark.asyncio
async def test_booking_search_hotels():
    """호텔 검색 — 성급 폴백·가격 추출"""
    service = _service()
    data = {"data": {"hotels": [
        {"hotel_id": 242715, "accessibilityLabel": "오라카이 인사동스위츠 ...",
         "property": {"name": "오라카이 인사동스위츠", "accuratePropertyClass": 4, "propertyClass": 4,
                      "reviewScore": 8.9, "reviewScoreWord": "우수함", "reviewCount": 4030,
                      "priceBreakdown": {"grossPrice": {"value": 3520000, "currency": "KRW"}},
                      "photoUrls": ["http://p.jpg"], "latitude": 37.5, "longitude": 127.0}},
    ]}}

    params = {"dest_id": "-716583", "search_type": "city",
              "arrival_date": "2026-06-25", "departure_date": "2026-06-27"}
    with patch("httpx.AsyncClient.get", return_value=_ok(data)):
        result = await service.process_task("booking", "search_hotels", params)

    assert result["status"] == "success"
    hotel = result["data"]["hotels"][0]
    assert hotel["hotel_id"] == 242715
    assert hotel["star"] == 4
    assert hotel["price"] == 3520000
    assert hotel["currency"] == "KRW"


@pytest.mark.asyncio
async def test_booking_get_room_list_recommended():
    """객실 목록 — 추천 block(room_recommendation) 우선 선택 + rooms 조인"""
    service = _service()
    data = {"data": {
        "block": [
            {"block_id": "b1", "room_id": 111, "room_name": "쿼드러플 스위트룸",
             "room_surface_in_m2": 33, "max_occupancy": 4, "breakfast_included": 1,
             "product_price_breakdown": {"gross_amount": {"value": 864000, "currency": "KRW"},
                                         "gross_amount_per_night": {"value": 432000}},
             "paymentterms": {"cancellation": {"type_translation": "부분 환불 가능"},
                              "prepayment": {"simple_translation": "선결제 없음"}}},
            {"block_id": "b2", "room_id": 111, "room_name": "쿼드러플 스위트룸",
             "product_price_breakdown": {"gross_amount": {"value": 900000}}},
        ],
        "rooms": {"111": {"description": "한글 상세 설명",
                          "photos": [{"url_max750": "http://r.jpg"}],
                          "bed_configurations": [{"bed_types": [{"name_with_count": "더블침대 2개"}]}],
                          "highlights": [{"translated_name": "WiFi"}]}},
        "room_recommendation": [{"block_id": "b1"}],
        "recommended_block_title": "성인 1명 숙박에 추천",
    }}

    params = {"hotel_id": 4054796, "arrival_date": "2026-06-25", "departure_date": "2026-06-27"}
    with patch("httpx.AsyncClient.get", return_value=_ok(data)):
        result = await service.process_task("booking", "get_room_list", params)

    assert result["status"] == "success"
    assert result["data"]["count"] == 1  # 추천 b1만
    room = result["data"]["rooms"][0]
    assert room["block_id"] == "b1"
    assert room["price"] == 864000
    assert room["description"] == "한글 상세 설명"
    assert "더블침대 2개" in room["bed_types"]


@pytest.mark.asyncio
async def test_booking_get_hotel_details_builds_url():
    """호텔 상세 — 예약 URL 결정론적 조립 (날짜·인원 포함)"""
    service = _service()
    data = {"data": {
        "url": "https://www.booking.com/hotel/kr/aiden-by-best-western-cheongdam.html",
        "hotel_id": 4054796, "hotel_name_trans": "에이든 바이 베스트 웨스턴 청담",
        "address_trans": "강남구 도산대로 216", "city_trans": "서울", "district": "강남구",
        "zip": "06047", "latitude": 37.5, "longitude": 127.0, "distance_to_cc": 6.36,
        "accommodation_type_name": "호텔", "review_nr": 1996, "soldout": 0,
        "available_rooms": 1, "timezone": "Asia/Seoul",
        "product_price_breakdown": {"gross_amount": {"value": 864000}},
    }}

    params = {"hotel_id": 4054796, "arrival_date": "2026-06-25", "departure_date": "2026-06-27",
              "adults": 1, "children_age": "0,17", "room_qty": 1}
    with patch("httpx.AsyncClient.get", return_value=_ok(data)):
        result = await service.process_task("booking", "get_hotel_details", params)

    assert result["status"] == "success"
    url = result["data"]["booking_url"]
    assert ".ko.html" in url
    assert "checkin=2026-06-25" in url
    assert "checkout=2026-06-27" in url
    assert "group_children=2" in url
    assert "age=0" in url and "age=17" in url
    assert "selected_currency=KRW" in url
    assert result["data"]["name"] == "에이든 바이 베스트 웨스턴 청담"


# ============================== Flights ============================== #
@pytest.mark.asyncio
async def test_booking_search_flight_location_airport_only():
    """항공 위치 — CITY 제외하고 AIRPORT만 선택"""
    service = _service()
    data = {"data": [
        {"id": "SEL.CITY", "type": "CITY", "name": "서울", "code": "SEL"},
        {"id": "ICN.AIRPORT", "type": "AIRPORT", "name": "인천국제공항",
         "code": "ICN", "cityName": "서울", "parent": "SEL"},
    ]}

    with patch("httpx.AsyncClient.get", return_value=_ok(data)):
        result = await service.process_task("booking", "search_flight_location", {"query": "ICN"})

    assert result["status"] == "success"
    assert result["data"]["selected"]["id"] == "ICN.AIRPORT"
    assert all(c["type"] == "AIRPORT" for c in result["data"]["candidates"])


@pytest.mark.asyncio
async def test_booking_search_flights():
    """항공편 검색 — 가격(units+nanos)·is_direct 파생·리스트 URL 조립"""
    service = _service()
    data = {"data": {
        "flightOffers": [{
            "token": "tok1", "tripType": "ROUNDTRIP",
            "priceBreakdown": {"total": {"units": 1200000, "nanos": 0, "currencyCode": "KRW"},
                               "totalRounded": {"units": 1200000}},
            "segments": [{
                "departureAirport": {"code": "ICN"}, "arrivalAirport": {"code": "LHR"},
                "departureTime": "2026-06-26T12:25:00", "arrivalTime": "2026-06-26T18:50:00",
                "totalTime": 48300,
                "legs": [{"departureAirport": {"code": "ICN"}, "arrivalAirport": {"code": "LHR"},
                          "flightInfo": {"flightNumber": 209},
                          "carriersData": [{"name": "Virgin Atlantic"}], "cabinClass": "ECONOMY"}],
            }],
        }],
        "aggregation": {"stops": [{"numberOfStops": 0, "count": 23}]},
    }}

    params = {"fromId": "ICN.AIRPORT", "toId": "LHR.AIRPORT", "departDate": "2026-06-26",
              "returnDate": "2026-07-03", "adults": 1, "children": "0,17"}
    with patch("httpx.AsyncClient.get", return_value=_ok(data)):
        result = await service.process_task("booking", "search_flights", params)

    assert result["status"] == "success"
    flight = result["data"]["flights"][0]
    assert flight["price"] == 1200000
    assert flight["is_direct"] is True
    assert flight["segments"][0]["stops"] == 0
    url = result["data"]["booking_list_url"]
    assert "ICN.AIRPORT-LHR.AIRPORT" in url
    assert "type=ROUNDTRIP" in url
    assert "children=0,17" in url


@pytest.mark.asyncio
async def test_booking_get_flight_details():
    """항공편 상세 — data 루트의 단일 offer 정제"""
    service = _service()
    data = {"data": {
        "token": "tok1", "tripType": "ONEWAY",
        "priceBreakdown": {"total": {"units": 600000, "currencyCode": "KRW"}},
        "segments": [{
            "departureAirport": {"code": "ICN"}, "arrivalAirport": {"code": "NRT"},
            "legs": [{"departureAirport": {"code": "ICN"}, "arrivalAirport": {"code": "NRT"},
                      "flightInfo": {"flightNumber": 100}, "carriersData": [{"name": "ANA"}]}],
        }],
    }}

    with patch("httpx.AsyncClient.get", return_value=_ok(data)):
        result = await service.process_task("booking", "get_flight_details", {"token": "tok1"})

    assert result["status"] == "success"
    assert result["data"]["flight"]["token"] == "tok1"
    assert result["data"]["flight"]["price"] == 600000


# ============================== 검증 / 에러 ============================== #
@pytest.mark.asyncio
async def test_booking_search_destination_missing_query():
    service = _service()
    result = await service.process_task("booking", "search_destination", {})
    assert result["status"] == "error"
    assert result["message"] == "query는 필수입니다."


@pytest.mark.asyncio
async def test_booking_search_hotels_missing_params():
    service = _service()
    result = await service.process_task("booking", "search_hotels", {"dest_id": "-716583"})
    assert result["status"] == "error"
    assert "필수 파라미터 누락" in result["message"]


@pytest.mark.asyncio
async def test_booking_get_room_list_missing_hotel_id():
    service = _service()
    result = await service.process_task(
        "booking", "get_room_list", {"arrival_date": "2026-06-25", "departure_date": "2026-06-27"}
    )
    assert result["status"] == "error"
    assert "hotel_id" in result["message"]


@pytest.mark.asyncio
async def test_booking_search_flights_missing_params():
    service = _service()
    result = await service.process_task("booking", "search_flights", {"fromId": "ICN.AIRPORT"})
    assert result["status"] == "error"
    assert "필수 파라미터 누락" in result["message"]


@pytest.mark.asyncio
async def test_booking_get_flight_details_missing_token():
    service = _service()
    result = await service.process_task("booking", "get_flight_details", {})
    assert result["status"] == "error"
    assert "token은 필수입니다." in result["message"]


@pytest.mark.asyncio
async def test_booking_invalid_action():
    service = _service()
    result = await service.process_task("booking", "invalid", {})
    assert result["status"] == "error"
    assert "지원하지 않는 액션" in result["message"]


@pytest.mark.asyncio
async def test_booking_missing_api_key():
    adapter = BookingAdapter()
    adapter.api_key = ""
    service = TravelAgentService({"booking": adapter})
    result = await service.process_task("booking", "search_destination", {"query": "서울"})
    assert result["status"] == "error"
    assert "BOOKING_API_KEY" in result["message"]


# ============================== 실호출 smoke (opt-in) ============================== #
@pytest.mark.realapi
@pytest.mark.asyncio
async def test_booking_search_flight_location_live():
    """[opt-in 실호출] 라이브 Booking API 도달성 확인 — `pytest -m realapi` 로만 실행.

    가장 가벼운 lookup(2-1, dest_id·날짜 불필요) 1콜만 사용해 RapidAPI 쿼터 최소 소모.
    .env의 BOOKING_API_KEY를 그대로 사용하며, AIRPORT 자동 선택까지 확인한다.
    (나머지 동작 검증은 쿼터 보호를 위해 mock 테스트가 담당)
    """
    adapter = BookingAdapter()
    result = await adapter.execute("search_flight_location", {"query": "Seoul"})

    assert result["status"] == "success"
    selected = result["data"]["selected"]
    assert selected["type"] == "AIRPORT"           # CITY 제외 로직
    assert selected["id"].endswith(".AIRPORT")     # 2-2 입력으로 바로 사용 가능한 형식


# ============================== Rate limit (429) ============================== #
@pytest.mark.asyncio
async def test_booking_retries_on_429_then_succeeds():
    """429 발생 시 backoff 후 재시도하여 성공한다."""
    service = _service()
    ok = _ok({"data": [
        {"dest_id": "-716583", "search_type": "city", "dest_type": "city", "name": "서울", "nr_hotels": 100},
    ]})
    with patch("httpx.AsyncClient.get", side_effect=[_status(429), ok]), \
         patch("asyncio.sleep", new=AsyncMock()) as sleep_mock:
        result = await service.process_task("booking", "search_destination", {"query": "서울"})

    assert result["status"] == "success"
    assert result["data"]["selected"]["dest_id"] == "-716583"
    sleep_mock.assert_awaited()  # backoff 대기 발생


@pytest.mark.asyncio
async def test_booking_gives_up_after_max_429():
    """계속 429면 최대 재시도 후 에러를 반환한다 (무한 루프 없음)."""
    service = _service()
    with patch("httpx.AsyncClient.get", return_value=_status(429)) as get_mock, \
         patch("asyncio.sleep", new=AsyncMock()):
        result = await service.process_task("booking", "search_destination", {"query": "서울"})

    assert result["status"] == "error"
    assert "429" in result["message"]
    assert get_mock.call_count == 4  # 최초 1 + 재시도 3


@pytest.mark.asyncio
async def test_booking_no_retry_on_monthly_quota():
    """월 쿼터 소진 429는 재시도해도 무의미하므로 즉시 포기한다 (backoff 낭비 방지)."""
    quota = Mock()
    quota.status_code = 429
    quota.text = '{"message":"You have exceeded the MONTHLY quota for requests on your current plan, BASIC."}'
    quota.headers = {"x-ratelimit-requests-limit": "50", "x-ratelimit-requests-remaining": "-2"}
    quota.json.return_value = {"status": True, "data": []}

    service = _service()
    with patch("httpx.AsyncClient.get", return_value=quota) as get_mock, \
         patch("asyncio.sleep", new=AsyncMock()) as sleep_mock:
        result = await service.process_task("booking", "search_destination", {"query": "서울"})

    assert result["status"] == "error"
    assert get_mock.call_count == 1       # 재시도 없이 1회로 종료
    sleep_mock.assert_not_awaited()       # backoff 대기도 없음
