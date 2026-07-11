from __future__ import annotations

import asyncio
import math
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any

from app.schemas.ai_message import DayPlanItem, ItemCost, OrchestratorResult
from app.services.adapters.booking_api import BookingAdapter
from app.services.adapters.google_maps import GoogleMapsAdapter
from app.services.agents.itinerary_pipeline import (
    _extract_english_city,
    _fetch_hotels,
    _normalize_booking_flights,
    _normalize_booking_hotels,
)
from app.services.travel_agent_service import TravelAgentService


_service = TravelAgentService({
    "booking": BookingAdapter(),
    "google_maps": GoogleMapsAdapter(),
})


async def _booking_flight_search(
    origin_iata: str, dest_iata: str, depart_date: str, adults: int, child_ages: list,
) -> dict:
    """PATCH용 Booking 항공 검색. 기존 아이템에서 IATA를 이미 알므로 위치해석 없이 {IATA}.AIRPORT 조립."""
    query = {
        "fromId": f"{origin_iata}.AIRPORT",
        "toId": f"{dest_iata}.AIRPORT",
        "departDate": depart_date,
        "adults": adults,
    }
    csv = ",".join(str(a) for a in (child_ages or []))
    if csv:
        query["children"] = csv
    raw = await _service.process_task("booking", "search_flights", query)
    return _normalize_booking_flights(raw)

_CHANGE_WORDS = ("바꿔", "바꿀", "변경", "수정", "교체", "다른", "새로", "말고", "대신", "타고", "이용", "먹고", "먹을", "먹자", "추가", "넣어", "싶어", "싶은")
_TRANSPORT_TERMS = (
    "공항버스",
    "리무진버스",
    "리무진",
    "고속버스",
    "시외버스",
    "대중교통",
    "지하철",
    "렌터카",
    "렌트카",
    "자가용",
    "택시",
    "버스",
    "기차",
    "열차",
    "KTX",
    "ktx",
    "SRT",
    "srt",
    "자차",
)
_FLIGHT_TERMS = ("항공편", "비행편", "비행기", "항공")
_HOTEL_TERMS = ("숙소", "호텔", "체크인")
_HOTEL_STRONG_TERMS = ("체크인", "호텔", "숙박")
_MEAL_TERMS = ("아침", "점심", "저녁", "브런치", "간식", "야식")
_MEAL_DESIRE_WORDS = ("먹고", "먹을", "먹자", "추가", "넣어", "싶어", "싶은")
_GENERIC_PLACE_PHRASES = {
    "다른 곳", "다른곳", "다른 데", "다른데", "다른 것", "다른거", "다른 걸", "다른걸",
    "새로운 곳", "새로운 데", "어딘가", "아무 데나", "아무데나", "다른 식당", "다른식당",
}
_MEAL_DEFAULT_TIMES = {
    "아침": "08:00 ~ 09:00",
    "브런치": "10:00 ~ 11:00",
    "점심": "12:00 ~ 13:00",
    "간식": "15:00 ~ 15:30",
    "저녁": "18:00 ~ 19:00",
    "야식": "21:00 ~ 22:00",
}
_MEAL_QUERY_SUFFIX = {
    "아침": "breakfast",
    "브런치": "brunch",
    "점심": "lunch",
    "간식": "snack",
    "저녁": "dinner",
    "야식": "late night",
}
_TARGET_HINTS = ("공항", "인천공항", "인천국제공항", "김포공항", "역", "터미널", "숙소", "호텔")
_IATA_RE = re.compile(r"\b[A-Z]{3}\b")


async def try_patch_itinerary_item(deps: Any, user_message: str) -> OrchestratorResult | None:
    itinerary = deps.current_itinerary
    if not itinerary:
        print("[itinerary_patch] skip: current_itinerary 없음", flush=True)
        return None
    if not itinerary.get("day_plans"):
        print("[itinerary_patch] skip: current_itinerary.day_plans 없음", flush=True)
        return None
    if not _has_change_intent(user_message):
        print("[itinerary_patch] skip: 변경 의도 없음", flush=True)
        return None

    if _is_transport_change(user_message):
        result = await _patch_transport(deps, user_message)
        if result is None:
            print("[itinerary_patch] transport patch 실패: 대상 항목 또는 변경 수단 식별 실패", flush=True)
        return result
    if _is_flight_change(user_message):
        result = await _patch_flight(deps, user_message)
        if result is None:
            print("[itinerary_patch] flight patch 실패: 대상 항목/구간 또는 항공 후보 식별 실패", flush=True)
        return result
    if _is_hotel_change(user_message):
        result = await _patch_hotel(deps, user_message)
        if result is None:
            print("[itinerary_patch] hotel patch 실패: 대상 항목/숙박 조건 또는 숙소 후보 식별 실패", flush=True)
        return result
    if _is_meal_preference_change(user_message):
        result = await _patch_meal_preference(deps, user_message)
        if result is None:
            print("[itinerary_patch] meal patch 실패: 대상 날짜/식사 또는 음식 식별 실패", flush=True)
        return result
    print("[itinerary_patch] skip: 부분 패치 대상 타입 아님", flush=True)
    return None


def _has_change_intent(message: str) -> bool:
    return any(word in message for word in _CHANGE_WORDS)


def _is_transport_change(message: str) -> bool:
    return any(term in message for term in _TRANSPORT_TERMS) and (
        "이동" in message or "갈" in message or "타고" in message or any(hint in message for hint in _TARGET_HINTS)
    )


def _is_flight_change(message: str) -> bool:
    return any(term in message for term in _FLIGHT_TERMS)


def _is_hotel_change(message: str) -> bool:
    return any(term in message for term in _HOTEL_TERMS)


def _is_meal_preference_change(message: str) -> bool:
    return any(term in message for term in _MEAL_TERMS) and (
        any(word in message for word in _MEAL_DESIRE_WORDS)
        or any(word in message for word in _CHANGE_WORDS)
    )


def _copy_day_plans(itinerary: dict) -> dict[str, list[dict]]:
    return deepcopy(itinerary.get("day_plans") or {})


def _to_items(day_items: list[dict | DayPlanItem]) -> list[DayPlanItem]:
    return [item if isinstance(item, DayPlanItem) else DayPlanItem(**item) for item in day_items]


def _to_day_plan_items(day_plans: dict[str, list[dict | DayPlanItem]]) -> dict[str, list[DayPlanItem]]:
    return {date_key: _to_items(items) for date_key, items in day_plans.items()}


def _find_best_item(
    day_plans: dict[str, list[dict]],
    user_message: str,
    *,
    item_terms: tuple[str, ...],
    score_movement: bool = True,
) -> tuple[str, int, dict] | None:
    best: tuple[int, str, int, dict] | None = None
    for date_key, items in day_plans.items():
        for index, item in enumerate(items):
            text = " ".join(
                str(item.get(key) or "")
                for key in ("plan_name", "place", "note", "time")
            )
            score = 0
            if any(term in text for term in item_terms):
                score += 5
            if score_movement and "이동" in text:
                score += 3
            for hint in _TARGET_HINTS:
                if hint in user_message and hint in text:
                    score += 3
            for term in _TRANSPORT_TERMS:
                if term in user_message and term in text:
                    score += 2
            if score and (best is None or score > best[0]):
                best = (score, date_key, index, item)
    if best is None:
        return None
    _, date_key, index, item = best
    return date_key, index, item


