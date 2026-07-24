from typing import Dict, Any
import pandas as pd

class YFinanceClient:
    """yfinance 해외주식 무료 데이터 client."""
    def __init__(self):
        try:
            import yfinance as yf
        except Exception as e:
            raise RuntimeError('yfinance is not installed. Run: pip install yfinance') from e
        self.yf = yf

    def ticker(self, symbol: str):
        return self.yf.Ticker(symbol)

    def info(self, symbol: str) -> Dict[str, Any]:
        try: return dict(self.ticker(symbol).info or {})
        except Exception: return {}

    def financial_tables(self, symbol: str) -> Dict[str, Any]:
        t = self.ticker(symbol)
        return {'financials': t.financials, 'balance_sheet': t.balance_sheet, 'cashflow': t.cashflow}

    def ohlcv(self, symbol: str, period='1y', interval='1d') -> pd.DataFrame:
        df = self.ticker(symbol).history(period=period, interval=interval)
        if df is None or df.empty: return pd.DataFrame()
        out = df.rename(columns={'Open':'open','High':'high','Low':'low','Close':'close','Volume':'volume'})
        return out[['open','high','low','close','volume']].dropna().reset_index(drop=True)

    def build_us_fundamentals_from_yf(self, symbol: str) -> Dict[str, Any]:
        info = self.info(symbol)
        return {
            'source':'yfinance','symbol':symbol,
            'market_cap':info.get('marketCap'),'per':info.get('trailingPE'),'pbr':info.get('priceToBook'),
            'roe':info.get('returnOnEquity'),'debt_to_equity':info.get('debtToEquity'),
            'net_margin':info.get('profitMargins'),'operating_margin':info.get('operatingMargins'),
            'sales_growth':info.get('revenueGrowth'),'eps_growth':info.get('earningsGrowth'),'beta':info.get('beta')
        }
