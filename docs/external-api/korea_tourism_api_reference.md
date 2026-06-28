# 한국관광공사 API (TourAPI · `KorService2`) — API 레퍼런스

> 어댑터 구현 전 채워넣는 작업용 노트. 각 엔드포인트마다 **Code Snippet(요청) / Query Params / Example Response / Results(정제 결과)** 를 채운다.
> Booking 문서(`booking_api_reference.md`)와 동일 형식.
>
> - **Base URL:** `https://apis.data.go.kr/B551011/KorService2`
> - **settings 변수명:** `KOREA_TOURISM_API_KEY`
> - **키 표기 규칙:** code snippet의 `serviceKey` 값은 적지 않는다. 위치만 `<SERVICE_KEY>` 로 명시.
> - **응답 형식:** `_type=json` 명시 (기본은 XML일 수 있음 → 반드시 json 지정)
> - **출처:** 한국관광공사 TourAPI `KorService2` 오퍼레이션 명세서(최신본). 본 문서는 매뉴얼 기재 항목을 그대로 반영.

## 사용 엔드포인트

| NO | 엔드포인트 | 설명 | 일일 트래픽 |
|---|---|---|---|
| 2 | `searchKeyword2` | 키워드로 검색, 타입별/전체 목록 조회 | 1000 |
| 3 | `searchFestival2` | 행사정보 목록 조회 (컨텐츠타입 '행사/공연/축제'일 때만 유효, 날짜 기반) | 1000 |
| 13 | `areaBasedList2` | 지역기반 관광정보 조회 (제목/수정일/등록일순 정렬) | 1000 |

> ⚠️ **일일 트래픽 각 1000건.** 어댑터에서 호출 수 관리 유의 (캐싱 고려).

> 📌 **지역/분류 코드 체계 변경 (중요):** 최신 `KorService2` 매뉴얼은 구버전의 `areaCode`/`sigunguCode`/`cat1~3` 대신
> **법정동 코드(`lDongRegnCd`/`lDongSignguCd`)** 와 **분류체계 코드(`lclsSystm1~3`)** 를 사용한다.
> 실제 코드값은 매뉴얼의 "법정동 코드조회" / "분류체계 코드조회" 오퍼레이션으로 받아야 한다.
> 예) 시도 `26`=부산, `50`=제주, `11`=서울 / 분류 `NA`=자연, `SH`=쇼핑, `EV`=행사 (대분류 예시).

---

## 공통 파라미터 (3개 엔드포인트 공통)

> TourAPI는 대부분 엔드포인트가 아래 공통 파라미터를 공유한다. 각 엔드포인트 표에는 **고유 파라미터만** 따로 표기.
> 항목구분: 매뉴얼 기준 필수(1)=✅, 옵션(0)=optional.

| 파라미터 | 필수 | 어댑터 사용 | 값 / 기본 | 비고 |
|---|---|---|---|---|
| serviceKey | ✅ | ✅ | `<SERVICE_KEY>` | `settings.KOREA_TOURISM_API_KEY`. URL 인코딩 주의 |
| MobileOS | ✅ | ✅ | `ETC` | IOS / AND / WEB / ETC |
| MobileApp | ✅ | ✅ | 앱명 (예: `Masil`) | 서비스명=어플명 |
| _type | optional | ✅ | **`json` 고정** | 미지정시 XML로 올 수 있음 |
| numOfRows | optional | ✅ | 기본 10 | 한 페이지 결과 수 |
| pageNo | optional | ✅ | 기본 1 | 현재 페이지 번호 |
| arrange | optional | △ | 정렬 | A=제목순 / C=수정일순 / D=생성일순 / 대표이미지 必 정렬은 O=제목순 / Q=수정일순 / R=생성일순 |
| lDongRegnCd | optional | △ | 법정동 시도 코드 | 법정동 코드조회 참고 |
| lDongSignguCd | optional | △ | 법정동 시군구 코드 | `lDongRegnCd` 필수 동반 |
| lclsSystm1 | optional | ❌ | 분류체계 대분류 | 분류체계 코드조회 참고 |
| lclsSystm2 | optional | ❌ | 분류체계 중분류 | `lclsSystm1` 필수 동반 |
| lclsSystm3 | optional | ❌ | 분류체계 소분류 | `lclsSystm1`, `lclsSystm2` 필수 동반 |

> 응답 공통 구조: `response.header.{resultCode, resultMsg}` + `response.body.{items.item[], numOfRows, pageNo, totalCount}`.
> `resultCode="0000"` / `resultMsg="OK"` 가 정상.