def _find_hotel_item(day_plans: dict[str, list[dict]]) -> tuple[str, int, dict] | None:
    best: tuple[int, str, int, dict] | None = None
    for date_key, items in day_plans.items():
        for index, item in enumerate(items):
            text = " ".join(str(item.get(key) or "") for key in ("plan_name", "place", "note"))
            is_movement = "이동" in text or "공항" in text or "→" in text
            score = 0
            if any(term in text for term in _HOTEL_STRONG_TERMS):
                score += 10
            elif "숙소" in text and not is_movement:
                score += 5
            if not score:
                continue
            if is_movement and "체크인" not in text:
                continue
            if best is None or score > best[0]:
                best = (score, date_key, index, item)
    if best is None:
        return None
    _, date_key, index, item = best
    return date_key, index, item


def _resolve_requested_date_key(day_plans: dict[str, list[dict]], user_message: str) -> str | None:
    date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", user_message)
    if date_match and date_match.group(1) in day_plans:
        return date_match.group(1)

    day_match = re.search(r"(\d+)\s*일차", user_message)
    if day_match:
        day_index = int(day_match.group(1)) - 1
        date_keys = sorted(day_plans)
        if 0 <= day_index < len(date_keys):
            return date_keys[day_index]

    return None


def _extract_meal_slot(user_message: str) -> str | None:
    for slot in _MEAL_TERMS:
        if slot in user_message:
            return slot
    return None


def _extract_requested_food(user_message: str, meal_slot: str) -> str | None:
    if meal_slot not in user_message:
        return None
    after_slot = user_message.split(meal_slot, 1)[1]
    after_slot = re.sub(r"^\s*(식사)?\s*(을|를|에|으로|로)?\s*", "", after_slot).strip()
    if not after_slot:
        return None

    food = re.split(
        r"\s*(?:으로|로)?\s*(?:먹고\s*싶|먹고싶|먹을래|먹자|추가|넣어|바꿔|변경|교체)",
        after_slot,
        maxsplit=1,
    )[0]
    food = re.sub(r"\s*(을|를|으로|로|에)$", "", food).strip()
    if food in _GENERIC_PLACE_PHRASES:
        return None
    return food or None


def _find_meal_item(day_items: list[dict], meal_slot: str) -> tuple[int, dict] | None:
    for index, item in enumerate(day_items):
        text = " ".join(str(item.get(key) or "") for key in ("plan_name", "place", "note"))
        if meal_slot in text and "이동" not in text:
            return index, item
    return None


def _meal_aliases_from_item(item: dict) -> list[str]:
    aliases = [
        str(item.get("place") or "").strip(),
        re.sub(r"\s*(아침|조식|점심|중식|저녁|석식|브런치|간식|야식)?\s*식사\s*", "", str(item.get("plan_name") or "")).strip(),
    ]
    match = re.search(r"\(([^)]+)\)", str(item.get("plan_name") or ""))
    if match:
        aliases.append(match.group(1).strip())
    return [alias for alias in dict.fromkeys(aliases) if alias]


def _is_movement_item(item: dict) -> bool:
    text = " ".join(str(item.get(key) or "") for key in ("plan_name", "note"))
    return "이동" in text or "→" in text


def _replace_route_endpoint(plan_name: str, old_aliases: list[str], new_name: str, *, endpoint: str) -> str:
    if "→" in plan_name:
        left, right = plan_name.split("→", 1)
        if endpoint == "origin":
            return f"{new_name} → {right.strip()}"

        suffix = ""
        destination = right.strip()
        match = re.match(r"(.+?)(\s+이동(?:\s*\([^)]*\))?.*)$", destination)
        if match:
            destination, suffix = match.group(1).strip(), match.group(2)
        return f"{left.strip()} → {new_name}{suffix}"

    result = plan_name
    for alias in sorted(old_aliases, key=len, reverse=True):
        if alias:
            result = result.replace(alias, new_name)
    return result


def _sync_meal_related_transfers(
    day_items: list[dict],
    meal_index: int,
    old_item: dict,
    new_name: str,
    new_place: str | None,
) -> None:
    old_aliases = _meal_aliases_from_item(old_item)
    if not old_aliases:
        return

    if meal_index > 0 and _is_movement_item(day_items[meal_index - 1]):
        prev = dict(day_items[meal_index - 1])
        prev["plan_name"] = _replace_route_endpoint(
            str(prev.get("plan_name") or ""),
            old_aliases,
            new_name,
            endpoint="destination",
        )
        prev["place"] = new_place or new_name
        prev["note"] = _replace_aliases(str(prev.get("note") or ""), old_aliases, new_name)
        day_items[meal_index - 1] = prev

    if meal_index + 1 < len(day_items) and _is_movement_item(day_items[meal_index + 1]):
        nxt = dict(day_items[meal_index + 1])
        nxt["plan_name"] = _replace_route_endpoint(
            str(nxt.get("plan_name") or ""),
            old_aliases,
            new_name,
            endpoint="origin",
        )
        nxt["note"] = _replace_aliases(str(nxt.get("note") or ""), old_aliases, new_name)
        day_items[meal_index + 1] = nxt


def _replace_aliases(text: str, aliases: list[str], replacement: str) -> str:
    result = text
    for alias in sorted(aliases, key=len, reverse=True):
        if alias:
            result = result.replace(alias, replacement)
    return result


def _meal_insert_index(day_items: list[dict], meal_slot: str) -> int:
    default_time = _MEAL_DEFAULT_TIMES.get(meal_slot, "")
    default_start = default_time[:5]
    for index, item in enumerate(day_items):
        item_time = str(item.get("time") or "")
        if item_time[:5] and default_start and item_time[:5] > default_start:
            return index
    return len(day_items)


async def _patch_meal_preference(deps: Any, user_message: str) -> OrchestratorResult | None:
    day_plans = _copy_day_plans(deps.current_itinerary)
    date_key = _resolve_requested_date_key(day_plans, user_message)
    meal_slot = _extract_meal_slot(user_message)
    if not date_key or not meal_slot:
        return None

    food = _extract_requested_food(user_message, meal_slot)
    if not food:
        return None

    stay = _find_stay_for_date(deps.current_itinerary, date_key)
    city = stay[0] if stay else None
    if not city:
        city = deps.current_itinerary.get("destination")
    if not city:
        destinations = deps.current_itinerary.get("destinations") or []
        if destinations:
            city = destinations[0].get("city")
    city = city or ""

    place_name, place_address, place_note = await _search_meal_place(city, meal_slot, food)

    day_items = day_plans.get(date_key) or []
    target = _find_meal_item(day_items, meal_slot)
    patched = {
        "plan_name": f"{meal_slot} 식사 ({place_name or food})",
        "time": _MEAL_DEFAULT_TIMES.get(meal_slot, ""),
        "place": place_address or place_name or city,
        "note": place_note or f"{food} 요청을 반영했습니다.",
        "cost": None,
    }

    if target:
        index, item = target
        patched = dict(item)
        old_item = dict(item)
        new_name = place_name or food
        new_place = place_address or place_name or city
        patched["plan_name"] = _replace_meal_plan_name(str(patched.get("plan_name") or ""), meal_slot, place_name or food)
        patched["place"] = new_place
        patched["note"] = place_note or f"{food} 요청을 반영했습니다."
        day_items[index] = patched
        _sync_meal_related_transfers(day_items, index, old_item, new_name, new_place)
    else:
        day_items.insert(_meal_insert_index(day_items, meal_slot), patched)

    day_plans[date_key] = day_items
    return OrchestratorResult(
        message=f"{date_key} {meal_slot} 일정에 {place_name or food}을(를) 반영했습니다.",
        ai_summary=_append_summary(deps.ai_summary, f"{date_key} {meal_slot}에 {place_name or food} 식사 반영"),
        preferences=_merge_food_preference(deps.preferences, food),
        day_plans={date_key: _to_items(day_items)},
    )


