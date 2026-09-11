"""
한국관광공사 TourAPI 파이프라인 주입 검증 (이슈 #23).
- 국내/해외 게이팅, 축제 addr1 후처리 필터, 프롬프트 섹션 포맷
- 순수 함수 위주 — 네트워크 불필요 (해외 게이팅은 resolve None에서 단락)
"""
import pytest

from app.services.agents import itinerary_pipeline as p


# ─────────────────────────── 축제 후처리 필터 ─────────────────────────── #

_JEJU_FESTIVAL = {
    "title": "서귀포 여름축제",
    "addr1": "제주특별자치도 서귀포시 중정로 22",
    "eventstartdate": "20260720",
    "eventenddate": "20260725",
}


def test_festival_matches_overlap_and_region():
    """여행 기간과 겹치고 목적지 시도(addr1 접두사)에 속하면 True."""
    assert p._festival_matches(_JEJU_FESTIVAL, "20260722", "20260724", ("제주",)) is True


def test_festival_matches_out_of_period():
    """여행 기간과 안 겹치면 False."""
    assert p._festival_matches(_JEJU_FESTIVAL, "20260801", "20260803", ("제주",)) is False


def test_festival_matches_wrong_region():
    """목적지 시도가 다르면 False."""
    assert p._festival_matches(_JEJU_FESTIVAL, "20260722", "20260724", ("서울",)) is False


def test_festival_matches_dummy_admin_region():
    """표준 외 더미 행정구역('전남광주통합특별시')은 '광주' 접두사로 걸리지 않는다.

    lDongRegnCd 숫자 매칭 대신 addr1 문자열 접두사(startswith) 필터를 쓰는 이유.
    """
    dummy = {
        "title": "더미축제",
        "addr1": "전남광주통합특별시 어딘가",
        "eventstartdate": "20260722",
        "eventenddate": "20260723",
    }
    assert p._festival_matches(dummy, "20260722", "20260724", ("광주",)) is False


def test_festival_matches_missing_start_date():
    """시작일 없으면 False."""
    assert p._festival_matches({"addr1": "제주특별자치도"}, "20260722", "20260724", ("제주",)) is False


# ─────────────────────────── 국내/해외 게이팅 ─────────────────────────── #

@pytest.mark.asyncio
async def test_fetch_korea_attractions_overseas_skipped():
    """해외 목적지는 TourAPI 호출 없이 skipped (resolve None에서 단락 → 네트워크 미발생)."""
    out = await p._fetch_korea_attractions([{"city": "Tokyo"}, {"city": "Paris"}])
    assert out["Tokyo"]["status"] == "skipped"
    assert out["Paris"]["status"] == "skipped"


@pytest.mark.asyncio
async def test_fetch_korea_festivals_overseas_empty():
    """해외만 있으면 축제 조회 없이 빈 리스트."""
    out = await p._fetch_korea_festivals([{"city": "Tokyo"}], ["2026-07-22", "2026-07-24"])
    assert out == []


# ─────────────────────────── 프롬프트 섹션 포맷 ─────────────────────────── #

def test_attractions_section_domestic():
    """국내 후보가 있으면 '## 후보 관광지' 섹션 생성."""
    korea = {"제주도": {"status": "success", "data": {"items": [
        {"title": "비자림", "addr1": "제주특별자치도 제주시"},
    ]}}}
    sec = "\n".join(p._korea_attractions_section(korea))
    assert "## 후보 관광지" in sec
    assert "비자림" in sec
    assert "제주특별자치도 제주시" in sec


def test_attractions_section_empty_for_skipped():
    """스킵(해외)뿐이면 빈 섹션."""
    assert p._korea_attractions_section({"Tokyo": {"status": "skipped"}}) == []


def test_festivals_section_format():
    """'## 기간 내 축제' 섹션에 제목·주소·기간(YYYY-MM-DD)이 포함."""
    sec = "\n".join(p._korea_festivals_section([_JEJU_FESTIVAL]))
    assert "## 기간 내 축제" in sec
    assert "서귀포 여름축제" in sec
    assert "2026-07-20 ~ 2026-07-25" in sec


def test_festivals_section_empty():
    assert p._korea_festivals_section([]) == []


def test_fmt_yyyymmdd():
    assert p._fmt_yyyymmdd("20260720") == "2026-07-20"
    assert p._fmt_yyyymmdd("") == ""
    assert p._fmt_yyyymmdd(None) == ""
    assert p._fmt_yyyymmdd("2026") == "2026"  # 형식 아니면 원본


# ─────────────────── Phase 3 축제 키워드 헛호출 차단 ─────────────────── #

def test_korea_keyword_skips_festivals():
    """축제류 검색어는 searchKeyword2 대상에서 제외(Phase 1에서 이미 받음 → quota 절약)."""
    assert p._korea_keyword("2026 서귀포 원도심 문화페스티벌 제주 (Festival)", "제주도") is None
    assert p._korea_keyword("제주비엔날레 제주 (Biennale)", "제주도") is None


def test_korea_keyword_keeps_real_attraction():
    """일반 관광지는 그대로 키워드 추출(스킵되지 않음)."""
    assert p._korea_keyword("성산일출봉 제주 (Seongsan Ilchulbong)", "제주도") == "성산일출봉"


# ─────────────────── 도시명 제거 실패로 전량 0건 (이슈 #25) ─────────────────── #

