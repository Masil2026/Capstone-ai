# tests/eval/golden.py
"""AI 성능 평가 골든 데이터셋.

L1: 요청 타입 라우팅 (classification + _correct_request_type) — 24건
L2: change payload 추출 (change_extractor_agent) — 6건
L3: 전체 일정 파이프라인 (run_itinerary_pipeline) — 3건 (pro 호출 비용 때문에 최소화)
"""

# 라우팅·추출 평가에 공통으로 쓰는 기존 여행 컨텍스트
SAMPLE_ITINERARY = {
    "destinations": [{"city": "도쿄", "start_date": "2026-08-15", "end_date": "2026-08-18"}],
    "start_date": "2026-08-15",
    "end_date": "2026-08-18",
    "total_days": 4,
    "budget": 1500000.0,
    "adult_count": 2,
    "child_count": 0,
    "child_ages": [],
    "origin": None,
    "day_plans": {
        "2026-08-15": [{"plan_name": "ICN → NRT 항공 이동 (대한항공)", "time": "09:00 ~ 11:30", "place": "ICN → NRT", "note": ""}],
        "2026-08-16": [{"plan_name": "아사쿠사 관광", "time": "10:00 ~ 13:00", "place": "아사쿠사", "note": ""}],
        "2026-08-17": [{"plan_name": "신주쿠 쇼핑", "time": "10:00 ~ 13:00", "place": "신주쿠", "note": ""}],
        "2026-08-18": [{"plan_name": "NRT → ICN 귀국 항공 (아시아나)", "time": "18:00 ~ 20:30", "place": "NRT → ICN", "note": ""}],
    },
}

# ── L1: 라우팅 골든셋 ─────────────────────────────────────────────────────────
# (message, expected_type, has_itinerary)

ROUTING_CASES = [
    # itinerary — 일정 생성/내용 수정
    ("도쿄 3박 4일 일정 짜줘", "itinerary", False),
    ("2일차 일정 다시 짜줘", "itinerary", True),
    ("경복궁 대신 창덕궁으로 바꿔줘", "itinerary", True),
    ("맛집 위주로 코스 짜줘", "itinerary", True),
    ("호텔 바꿔줘", "itinerary", True),
    ("항공편 바꿔줘", "itinerary", True),
    ("인천공항 갈 때 택시 말고 버스 타고 갈래", "itinerary", True),
    ("3일차에 시장 방문 일정 추가해줘", "itinerary", True),
    ("저녁은 해산물 식당으로 바꿔줘", "itinerary", True),
    # change — 여행 기본 정보 변경
    ("여행 날짜 5월 3일부터 7일로 바꿔줘", "change", True),
    ("출발 날짜를 7월 20일로 바꿔줘", "change", True),
    ("도착일을 8월 5일로 수정해줘", "change", True),
    ("여행 일정을 하루 늘려줘", "change", True),
    ("예산 100만원으로 늘려줘", "change", True),
    ("성인 2명으로 변경해줘", "change", True),
    ("아이 한 명 추가할게. 나이는 7살이야", "change", True),
    ("여행지를 파리로 바꿔줘", "change", True),
    ("2박 3일로 바꿔줘", "change", True),
    # chat — 단순 질문
    ("오사카 날씨 어때?", "chat", True),
    ("여행 날짜가 언제야?", "chat", True),
    ("도쿄 라멘 맛집 알려줘", "chat", True),
    # reservation / cancel
    ("이 호텔 예약해줘", "reservation", True),
    ("대한항공 KE705편으로 예약해줘", "reservation", True),
    ("호텔 예약 취소해줘", "cancel", True),
]

# ── L2: change 추출 골든셋 ────────────────────────────────────────────────────
# (user_message, ai_message, expected_fields) — expected_fields는 부분 일치 검사

CHANGE_EXTRACTION_CASES = [
    (
        "출발 날짜를 8월 16일로 바꿔줘",
        "여행 기간을 2026년 8월 16일부터 19일까지로 변경했습니다.",
        {"start_date": "2026-08-16", "end_date": "2026-08-19"},
    ),
    (
        "여행 날짜를 8월 20일부터 23일로 바꿔줘",
        "여행 날짜를 2026년 8월 20일부터 23일까지로 변경했습니다.",
        {"start_date": "2026-08-20", "end_date": "2026-08-23"},
    ),
    (
        "예산 200만원으로 올려줘",
        "여행 예산을 200만원으로 변경했습니다.",
        {"budget": 2000000},
    ),
    (
        "성인 3명으로 바꿔줘",
        "성인 인원을 3명으로 변경했습니다.",
        {"adult_count": 3},
    ),
    (
        "아이 한 명 추가할게. 6살이야",
        "어린이 1명(6세)을 추가해 인원을 변경했습니다.",
        {"child_count": 1, "child_ages": [6]},
    ),
    (
        "여행지를 오사카로 바꿔줘",
        "여행지를 도쿄에서 오사카로 변경했습니다. 기간은 기존과 동일합니다.",
        {"destinations": [{"city": "오사카"}]},  # city만 비교
    ),
]

# ── L3: 파이프라인 골든 시나리오 ──────────────────────────────────────────────

PIPELINE_SCENARIOS = [
    {
        "name": "국내 신규 (부산 2박3일)",
        "user_message": "부산 2박 3일 여행 일정 짜줘",
        "itinerary": {
            "destinations": [{"city": "부산", "start_date": "2026-09-10", "end_date": "2026-09-12"}],
            "start_date": "2026-09-10",
            "end_date": "2026-09-12",
            "total_days": 3,
            "budget": None,
            "adult_count": 2,
            "child_count": 0,
            "child_ages": [],
            "origin": None,
            "day_plans": None,
        },
        "expected_dates": ["2026-09-10", "2026-09-11", "2026-09-12"],
    },
    {
        "name": "해외 신규 (도쿄 3박4일, 아이 동반, 예산)",
        "user_message": "도쿄 3박 4일 가족 여행 일정 짜줘. 아이랑 같이 가.",
        "itinerary": {
            "destinations": [{"city": "도쿄", "start_date": "2026-09-15", "end_date": "2026-09-18"}],
            "start_date": "2026-09-15",
            "end_date": "2026-09-18",
            "total_days": 4,
            "budget": 3000000.0,
            "adult_count": 2,
            "child_count": 1,
            "child_ages": [6],
            "origin": None,
            "day_plans": None,
        },
        "expected_dates": ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"],
    },
    {
        "name": "날짜 변경 재계획 (도쿄 8/15~18 → 8/16~19)",
        "user_message": "변경된 날짜에 맞게 일정 다시 짜줘",
        "itinerary": {
            **SAMPLE_ITINERARY,
            "destinations": [{"city": "도쿄", "start_date": "2026-08-16", "end_date": "2026-08-19"}],
            "start_date": "2026-08-16",
            "end_date": "2026-08-19",
        },
        # 재계획: 반환 키는 새 범위 안에만 있어야 하고, 새 마지막 날은 반드시 포함
        "expected_dates": None,
        "expected_within": ("2026-08-16", "2026-08-19"),
        "expected_contains": ["2026-08-19"],
    },
]
