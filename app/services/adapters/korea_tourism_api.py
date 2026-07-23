import re

import httpx

from typing import Any, Dict, List, Optional, Tuple
from app.core.ApiToolsInterfaces import ApiTools
from app.core.config import settings


# 시도(17개) 법정동 코드 매핑.
# (lDongRegnCd, addr1 접두사, 도시명 별칭들) — 별칭엔 대표 관광도시(시군구)도 포함해 게이팅 히트율을 높인다.
# ⚠️ 강원=51 / 전북=52 는 특별자치도 신규 코드(구코드 42·45는 0건). addr1 접두사는 startswith 매칭용.
_SIDO_TABLE: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("11", "서울",     ("서울", "서울특별시", "서울시")),
    ("26", "부산",     ("부산", "부산광역시", "부산시")),
    ("27", "대구",     ("대구", "대구광역시")),
    ("28", "인천",     ("인천", "인천광역시")),
    ("29", "광주",     ("광주", "광주광역시")),           # 광주=광역시(전남 아님)
    ("30", "대전",     ("대전", "대전광역시")),
    ("31", "울산",     ("울산", "울산광역시")),
    ("36", "세종",     ("세종", "세종특별자치시", "세종시")),
    ("41", "경기",     ("경기", "경기도", "수원", "수원시", "가평", "파주", "용인", "고양", "성남")),
    ("51", "강원",     ("강원", "강원도", "강원특별자치도", "강릉", "속초", "춘천", "원주", "평창", "정선")),
    ("43", "충청북도", ("충북", "충청북도", "청주", "청주시", "제천", "단양")),
    ("44", "충청남도", ("충남", "충청남도", "천안", "보령", "공주", "부여")),
    ("52", "전북",     ("전북", "전라북도", "전북특별자치도", "전주", "전주시", "군산", "남원")),
    ("46", "전라남도", ("전남", "전라남도", "여수", "순천", "목포", "담양")),
    ("47", "경상북도", ("경북", "경상북도", "경주", "경주시", "안동", "포항")),
    ("48", "경상남도", ("경남", "경상남도", "통영", "거제", "창원", "진주", "남해")),
    ("50", "제주",     ("제주", "제주도", "제주특별자치도", "제주시", "서귀포", "서귀포시")),
)

# 별칭 → 코드 / 코드 → addr1 접두사 (모듈 로드 시 1회 구성)
_ALIAS_TO_CODE: Dict[str, str] = {
    alias: code for code, _prefix, aliases in _SIDO_TABLE for alias in aliases
}
_CODE_TO_PREFIX: Dict[str, str] = {code: prefix for code, prefix, _aliases in _SIDO_TABLE}


def _normalize_city(city: Optional[str]) -> str:
    """도시명 정규화 — 앞 토큰만 취해 공백 제거. 예) '제주도, Jeju' → '제주도'."""
    if not city:
        return ""
    head = re.split(r"[,(（/]", city)[0]
    return re.sub(r"\s+", "", head)


def resolve_regn_code(city: Optional[str]) -> Optional[str]:
    """도시명 → 시도 법정동 코드(lDongRegnCd). 매핑에 없으면 None (해외·미매핑 → 스킵)."""
    return _ALIAS_TO_CODE.get(_normalize_city(city))


def resolve_addr_prefix(city: Optional[str]) -> Optional[str]:
    """도시명 → addr1 후처리 필터용 시도 접두사. 예) '제주도' → '제주'."""
    code = resolve_regn_code(city)
    return _CODE_TO_PREFIX.get(code) if code else None


