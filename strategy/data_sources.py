from typing import Optional, Dict, Any, List
import numpy as np
import pandas as pd



class SapienceClient:
    """
    Sapience/외부 예측시장 가격 client.

    v9:
    - placeholder가 아니라 실제 HTTP 호출 구조를 넣었다.
    - 단, Sapience endpoint는 배포/버전마다 다를 수 있으므로 .env에서 URL template를 지정한다.
    - direct trading은 하지 않고 price/reference probability만 읽는다.

    설정 예:
    ENABLE_SAPIENCE_PRICE=true
    SAPIENCE_BASE_URL=https://api.sapience.xyz
    SAPIENCE_PRICE_ENDPOINT=/markets/{event_id}
    SAPIENCE_API_KEY=
    """

    def __init__(self, settings=None):
        import os
        self.enabled = _env_bool("ENABLE_SAPIENCE_PRICE", False)
        self.base_url = os.getenv("SAPIENCE_BASE_URL", "").rstrip("/")
        self.endpoint = os.getenv("SAPIENCE_PRICE_ENDPOINT", "/markets/{event_id}")
        self.api_key = os.getenv("SAPIENCE_API_KEY", "")
        self.timeout = float(os.getenv("SAPIENCE_TIMEOUT_SECONDS", "10"))
        self.settings = settings

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        return headers

    def _extract_price(self, data: Any) -> Optional[float]:
        """
        여러 prediction market API 응답 형식을 방어적으로 파싱한다.
        0~1 범위 가격/확률만 인정.
        """
        if data is None:
            return None

        if isinstance(data, (int, float)):
            v = float(data)
            return v if 0.0 <= v <= 1.0 else None

        if isinstance(data, list):
            for item in data:
                v = self._extract_price(item)
                if v is not None:
                    return v
            return None

        if not isinstance(data, dict):
            return None

        # 직접 필드
        keys = [
            "yes_price", "yesPrice", "yes_probability", "yesProbability",
            "probability", "price", "midPrice", "lastPrice", "markPrice",
        ]
        for key in keys:
            if key in data:
                try:
                    v = float(data[key])
                    if v > 1.0 and v <= 100.0:
                        v = v / 100.0
                    if 0.0 <= v <= 1.0:
                        return v
                except Exception:
                    pass

        # outcomePrices / prices
        for key in ["outcomePrices", "outcome_prices", "prices", "tokens"]:
            value = data.get(key)
            if isinstance(value, list) and value:
                for item in value:
                    if isinstance(item, dict):
                        label = str(item.get("outcome") or item.get("name") or item.get("side") or "").lower()
                        if label in {"yes", "y", "true", "up"}:
                            v = self._extract_price(item)
                            if v is not None:
                                return v
                    else:
                        try:
                            v = float(item)
                            if 0.0 <= v <= 1.0:
                                return v
                        except Exception:
                            pass

        # orderbook best bid/ask midpoint
        bids = data.get("bids") or data.get("yesBids")
        asks = data.get("asks") or data.get("yesAsks")
        try:
            best_bid = max(float(x.get("price", x[0] if isinstance(x, list) else x)) for x in bids) if bids else None
            best_ask = min(float(x.get("price", x[0] if isinstance(x, list) else x)) for x in asks) if asks else None
            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / 2.0
                if 0.0 <= mid <= 1.0:
                    return mid
        except Exception:
            pass

        # nested result/data/market
        for key in ["result", "data", "market", "event"]:
            if key in data:
                v = self._extract_price(data[key])
                if v is not None:
                    return v

        return None

    def get_event_price(self, event_id: str) -> Optional[float]:
        if not self.enabled or not self.base_url:
            return None

        import requests
        endpoint = self.endpoint.format(event_id=event_id)
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}{endpoint}"

        try:
            response = requests.get(url, headers=self._headers(), timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            return self._extract_price(data)
        except Exception:
            return None


def _env_bool(name: str, default: bool) -> bool:
    import os
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


class GeminiSearchClient:
    """
    선택 모듈.
    Gemini Search grounding을 붙이면 뉴스/날씨/스포츠/온체인 요약을 여기서 생성한다.
    v1에서는 비용 방지를 위해 빈 context 반환.
    """

    def build_context(self, question: str) -> str:
        return ""


def make_mock_universe(symbols: List[str]) -> pd.DataFrame:
    """
    Deprecated dry-run compatibility helper.

    It intentionally returns neutral placeholders instead of random financial
    ratios.  Random values must never leak into an analysis or order decision;
    ``strategy.research_packet.build_research_universe`` is the live path.
    """
    from strategy.research_packet import FACTOR_COLUMNS

    return pd.DataFrame([{"symbol": symbol, **{column: 0.0 for column in FACTOR_COLUMNS}} for symbol in symbols])


def make_mock_ohlcv(n: int = 150) -> pd.DataFrame:
    """Create synthetic OHLCV only for explicitly labelled dry-run paths."""
    n = max(2, int(n))
    returns = np.random.normal(0.001, 0.02, n)
    close = 100 * np.cumprod(1 + returns)
    high = close * (1 + np.random.uniform(0.001, 0.025, n))
    low = close * (1 - np.random.uniform(0.001, 0.025, n))
    open_ = close * (1 + np.random.normal(0, 0.005, n))
    volume = np.random.randint(10000, 2000000, n)

    return pd.DataFrame({
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })
