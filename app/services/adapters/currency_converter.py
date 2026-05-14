"""
환율 변환 유틸리티 — 모든 통화를 KRW로 변환.

실시간 환율: Frankfurter API (https://api.frankfurter.app) — 무료, API 키 불필요, ECB 기준.
캐시 TTL 6시간. API 실패 시 fallback 환율로 대체.
"""
import httpx
from datetime import datetime, timedelta

# {통화코드: (KRW 환율, 캐시 시각)}
_cache: dict[str, tuple[float, datetime]] = {}
_CACHE_TTL = timedelta(hours=6)

_FALLBACK: dict[str, float] = {
    # 기준 통화
    "KRW": 1.0,
    # 주요 기축통화
    "USD": 1380.0,
    "EUR": 1510.0,
    "GBP": 1760.0,
    "JPY": 9.3,
    "CHF": 1540.0,
    # 아시아
    "CNY": 190.0,
    "HKD": 177.0,
    "SGD": 1025.0,
    "TWD": 43.0,
    "MYR": 310.0,
    "THB": 38.5,
    "VND": 0.054,
    "IDR": 0.086,
    "PHP": 24.0,
    "INR": 16.5,
    "BDT": 11.5,
    "LKR": 4.6,
    "NPR": 10.3,
    "MMK": 0.65,
    "KHR": 0.34,
    "LAK": 0.065,
    # 오세아니아
    "AUD": 880.0,
    "NZD": 820.0,
    # 아메리카
    "CAD": 1010.0,
    "MXN": 72.0,
    "BRL": 248.0,
    "ARS": 1.4,
    "CLP": 1.5,
    "COP": 0.34,
    "PEN": 368.0,
    # 유럽
    "NOK": 128.0,
    "SEK": 130.0,
    "DKK": 202.0,
    "PLN": 348.0,
    "CZK": 62.0,
    "HUF": 3.8,
    "RON": 303.0,
    "HRK": 200.0,
    "TRY": 40.0,
    # 중동
    "AED": 376.0,
    "SAR": 368.0,
    "QAR": 379.0,
    "KWD": 4490.0,
    "BHD": 3660.0,
    "ILS": 376.0,
    # 아프리카
    "ZAR": 76.0,
    "EGP": 28.0,
    "MAD": 138.0,
    "KES": 10.7,
}


async def to_krw(amount: float | str, currency: str) -> int:
    """amount를 KRW 정수로 변환. 실패 시 0 반환."""
    try:
        numeric = float(amount)
    except (ValueError, TypeError):
        return 0

    currency = currency.upper().strip()
    if currency == "KRW":
        return round(numeric)

    rate = await _get_rate(currency)
    return round(numeric * rate)


async def _get_rate(currency: str) -> float:
    if currency in _cache:
        rate, fetched_at = _cache[currency]
        if datetime.now() - fetched_at < _CACHE_TTL:
            return rate

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.frankfurter.app/latest",
                params={"from": currency, "to": "KRW"},
            )
            if resp.status_code == 200:
                rate = float(resp.json()["rates"]["KRW"])
                _cache[currency] = (rate, datetime.now())
                print(f"[CurrencyConverter] {currency}→KRW 환율: {rate} (Frankfurter)")
                return rate
    except Exception as e:
        print(f"[CurrencyConverter] 환율 API 실패 ({currency}): {e} → fallback 사용")

    return _FALLBACK.get(currency, 1380.0)
