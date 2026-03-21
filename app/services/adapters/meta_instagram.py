import httpx

from app.core.ApiToolsInterfaces import ApiTools
from typing import Any, Dict

# ─────────────────────────────────────────
# Mock 데이터
# USE_MOCK = True 상태에서 사용
# 실제 API 연동 시 USE_MOCK = False로 변경
# ─────────────────────────────────────────
USE_MOCK = True

GRAPH_API_VERSION = "v19.0"
GRAPH_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

MOCK_POSTS = {
    "오사카": [
        {
            "id": "mock_001",
            "permalink": "https://www.instagram.com/p/mock001/",
            "caption": "오사카 도톤보리 야경 진짜 너무 예쁘다🌙 글리코상도 보고 타코야키도 먹고 완벽한 하루 #오사카 #도톤보리 #일본여행",
            "media_type": "IMAGE",
            "like_count": 342,
            "timestamp": "2025-03-10T18:30:00+0000",
        },
        {
            "id": "mock_002",
            "permalink": "https://www.instagram.com/p/mock002/",
            "caption": "오사카성 벚꽃 시즌 완전 인생샷🌸 4월 초가 딱 피크예요! #오사카성 #벚꽃 #오사카여행",
            "media_type": "IMAGE",
            "like_count": 521,
            "timestamp": "2025-04-02T10:15:00+0000",
        },
        {
            "id": "mock_003",
            "permalink": "https://www.instagram.com/p/mock003/",
            "caption": "USJ 해리포터 구역 드디어 왔다✨ 웨이팅 2시간이지만 그만한 가치 있음 #유니버셜스튜디오 #오사카",
            "media_type": "IMAGE",
            "like_count": 897,
            "timestamp": "2025-03-25T14:00:00+0000",
        },
    ],
    "도쿄": [
        {
            "id": "mock_004",
            "permalink": "https://www.instagram.com/p/mock004/",
            "caption": "시부야 스크램블 교차로 한복판에서🗼 역시 도쿄 에너지가 다름 #도쿄 #시부야 #일본여행",
            "media_type": "IMAGE",
            "like_count": 654,
            "timestamp": "2025-03-15T20:00:00+0000",
        },
        {
            "id": "mock_005",
            "permalink": "https://www.instagram.com/p/mock005/",
            "caption": "아사쿠사 센소지 새벽 6시에 갔더니 사람 없었음👘 이른 아침 강추! #아사쿠사 #도쿄여행",
            "media_type": "IMAGE",
            "like_count": 413,
            "timestamp": "2025-03-20T06:30:00+0000",
        },
    ],
    "교토": [
        {
            "id": "mock_006",
            "permalink": "https://www.instagram.com/p/mock006/",
            "caption": "후시미이나리 천 개의 도리이 새벽에 혼자 걸었는데 진짜 감동🦊 #교토 #후시미이나리 #일본여행",
            "media_type": "IMAGE",
            "like_count": 1023,
            "timestamp": "2025-03-18T05:30:00+0000",
        },
    ],
}