async def _search_meal_place(city: str, meal_slot: str, food: str) -> tuple[str, str | None, str | None]:
    city_en = await _extract_english_city(city) if city else None
    query_city = city_en or city
    city_center = await _lookup_city_center(city, city_en) if city else None
    query = f"{food} {city} {query_city} {_MEAL_QUERY_SUFFIX.get(meal_slot, '')}".strip()
    search_params = {"query": query}
    if city_center:
        search_params["location"] = f"{city_center[0]},{city_center[1]}"
        search_params["radius"] = 100000

    place_result = await _service.process_task("google_maps", "search_place", search_params)
    place_name = food
    place_address = None
    if place_result.get("status") == "success":
        places = place_result.get("data", {}).get("places", [])
        valid_places = [place for place in places if _place_near_center(place, city_center)] if city_center else places
        if valid_places:
            first = valid_places[0]
            place_name = first.get("name") or place_name
            place_address = first.get("formatted_address") or first.get("name")

    tavily_result = await _service.process_task("tavily_search", "search", {
        "query": f"{food} {city} {meal_slot} 맛집",
        "search_depth": "basic",
        "max_results": 3,
    })
    note_parts = [f"{food} 요청을 반영했습니다."]
    if place_name:
        note_parts.append(f"검색 후보: {place_name}.")
    if tavily_result.get("status") == "success":
        items = tavily_result.get("data", [])
        if items:
            snippet = str(items[0].get("content") or "").strip()
            if snippet:
                note_parts.append(snippet[:120])
    return place_name, place_address, " ".join(note_parts)


async def _lookup_city_center(city: str, city_en: str | None = None) -> tuple[float, float] | None:
    query = " ".join(part for part in (city, city_en) if part).strip()
    if not query:
        return None

    result = await _service.process_task("google_maps", "search_place", {"query": query})
    if result.get("status") != "success":
        return None

    places = result.get("data", {}).get("places", [])
    if not places:
        return None

    lat = places[0].get("lat")
    lng = places[0].get("lng")
    if lat is None or lng is None:
        return None
    return float(lat), float(lng)


def _place_near_center(place: dict, center: tuple[float, float] | None, radius_km: float = 100.0) -> bool:
    if center is None:
        return True

    lat = place.get("lat")
    lng = place.get("lng")
    if lat is None or lng is None:
        return False

    return _distance_km(center[0], center[1], float(lat), float(lng)) <= radius_km


def _distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def _replace_meal_plan_name(plan_name: str, meal_slot: str, place_name: str) -> str:
    if "식사" in plan_name:
        return f"{meal_slot} 식사 ({place_name})"
    if "-" in plan_name:
        return f"{meal_slot} - {place_name}"
    return f"{meal_slot} 식사 ({place_name})"


def _infer_transport_change(message: str) -> tuple[str | None, str | None]:
    terms = [term for term in _TRANSPORT_TERMS if term in message]
    old_transport = None
    new_transport = terms[-1] if terms else None

    for marker in ("말고", "대신"):
        if marker in message:
            before, after = message.split(marker, 1)
            before_terms = [term for term in _TRANSPORT_TERMS if term in before]
            after_terms = [term for term in _TRANSPORT_TERMS if term in after]
            old_transport = before_terms[-1] if before_terms else old_transport
            new_transport = after_terms[-1] if after_terms else new_transport
            break

    return _normalize_transport(old_transport), _normalize_transport(new_transport)


def _normalize_transport(transport: str | None) -> str | None:
    if transport in {"ktx", "KTX"}:
        return "KTX"
    if transport in {"srt", "SRT"}:
        return "SRT"
    if transport == "렌트카":
        return "렌터카"
    if transport == "리무진":
        return "공항 리무진"
    if transport == "리무진버스":
        return "공항 리무진버스"
    return transport


async def _patch_transport(deps: Any, user_message: str) -> OrchestratorResult | None:
    day_plans = _copy_day_plans(deps.current_itinerary)
    target = _find_best_item(day_plans, user_message, item_terms=("이동",) + _TRANSPORT_TERMS)
    if target is None:
        print(f"[itinerary_patch] transport target 없음. day_keys={list(day_plans.keys())}", flush=True)
        return None

    date_key, index, item = target
    old_transport, new_transport = _infer_transport_change(user_message)
    if not new_transport:
        print(f"[itinerary_patch] transport 변경 수단 없음. message={user_message!r}", flush=True)
        return None

    origin, dest = _extract_route_points(item)
    route = await _find_route_if_possible(origin, dest, new_transport)

    patched = dict(item)
    patched["plan_name"] = _replace_transport_text(
        patched.get("plan_name") or "이동",
        old_transport,
        new_transport,
    )
    patched["note"] = _build_transport_note(patched.get("note") or "", old_transport, new_transport, route)
    patched["cost"] = _transport_cost(route, deps.current_itinerary)

    day_plans[date_key][index] = patched
    message = f"{date_key} 일정의 이동수단을 {old_transport or '기존 수단'}에서 {new_transport}(으)로 변경했습니다."
    return OrchestratorResult(
        message=message,
        ai_summary=_append_summary(deps.ai_summary, f"{date_key} 이동수단을 {new_transport}(으)로 변경"),
        preferences=_merge_transport_preference(deps.preferences, new_transport),
        day_plans={date_key: _to_items(day_plans[date_key])},
    )


def _replace_transport_text(text: str, old_transport: str | None, new_transport: str) -> str:
    result = text
    if old_transport and old_transport in result:
        result = result.replace(old_transport, new_transport)
    elif "(" in result and ")" in result:
        result = re.sub(r"\([^)]*\)", f"({new_transport})", result, count=1)
    else:
        result = f"{result} ({new_transport})"
    return result


def _extract_route_points(item: dict) -> tuple[str | None, str | None]:
    candidates = [item.get("place") or "", item.get("plan_name") or ""]
    for text in candidates:
        clean = re.sub(r"\([^)]*\)", "", text)
        if "→" in clean:
            left, right = clean.split("→", 1)
            origin = left.strip()
            dest = right.strip().split()[0].strip()
            if origin and dest:
                return origin, dest
    return None, None