---

# 1. 키워드 검색 조회 (`searchKeyword2`)
**용도:** 키워드로 전체/타입별 관광정보 목록 검색. 제목순/수정일순(최신순)/등록일순 정렬 지원.
**Endpoint:** `https://apis.data.go.kr/B551011/KorService2/searchKeyword2`

### Query Params (고유 — 공통 외)
| 파라미터 | 필수 | 어댑터 사용 | 값 / 기본 | 비고 |
|---|---|---|---|---|
| keyword | ✅ | ✅ | 사용자 입력(검색어) | 한글 가능, **URL 인코딩 필요** |
| contentTypeId | optional | △ | 타입 필터 | 12=관광지/14=문화시설/15=축제공연행사/25=여행코스/28=레포츠/32=숙박/38=쇼핑/39=음식점. *매뉴얼 요청표엔 미기재이나 관례상 지원* |
| areaCode | optional | △ | 지역 필터 | *구버전 파라미터. 최신 매뉴얼은 `lDongRegnCd` 권장* |

### Code Snippet (요청)
```
GET https://apis.data.go.kr/B551011/KorService2/searchKeyword2
  ?serviceKey=<SERVICE_KEY>
  &numOfRows=10
  &pageNo=1
  &MobileOS=ETC
  &MobileApp=AppTest
  &_type=json
  &arrange=C
  &keyword=시장          # URL 인코딩 필요 (예: %EC%8B%9C%EC%9E%A5)
  &lDongRegnCd=50
  &lDongSignguCd=130
  &lclsSystm1=SH
  &lclsSystm2=SH06
  &lclsSystm3=SH060100
```

### Example Response
```json
{
  "response": {
    "header": { "resultCode": "0000", "resultMsg": "OK" },
    "body": {
      "items": {
        "item": [
          {
            "addr1": "제주특별자치도 서귀포시 천제연로188번길 12",
            "addr2": "",
            "zipcode": "63546",
            "contentid": "132595",
            "contenttypeid": "38",
            "createdtime": "20040213090000",
            "firstimage": "http://tong.visitkorea.or.kr/cms/resource/50/3477250_image2_1.jpg",
            "firstimage2": "http://tong.visitkorea.or.kr/cms/resource/50/3477250_image3_1.jpg",
            "cpyrhtDivCd": "Type3",
            "mapx": "126.4242345901",
            "mapy": "33.2507951855",
            "mlevel": "6",
            "modifiedtime": "20250414130425",
            "tel": "064-760-2633",
            "title": "중문향토오일시장",
            "lDongRegnCd": "50",
            "lDongSignguCd": "130",
            "lclsSystm1": "SH",
            "lclsSystm2": "SH06",
            "lclsSystm3": "SH060100"
          }
        ]
      },
      "numOfRows": 5,
      "pageNo": 1,
      "totalCount": 5
    }
  }
}
```

### Results (어댑터가 뽑아 쓸 필드)
| 필드 | 응답 경로 | 비고 |
|---|---|---|
| 콘텐츠 ID | `response.body.items.item[].contentid` | 상세조회 키 |
| 관광타입 ID | `...item[].contenttypeid` | 12~39 |
| 제목 | `...item[].title` | |
| 주소 | `...item[].addr1` (+ `addr2`) | |
| 우편번호 | `...item[].zipcode` | |
| 전화번호 | `...item[].tel` | optional |
| 대표이미지(원본/썸네일) | `...item[].firstimage` / `firstimage2` | 없을 수 있음 |
| 좌표 | `...item[].mapx`(경도) / `mapy`(위도) | WGS84 |
| 수정일 | `...item[].modifiedtime` | YYYYMMDDhhmmss |
| 페이지 메타 | `response.body.{numOfRows, pageNo, totalCount}` | 페이지네이션 |

---

# 2. 행사정보 조회 (`searchFestival2`)
**용도:** 행사/공연/축제 목록을 날짜 기반으로 조회. 콘텐츠타입이 "행사/공연/축제(15)"인 경우만 유효.
**Endpoint:** `https://apis.data.go.kr/B551011/KorService2/searchFestival2`

### Query Params (고유 — 공통 외)
| 파라미터 | 필수 | 어댑터 사용 | 값 / 기본 | 비고 |
|---|---|---|---|---|
| eventStartDate | ✅ | ✅ | `yyyyMMdd` (예: `20260101`) | 이 날짜 이후 시작 행사 |
| eventEndDate | optional | ✅ | `yyyyMMdd` (예: `20261231`) | 이 날짜 이전 종료 행사 |
| modifiedtime | optional | ❌ | `yyyyMMdd` | 콘텐츠 변경일자 필터 |

