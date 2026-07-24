import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy.events import resolve_symbol_tokens
from strategy.symbol_registry import load_symbols, symbols_by_country
from strategy.toss_symbol_verifier import verify_execution_symbols, write_verification_report


def _write_symbols(path: Path):
    rows = [
        {
            "symbol": "005930", "name": "Samsung", "raw_name": "Samsung", "market": "KR",
            "sector": "Semiconductor", "theme": "AI Semiconductor", "enabled": "true",
            "resolved": "seeded", "note": "test",
        },
        {
            "symbol": "7203.T", "name": "Toyota", "raw_name": "Toyota", "market": "JP",
            "sector": "Auto", "theme": "Autonomous Driving", "enabled": "true",
            "resolved": "seeded", "note": "must_not_execute",
        },
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_country_markets_are_enabled_execution_candidates_and_resolve_into_country_event():
    symbols = load_symbols()
    country_etfs = [row for row in symbols.values() if row.get("sector") == "Country ETF"]
    country_equities = [row for row in symbols.values() if row.get("sector") == "Country Equity"]

    # 2026-07-24: 필리핀(EPHE)·뉴질랜드(ENZL) 국가 ETF 추가로 34 → 36
    assert len(country_etfs) == 36
    assert len(country_equities) >= 120
    assert {row["market"] for row in country_etfs} == {"US"}
    assert {row["theme"] for row in country_etfs} >= {"Country: Japan", "Country: China", "Country: India"}
    assert set(resolve_symbol_tokens("SECTOR:Country ETF")) == {row["symbol"] for row in country_etfs}
    assert {"INDY", "INFY", "TM", "BABA", "SHOP", "NVS", "UBS", "TSM"} <= set(symbols_by_country("ALL"))
    assert set(resolve_symbol_tokens("COUNTRY:ALL")) == set(symbols_by_country("ALL"))
    assert {"INDY", "INFY", "HDB"} <= set(symbols_by_country("India"))


def test_toss_verifier_only_queries_enabled_kr_us_candidates(tmp_path):
    symbols_path = tmp_path / "symbols.csv"
    _write_symbols(symbols_path)

    class Client:
        def __init__(self):
            self.calls = []

        def stocks(self, symbols):
            self.calls.append(("stocks", symbols))
            return {"result": [{"symbol": "005930", "currency": "KRW"}]}

        def prices(self, symbols):
            self.calls.append(("prices", symbols))
            return {"result": [{"symbol": "005930", "lastPrice": "72000", "currency": "KRW"}]}

    client = Client()
    rows = verify_execution_symbols(client, symbols_path=str(symbols_path), verified_at_utc="2026-07-16T00:00:00Z")

    assert [row["symbol"] for row in rows] == ["005930"]
    assert rows[0]["verification_status"] == "verified"
    assert all(call[1] == ["005930"] for call in client.calls)

    output = write_verification_report(rows, tmp_path / "verified.csv")
    with output.open(encoding="utf-8-sig", newline="") as handle:
        persisted = list(csv.DictReader(handle))
    assert persisted[0]["last_price"] == "72000"


def test_dashboard_exposes_only_live_execution_symbols():
    from dashboard_server import app

    client = app.test_client()
    execution = client.get("/api/bot/execution-symbols").get_json()["result"]

    execution_rows = list(execution.values())
    assert execution_rows
    assert {row["market"] for row in execution_rows} <= {"KR", "US"}
    assert all(row.get("enabled") for row in execution_rows)
    assert client.get("/api/bot/research-universe").status_code == 404