async def _find_route_if_possible(origin: str | None, dest: str | None, transport: str) -> dict | None:
    if not origin or not dest:
        return None
    mode = "driving" if transport in {"자차", "자가용", "렌터카"} else "transit"
    result = await _service.process_task("google_maps", "find_route", {
        "origin": origin,
        "dest": dest,
        "mode": mode,
    })
    if result.get("status") != "success":
        return None
    routes = result.get("data", {}).get("routes", [])
    return routes[0] if routes else None


def _build_transport_note(base_note: str, old_transport: str | None, new_transport: str, route: dict | None) -> str:
    parts = []
    if base_note:
        parts.append(base_note)
    parts.append(f"{old_transport or '기존 이동수단'} 대신 {new_transport} 이용으로 변경.")
    if route:
        if route.get("duration_text"):
            parts.append(f"예상 소요시간: {route['duration_text']}.")
        if route.get("distance_text"):
            parts.append(f"거리: {route['distance_text']}.")
        fare = route.get("fare")
        if fare:
            parts.append(f"예상 요금: 1인 {fare.get('text')}.")
    else:
        parts.append("정확한 노선·배차·요금은 출발 전 확인 필요.")
    return " ".join(parts)


def _transport_cost(route: dict | None, itinerary: dict) -> ItemCost | None:
    if not route or not route.get("fare"):
        return None
    fare = route["fare"]
    value = fare.get("value")
    currency = fare.get("currency")
    if value is None or not currency:
        return None
    adults = itinerary.get("adult_count") or 1
    children = itinerary.get("child_count") or 0
    amount = float(value) * adults + float(value) * 0.5 * children
    return ItemCost(amount=amount, currency=currency)


async def _patch_flight(deps: Any, user_message: str) -> OrchestratorResult | None:
    day_plans = _copy_day_plans(deps.current_itinerary)
    end_date = _get_itinerary_end_date(deps.current_itinerary)
    start_date = _get_itinerary_start_date(deps.current_itinerary)
    origin = deps.current_itinerary.get("origin")
    target = _find_flight_item(
        day_plans, user_message, end_date=end_date, start_date=start_date, origin=origin,
    )
    if target is None:
        return None
    date_key, index, item = target
    route = _extract_iata_route(item)
    if route is None:
        return _unavailable_patch_result(
            deps,
            day_plans,
            date_key,
            "기존 항공 이동 항목에서 공항 코드를 확인하지 못해 항공편만 부분 변경할 수 없습니다.",
        )
    origin, destination = route

    # 파이프라인과 동일: end_date 기준으로 귀국편 판별 (마지막 2일 = 귀국 구간)
    is_return = (
        end_date is not None
        and date_key >= str(date.fromisoformat(end_date) - timedelta(days=1))
    )
    adults = deps.current_itinerary.get("adult_count") or 1
    child_ages = deps.current_itinerary.get("child_ages") or []

    if is_return and end_date:
        # 파이프라인과 동일: end_date + end_date-1 양쪽 검색 후 합산, arriving_at <= end_date 필터
        end_dt = date.fromisoformat(end_date)
        r_cur, r_prev = await asyncio.gather(
            _booking_flight_search(origin, destination, end_date, adults, child_ages),
            _booking_flight_search(origin, destination, str(end_dt - timedelta(days=1)), adults, child_ages),
        )
        merged: list[dict] = []
        for r in (r_cur, r_prev):
            if r.get("status") == "success":
                merged.extend(r.get("data") or [])
        offers = [f for f in merged if (f.get("arriving_at") or "")[:10] <= end_date]
        if not offers:
            print(f"[itinerary_patch] 귀국편 end_date({end_date}) 이내 도착편 없음 — 전체 결과 사용", flush=True)
            offers = merged
    else:
        result = await _booking_flight_search(origin, destination, date_key, adults, child_ages)
        offers = result.get("data") or []

    requested_airline = _extract_requested_airline(user_message)
    if not offers:
        if requested_airline:
            return _confirmation_result(f"{requested_airline} 항공편을 찾지 못했습니다. 검색 가능한 항공편 후보도 없습니다.")
        return _unavailable_patch_result(deps, day_plans, date_key, "항공편 실시간 후보를 조회할 수 없어 기존 항공 일정을 유지했습니다.")

    time_pref = _extract_time_of_day_preference(user_message)
    if time_pref:
        filtered = _filter_offers_by_time_of_day(offers, time_pref)
        if filtered:
            offers = filtered
        else:
            pref_label = {"morning": "오전", "afternoon": "오후", "evening": "저녁/야간"}.get(time_pref, time_pref)
            return _confirmation_result(
                f"원하시는 {pref_label} 시간대 항공편이 없습니다. "
                f"검색 가능한 후보로 {_format_flight_candidates(offers)}가 있습니다. 이 중 하나로 바꿀까요?"
            )

    if requested_airline:
        offer = _select_matching_flight_offer(offers, requested_airline)
        if offer is None:
            return _confirmation_result(
                f"{requested_airline} 항공편을 찾지 못했습니다. "
                f"검색 가능한 후보로 {_format_flight_candidates(offers)}가 있습니다. 이 중 하나로 바꿀까요?"
            )
    else:
        offer = offers[0]

    departing_at = str(offer.get("departing_at") or "")
    arriving_at = str(offer.get("arriving_at") or "")
    depart_dt = _parse_iso_datetime(departing_at)
    arrive_dt = _parse_iso_datetime(arriving_at)

    patched = dict(item)
    patched["plan_name"] = f"{offer['origin']} → {offer['destination']} 항공 이동 ({offer['airline']})"
    patched["place"] = f"{offer['origin']} → {offer['destination']}"
    patched["note"] = f"비행시간 {offer.get('duration', '?')} | {'직항' if offer.get('stops', 0) == 0 else str(offer.get('stops')) + '회 경유'}"
    patched["cost"] = _cost_from_amount(offer.get("price_original"), offer.get("currency"), offer.get("price_krw"))
    patched["image_url"] = offer.get("image_url")  # 항공사 로고
    patched["url"] = offer.get("url")              # Booking 검색 리스트 URL

    if depart_dt and arrive_dt and depart_dt.date() != arrive_dt.date():
        patched["time"] = f"{depart_dt.strftime('%H:%M')} ~ 23:59"
        day_plans[date_key][index] = patched
        arrival_date_key = arrive_dt.date().isoformat()
        arrival_item = {
            "plan_name": f"{offer['airline']} 기내 (비행 중) → {offer['destination']} 도착",
            "time": f"00:00 ~ {arrive_dt.strftime('%H:%M')}",
            "place": offer["destination"],
            "note": f"출발 {depart_dt.strftime('%H:%M')} {offer['origin']} | 도착 {arrive_dt.strftime('%H:%M')} {offer['destination']} | 총 비행시간 약 {offer.get('duration', '?')} | 시차 0h | {'직항' if offer.get('stops', 0) == 0 else str(offer.get('stops')) + '회 경유'}",
            "cost": None,
            "image_url": offer.get("image_url"),  # 항공사 로고
        }
        arrival_items = day_plans.setdefault(arrival_date_key, [])
        arrival_items.insert(0, arrival_item)
        # 자택/귀가 항목을 먼저 도착일로 이동한 뒤 고아 항목 정리 (이동 대상을 제거하지 않도록)
        _move_post_arrival_transfer(day_plans, date_key, arrival_date_key, arrive_dt)
        day_plans[date_key] = _cleanup_post_arrival_orphans(day_plans[date_key], destination, depart_dt)
        flight_idx = next((i for i, it in enumerate(day_plans[date_key]) if it is patched), index)
        _retime_departure_transfer(day_plans[date_key], flight_idx, depart_dt)
        _sort_day_items(day_plans[date_key])
        _sort_day_items(day_plans[arrival_date_key])
        message = f"{date_key} 항공 이동 항목을 새 항공편 후보로 변경했습니다."
        return OrchestratorResult(
            message=message,
            ai_summary=_append_summary(deps.ai_summary, f"{date_key} 항공 이동 항목 변경"),
            preferences=deps.preferences,
            day_plans=_to_day_plan_items(day_plans),
        )

    patched["time"] = f"{depart_dt.strftime('%H:%M') if depart_dt else departing_at[11:16]} ~ {arrive_dt.strftime('%H:%M') if arrive_dt else arriving_at[11:16]}"
    day_plans[date_key][index] = patched
    if depart_dt:
        day_plans[date_key] = _cleanup_post_arrival_orphans(day_plans[date_key], destination, depart_dt)
        flight_idx = next((i for i, it in enumerate(day_plans[date_key]) if it is patched), index)
    else:
        flight_idx = index
    _retime_departure_transfer(day_plans[date_key], flight_idx, depart_dt)
    _sort_day_items(day_plans[date_key])

    return OrchestratorResult(
        message=f"{date_key} 항공 이동 항목을 새 항공편 후보로 변경했습니다.",
        ai_summary=_append_summary(deps.ai_summary, f"{date_key} 항공 이동 항목 변경"),
        preferences=deps.preferences,
        day_plans=_to_day_plan_items(day_plans),
    )