class MetaGraphAdapter(ApiTools):
    @property
    def tool_name(self) -> str:
        return "meta_graph"

    async def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        # TODO: Meta for Developers에서 Access Token 발급 및 인증 로직 구현
        # 예시: params = {"user_id": "12345", "fields": "id,caption,media_url"}
        # 1. 해시태그로 여행지 인스타 게시물 검색 (search_by_hashtag)
        if action == "search_by_hashtag":
            query = params.get("query")
            max_results = int(params.get("max_results", 10))
            min_likes = int(params.get("min_likes", 0))  # 인기도 기반 필터링용

            if not query:
                return {"status": "error", "message": "query는 필수입니다."}

            # Mock 모드
            if USE_MOCK:
                results = []
                for keyword, posts in MOCK_POSTS.items():
                    if keyword in query:
                        results.extend(posts)

                # 매칭 없으면 전체 반환 (데모용 fallback)
                if not results:
                    for posts in MOCK_POSTS.values():
                        results.extend(posts)

                # 인기도 필터링 (min_likes 이상만)
                results = [p for p in results if p.get("like_count", 0) >= min_likes]

                # 최신순 정렬 (최신 트렌드 파악)
                results = sorted(results, key=lambda p: p.get("timestamp", ""), reverse=True)

                results = results[:max_results]

                return {
                    "status": "success",
                    "count": len(results),
                    "data": self._format_posts(results),
                }

            # 실제 Meta Graph API 호출
            # TODO: .env에 INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_BUSINESS_ID 설정 후 USE_MOCK = False
            import os
            access_token = os.getenv("INSTAGRAM_ACCESS_TOKEN")
            business_id = os.getenv("INSTAGRAM_BUSINESS_ID")

            if not access_token or not business_id:
                return {"status": "error", "message": ".env에 INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_BUSINESS_ID를 설정해주세요."}

            hashtag = query.replace(" ", "")  # 해시태그는 공백 없음

            async with httpx.AsyncClient(timeout=10.0) as client:

                # Step 1: 해시태그 ID 조회
                try:
                    hashtag_response = await client.get(
                        f"{GRAPH_BASE_URL}/ig_hashtag_search",
                        params={
                            "user_id": business_id,
                            "q": hashtag,
                            "access_token": access_token,
                        },
                    )
                except httpx.TimeoutException:
                    return {"status": "error", "message": "Meta Graph API 타임아웃 (10초 초과)"}

                try:
                    hashtag_data = hashtag_response.json()
                except Exception:
                    return {"status": "error", "message": f"Meta Graph API 응답이 JSON 형식이 아닙니다: {hashtag_response.text[:100]}"}

                if hashtag_response.status_code != 200:
                    return {"status": "error", "message": f"Meta Graph API 오류: {hashtag_response.status_code}"}

                ids = hashtag_data.get("data", [])
                if not ids:
                    return {"status": "error", "message": f"해시태그 '{hashtag}'를 찾을 수 없습니다."}

                hashtag_id = ids[0]["id"]

                # Step 2: 인기 게시물 조회
                try:
                    media_response = await client.get(
                        f"{GRAPH_BASE_URL}/{hashtag_id}/top_media",
                        params={
                            "user_id": business_id,
                            "fields": "id,permalink,caption,media_type,like_count,timestamp",
                            "access_token": access_token,
                            "limit": max_results,
                        },
                    )
                except httpx.TimeoutException:
                    return {"status": "error", "message": "Media API 타임아웃 (10초 초과)"}

                try:
                    media_data = media_response.json()
                except Exception:
                    return {"status": "error", "message": f"Media API 응답이 JSON 형식이 아닙니다: {media_response.text[:100]}"}

                if media_response.status_code != 200:
                    return {"status": "error", "message": f"Media API 오류: {media_response.status_code}"}

                posts = media_data.get("data", [])

                # 인기도 필터링
                posts = [p for p in posts if p.get("like_count", 0) >= min_likes]

                # 최신순 정렬
                posts = sorted(posts, key=lambda p: p.get("timestamp", ""), reverse=True)

                return {
                    "status": "success",
                    "count": len(posts),
                    "data": self._format_posts(posts),
                }

        if action == "get_user_posts":
            # user_id = params.get("user_id")
            # fields = params.get("fields", "id,caption")
            # url = f"https://graph.instagram.com/{user_id}/media?fields={fields}&access_token={self.access_token}"
            return {"status": "success", "data": "인스타그램 포스트 데이터 (Mock)"}
            
        return {"status": "error", "message": f"지원하지 않는 액션: {action}"}

    def _format_posts(self, posts: list) -> list:
        """
        AI에게 필요한 필드만 정제해서 반환.
        - caption   : 여행지 분위기 파악 + 장소 추천 근거
        - like_count: 인기도 판단
        - timestamp : 최신 트렌드 파악 (최신순 정렬 후 전달)
        - permalink : 출처 링크
        """
        return [
            {
                "caption": post.get("caption", ""),
                "like_count": post.get("like_count", 0),
                "timestamp": post.get("timestamp", "")[:10],  # YYYY-MM-DD 까지만
                "permalink": post.get("permalink", ""),
            }
            for post in posts
        ]