class KoreaTourismAdapter(ApiTools):
    """
    한국관광공사 TourAPI (KorService2) 어댑터.
    레퍼런스: docs/external-api/korea_tourism_api_reference.md

    - searchKeyword2   : 키워드 검색 조회 (action: search_keyword)
    - searchFestival2  : 행사정보 조회   (action: search_festival)
    - areaBasedList2   : 지역기반 관광정보 조회 (action: area_based_list)
    """

    BASE_URL = "https://apis.data.go.kr/B551011/KorService2"

    # 응답 item에서 공통으로 뽑아 쓰는 필드 (모든 오퍼레이션 공유)
    _COMMON_FIELDS = (
        "contentid", "contenttypeid", "title",
        "addr1", "addr2", "zipcode", "tel",
        "firstimage", "firstimage2",
        "mlevel", "createdtime", "modifiedtime",
        "lDongRegnCd", "lDongSignguCd",
        "lclsSystm1", "lclsSystm2", "lclsSystm3",
        "cpyrhtDivCd",
    )

    def __init__(self):
        self.api_key = (settings.KOREA_TOURISM_API_KEY or "").strip()

    @property
    def tool_name(self) -> str:
        return "korea_tourism"

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.api_key:
            return {"status": "error", "message": "KOREA_TOURISM_API_KEY가 설정되지 않았습니다."}

        if action == "search_keyword":
            return await self._search_keyword(params)
        if action == "search_festival":
            return await self._search_festival(params)
        if action == "area_based_list":
            return await self._area_based_list(params)

        return {"status": "error", "message": f"지원하지 않는 액션: {action}"}

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    async def _search_keyword(self, params: Dict[str, Any]) -> Dict[str, Any]:
        keyword = params.get("keyword")
        if not keyword:
            return {"status": "error", "message": "keyword는 필수입니다."}

        query = self._common_params(params)
        query["keyword"] = keyword
        self._apply_optional(query, params, ("contentTypeId", "lDongRegnCd", "lDongSignguCd"))

        return await self._call("searchKeyword2", query)

    async def _search_festival(self, params: Dict[str, Any]) -> Dict[str, Any]:
        event_start = params.get("eventStartDate")
        if not event_start:
            return {"status": "error", "message": "eventStartDate는 필수입니다. (형식: YYYYMMDD)"}

        query = self._common_params(params)
        query["eventStartDate"] = event_start
        self._apply_optional(query, params, ("eventEndDate", "lDongRegnCd", "lDongSignguCd"))

        return await self._call("searchFestival2", query, festival=True)

    async def _area_based_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        query = self._common_params(params)
        self._apply_optional(query, params, ("contentTypeId", "lDongRegnCd", "lDongSignguCd"))

        return await self._call("areaBasedList2", query)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _common_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """모든 오퍼레이션이 공유하는 요청 파라미터 구성."""
        return {
            "serviceKey": self.api_key,
            "MobileOS": "ETC",
            "MobileApp": params.get("MobileApp", "Masil"),
            "_type": "json",
            "numOfRows": params.get("numOfRows", 10),
            "pageNo": params.get("pageNo", 1),
            "arrange": params.get("arrange", "C"),  # C=수정일순(최신순)
        }

    @staticmethod
    def _apply_optional(query: Dict[str, Any], params: Dict[str, Any], keys) -> None:
        for key in keys:
            if params.get(key) is not None:
                query[key] = params[key]

    async def _call(self, operation: str, query: Dict[str, Any], festival: bool = False) -> Dict[str, Any]:
        url = f"{self.BASE_URL}/{operation}"

        debug_params = {**query, "serviceKey": "***REDACTED***"}
        print(f"\n[KoreaTourismAdapter] {operation} 요청 전송")
        print(f"URL: {url}")
        print(f"Params: {debug_params}")

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.get(url, params=query)
            except httpx.TimeoutException:
                return {"status": "error", "message": f"한국관광공사 API 타임아웃 (15초 초과): {operation}"}
            except httpx.RequestError as e:
                return {"status": "error", "message": f"한국관광공사 API 요청 실패: {str(e)}"}

        print(f"[KoreaTourismAdapter] HTTP Status: {response.status_code}")

        if response.status_code != 200:
            return {"status": "error", "message": f"HTTP 오류: {response.status_code} - {response.text[:200]}"}

        try:
            data = response.json()
        except Exception:
            # 인증 실패/쿼터 초과 등은 XML 에러로 내려오는 경우가 많음
            return {
                "status": "error",
                "message": f"API 응답이 JSON 형식이 아닙니다 (인증/쿼터 확인): {response.text[:200]}",
            }

        header = data.get("response", {}).get("header", {})
        result_code = header.get("resultCode")
        if result_code != "0000":
            return {
                "status": "error",
                "message": f"API 오류 [{result_code}]: {header.get('resultMsg')}",
            }

        body = data.get("response", {}).get("body", {}) or {}
        items = self._extract_items(body)
        results = [self._simplify(it, festival=festival) for it in items]

        print(f"[KoreaTourismAdapter] 결과 개수: {len(results)} / totalCount: {body.get('totalCount')}")

        return {
            "status": "success",
            "data": {
                "type": f"한국관광공사 {operation} 데이터",
                "count": len(results),
                "total_count": self._to_int(body.get("totalCount")),
                "page_no": self._to_int(body.get("pageNo")),
                "num_of_rows": self._to_int(body.get("numOfRows")),
                "items": results,
            },
        }

    @staticmethod
    def _extract_items(body: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        response.body.items.item[] 추출.
        - 결과 0건이면 items가 "" (빈 문자열)로 올 수 있음
        - 결과 1건이면 item이 list가 아닌 단일 dict로 올 수 있음 (XML→JSON 변환 특성)
        """
        items = body.get("items")
        if not items or not isinstance(items, dict):
            return []
        item = items.get("item")
        if not item:
            return []
        if isinstance(item, dict):
            return [item]
        return item

    def _simplify(self, item: Dict[str, Any], festival: bool = False) -> Dict[str, Any]:
        result: Dict[str, Any] = {field: item.get(field) for field in self._COMMON_FIELDS}
        result["mapx"] = self._to_float(item.get("mapx"))  # 경도 (WGS84)
        result["mapy"] = self._to_float(item.get("mapy"))  # 위도 (WGS84)
        if festival:
            result["eventstartdate"] = item.get("eventstartdate")
            result["eventenddate"] = item.get("eventenddate")
            result["progresstype"] = item.get("progresstype")
            result["festivaltype"] = item.get("festivaltype")
        return result

    @staticmethod
    def _to_float(value: Any):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
