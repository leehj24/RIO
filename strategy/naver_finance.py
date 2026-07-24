import re, time
from io import StringIO
from pathlib import Path
from typing import Dict, Any
import pandas as pd
import requests

class NaverWiseReportClient:
    """네이버/WiseReport 주요재무정보 스크래핑 client."""
    def __init__(self, cache_dir='data_cache/naver', ttl_seconds=24*3600):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds

    def get_encparam(self, code: str):
        html = requests.get(f'https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={code}', timeout=20).text
        enc = re.search(r"encparam: '(.*)'", html, re.I)
        encid = re.search(r"id: '([a-zA-Z0-9]*)' ?", html, re.I)
        if not enc or not encid:
            raise RuntimeError(f'encparam/id not found for {code}')
        return enc.group(1), encid.group(1)

    def financial_summary(self, code: str, freq_type='A') -> pd.DataFrame:
        cache_path = self.cache_dir / f'{code}_{freq_type}.pkl'
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime <= self.ttl_seconds:
            return pd.read_pickle(cache_path)
        encparam, encid = self.get_encparam(code)
        raw = requests.get('https://navercomp.wisereport.co.kr/v2/company/ajax/cF1001.aspx', params={
            'cmp_cd': code, 'fin_typ': '0', 'freq_typ': freq_type, 'encparam': encparam, 'id': encid
        }, timeout=20).text
        tables = pd.read_html(StringIO(raw))
        if not tables:
            raise RuntimeError(f'financial table not found for {code}')
        fs = tables[1] if len(tables) >= 2 else tables[0]
        if isinstance(fs.columns, pd.MultiIndex): fs.columns = fs.columns.droplevel(0)
        fs.set_index('주요재무정보', inplace=True)
        fs.to_pickle(cache_path)
        return fs.copy()

    def _latest_numeric(self, fs: pd.DataFrame, row_name: str):
        if row_name not in fs.index: return None
        vals=[]
        for v in fs.loc[row_name].tolist():
            try:
                txt=str(v).replace(',','').replace('%','').strip()
                if txt and txt.lower()!='nan': vals.append(float(txt))
            except Exception: pass
        return vals[-1] if vals else None

    def build_kr_fundamentals_from_naver(self, code: str) -> Dict[str, Any]:
        fs = self.financial_summary(code, 'A')
        return {
            'source':'naver_wisereport','symbol':code,
            'revenue':self._latest_numeric(fs,'매출액'),'operating_income':self._latest_numeric(fs,'영업이익'),
            'net_income':self._latest_numeric(fs,'당기순이익'),'eps':self._latest_numeric(fs,'EPS(원)'),
            'bps':self._latest_numeric(fs,'BPS(원)'),'per':self._latest_numeric(fs,'PER(배)'),
            'pbr':self._latest_numeric(fs,'PBR(배)'),'roe':self._latest_numeric(fs,'ROE(%)'),
            'debt_ratio':self._latest_numeric(fs,'부채비율'),'operating_margin':self._latest_numeric(fs,'영업이익률'),
            'net_margin':self._latest_numeric(fs,'순이익률'),'dividend_yield':self._latest_numeric(fs,'시가배당률(%)')
        }