def _find_flight_item(
    day_plans: dict[str, list[dict]],
    user_message: str,
    *,
    end_date: str | None = None,
    start_date: str | None = None,
    origin: str | None = None,
) -> tuple[str, int, dict] | None:
    direction_hint = _flight_direction_hint(user_message, origin)
    return_threshold = str(date.fromisoformat(end_date) - timedelta(days=1)) if end_date else None
    depart_threshold = str(date.fromisoformat(start_date) + timedelta(days=1)) if start_date else None
    best: tuple[int, str, int, dict] | None = None
    for date_key, items in day_plans.items():
        for index, item in enumerate(items):
            text = " ".join(str(item.get(key) or "") for key in ("plan_name", "place", "note", "time"))
            codes = _IATA_RE.findall(text)
            score = 0
            if any(term in text for term in _FLIGHT_TERMS):
                score += 5
            if "항공 이동" in text or "비행" in text:
                score += 4
            if len(codes) >= 2:
                score += 3
            if "공항" in user_message and len(codes) >= 2:
                score += 1
            if "공항" in text and "항공" not in text:
                score -= 4
            if direction_hint:
                # 파이프라인과 동일: end_date/start_date 기반으로 귀국편·출발편 구분
                if direction_hint == "return":
                    if return_threshold and date_key >= return_threshold:
                        score += 8
                    elif depart_threshold and date_key <= depart_threshold:
                        score -= 4
                elif direction_hint == "depart":
                    if depart_threshold and date_key <= depart_threshold:
                        score += 8
                    elif return_threshold and date_key >= return_threshold:
                        score -= 4
            if score and (best is None or score > best[0]):
                best = (score, date_key, index, item)
    if best is None:
        return None
    _, date_key, index, item = best
    return date_key, index, item


def _flight_direction_hint(user_message: str, origin: str | None = None) -> str | None:
    return_words = ("귀국", "돌아오는", "오는", "인천으로")
    depart_words = ("출발", "가는", "떠나는", "서울에서", "인천에서")
    if origin:
        return_words += (f"{origin}으로", f"{origin}로")
        depart_words += (f"{origin}에서",)
    if any(word in user_message for word in return_words):
        return "return"
    if any(word in user_message for word in depart_words):
        return "depart"
    return None


def _get_itinerary_end_date(itinerary: dict) -> str | None:
    raw = (
        itinerary.get("end_date")
        or ((itinerary.get("destinations") or [{}])[-1].get("end_date") or "")
    )
    return raw[:10] if raw else None


