from typing import Dict, Any

import numpy as np

from strategy.sec_edgar_client import SecEdgarClient
from strategy.naver_finance import NaverWiseReportClient
from strategy.opendart_client import OpenDartClient
from strategy.krx_pykrx_client import PyKrxClient


US_CIK_MAP = {
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "NVDA": "0001045810",
    "AMD": "0000002488",
    "GOOGL": "0001652044",
    "GOOG": "0001652044",
    "AMZN": "0001018724",
    "META": "0001326801",
    "TSLA": "0001318605",
    "AVGO": "0001730168",
    "MSTR": "0001050446",
    "COIN": "0001679788",
}


def _safe_float(v, default=np.nan):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def merge_fundamentals(primary: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(fallback or {})
    out.update({k: v for k, v in (primary or {}).items() if v not in (None, "", [], {})})
    out["fallback_source"] = (fallback or {}).get("source")
    return out


class FreeFinancialSources:
    """
    무료 재무/시장 데이터 통합.

    US:
    - SEC EDGAR companyfacts 공식 재무
    - yfinance fallback/market cap/ratios

    KR:
    - OpenDART 공식 재무
    - Naver/WiseReport 재무요약 fallback
    - pykrx KRX 가격/시총/거래량/거래대금
    """

    def __init__(self, settings):
        self.settings = settings

    def build_us(self, symbol: str) -> Dict[str, Any]:
        cik = US_CIK_MAP.get(symbol.upper())

        yf_data = {}
        if getattr(self.settings, "enable_yfinance", True):
            try:
                from strategy.yfinance_client import YFinanceClient
                yf_data = YFinanceClient().build_us_fundamentals_from_yf(symbol)
            except Exception as e:
                yf_data = {"source": "yfinance_error", "error": str(e)}

        sec_data = {}
        if getattr(self.settings, "enable_sec_edgar", True) and cik:
            try:
                sec = SecEdgarClient(
                    user_agent=getattr(self.settings, "sec_user_agent", ""),
                    cache_dir=getattr(self.settings, "sec_cache_dir", "data_cache/sec"),
                )
                sec_data = sec.build_us_fundamentals_from_sec(cik)
            except Exception as e:
                sec_data = {"source": "sec_edgar_error", "error": str(e)}

        merged = merge_fundamentals(sec_data, yf_data)
        merged["symbol"] = symbol
        merged["market"] = "US"
        return merged

    def build_kr(self, symbol: str) -> Dict[str, Any]:
        dart_data = {}
        if getattr(self.settings, "enable_opendart", True):
            try:
                dart = OpenDartClient(
                    api_key=getattr(self.settings, "open_dart_api_key", ""),
                    cache_dir=getattr(self.settings, "opendart_cache_dir", "data_cache/opendart"),
                )
                dart_data = dart.build_kr_fundamentals_from_dart(symbol)
            except Exception as e:
                dart_data = {"source": "opendart_error", "symbol": symbol, "error": str(e)}

        naver_data = {}
        if getattr(self.settings, "enable_naver_finance", True):
            try:
                client = NaverWiseReportClient(cache_dir=getattr(self.settings, "naver_cache_dir", "data_cache/naver"))
                naver_data = client.build_kr_fundamentals_from_naver(symbol)
            except Exception as e:
                naver_data = {"source": "naver_wisereport_error", "symbol": symbol, "error": str(e)}

        krx_data = {}
        if getattr(self.settings, "enable_pykrx", True):
            try:
                krx = PyKrxClient(cache_dir=getattr(self.settings, "pykrx_cache_dir", "data_cache/pykrx"))
                krx_data = krx.build_kr_market_data(symbol)
            except Exception as e:
                krx_data = {"source": "pykrx_error", "symbol": symbol, "error": str(e)}

        # 재무는 OpenDART 우선, Naver fallback. 시장데이터는 KRX를 덮어쓴다.
        merged = merge_fundamentals(dart_data, naver_data)
        merged.update({k: v for k, v in krx_data.items() if v not in (None, "", [], {})})
        merged["symbol"] = symbol
        merged["market"] = "KR"
        merged["fundamental_primary_source"] = dart_data.get("source")
        merged["fundamental_fallback_source"] = naver_data.get("source")
        merged["market_data_source"] = krx_data.get("source")
        return merged

    def build(self, symbol: str, symbol_info: Dict[str, Any]) -> Dict[str, Any]:
        market = str(symbol_info.get("market", "")).upper()
        if market == "KR" or (symbol.isdigit() and len(symbol) == 6):
            return self.build_kr(symbol)
        if market and market != "US":
            # Do not label a Japanese/Indian/etc. local ticker as US merely
            # because yfinance can sometimes look it up.  This project has no
            # matching live order/data route for those markets yet.
            return {
                "source": "unsupported_market_for_current_execution",
                "symbol": symbol,
                "market": market,
            }
        return self.build_us(symbol)

    def to_factor_overrides(self, fundamentals: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        mapping = {
            "per": "per",
            "pbr": "pbr",
            "roe": "roe",
            "debt_ratio": "debt_ratio",
            "operating_margin": "operating_margin",
            "net_margin": "net_margin",
            "sales_growth": "sales_growth",
            "eps_growth": "eps_growth",
            "beta": "beta",
            "free_cash_flow": "free_cash_flow",
            "market_cap": "market_cap",
            "volume": "volume",
            "trading_value": "trading_value",
        }
        for src, dst in mapping.items():
            v = _safe_float(fundamentals.get(src), np.nan)
            if not np.isnan(v):
                # Naver는 % 단위, DART는 비율 단위가 섞일 수 있음. 너무 큰 비율값은 %로 보고 정규화.
                if dst in {"roe", "operating_margin", "net_margin", "debt_ratio"} and abs(v) > 3:
                    v = v / 100.0
                out[dst] = v

        # FCF yield / earnings yield / book-to-market 직접 계산
        market_cap = _safe_float(fundamentals.get("market_cap"), np.nan)
        if not np.isnan(market_cap) and market_cap > 0:
            fcf = _safe_float(fundamentals.get("free_cash_flow"), np.nan)
            ni = _safe_float(fundamentals.get("net_income"), np.nan)
            equity = _safe_float(fundamentals.get("equity"), np.nan)
            if not np.isnan(fcf):
                out["fcf_yield"] = fcf / market_cap
            if not np.isnan(ni):
                out["earnings_yield"] = ni / market_cap
            if not np.isnan(equity):
                out["book_to_market"] = equity / market_cap

        # turnover proxy
        trading_value = _safe_float(fundamentals.get("trading_value"), np.nan)
        if not np.isnan(trading_value) and not np.isnan(market_cap) and market_cap > 0:
            out["turnover"] = trading_value / market_cap

        return out
