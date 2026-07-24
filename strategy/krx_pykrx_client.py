import json
import time
from pathlib import Path
from typing import Dict, Any

import pandas as pd


class PyKrxClient:
    """
    pykrx 기반 국내 KRX 가격/시총/거래량/거래대금 client.

    역할:
    - Momentum/Liquidity/Size에 필요한 실제 시장 데이터
    - 국내 OHLCV fallback
    """

    def __init__(self, cache_dir: str = "data_cache/pykrx", ttl_seconds: int = 6 * 3600):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds

    def _import_stock(self):
        try:
            from pykrx import stock
            return stock
        except Exception as e:
            raise RuntimeError("pykrx is not installed. Run: pip install pykrx") from e

    def _date(self, yyyymmdd: str | None = None) -> str:
        if yyyymmdd:
            return yyyymmdd.replace("-", "")
        import datetime as dt
        # KRX는 당일 장중/휴일 이슈가 있으므로 기본은 어제.
        return (dt.date.today() - dt.timedelta(days=1)).strftime("%Y%m%d")

    def market_cap_by_ticker(self, date: str | None = None) -> pd.DataFrame:
        stock = self._import_stock()
        date = self._date(date)
        cache_path = self.cache_dir / f"market_cap_{date}.pkl"
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime <= self.ttl_seconds:
            return pd.read_pickle(cache_path)
        df = stock.get_market_cap_by_ticker(date)
        df.to_pickle(cache_path)
        return df

    def ohlcv_by_date(self, symbol: str, start: str, end: str | None = None) -> pd.DataFrame:
        stock = self._import_stock()
        end = self._date(end)
        start = start.replace("-", "")
        cache_path = self.cache_dir / f"ohlcv_{symbol}_{start}_{end}.pkl"
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime <= self.ttl_seconds:
            return pd.read_pickle(cache_path)

        df = stock.get_market_ohlcv_by_date(start, end, symbol)
        out = df.rename(columns={
            "시가": "open",
            "고가": "high",
            "저가": "low",
            "종가": "close",
            "거래량": "volume",
            "거래대금": "trading_value",
        })
        out.to_pickle(cache_path)
        return out.reset_index(drop=True)

    def ticker_name(self, symbol: str) -> str:
        stock = self._import_stock()
        try:
            return stock.get_market_ticker_name(symbol)
        except Exception:
            return ""

    def build_kr_market_data(self, symbol: str, date: str | None = None) -> Dict[str, Any]:
        date = self._date(date)
        cap = self.market_cap_by_ticker(date)

        if symbol not in cap.index:
            # 휴일이면 가까운 영업일이 아닐 수 있으므로 실패를 명확히 반환.
            return {"source": "pykrx_no_symbol", "symbol": symbol, "date": date}

        row = cap.loc[symbol]
        def pick(*names):
            for n in names:
                if n in row.index:
                    try:
                        return float(row[n])
                    except Exception:
                        return None
            return None

        return {
            "source": "pykrx",
            "symbol": symbol,
            "date": date,
            "name": self.ticker_name(symbol),
            "current_price": pick("종가"),
            "market_cap": pick("시가총액"),
            "volume": pick("거래량"),
            "trading_value": pick("거래대금"),
            "shares": pick("상장주식수"),
        }