def _get_itinerary_start_date(itinerary: dict) -> str | None:
    raw = (
        itinerary.get("start_date")
        or ((itinerary.get("destinations") or [{}])[0].get("start_date") or "")
    )
    return raw[:10] if raw else None


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_time_range(time_text: str) -> tuple[str, str] | None:
    match = re.match(r"\s*(\d{2}:\d{2})\s*~\s*(\d{2}:\d{2})\s*", time_text or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def _set_item_time(item: dict, start_dt: datetime, end_dt: datetime) -> None:
    item["time"] = f"{start_dt.strftime('%H:%M')} ~ {end_dt.strftime('%H:%M')}"


def _retime_departure_transfer(items: list[dict], flight_index: int, depart_dt: datetime | None) -> None:
    if not depart_dt:
        return
    for index in range(min(flight_index, len(items))):
        text = " ".join(str(items[index].get(key) or "") for key in ("plan_name", "place", "note"))
        if "공항" not in text or "이동" not in text or "항공" in text:
            continue
        start_dt = depart_dt - timedelta(hours=2, minutes=30)
        end_dt = depart_dt - timedelta(hours=2, minutes=10)
        _set_item_time(items[index], start_dt, end_dt)
        break


def _move_post_arrival_transfer(
    day_plans: dict[str, list[dict]],
    departure_date_key: str,
    arrival_date_key: str,
    arrive_dt: datetime,
) -> None:
    departure_items = day_plans.get(departure_date_key) or []
    for index, item in enumerate(list(departure_items)):
        text = " ".join(str(item.get(key) or "") for key in ("plan_name", "place", "note"))
        if any(word in text for word in ("자택", "귀가", "집")) and "이동" in text:
            moved = departure_items.pop(index)
            moved["time"] = f"{(arrive_dt + timedelta(minutes=30)).strftime('%H:%M')} ~ {(arrive_dt + timedelta(minutes=90)).strftime('%H:%M')}"
            arrival_items = day_plans.setdefault(arrival_date_key, [])
            arrival_items.append(moved)
            break


def _sort_day_items(items: list[dict]) -> None:
    def _key(item: dict) -> tuple[int, str]:
        parsed = _parse_time_range(str(item.get("time") or ""))
        if not parsed:
            return (9999, str(item.get("time") or ""))
        return (int(parsed[0][:2]) * 60 + int(parsed[0][3:5]), str(item.get("time") or ""))

    items.sort(key=_key)


def _extract_requested_airline(user_message: str) -> str | None:
    if "항공" not in user_message and "비행" not in user_message:
        return None
    match = re.search(r"(.+?)(?:으로|로)\s*(?:바꿔|변경|수정|교체)", user_message.strip())
    if not match:
        return None
    name = match.group(1).strip()
    name = re.sub(r"^(항공편|비행편|비행기)\s*", "", name).strip()
    name = re.sub(r"\s+(항공편|비행편|비행기)$", "", name).strip()
    if name in {"다른 것", "다른거", "다른 걸", "다른걸", "다른 항공편", "새 항공편", "오전", "오후", "저녁"}:
        return None
    if any(word in name for word in ("시간", "오전", "오후", "아침", "저녁", "새벽", "밤", "출발", "도착", "오는", "가는")):
        return None
    return name or None


def _select_matching_flight_offer(offers: list[dict], requested_airline: str) -> dict | None:
    requested = _normalize_name(requested_airline)
    for offer in offers:
        candidate = _normalize_name(str(offer.get("airline") or ""))
        if requested and (requested in candidate or candidate in requested):
            return offer
    return None


def _format_flight_candidates(offers: list[dict], limit: int = 3) -> str:
    names: list[str] = []
    for offer in offers[:limit]:
        airline = offer.get("airline") or "항공편"
        departing = str(offer.get("departing_at") or "")
        time = departing[11:16] if len(departing) >= 16 else ""
        label = f"{airline} {time}".strip()
        if label not in names:
            names.append(label)
    return ", ".join(names) if names else "대체 항공편"


def _extract_iata_route(item: dict) -> tuple[str, str] | None:
    text = " ".join(str(item.get(key) or "") for key in ("plan_name", "place", "note"))
    codes = _IATA_RE.findall(text)
    if len(codes) >= 2:
        return codes[0], codes[1]
    return None


async def _patch_hotel(deps: Any, user_message: str) -> OrchestratorResult | None:
    day_plans = _copy_day_plans(deps.current_itinerary)
    target = _find_hotel_item(day_plans)
    if target is None:
        return None
    date_key, index, item = target
    stay = _find_stay_for_date(deps.current_itinerary, date_key)
    if stay is None:
        return None

    city, check_in, check_out = stay
    result = await _search_hotels_with_city_fallbacks(deps.current_itinerary, city, check_in, check_out)
    hotels = result.get("data") or []
    requested_hotel = _extract_requested_hotel_name(user_message)
    excluded_hotels = _extract_excluded_hotel_names(user_message)
    if result.get("status") != "success" or not hotels:
        if requested_hotel:
            return _confirmation_result(f"{requested_hotel}을(를) 찾지 못했습니다. 검색 가능한 숙소 후보도 없습니다.")
        return _patch_hotel_locally(deps, user_message, day_plans, date_key, index, item, check_in, check_out, requested_hotel)

    if excluded_hotels:
        hotels = _filter_excluded_hotels(hotels, excluded_hotels)
        if not hotels:
            return _confirmation_result(f"{', '.join(excluded_hotels)}을(를) 제외한 검색 가능한 숙소 후보가 없습니다.")

    if requested_hotel:
        matched_hotel = _select_matching_hotel(hotels, requested_hotel)
        if matched_hotel is None:
            return _confirmation_result(
                f"{requested_hotel}을(를) 찾지 못했습니다. "
                f"검색 가능한 후보로 {_format_hotel_candidates(hotels)}가 있습니다. 이 중 하나로 바꿀까요?"
            )
        hotel = matched_hotel
    elif any(word in user_message for word in ("저렴", "싼", "가성비", "저가")):
        hotels = sorted(hotels, key=lambda h: float(h.get("price_original") or h.get("price_krw") or 10**12))
        hotel = hotels[0]
    elif any(word in user_message for word in ("시설", "좋은", "고급", "깨끗", "평점", "별점")):
        hotels = sorted(hotels, key=lambda h: float(h.get("rating") or 0), reverse=True)
        hotel = hotels[0]
    else:
        hotel = hotels[0]

    # booking_url은 선택된 호텔에 대해서만 상세 호출로 채움 (파이프라인과 동일 정책)
    booking_url = None
    if hotel.get("hotel_id"):
        detail_query = {
            "hotel_id": hotel["hotel_id"],
            "arrival_date": check_in[:10],
            "departure_date": check_out[:10],
            "adults": deps.current_itinerary.get("adult_count") or 1,
        }
        _child_ages = deps.current_itinerary.get("child_ages") or []
        if _child_ages:
            detail_query["children_age"] = ",".join(str(a) for a in _child_ages)
        detail = await _service.process_task("booking", "get_hotel_details", detail_query)
        if isinstance(detail, dict) and detail.get("status") == "success":
            booking_url = (detail.get("data") or {}).get("booking_url")

    patched = dict(item)
    patched["plan_name"] = f"{hotel.get('name', '숙소')} 체크인"
    patched["place"] = hotel.get("address") or city
    patched["note"] = f"{check_in} ~ {check_out} 숙박. 평점: {hotel.get('rating') or '정보 없음'}."
    patched["cost"] = _cost_from_amount(hotel.get("price_original"), hotel.get("currency"), hotel.get("price_krw"))
    patched["image_url"] = hotel.get("image_url")  # Booking 호텔 사진
    patched["url"] = booking_url                    # Booking 예약 deeplink
    day_plans[date_key][index] = patched
    _sync_hotel_related_items(
        day_plans,
        index,
        check_in,
        check_out,
        hotel.get("name") or "숙소",
        patched["place"],
        _hotel_aliases_from_item(item),
    )

    return OrchestratorResult(
        message=f"{date_key} 숙소 항목을 {hotel.get('name', '새 숙소')}(으)로 변경했습니다.",
        ai_summary=_append_summary(deps.ai_summary, f"{date_key} 숙소 항목 변경"),
        preferences=deps.preferences,
        day_plans=_to_day_plan_items(day_plans),
    )


_GENERIC_HOTEL_REQUEST_NAMES = {
    "다른",
    "아무",
    "새",
    "새로운",
    "다른 곳",
    "다른곳",
    "다른 숙소",
    "다른 호텔",
    "새 숙소",
    "새 호텔",
}


def _clean_requested_hotel_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"^(숙소|호텔)\s*", "", name).strip()
    name = re.sub(r"\s+(숙소|호텔)$", "", name).strip()
    return name


def _is_generic_hotel_request_name(name: str) -> bool:
    cleaned = _clean_requested_hotel_name(name)
    return cleaned in _GENERIC_HOTEL_REQUEST_NAMES


def _extract_requested_hotel_name(user_message: str) -> str | None:
    text = user_message.strip()
    if any(word in text for word in ("저렴", "싼", "가성비", "저가", "시설", "좋은", "고급", "깨끗", "넓은", "평점", "별점")):
        return None
    for marker in ("말고", "대신"):
        if marker not in text:
            continue
        after_marker = text.split(marker, 1)[1].strip()
        match = re.search(r"(.+?)(?:으로|로)\s*(?:바꿔|변경|수정|교체)", after_marker)
        if not match:
            return None
        name = _clean_requested_hotel_name(match.group(1))
        if _is_generic_hotel_request_name(name):
            return None
        return name or None

    match = re.search(r"(.+?)(?:으로|로)\s*(?:바꿔|변경|수정|교체)", text)
    if not match:
        return None
    name = _clean_requested_hotel_name(match.group(1))
    if _is_generic_hotel_request_name(name):
        return None
    return name or None