### Code Snippet (요청)
```
GET https://apis.data.go.kr/B551011/KorService2/searchFestival2
  ?serviceKey=<SERVICE_KEY>
  &numOfRows=10
  &pageNo=1
  &MobileOS=ETC
  &MobileApp=AppTest
  &_type=json
  &arrange=C
  &eventStartDate=20260101
  &eventEndDate=20261231
```

### Example Response
```json
{
  "response": {
    "header": { "resultCode": "0000", "resultMsg": "OK" },
    "body": {
      "items": {
        "item": [
          {
            "addr1": "서울특별시 양천구 남부순환로64길 2 (신월동)",
            "addr2": "서울문화예술교육센터 양천",
            "zipcode": "07916",
            "cat1": "", "cat2": "", "cat3": "",
            "contentid": "3310483",
            "contenttypeid": "15",
            "createdtime": "20240614132716",
            "eventstartdate": "20260502",
            "eventenddate": "20260503",
            "firstimage": "https://tong.visitkorea.or.kr/cms/resource/18/4012118_image2_1.jpg",
            "firstimage2": "https://tong.visitkorea.or.kr/cms/resource/18/4012118_image3_1.jpg",
            "cpyrhtDivCd": "Type3",
            "mapx": "126.83214572562274",
            "mapy": "37.52822008764788",
            "mlevel": "6",
            "modifiedtime": "20260205165336",
            "areacode": "", "sigungucode": "",
            "tel": "02-2697-0014",
            "title": "어린이·가족 예술축제",
            "lDongRegnCd": "11",
            "lDongSignguCd": "470",
            "lclsSystm1": "EV",
            "lclsSystm2": "EV01",
            "lclsSystm3": "EV010200",
            "progresstype": "선택안함",
            "festivaltype": ""
          }
        ]
      },
      "numOfRows": 1,
      "pageNo": 1,
      "totalCount": 1
    }
  }
}
```

### Results (어댑터가 뽑아 쓸 필드)
| 필드 | 응답 경로 | 비고 |
|---|---|---|
| 콘텐츠 ID | `...item[].contentid` | |
| 제목 | `...item[].title` | |
| **행사 시작/종료일** | `...item[].eventstartdate` / `eventenddate` | YYYYMMDD (행사 고유) |
| 진행상태 | `...item[].progresstype` | 행사 고유 |
| 축제유형 | `...item[].festivaltype` | 행사 고유, 빈값 가능 |
| 주소 | `...item[].addr1` (+ `addr2`) | |
| 좌표 | `...item[].mapx` / `mapy` | WGS84 |
| 대표이미지 | `...item[].firstimage` / `firstimage2` | |
| 전화번호 | `...item[].tel` | |
| 수정일 | `...item[].modifiedtime` | |
| 페이지 메타 | `response.body.{numOfRows, pageNo, totalCount}` | |

> ⚠️ 요청은 `eventStartDate`(카멜), 응답은 `eventstartdate`(소문자). 어댑터에서 키 케이스 혼동 주의.
> 응답에 구버전 `cat1~3`, `areacode`, `sigungucode` 필드가 빈 문자열로 함께 내려올 수 있음.

---

# 3. 지역기반 관광정보 조회 (`areaBasedList2`)
**용도:** 지역 및 시군구 기반 관광정보 목록. 제목순/수정일순(최신순)/등록일순 정렬 지원.
**Endpoint:** `https://apis.data.go.kr/B551011/KorService2/areaBasedList2`

### Query Params (고유 — 공통 외)
| 파라미터 | 필수 | 어댑터 사용 | 값 / 기본 | 비고 |
|---|---|---|---|---|
| contentTypeId | optional | △ | 타입 필터 | 12=관광지/14=문화시설/15=축제공연행사/25=여행코스/28=레포츠/32=숙박/38=쇼핑/39=음식점 |
| modifiedtime | optional | ❌ | `yyyyMMdd` | 콘텐츠 수정일 필터 |

> 지역 필터는 **공통 파라미터의 `lDongRegnCd`/`lDongSignguCd`** 사용 (이 엔드포인트의 핵심 필터).
> 분류 필터는 `lclsSystm1~3` 사용. 구버전 `areaCode`/`sigunguCode`/`cat1~3`는 최신 매뉴얼에서 대체됨.