@pytest.mark.parametrize(
    "city_kr",
    ["부산광역시, 대한민국", "부산 (Busan)", "부산광역시", "부산시", "부산"],
)
def test_korea_keyword_strips_autocomplete_city(city_kr):
    """city_kr이 Place Autocomplete 원본 형태여도 도시명을 제거한다.

    원본을 그대로 replace하면 '광안대교 야경 부산'처럼 도시명이 남아
    searchKeyword2가 전부 0건이 됐다(#25).
    """
    query = "광안대교 야경 부산 (Gwangandaegyo Bridge Night View Busan)"
    assert p._korea_keyword(query, city_kr) == "광안대교 야경"


def test_korea_keyword_fallbacks_order():
    """0건 완화 후보는 공백 제거 → 마지막 어절 제거 순."""
    assert p._korea_keyword_fallbacks("광안대교 야경") == ["광안대교야경", "광안대교"]
    assert p._korea_keyword_fallbacks("송도 해상케이블카") == ["송도해상케이블카"]


def test_korea_keyword_fallbacks_drops_generic_place():
    """어절을 떼어 일반 지명만 남는 후보는 버린다 (#23 오매칭 재발 방지).

    '해운대 해수욕장' → '해운대', '기장 칠암 붕장어마을' → '기장' 은 후보에서 제외.
    """
    assert p._korea_keyword_fallbacks("해운대 해수욕장") == ["해운대해수욕장"]
    assert "기장" not in p._korea_keyword_fallbacks("기장 칠암 붕장어마을")


def test_korea_keyword_fallbacks_single_token_empty():
    """어절이 하나면 완화할 여지가 없어 빈 리스트."""
    assert p._korea_keyword_fallbacks("동백섬") == []


def test_korea_has_items_distinguishes_empty_success():
    """0건 응답도 status=success라 items 유무로 판정해야 한다."""
    assert p._korea_has_items({"status": "success", "data": {"items": [{"title": "x"}]}}) is True
    assert p._korea_has_items({"status": "success", "data": {"items": [], "total_count": 0}}) is False
    assert p._korea_has_items({"status": "error"}) is False
    assert p._korea_has_items({"status": "skip"}) is False


@pytest.mark.asyncio
async def test_korea_cached_does_not_store_empty(monkeypatch):
    """0건은 캐싱하지 않는다 — 캐싱하면 TTL 24h 동안 빈 결과가 재사용돼 수정 검증이 막힌다(#25)."""
    stored: dict = {}

    class _FakeRedis:
        async def get(self, key):
            return stored.get(key)

        async def set(self, key, value, ex=None):
            stored[key] = value

    class _FakeService:
        def __init__(self, result):
            self.result = result

        async def process_task(self, *args, **kwargs):
            return self.result

    empty = {"status": "success", "data": {"items": [], "total_count": 0}}
    monkeypatch.setattr(p, "_redis", _FakeRedis())
    monkeypatch.setattr(p, "_service", _FakeService(empty))

    assert await p._korea_cached("search_keyword", {"keyword": "없는곳"}, "keyword:없는곳") == empty
    assert stored == {}, "0건이 캐싱되면 안 된다"

    filled = {"status": "success", "data": {"items": [{"title": "성산일출봉"}]}}
    monkeypatch.setattr(p, "_service", _FakeService(filled))
    await p._korea_cached("search_keyword", {"keyword": "성산일출봉"}, "keyword:성산일출봉")
    assert "tourapi:keyword:성산일출봉" in stored, "항목이 있으면 캐싱돼야 한다"


# ─────────────────── image_url 오매칭(일반 지명 오염) 방지 ─────────────────── #

@pytest.mark.asyncio
async def test_attach_media_no_generic_place_bleed():
    """항목의 일반 지명(place='성산')이 관광지명(성산일출봉) 이미지를 끌어오지 않는다.

    반면 실제 관광지 항목(place='성산일출봉')에는 정상적으로 이미지가 붙는다.
    과거 'np in key' 부분문자열 매칭이 호텔·식당 등 무관 항목에 이미지를 오배치하던 버그 회귀 방지.
    """
    place_results = {
        "성산일출봉 제주 (Seongsan)": {
            "status": "success",
            "image_url": "IMG_SEONGSAN",
            "data": {"places": [{"name": "성산일출봉"}]},
        },
    }
    day_plans = {
        "2026-09-01": [
            {"plan_name": "호텔 체크인 및 휴식", "place": "성산", "note": ""},      # 일반 지명 → 이미지 X
            {"plan_name": "성산일출봉 탐방", "place": "성산일출봉", "note": ""},      # 정확 → 이미지 O
            {"plan_name": "관람", "place": "제주특별자치도 서귀포시 성산일출봉로 284", "note": ""},  # 주소 폴백 → O
            {"plan_name": "성산일출봉 → 우도 이동 (배)", "place": "우도", "note": ""},  # 이동: 도착지(우도) 이미지 없음 → X
            {"plan_name": "숙소 → 성산일출봉 이동 (택시)", "place": "제주특별자치도 서귀포시 성산읍", "note": ""},  # 이동: 도착지 이미지 O
        ]
    }
    out = await p._attach_media(
        day_plans, p.PlannerOutput(days=[]), place_results,
        flight_legs=[], hotels_by_city={}, adults=2, child_ages=[],
    )
    items = out["2026-09-01"]
    assert items[0].get("image_url") is None            # 호텔 항목엔 오염 없음
    assert items[1].get("image_url") == "IMG_SEONGSAN"  # 관광지 항목엔 정상 부착
    assert items[2].get("image_url") == "IMG_SEONGSAN"  # 주소에 관광지명 포함 → 폴백 정상 동작
    assert items[3].get("image_url") is None            # 이동 항목: 출발지(성산일출봉) 이미지 오부착 안 함
    assert items[4].get("image_url") == "IMG_SEONGSAN"  # 이동 항목: 도착지(성산일출봉) 이미지는 정상 부착