def _extract_excluded_hotel_names(user_message: str) -> list[str]:
    excluded: list[str] = []
    for marker in ("말고", "대신"):
        if marker not in user_message:
            continue
        before = user_message.split(marker, 1)[0].strip()
        name = re.sub(r"^(숙소|호텔)\s*", "", before).strip()
        name = re.sub(r"\s+(숙소|호텔)$", "", name).strip()
        if name:
            excluded.append(name)
    return _dedupe(excluded)


def _filter_excluded_hotels(hotels: list[dict], excluded_hotels: list[str]) -> list[dict]:
    excluded = [_normalize_name(name) for name in excluded_hotels if name]
    if not excluded:
        return hotels
    filtered = []
    for hotel in hotels:
        candidate = _normalize_name(str(hotel.get("name") or ""))
        if any(name and (name in candidate or candidate in name) for name in excluded):
            continue
        filtered.append(hotel)
    return filtered


def _select_matching_hotel(hotels: list[dict], requested_hotel: str) -> dict | None:
    requested = _normalize_name(requested_hotel)
    for hotel in hotels:
        candidate = _normalize_name(str(hotel.get("name") or ""))
        if requested and (requested in candidate or candidate in requested):
            return hotel
    return None


def _format_hotel_candidates(hotels: list[dict], limit: int = 3) -> str:
    names = [str(hotel.get("name") or "숙소") for hotel in hotels[:limit]]
    return ", ".join(_dedupe(names)) if names else "대체 숙소"


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", "", name).lower()


def _confirmation_result(message: str) -> OrchestratorResult:
    return OrchestratorResult(message=message)


async def _search_hotels_with_city_fallbacks(itinerary: dict, city: str, check_in: str, check_out: str) -> dict:
    last_result: dict = {"status": "error", "data": []}
    city_name = await _normalize_city_for_search(city)
    for city_name in _dedupe([city_name, city]):
        # Booking 2단계(search_destination→search_hotels) + 평면 정규화 — 파이프라인 _fetch_hotels 재사용
        result = await _fetch_hotels(
            city_name, check_in, check_out,
            itinerary.get("adult_count") or 1,
            itinerary.get("child_count") or 0,
            itinerary.get("child_ages") or [],
        )
        last_result = result
        if result.get("status") == "success" and result.get("data"):
            return result
    return last_result


async def _normalize_city_for_search(city: str | None) -> str:
    raw = (city or "").strip()
    if not raw:
        return raw
    try:
        return await _extract_english_city(raw)
    except Exception:
        return raw


