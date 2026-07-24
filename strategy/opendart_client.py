import io
import json
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Any, Optional, List

import requests


class OpenDartClient:
    """
    OpenDART 공식 재무제표 client.

    기능:
    - corpCode.xml 다운로드/캐시
    - stock_code -> corp_code 매핑
    - 단일회사 전체 재무제표 API 호출
    - 매출/영업이익/순이익/자산/부채/자본 등 팩터용 값 추출

    필요:
    OPEN_DART_API_KEY
    """

    BASE_URL = "https://opendart.fss.or.kr/api"

    def __init__(self, api_key: str, cache_dir: str = "data_cache/opendart", ttl_seconds: int = 7 * 24 * 3600):
        self.api_key = api_key or ""
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds

    def _get_json_cached(self, url: str, params: Dict[str, Any], cache_name: str, ttl_seconds: int = 24 * 3600) -> Dict[str, Any]:
        path = self.cache_dir / cache_name
        if path.exists() and time.time() - path.stat().st_mtime <= ttl_seconds:
            return json.loads(path.read_text(encoding="utf-8"))

        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def corp_code_map(self) -> Dict[str, Dict[str, str]]:
        """
        return:
        {
          "005930": {"corp_code": "...", "corp_name": "...", "stock_code": "005930"}
        }
        """
        if not self.api_key:
            raise RuntimeError("OPEN_DART_API_KEY is missing")

        cache_path = self.cache_dir / "corp_code_map.json"
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime <= self.ttl_seconds:
            return json.loads(cache_path.read_text(encoding="utf-8"))

        url = f"{self.BASE_URL}/corpCode.xml"
        resp = requests.get(url, params={"crtfc_key": self.api_key}, timeout=60)
        resp.raise_for_status()

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        xml_name = zf.namelist()[0]
        root = ET.fromstring(zf.read(xml_name))

        mapping: Dict[str, Dict[str, str]] = {}
        for item in root.findall("list"):
            corp_code = item.findtext("corp_code", default="").strip()
            corp_name = item.findtext("corp_name", default="").strip()
            stock_code = item.findtext("stock_code", default="").strip()
            modify_date = item.findtext("modify_date", default="").strip()
            if stock_code:
                mapping[stock_code] = {
                    "corp_code": corp_code,
                    "corp_name": corp_name,
                    "stock_code": stock_code,
                    "modify_date": modify_date,
                }

        cache_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        return mapping

    def corp_code_by_stock_code(self, stock_code: str) -> Optional[str]:
        return self.corp_code_map().get(stock_code, {}).get("corp_code")

    def financial_statement_all(
        self,
        stock_code: str,
        bsns_year: int,
        reprt_code: str = "11011",
        fs_div: str = "CFS",
    ) -> Dict[str, Any]:
        corp_code = self.corp_code_by_stock_code(stock_code)
        if not corp_code:
            raise RuntimeError(f"corp_code not found for stock_code={stock_code}")

        params = {
            "crtfc_key": self.api_key,
            "corp_code": corp_code,
            "bsns_year": str(bsns_year),
            "reprt_code": reprt_code,
            "fs_div": fs_div,
        }
        url = f"{self.BASE_URL}/fnlttSinglAcntAll.json"
        cache_name = f"fs_all_{stock_code}_{bsns_year}_{reprt_code}_{fs_div}.json"
        return self._get_json_cached(url, params, cache_name, ttl_seconds=24 * 3600)

    def _find_amount(self, rows: List[Dict[str, Any]], names: List[str], sj_divs: tuple[str, ...] | None = None) -> Optional[float]:
        sj_divs = sj_divs or tuple()
        normalized_names = [n.replace(" ", "") for n in names]

        for row in rows:
            if sj_divs and row.get("sj_div") not in sj_divs:
                continue

            account_nm = str(row.get("account_nm", "")).replace(" ", "")
            if not any(n in account_nm for n in normalized_names):
                continue

            for key in ["thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount"]:
                raw = row.get(key)
                if raw not in (None, "", "-"):
                    try:
                        return float(str(raw).replace(",", ""))
                    except Exception:
                        pass
        return None

    def build_kr_fundamentals_from_dart(
        self,
        stock_code: str,
        year: int | None = None,
        reprt_code: str = "11011",
        fs_div: str = "CFS",
    ) -> Dict[str, Any]:
        import datetime as dt

        if not self.api_key:
            return {"source": "opendart_disabled", "symbol": stock_code}

        if year is None:
            year = dt.date.today().year - 1

        # 최근 사업보고서가 아직 없을 수 있어 최대 3년 후퇴.
        last_data = None
        used_year = None
        for y in [year, year - 1, year - 2]:
            data = self.financial_statement_all(stock_code, y, reprt_code=reprt_code, fs_div=fs_div)
            if data.get("status") == "000" and data.get("list"):
                last_data = data
                used_year = y
                break

        if not last_data:
            return {"source": "opendart_no_data", "symbol": stock_code}

        rows = last_data.get("list", [])

        revenue = self._find_amount(rows, ["매출액", "영업수익", "수익"], ("IS", "CIS"))
        op_income = self._find_amount(rows, ["영업이익"], ("IS", "CIS"))
        net_income = self._find_amount(rows, ["당기순이익", "분기순이익", "반기순이익"], ("IS", "CIS"))
        assets = self._find_amount(rows, ["자산총계"], ("BS",))
        liabilities = self._find_amount(rows, ["부채총계"], ("BS",))
        equity = self._find_amount(rows, ["자본총계"], ("BS",))

        def div(a, b):
            try:
                return float(a) / float(b) if b not in (None, 0) else None
            except Exception:
                return None

        debt_ratio = div(liabilities, assets)
        roe = div(net_income, equity)
        operating_margin = div(op_income, revenue)
        net_margin = div(net_income, revenue)

        return {
            "source": "opendart",
            "symbol": stock_code,
            "year": used_year,
            "revenue": revenue,
            "operating_income": op_income,
            "net_income": net_income,
            "assets": assets,
            "liabilities": liabilities,
            "equity": equity,
            "debt_ratio": debt_ratio,
            "roe": roe,
            "operating_margin": operating_margin,
            "net_margin": net_margin,
        }
