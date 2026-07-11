import asyncio
import httpx

from typing import Any, Dict, List, Optional, Tuple
from app.core.ApiToolsInterfaces import ApiTools
from app.core.config import settings


# 무료 티어(RapidAPI) rate limit 대응 — 동시 호출 제한 + 429 backoff 재시도
_BOOKING_MAX_CONCURRENCY = 2
_BOOKING_MAX_RETRIES = 3


def _to_int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class BookingAdapter(ApiTools):
    """
    Booking.com (RapidAPI · booking-com15) 어댑터.
    레퍼런스: docs/external-api/booking_api_reference.md

    Hotels
      - search_destination   : 도시/지역명 → dest_id 변환 (어댑터가 후보 1개 자동 선택)
      - search_hotels        : 호텔 목록 (이름·가격·평점)
      - get_room_list        : 선택 호텔의 객실 타입별 상세 + 가격
      - get_hotel_details    : 호텔 상세 + 예약 URL(deeplink)
    Flights
      - search_flight_location : 도시/공항명 → 공항 id 변환 (AIRPORT만 선택)
      - search_flights         : 항공편 검색 (직항+경유 한 번에)
      - get_flight_details     : 항공편 상세 (token 기반)
    """

    HOST = "booking-com15.p.rapidapi.com"
    BASE_URL = "https://booking-com15.p.rapidapi.com/api/v1"

    # 국내 중심 정책 고정값
    _CURRENCY = "KRW"
    _LANG = "ko"

    # 모든 인스턴스가 공유하는 동시성 제한 (Booking 호출이 한꺼번에 몰리는 것 방지)
    _rate_limit = asyncio.Semaphore(_BOOKING_MAX_CONCURRENCY)

    def __init__(self):
        self.api_key = (settings.BOOKING_API_KEY or "").strip()

    @property
    def tool_name(self) -> str:
        return "booking"

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.api_key:
            return {"status": "error", "message": "BOOKING_API_KEY가 설정되지 않았습니다."}

        handlers = {
            "search_destination": self._search_destination,
            "search_hotels": self._search_hotels,
            "get_room_list": self._get_room_list,
            "get_hotel_details": self._get_hotel_details,
            "search_flight_location": self._search_flight_location,
            "search_flights": self._search_flights,
            "get_flight_details": self._get_flight_details,
        }
        handler = handlers.get(action)
        if handler is None:
            return {"status": "error", "message": f"지원하지 않는 액션: {action}"}
        return await handler(params)

    # ================================================================== #
    # Hotels
    # ================================================================== #
    async def _search_destination(self, params: Dict[str, Any]) -> Dict[str, Any]:
        query = params.get("query")
        if not query:
            return {"status": "error", "message": "query는 필수입니다."}

        data, err = await self._get("/hotels/searchDestination", {"query": query})
        if err:
            return err

        candidates = data.get("data") or []
        # dest_type == "hotel" 은 단일 숙소라 검색 영역이 아님 → 제외
        usable = [c for c in candidates if c.get("dest_type") != "hotel"]
        if not usable:
            return {"status": "error", "message": f"'{query}'에 대한 검색 가능한 지역을 찾지 못했습니다."}

        selected = max(usable, key=lambda c: self._dest_score(c, query))
        return {
            "status": "success",
            "data": {
                "type": "Booking 호텔 지역 검색",
                "selected": self._simplify_destination(selected),
                "candidates": [self._simplify_destination(c) for c in usable[:10]],
            },
        }

    async def _search_hotels(self, params: Dict[str, Any]) -> Dict[str, Any]:
        required = ("dest_id", "search_type", "arrival_date", "departure_date")
        missing = [k for k in required if not params.get(k)]
        if missing:
            return {"status": "error", "message": f"필수 파라미터 누락: {', '.join(missing)}"}

        query = {
            "dest_id": params["dest_id"],
            "search_type": params["search_type"],  # 1-1 응답값 그대로 (대소문자 변환 X)
            "arrival_date": params["arrival_date"],
            "departure_date": params["departure_date"],
            "adults": params.get("adults", 1),
            "room_qty": params.get("room_qty", 1),
            "page_number": params.get("page_number", 1),
            "currency_code": self._CURRENCY,
            "languagecode": self._LANG,
        }
        self._apply_optional(query, params, ("children_age", "price_min", "price_max"))

        data, err = await self._get("/hotels/searchHotels", query)
        if err:
            return err

        hotels = self._dig(data, "data", "hotels", default=[]) or []
        results = [self._simplify_hotel(h) for h in hotels[:10]]
        return {
            "status": "success",
            "data": {"type": "Booking 호텔 검색", "count": len(results), "hotels": results},
        }

    async def _get_room_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        required = ("hotel_id", "arrival_date", "departure_date")
        missing = [k for k in required if not params.get(k)]
        if missing:
            return {"status": "error", "message": f"필수 파라미터 누락: {', '.join(missing)}"}

        query = self._hotel_stay_params(params)
        query["hotel_id"] = params["hotel_id"]

        data, err = await self._get("/hotels/getRoomList", query)
        if err:
            return err

        body = data.get("data") or {}
        blocks = body.get("block") or []
        rooms = body.get("rooms") or {}

        # block 중복 정리: 1순위 추천 block, 폴백 room_id별 최저가
        recommended_ids = {
            r.get("block_id") for r in (body.get("room_recommendation") or []) if r.get("block_id")
        }
        chosen = [b for b in blocks if b.get("block_id") in recommended_ids]
        if not chosen:
            chosen = self._cheapest_per_room(blocks)

        results = [self._simplify_block(b, rooms) for b in chosen]
        return {
            "status": "success",
            "data": {
                "type": "Booking 객실 목록",
                "recommended_title": body.get("recommended_block_title"),
                "count": len(results),
                "rooms": results,
            },
        }

    async def _get_hotel_details(self, params: Dict[str, Any]) -> Dict[str, Any]:
        required = ("hotel_id", "arrival_date", "departure_date")
        missing = [k for k in required if not params.get(k)]
        if missing:
            return {"status": "error", "message": f"필수 파라미터 누락: {', '.join(missing)}"}

        query = self._hotel_stay_params(params)
        query["hotel_id"] = params["hotel_id"]

        data, err = await self._get("/hotels/getHotelDetails", query)
        if err:
            return err

        d = data.get("data") or {}
        booking_url = self._build_hotel_url(
            d.get("url"),
            params["arrival_date"],
            params["departure_date"],
            params.get("adults", 1),
            self._ages_list(params.get("children_age")),
            params.get("room_qty", 1),
        )
        return {
            "status": "success",
            "data": {
                "type": "Booking 호텔 상세",
                "booking_url": booking_url,  # 날짜·인원 채워진 예약 deeplink
                "hotel_id": d.get("hotel_id"),
                "name": d.get("hotel_name_trans") or d.get("hotel_name"),
                "address": d.get("address_trans") or d.get("address"),
                "city": d.get("city_trans"),
                "district": d.get("district"),
                "zip": d.get("zip"),
                "latitude": d.get("latitude"),
                "longitude": d.get("longitude"),
                "distance_to_center_km": d.get("distance_to_cc"),
                "accommodation_type": d.get("accommodation_type_name"),
                "review_count": d.get("review_nr"),
                "soldout": d.get("soldout"),
                "available_rooms": d.get("available_rooms"),
                "timezone": d.get("timezone"),
                "price": self._price(self._dig(d, "product_price_breakdown", "gross_amount")),
            },
        }

    # ================================================================== #
    # Flights
    # ================================================================== #
    async def _search_flight_location(self, params: Dict[str, Any]) -> Dict[str, Any]:
        query = params.get("query")
        if not query:
            return {"status": "error", "message": "query는 필수입니다. (영문 도시명 또는 IATA 코드)"}

        data, err = await self._get(
            "/flights/searchDestination", {"query": query, "languagecode": self._LANG}
        )
        if err:
            return err

        # 2-2 Search Flights는 CITY id를 거부 → AIRPORT만 사용
        airports = [c for c in (data.get("data") or []) if c.get("type") == "AIRPORT"]
        if not airports:
            return {"status": "error", "message": f"'{query}'에 해당하는 공항을 찾지 못했습니다."}

        selected = max(airports, key=lambda c: self._airport_score(c, query))
        return {
            "status": "success",
            "data": {
                "type": "Booking 항공 위치 검색",
                "selected": self._simplify_location(selected),
                "candidates": [self._simplify_location(c) for c in airports[:10]],
            },
        }

    async def _search_flights(self, params: Dict[str, Any]) -> Dict[str, Any]:
        required = ("fromId", "toId", "departDate")
        missing = [k for k in required if not params.get(k)]
        if missing:
            return {"status": "error", "message": f"필수 파라미터 누락: {', '.join(missing)}"}

        cabin = params.get("cabinClass", "ECONOMY")
        sort = params.get("sort", "BEST")
        return_date = params.get("returnDate")

        query = {
            "fromId": params["fromId"],
            "toId": params["toId"],
            "departDate": params["departDate"],
            "adults": params.get("adults", 1),
            "cabinClass": cabin,
            "sort": sort,
            "currency_code": self._CURRENCY,
            "pageNo": params.get("pageNo", 1),
        }
        if return_date:
            query["returnDate"] = return_date
        self._apply_optional(query, params, ("children", "stops"))

        data, err = await self._get("/flights/searchFlights", query)
        if err:
            return err

        offers = self._dig(data, "data", "flightOffers", default=[]) or []
        results = [self._simplify_offer(o) for o in offers[:10]]

        list_url = self._build_flight_list_url(
            params["fromId"], params["toId"], params["departDate"], return_date,
            params.get("adults", 1), self._csv(params.get("children")), cabin, sort,
        )
        return {
            "status": "success",
            "data": {
                "type": "Booking 항공편 검색",
                "booking_list_url": list_url,  # 특정 편 deeplink 불가 → 검색 리스트 URL
                "count": len(results),
                "stops_summary": self._dig(data, "data", "aggregation", "stops", default=[]),
                "flights": results,
            },
        }

    async def _get_flight_details(self, params: Dict[str, Any]) -> Dict[str, Any]:
        token = params.get("token")
        if not token:
            return {"status": "error", "message": "token은 필수입니다. (search_flights 결과의 token)"}

        data, err = await self._get(
            "/flights/getFlightDetails", {"token": token, "currency_code": self._CURRENCY}
        )
        if err:
            return err

        # 2-4 응답은 data 바로 아래에 token/segments/priceBreakdown (offer 1개 형태)
        offer = data.get("data") or {}
        return {
            "status": "success",
            "data": {"type": "Booking 항공편 상세", "flight": self._simplify_offer(offer)},
        }

    # ================================================================== #
    # HTTP
    # ================================================================== #
    async def _get(self, path: str, query: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """(data, None) 성공 / (None, error_dict) 실패."""
        url = f"{self.BASE_URL}{path}"
        headers = {"x-rapidapi-host": self.HOST, "x-rapidapi-key": self.api_key}

        print(f"\n[BookingAdapter] GET {path}")
        print(f"Params: {query}")

        response = None
        for attempt in range(_BOOKING_MAX_RETRIES + 1):
            # 세마포어는 실제 요청 구간만 점유 → backoff 대기 중엔 다른 호출이 진행 가능
            async with self._rate_limit:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    try:
                        response = await client.get(url, params=query, headers=headers)
                    except httpx.TimeoutException:
                        return None, {"status": "error", "message": f"Booking API 타임아웃 (30초 초과): {path}"}
                    except httpx.RequestError as e:
                        return None, {"status": "error", "message": f"Booking API 요청 실패: {str(e)}"}

            print(f"[BookingAdapter] HTTP Status: {response.status_code}")

            # 429는 두 종류: (a) 초당 rate 초과 → backoff 재시도 / (b) 월 쿼터 소진 → 재시도 무의미, 즉시 포기
            if response.status_code == 429:
                h = response.headers
                body = response.text
                remaining = h.get("x-ratelimit-requests-remaining")
                quota_exhausted = "monthly" in body.lower() or (remaining is not None and _to_int_or(remaining, 1) <= 0)
                print(
                    "[BookingAdapter] 429 정보 — "
                    f"월한도={h.get('x-ratelimit-requests-limit')} 월잔여={remaining} "
                    f"초당한도={h.get('x-ratelimit-rate-limit-limit')} "
                    f"{'[월 쿼터 소진 — 재시도 안 함]' if quota_exhausted else '[초당 rate — 재시도]'} "
                    f"body={body[:120]}"
                )
                if not quota_exhausted and attempt < _BOOKING_MAX_RETRIES:
                    wait = 1.0 * (2 ** attempt)  # 1s → 2s → 4s
                    print(f"[BookingAdapter] backoff {wait:.0f}s 후 재시도 ({attempt + 1}/{_BOOKING_MAX_RETRIES})")
                    await asyncio.sleep(wait)
                    continue
            break

        if response.status_code != 200:
            return None, {"status": "error", "message": f"HTTP 오류: {response.status_code} - {response.text[:200]}"}

        try:
            data = response.json()
        except Exception:
            return None, {"status": "error", "message": f"응답이 JSON 형식이 아닙니다: {response.text[:200]}"}

        if data.get("status") is False:
            return None, {"status": "error", "message": f"Booking API 오류: {data.get('message')}"}

        return data, None

    # ================================================================== #
    # helpers — 선택 로직
    # ================================================================== #
    @staticmethod
    def _dest_score(c: Dict[str, Any], query: str):
        q = query.lower().strip()
        name = (c.get("name") or "").lower()
        label = (c.get("label") or "").lower()
        type_rank = {"city": 3, "district": 2, "landmark": 1}.get(c.get("dest_type"), 0)
        return (name == q, q in name or q in label, type_rank, c.get("nr_hotels") or 0)

    @staticmethod
    def _airport_score(c: Dict[str, Any], query: str):
        q = query.lower().strip()
        code = (c.get("code") or "").lower()
        name = (c.get("name") or "").lower()
        city = (c.get("cityName") or "").lower()
        return (code == q, q in name or q in city, c.get("code") is not None)

    # ================================================================== #
    # helpers — 요청 파라미터
    # ================================================================== #
    def _hotel_stay_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """get_room_list / get_hotel_details 공통 (날짜·인원·통화·언어)."""
        query = {
            "arrival_date": params["arrival_date"],
            "departure_date": params["departure_date"],
            "adults": params.get("adults", 1),
            "room_qty": params.get("room_qty", 1),
            "currency_code": self._CURRENCY,
            "languagecode": self._LANG,
        }
        self._apply_optional(query, params, ("children_age",))
        return query

    @staticmethod
    def _apply_optional(query: Dict[str, Any], params: Dict[str, Any], keys) -> None:
        for key in keys:
            value = params.get(key)
            if value is None:
                continue
            if key == "children_age" or key == "children":
                query[key] = BookingAdapter._csv(value)
            else:
                query[key] = value

    @staticmethod
    def _csv(value) -> Optional[str]:
        """children CSV 정규화: list → '0,17', str → 그대로."""
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return ",".join(str(v) for v in value)
        return str(value)

    @staticmethod
    def _ages_list(value) -> List[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        return [a for a in str(value).split(",") if a != ""]

    # ================================================================== #
    # helpers — URL 조립 (LLM 아닌 어댑터 책임)
    # ================================================================== #
    @staticmethod
    def _build_hotel_url(base_url, arrival, departure, adults, ages: List[str], room_qty) -> Optional[str]:
        if not base_url:
            return None
        url = base_url.replace(".html", ".ko.html")
        parts = [
            f"checkin={arrival}",
            f"checkout={departure}",
            f"group_adults={adults}",
            f"group_children={len(ages)}",
        ]
        parts += [f"age={a}" for a in ages]
        parts += [f"no_rooms={room_qty}", "selected_currency=KRW"]
        sep = "&" if "?" in url else "?"
        return url + sep + "&".join(parts)

    @staticmethod
    def _build_flight_list_url(from_id, to_id, depart, return_date, adults, children_csv, cabin, sort) -> str:
        trip = "ROUNDTRIP" if return_date else "ONEWAY"
        parts = [f"type={trip}", f"adults={adults}", f"cabinClass={cabin}"]
        if children_csv:
            parts.append(f"children={children_csv}")
        parts += [f"from={from_id}", f"to={to_id}", f"depart={depart}"]
        if return_date:
            parts.append(f"return={return_date}")
        parts.append(f"sort={sort}")
        return f"https://flights.booking.com/flights/{from_id}-{to_id}?" + "&".join(parts)

    # ================================================================== #
    # helpers — 응답 정제
    # ================================================================== #
    def _simplify_destination(self, c: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "dest_id": c.get("dest_id"),
            "search_type": c.get("search_type"),  # search_hotels 필수 입력
            "dest_type": c.get("dest_type"),
            "name": c.get("name"),
            "city_name": c.get("city_name"),
            "label": c.get("label"),
            "country": c.get("country"),
            "nr_hotels": c.get("nr_hotels"),
            "latitude": c.get("latitude"),
            "longitude": c.get("longitude"),
        }

    def _simplify_location(self, c: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": c.get("id"),  # 2-2 입력 핵심 (ICN.AIRPORT)
            "type": c.get("type"),
            "name": c.get("name"),
            "code": c.get("code"),
            "city_name": c.get("cityName"),
            "country": c.get("countryName"),
            "parent": c.get("parent"),
        }

    def _simplify_hotel(self, h: Dict[str, Any]) -> Dict[str, Any]:
        prop = h.get("property") or {}
        # 성급: accuratePropertyClass 우선, 0이면 propertyClass 폴백
        star = prop.get("accuratePropertyClass") or prop.get("propertyClass")
        return {
            "hotel_id": h.get("hotel_id") or prop.get("id"),
            "name": prop.get("name"),
            "star": star,
            "review_score": prop.get("reviewScore"),
            "review_word": prop.get("reviewScoreWord"),
            "review_count": prop.get("reviewCount"),
            "price": self._price(self._dig(prop, "priceBreakdown", "grossPrice")),
            "strikethrough_price": self._price(self._dig(prop, "priceBreakdown", "strikethroughPrice")),
            "currency": self._dig(prop, "priceBreakdown", "grossPrice", "currency"),
            "photo": (prop.get("photoUrls") or [None])[0],
            "latitude": prop.get("latitude"),
            "longitude": prop.get("longitude"),
            "summary": h.get("accessibilityLabel"),
        }

    def _simplify_block(self, b: Dict[str, Any], rooms: Dict[str, Any]) -> Dict[str, Any]:
        room_id = b.get("room_id")
        room = (rooms.get(str(room_id)) if room_id is not None else None) or {}
        pb = b.get("product_price_breakdown") or {}
        return {
            "block_id": b.get("block_id"),
            "room_id": room_id,
            "room_name": b.get("room_name") or b.get("name"),
            "surface_m2": b.get("room_surface_in_m2"),
            "max_occupancy": b.get("max_occupancy"),
            "mealplan": b.get("mealplan"),
            "breakfast_included": b.get("breakfast_included"),
            "price": self._price(pb.get("gross_amount")),
            "price_per_night": self._price(pb.get("gross_amount_per_night")),
            "strikethrough_price": self._price(pb.get("strikethrough_amount")),
            "cancellation": self._dig(b, "paymentterms", "cancellation", "type_translation"),
            "prepayment": self._dig(b, "paymentterms", "prepayment", "simple_translation"),
            "description": room.get("description"),
            "photo": (self._dig(room, "photos", default=[{}])[0] or {}).get("url_max750") if room.get("photos") else None,
            "bed_types": [
                bt.get("name_with_count")
                for bc in (room.get("bed_configurations") or [])
                for bt in (bc.get("bed_types") or [])
            ],
            "highlights": [h.get("translated_name") for h in (room.get("highlights") or [])],
        }

    def _simplify_offer(self, offer: Dict[str, Any]) -> Dict[str, Any]:
        segments = offer.get("segments") or []
        is_direct = all(len(s.get("legs") or []) == 1 for s in segments) if segments else None
        return {
            "token": offer.get("token"),
            "trip_type": offer.get("tripType"),
            "is_direct": is_direct,
            "price": self._price(self._dig(offer, "priceBreakdown", "total")),
            "price_rounded": self._dig(offer, "priceBreakdown", "totalRounded", "units"),
            "currency": self._dig(offer, "priceBreakdown", "total", "currencyCode"),
            "segments": [self._simplify_segment(s) for s in segments],
        }

    def _simplify_segment(self, s: Dict[str, Any]) -> Dict[str, Any]:
        legs = s.get("legs") or []
        return {
            "from": self._dig(s, "departureAirport", "code"),
            "to": self._dig(s, "arrivalAirport", "code"),
            "departure_time": s.get("departureTime"),
            "arrival_time": s.get("arrivalTime"),
            "total_time_sec": s.get("totalTime"),
            "stops": max(len(legs) - 1, 0),
            "legs": [
                {
                    "from": self._dig(leg, "departureAirport", "code"),
                    "to": self._dig(leg, "arrivalAirport", "code"),
                    "flight_number": self._dig(leg, "flightInfo", "flightNumber"),
                    "carriers": [c.get("name") for c in (leg.get("carriersData") or [])],
                    "logo": ((leg.get("carriersData") or [{}])[0] or {}).get("logo"),  # 대표(첫) 항공사 로고
                    "cabin_class": leg.get("cabinClass"),
                }
                for leg in legs
            ],
        }

    # ================================================================== #
    # helpers — 공통
    # ================================================================== #
    @staticmethod
    def _cheapest_per_room(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """room_id별 최저가 block 1개씩 (추천 block 없을 때 폴백)."""
        best: Dict[Any, Dict[str, Any]] = {}
        for b in blocks:
            rid = b.get("room_id")
            price = BookingAdapter._price(BookingAdapter._dig(b, "product_price_breakdown", "gross_amount"))
            price = price if price is not None else float("inf")
            cur = best.get(rid)
            cur_price = BookingAdapter._price(
                BookingAdapter._dig(cur, "product_price_breakdown", "gross_amount")
            ) if cur else None
            cur_price = cur_price if cur_price is not None else float("inf")
            if cur is None or price < cur_price:
                best[rid] = b
        return list(best.values())

    @staticmethod
    def _price(amount: Any) -> Optional[float]:
        """가격 객체 → float. units+nanos 형식과 value 형식 모두 지원."""
        if not isinstance(amount, dict):
            return None
        if "value" in amount:
            try:
                return float(amount["value"])
            except (TypeError, ValueError):
                return None
        units = amount.get("units")
        if units is None:
            return None
        nanos = amount.get("nanos") or 0
        try:
            return float(units) + float(nanos) / 1e9
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _dig(d: Any, *keys, default=None):
        cur = d
        for k in keys:
            if isinstance(cur, dict):
                cur = cur.get(k)
            else:
                return default
        return cur if cur is not None else default