def _dedupe(values: list[str | None]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _patch_hotel_locally(
    deps: Any,
    user_message: str,
    day_plans: dict[str, list[dict]],
    date_key: str,
    index: int,
    item: dict,
    check_in: str,
    check_out: str,
    requested_hotel: str | None = None,
) -> OrchestratorResult:
    patched = dict(item)
    cheap_requested = any(word in user_message for word in ("저렴", "싼", "가성비", "저가"))
    if requested_hotel:
        patched["plan_name"] = f"{requested_hotel} 체크인"
        patched["place"] = requested_hotel
        patched["cost"] = None
        patched["note"] = f"{check_in} ~ {check_out} 숙박. 실시간 후보에서 정확히 일치하는 숙소를 찾지 못해 요청한 숙소명으로 일정만 반영했습니다."
        preferences = _merge_accommodation_preference(deps.preferences, requested_hotel)
        summary_line = f"{date_key} 숙소를 {requested_hotel}(으)로 변경"
        message = f"{date_key} 숙소 항목을 {requested_hotel}(으)로 변경했습니다."
        hotel_name = requested_hotel
        hotel_place = requested_hotel
    elif cheap_requested:
        hotel_name = "저렴한 숙소"
        hotel_place = patched.get("place") or hotel_name
        patched["plan_name"] = f"{hotel_name} 체크인"
        patched["note"] = _append_note(
            patched.get("note") or "",
            f"{check_in} ~ {check_out} 숙박. 실시간 숙소 후보를 조회할 수 없어 저렴한 숙소 선호만 우선 반영했습니다.",
        )
        preferences = _merge_accommodation_preference(deps.preferences, hotel_name)
        summary_line = f"{date_key} 숙소를 저렴한 숙소 선호로 변경"
        message = f"{date_key} 숙소 항목에 저렴한 숙소 선호를 반영했습니다."
    else:
        hotel_name = str(patched.get("place") or "숙소")
        hotel_place = patched.get("place") or hotel_name
        patched["note"] = _append_note(
            patched.get("note") or "",
            "실시간 숙소 후보를 조회할 수 없어 기존 숙소를 유지했습니다.",
        )
        preferences = deps.preferences
        summary_line = f"{date_key} 숙소 변경 요청 확인"
        message = f"{date_key} 숙소 후보를 조회할 수 없어 기존 숙소 일정을 유지했습니다."

    day_plans[date_key][index] = patched
    _sync_hotel_related_items(day_plans, index, check_in, check_out, hotel_name, hotel_place, _hotel_aliases_from_item(item))
    return OrchestratorResult(
        message=message,
        ai_summary=_append_summary(deps.ai_summary, summary_line),
        preferences=preferences,
        day_plans=_to_day_plan_items(day_plans),
    )


def _sync_hotel_related_items(
    day_plans: dict[str, list[dict]],
    hotel_index: int,
    check_in: str,
    check_out: str,
    hotel_name: str,
    hotel_place: str | None,
    old_hotel_aliases: list[str],
) -> None:
    for date_key, items in day_plans.items():
        if not (check_in <= date_key <= check_out):
            continue
        index_to_skip = hotel_index if date_key == check_in else -1
        _sync_hotel_related_transfers(items, index_to_skip, hotel_name, hotel_place, old_hotel_aliases)
        _sync_hotel_mentions(items, index_to_skip, hotel_name, hotel_place, old_hotel_aliases)


def _sync_hotel_related_transfers(
    items: list[dict],
    hotel_index: int,
    hotel_name: str,
    hotel_place: str | None,
    old_hotel_aliases: list[str],
) -> None:
    for index, item in enumerate(items):
        if index == hotel_index:
            continue
        text = " ".join(str(item.get(key) or "") for key in ("plan_name", "place", "note"))
        if "이동" not in text:
            continue
        if not any(term in text for term in ("숙소", "호텔", "체크인")):
            continue
        if not any(term in text for term in ("공항", "역", "터미널")):
            continue
        if not _is_inbound_hotel_transfer(item, old_hotel_aliases):
            continue

        patched = dict(item)
        patched["plan_name"] = _replace_transfer_hotel_destination(
            patched.get("plan_name") or "숙소 이동",
            hotel_name,
        )
        patched["place"] = hotel_place or hotel_name
        patched["note"] = _replace_hotel_mentions_in_note(patched.get("note") or "", hotel_name)
        items[index] = patched


def _sync_hotel_mentions(
    items: list[dict],
    hotel_index: int,
    hotel_name: str,
    hotel_place: str | None,
    old_hotel_aliases: list[str],
) -> None:
    for index, item in enumerate(items):
        if index == hotel_index:
            continue
        text = " ".join(str(item.get(key) or "") for key in ("plan_name", "place", "note"))
        if not any(alias and alias in text for alias in old_hotel_aliases):
            continue

        patched = dict(item)
        for key in ("plan_name", "note"):
            patched[key] = _replace_hotel_aliases(str(patched.get(key) or ""), hotel_name, old_hotel_aliases)
        if any(alias and alias in str(patched.get("place") or "") for alias in old_hotel_aliases):
            patched["place"] = hotel_place or hotel_name
        items[index] = patched


def _replace_hotel_aliases(text: str, hotel_name: str, old_hotel_aliases: list[str]) -> str:
    result = text
    for alias in sorted(old_hotel_aliases, key=len, reverse=True):
        result = result.replace(alias, hotel_name)
    return result


def _is_inbound_hotel_transfer(item: dict, old_hotel_aliases: list[str]) -> bool:
    text = str(item.get("plan_name") or "")
    if "→" not in text:
        return False

    without_suffix = re.sub(r"\([^)]*\)", "", text)
    left, right = without_suffix.split("→", 1)
    origin = left.strip()
    destination = right.strip()

    if any(term in origin for term in ("숙소", "호텔", "체크인")):
        return False
    if not any(term in origin for term in ("공항", "역", "터미널")):
        return False
    return (
        any(term in destination for term in ("숙소", "호텔", "체크인"))
        or any(alias and alias in destination for alias in old_hotel_aliases)
    )


def _hotel_aliases_from_item(item: dict) -> list[str]:
    aliases = [
        str(item.get("place") or "").strip(),
        re.sub(r"\s*체크인\s*$", "", str(item.get("plan_name") or "")).strip(),
    ]
    return _dedupe([alias for alias in aliases if alias])


def _replace_transfer_hotel_destination(text: str, hotel_name: str) -> str:
    suffix = ""
    match = re.search(r"\s*(\([^)]*\))\s*$", text)
    if match:
        suffix = f" {match.group(1)}"
        text = text[:match.start()].rstrip()

    if "→" in text:
        origin = text.split("→", 1)[0].strip()
        return f"{origin} → {hotel_name} 이동{suffix}"

    return re.sub(r"(숙소|호텔)(?=\s*이동)", hotel_name, text) + suffix


def _replace_hotel_mentions_in_note(note: str, hotel_name: str) -> str:
    if not note:
        return note
    return re.sub(r"(숙소|호텔|[^,\s]+호텔|신라스테이\s*제주)", hotel_name, note)


def _unavailable_patch_result(
    deps: Any,
    day_plans: dict[str, list[dict]],
    date_key: str,
    message: str,
) -> OrchestratorResult:
    return OrchestratorResult(
        message=message,
        ai_summary=None,
        preferences=None,
        day_plans={date_key: _to_items(day_plans[date_key])},
    )


def _append_note(existing: str, addition: str) -> str:
    if not existing:
        return addition
    if addition in existing:
        return existing
    return f"{existing.rstrip()} {addition}"


def _find_stay_for_date(itinerary: dict, date_key: str) -> tuple[str, str, str] | None:
    destinations = itinerary.get("destinations") or []
    for dest in destinations:
        start = dest.get("start_date", "")[:10]
        end = dest.get("end_date", "")[:10]
        if start <= date_key <= end:
            return dest.get("city"), start, end
    if itinerary.get("destination") and itinerary.get("start_date") and itinerary.get("end_date"):
        return itinerary["destination"], itinerary["start_date"][:10], itinerary["end_date"][:10]
    return None


def _cost_from_amount(amount: Any, currency: str | None, amount_krw: Any = None) -> ItemCost | None:
    if amount is None or not currency:
        return None
    return ItemCost(
        amount=float(amount),
        currency=currency,
        amount_krw=int(amount_krw) if amount_krw and currency != "KRW" else None,
    )


def _append_summary(existing: str | list[str] | None, line: str) -> str:
    if isinstance(existing, list):
        existing_text = "\n".join(str(item) for item in existing)
    else:
        existing_text = existing or ""
    numbers = [int(match) for match in re.findall(r"(?m)^(\d+)\.", existing_text)]
    next_no = max(numbers, default=0) + 1
    addition = f"{next_no}. {line}"
    return f"{existing_text.rstrip()}\n{addition}".strip()


def _merge_transport_preference(preferences: dict | None, transport: str) -> dict:
    merged = dict(preferences or {})
    merged["transport"] = transport
    return merged


def _merge_accommodation_preference(preferences: dict | None, accommodation: str) -> dict:
    merged = dict(preferences or {})
    existing = merged.get("accommodation")
    if existing and accommodation not in str(existing):
        merged["accommodation"] = f"{existing}, {accommodation}"
    else:
        merged["accommodation"] = existing or accommodation
    return merged


def _extract_time_of_day_preference(user_message: str) -> str | None:
    if "오전" in user_message:
        return "morning"
    if "오후" in user_message:
        return "afternoon"
    if any(word in user_message for word in ("저녁", "야간", "밤")):
        return "evening"
    return None


def _filter_offers_by_time_of_day(offers: list[dict], preference: str) -> list[dict]:
    filtered = []
    for offer in offers:
        departing_at = str(offer.get("departing_at") or "")
        if len(departing_at) < 16:
            continue
        try:
            hour = int(departing_at[11:13])
        except ValueError:
            continue
        if preference == "morning" and hour < 12:
            filtered.append(offer)
        elif preference == "afternoon" and 12 <= hour < 18:
            filtered.append(offer)
        elif preference == "evening" and hour >= 18:
            filtered.append(offer)
    return filtered


def _cleanup_post_arrival_orphans(
    items: list[dict],
    destination_iata: str,
    new_depart_dt: datetime,
) -> list[dict]:
    """출발 전 시간에 도착지 공항을 언급하는 항목 제거 (이전 도착 시각 기준으로 생성된 고아 항목)."""
    iata_upper = destination_iata.upper()
    depart_time_str = new_depart_dt.strftime("%H:%M")
    result = []
    for item in items:
        text = " ".join(str(item.get(key) or "") for key in ("plan_name", "place", "note"))
        if iata_upper not in text:
            result.append(item)
            continue
        parsed = _parse_time_range(str(item.get("time") or ""))
        if parsed and parsed[0] < depart_time_str:
            print(
                f"[itinerary_patch] 고아 도착후 항목 제거: {item.get('plan_name')!r} at {parsed[0]}",
                flush=True,
            )
            continue
        result.append(item)
    return result


def _merge_food_preference(preferences: dict | None, food: str) -> dict:
    merged = dict(preferences or {})
    existing = merged.get("food")
    if isinstance(existing, list):
        if food not in existing:
            merged["food"] = [*existing, food]
        return merged
    if existing:
        merged["food"] = existing if food in str(existing) else f"{existing}, {food}"
    else:
        merged["food"] = food
    return merged