### Code Snippet (요청)
```
GET https://apis.data.go.kr/B551011/KorService2/areaBasedList2
  ?serviceKey=<SERVICE_KEY>
  &numOfRows=10
  &pageNo=1
  &MobileOS=ETC
  &MobileApp=AppTest
  &_type=json
  &arrange=C
  &contentTypeId=12
  &lDongRegnCd=26
  &lDongSignguCd=380
  &lclsSystm1=NA
  &lclsSystm2=NA04
  &lclsSystm3=NA040500
```

### Example Response
```json
{
  "response": {
    "header": { "resultCode": "0000", "resultMsg": "OK" },
    "body": {
      "items": {
        "item": [
          {
            "addr1": "부산광역시 사하구 낙동남로 1240 (하단동)",
            "addr2": "",
            "contentid": "127974",
            "contenttypeid": "12",
            "createdtime": "20031208090000",
            "firstimage": "http://tong.visitkorea.or.kr/cms/resource/21/3497121_image2_1.jpg",
            "firstimage2": "http://tong.visitkorea.or.kr/cms/resource/21/3497121_image3_1.jpg",
            "cpyrhtDivCd": "Type1",
            "mapx": "128.9460030322",
            "mapy": "35.1045320626",
            "mlevel": "6",
            "modifiedtime": "20250618095454",
            "tel": "",
            "title": "을숙도 공원",
            "zipcode": "49435",
            "lDongRegnCd": "26",
            "lDongSignguCd": "380",
            "lclsSystm1": "NA",
            "lclsSystm2": "NA04",
            "lclsSystm3": "NA040500"
          }
        ]
      },
      "numOfRows": 3,
      "pageNo": 1,
      "totalCount": 3
    }
  }
}
```

### Results (어댑터가 뽑아 쓸 필드)
| 필드 | 응답 경로 | 비고 |
|---|---|---|
| 콘텐츠 ID | `...item[].contentid` | |
| 관광타입 ID | `...item[].contenttypeid` | |
| 제목 | `...item[].title` | |
| 주소 | `...item[].addr1` (+ `addr2`) | |
| 우편번호 | `...item[].zipcode` | |
| 전화번호 | `...item[].tel` | 빈값 가능 |
| 대표이미지 | `...item[].firstimage` / `firstimage2` | |
| 좌표 | `...item[].mapx` / `mapy` | WGS84 |
| 분류체계 | `...item[].lclsSystm1~3` | |
| 법정동 코드 | `...item[].lDongRegnCd` / `lDongSignguCd` | |
| 수정일 | `...item[].modifiedtime` | |
| 페이지 메타 | `response.body.{numOfRows, pageNo, totalCount}` | |

---

## 결과 / 메모

- **응답 공통 구조:** `response.header.{resultCode, resultMsg}` + `response.body.{items.item[], numOfRows, pageNo, totalCount}`.
  - 정상: `resultCode="0000"`, `resultMsg="OK"`.
  - 결과 0건일 때 `items`가 빈 문자열(`""`)로 올 수 있으니 `items.item` 접근 전 방어 코드 필요.
  - 결과 1건일 때 `item`이 배열이 아니라 단일 객체로 올 수 있음 (XML→JSON 변환 특성). 어댑터에서 list 정규화 권장.
- **좌표:** `mapx`=경도(longitude), `mapy`=위도(latitude), WGS84. 문자열로 내려오므로 float 변환 필요.
- **날짜 포맷:** `createdtime`/`modifiedtime`은 `YYYYMMDDhhmmss`, 행사 `eventstartdate`/`eventenddate`는 `YYYYMMDD`.
- **이미지:** `firstimage`(원본 ~500×333) / `firstimage2`(썸네일 ~150×100). 없는 콘텐츠 다수 → 빈 문자열 가능. 대표이미지 필수 결과만 원하면 `arrange=O/Q/R` 사용.
- **저작권(`cpyrhtDivCd`):** Type1=제1유형(출처표시), Type3=제3유형(출처표시+변경금지).
- **키 케이스 주의:** 요청은 `contentTypeId`/`eventStartDate`(카멜), 응답은 `contenttypeid`/`eventstartdate`(소문자).
- **코드 체계:** 지역=법정동 코드(`lDong*`), 분류=분류체계 코드(`lclsSystm*`). 실제 값은 별도 "법정동 코드조회"/"분류체계 코드조회" 오퍼레이션으로 매핑 필요 (현재 사용 엔드포인트 3개에는 미포함).
