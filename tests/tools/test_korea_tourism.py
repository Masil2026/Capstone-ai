"""
한국관광공사 TourAPI (KorService2) 어댑터 테스트.
- 무료 / 일 1000건 쿼터라 실제 API 호출 통합 테스트 (weather/tavily 패턴)
- 검증·에러 케이스는 네트워크 불필요 (HTTP 호출 전 반환)
- 레퍼런스: docs/external-api/korea_tourism_api_reference.md
"""
import json
import pytest

from app.services.adapters.korea_tourism_api import KoreaTourismAdapter
from app.services.travel_agent_service import TravelAgentService


def _service():
    return TravelAgentService({"korea_tourism": KoreaTourismAdapter()})


def _print(name, result):
    print(f"\n[{name}] status={result['status']}")
    if result["status"] == "success":
        data = result["data"]
        print(f"  count={data['count']} / total={data['total_count']}")
        for item in data["items"][:3]:
            print(f"  - {item.get('title')} ({item.get('mapx')}, {item.get('mapy')})")
    else:
        print(f"  message={result.get('message')}")


# ─────────────────────────── 실제 호출 통합 테스트 ─────────────────────────── #
@pytest.mark.asyncio
async def test_korea_tourism_search_keyword():
    """키워드 검색 — '시장' 검색 결과 구조 확인"""
    service = _service()
    result = await service.process_task(
        "korea_tourism", "search_keyword", {"keyword": "시장", "numOfRows": 5}
    )
    _print("search_keyword", result)

    assert result["status"] == "success"
    assert isinstance(result["data"]["items"], list)
    if result["data"]["items"]:
        item = result["data"]["items"][0]
        assert item["contentid"]
        assert item["title"]
        # 좌표가 있으면 float 으로 변환돼 있어야 함
        assert item["mapx"] is None or isinstance(item["mapx"], float)


@pytest.mark.asyncio
async def test_korea_tourism_search_festival():
    """행사 조회 — 2026년 행사 목록 + 행사 고유 필드 확인"""
    service = _service()
    result = await service.process_task(
        "korea_tourism", "search_festival",
        {"eventStartDate": "20260101", "eventEndDate": "20261231", "numOfRows": 5},
    )
    _print("search_festival", result)

    assert result["status"] == "success"
    assert isinstance(result["data"]["items"], list)
    if result["data"]["items"]:
        item = result["data"]["items"][0]
        assert item["contentid"]
        # 행사 오퍼레이션 고유 필드 키가 정제 결과에 포함돼야 함
        assert "eventstartdate" in item
        assert "eventenddate" in item


@pytest.mark.asyncio
async def test_korea_tourism_area_based_list():
    """지역기반 조회 — 관광지(12) 목록 구조 확인"""
    service = _service()
    result = await service.process_task(
        "korea_tourism", "area_based_list", {"contentTypeId": 12, "numOfRows": 5}
    )
    _print("area_based_list", result)

    assert result["status"] == "success"
    assert isinstance(result["data"]["items"], list)
    if result["data"]["items"]:
        assert result["data"]["items"][0]["contentid"]


# ─────────────────────────── 검증 / 에러 (네트워크 불필요) ─────────────────────────── #
@pytest.mark.asyncio
async def test_korea_tourism_search_keyword_missing_keyword():
    """keyword 누락 검증"""
    service = _service()
    result = await service.process_task("korea_tourism", "search_keyword", {})
    assert result["status"] == "error"
    assert result["message"] == "keyword는 필수입니다."


@pytest.mark.asyncio
async def test_korea_tourism_search_festival_missing_date():
    """eventStartDate 누락 검증"""
    service = _service()
    result = await service.process_task("korea_tourism", "search_festival", {})
    assert result["status"] == "error"
    assert "eventStartDate는 필수입니다." in result["message"]


@pytest.mark.asyncio
async def test_korea_tourism_invalid_action():
    """지원하지 않는 액션"""
    service = _service()
    result = await service.process_task("korea_tourism", "invalid", {})
    assert result["status"] == "error"
    assert "지원하지 않는 액션" in result["message"]


@pytest.mark.asyncio
async def test_korea_tourism_missing_api_key():
    """API 키 미설정 시 에러 (네트워크 호출 전 반환)"""
    adapter = KoreaTourismAdapter()
    adapter.api_key = ""
    service = TravelAgentService({"korea_tourism": adapter})
    result = await service.process_task("korea_tourism", "search_keyword", {"keyword": "시장"})
    assert result["status"] == "error"
    assert "KOREA_TOURISM_API_KEY" in result["message"]


# ─────────────────────── 시도 매핑 (resolve_regn_code) ─────────────────────── #
# 네트워크 불필요 — 정적 매핑 단위 테스트

from app.services.adapters.korea_tourism_api import resolve_regn_code, resolve_addr_prefix


def test_resolve_regn_code_basic():
    """도시명 정규화 후 시도 법정동 코드 매핑."""
    assert resolve_regn_code("서울특별시") == "11"
    assert resolve_regn_code("부산") == "26"
    assert resolve_regn_code("제주도") == "50"
    assert resolve_regn_code("제주특별자치도") == "50"


def test_resolve_regn_code_new_special_provinces():
    """강원=51 / 전북=52 신규 특별자치도 코드 (구코드 42·45 아님)."""
    assert resolve_regn_code("강원") == "51"
    assert resolve_regn_code("강원특별자치도") == "51"
    assert resolve_regn_code("전북") == "52"
    assert resolve_regn_code("전라북도") == "52"


def test_resolve_regn_code_major_tourist_cities():
    """대표 관광도시(시군구)도 시도로 매핑돼 게이팅 히트율을 높인다."""
    assert resolve_regn_code("강릉") == "51"   # 강원
    assert resolve_regn_code("경주") == "47"   # 경북
    assert resolve_regn_code("전주") == "52"   # 전북
    assert resolve_regn_code("서귀포시") == "50"  # 제주


def test_resolve_regn_code_normalization():
    """앞 토큰만 취해 정규화 — '제주도, Jeju' 같은 혼합 입력도 처리."""
    assert resolve_regn_code("제주도, Jeju") == "50"
    assert resolve_regn_code(" 부산광역시 ") == "26"


def test_resolve_regn_code_overseas_returns_none():
    """해외·미매핑 도시는 None → TourAPI 호출 스킵(게이팅)."""
    assert resolve_regn_code("Tokyo") is None
    assert resolve_regn_code("도쿄") is None
    assert resolve_regn_code("Paris") is None
    assert resolve_regn_code(None) is None
    assert resolve_regn_code("") is None


def test_resolve_addr_prefix():
    """addr1 후처리 필터용 시도 접두사."""
    assert resolve_addr_prefix("제주도") == "제주"
    assert resolve_addr_prefix("강원특별자치도") == "강원"
    assert resolve_addr_prefix("경주") == "경상북도"
    assert resolve_addr_prefix("Tokyo") is None
