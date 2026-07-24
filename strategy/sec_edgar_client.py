import time
from pathlib import Path
from typing import Dict, Any, Optional, List
import requests

class SecEdgarClient:
    """SEC EDGAR 무료 공식 데이터 client: submissions + companyfacts."""
    def __init__(self, user_agent: str, cache_dir: str = 'data_cache/sec', sleep_seconds: float = 0.15):
        self.user_agent = user_agent or 'personal-research-bot email@example.com'
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sleep_seconds = sleep_seconds

    def _headers(self):
        return {'User-Agent': self.user_agent, 'Accept-Encoding': 'gzip, deflate', 'Host': 'data.sec.gov'}

    def _cik10(self, cik: str) -> str:
        return str(cik).replace('CIK', '').strip().zfill(10)

    def _get_json_cached(self, url: str, cache_name: str, ttl_seconds: int = 86400) -> Dict[str, Any]:
        path = self.cache_dir / cache_name
        if path.exists() and time.time() - path.stat().st_mtime <= ttl_seconds:
            import json
            return json.loads(path.read_text(encoding='utf-8'))
        time.sleep(self.sleep_seconds)
        r = requests.get(url, headers=self._headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
        import json
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return data

    def submissions(self, cik: str) -> Dict[str, Any]:
        cik10 = self._cik10(cik)
        return self._get_json_cached(f'https://data.sec.gov/submissions/CIK{cik10}.json', f'submissions_{cik10}.json', 6*3600)

    def companyfacts(self, cik: str) -> Dict[str, Any]:
        cik10 = self._cik10(cik)
        return self._get_json_cached(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json', f'companyfacts_{cik10}.json', 24*3600)

    def latest_fact(self, companyfacts: Dict[str, Any], tags: List[str], unit_candidates: Optional[List[str]] = None, forms=('10-K','10-Q')) -> Optional[Dict[str, Any]]:
        unit_candidates = unit_candidates or ['USD', 'shares', 'USD/shares']
        us_gaap = companyfacts.get('facts', {}).get('us-gaap', {})
        candidates = []
        for tag in tags:
            item = us_gaap.get(tag) or {}
            for unit in unit_candidates:
                for v in item.get('units', {}).get(unit, []):
                    if v.get('form') in forms and v.get('val') is not None:
                        candidates.append({**v, '_tag': tag, '_unit': unit})
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x.get('filed',''), x.get('end','')), reverse=True)
        return candidates[0]

    def build_us_fundamentals_from_sec(self, cik: str) -> Dict[str, Any]:
        facts = self.companyfacts(cik)
        def val(tags, units=None):
            item = self.latest_fact(facts, tags, units)
            return item.get('val') if item else None
        revenue = val(['Revenues','SalesRevenueNet'])
        net_income = val(['NetIncomeLoss','ProfitLoss'])
        op_income = val(['OperatingIncomeLoss'])
        assets = val(['Assets'])
        liabilities = val(['Liabilities'])
        equity = val(['StockholdersEquity','StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'])
        cfo = val(['NetCashProvidedByUsedInOperatingActivities'])
        capex = val(['PaymentsToAcquirePropertyPlantAndEquipment'])
        eps = val(['EarningsPerShareDiluted'], ['USD/shares'])
        def ratio(a,b):
            try: return float(a)/float(b) if b else None
            except Exception: return None
        fcf = None
        try:
            if cfo is not None and capex is not None:
                fcf = float(cfo) - abs(float(capex))
        except Exception:
            pass
        return {
            'source':'sec_edgar_companyfacts','cik':str(cik).zfill(10),
            'revenue':revenue,'net_income':net_income,'operating_income':op_income,
            'assets':assets,'liabilities':liabilities,'equity':equity,
            'cfo':cfo,'capex':capex,'free_cash_flow':fcf,'eps_diluted':eps,
            'debt_ratio':ratio(liabilities, assets),'roe':ratio(net_income, equity),
            'operating_margin':ratio(op_income, revenue),'net_margin':ratio(net_income, revenue)
        }
