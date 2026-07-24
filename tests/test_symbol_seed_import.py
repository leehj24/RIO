import csv
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy.symbol_seed_import import (
    SYMBOL_COLUMNS,
    SymbolSeedImportError,
    merge_seed_into_symbols,
    preview_seed_merge,
)
from strategy.symbol_registry import load_symbols
from strategy.order_builder import build_buy_order, build_sell_order
from strategy.free_financial_sources import FreeFinancialSources


def _write_rows(path: Path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SYMBOL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _row(symbol: str, market: str = "JP", **overrides):
    row = {
        "symbol": symbol,
        "name": f"Company {symbol}",
        "raw_name": f"Company {symbol}",
        "market": market,
        "sector": "Technology",
        "theme": "AI",
        "enabled": "False",
        "resolved": "False",
        "note": "seed_candidate",
    }
    row.update(overrides)
    return row


def test_seed_preview_and_merge_keep_non_kr_us_rows_disabled(tmp_path):
    symbols_path = tmp_path / "symbols.csv"
    seed_path = tmp_path / "seed.csv"
    _write_rows(symbols_path, [_row("005930", "KR", enabled="true", resolved="seeded")])
    _write_rows(seed_path, [_row("7203.T"), _row("005930", "KR")])

    preview = preview_seed_merge(seed_path=seed_path, symbols_path=symbols_path)
    assert preview["add_rows"] == 1
    assert preview["skip_existing_symbols_sample"] == ["005930"]
    assert preview["all_imported_rows_disabled"] is True

    result = merge_seed_into_symbols(seed_path=seed_path, symbols_path=symbols_path)
    assert result["imported_rows"] == 1
    assert result["total_rows_after_import"] == 2

    with symbols_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    imported = next(row for row in rows if row["symbol"] == "7203.T")
    assert imported["enabled"] == "false"
    assert imported["resolved"] == "unresolved"
    assert "current_toss_live_support=KR_US_only" in imported["note"]
    assert "7203.T" not in load_symbols(str(symbols_path))
    assert "7203.T" in load_symbols(str(symbols_path), include_disabled=True)


def test_seed_import_rejects_duplicate_symbols_before_touching_target(tmp_path):
    symbols_path = tmp_path / "symbols.csv"
    seed_path = tmp_path / "seed.csv"
    _write_rows(symbols_path, [_row("005930", "KR")])
    _write_rows(seed_path, [_row("7203.T"), _row("7203.T")])

    with pytest.raises(SymbolSeedImportError, match="duplicate symbols"):
        merge_seed_into_symbols(seed_path=seed_path, symbols_path=symbols_path)
    assert len(load_symbols(str(symbols_path), include_disabled=True)) == 1


def test_declared_local_market_is_never_coerced_into_us_order():
    settings = SimpleNamespace(min_order_krw=10_000, min_order_usd=5, usd_krw_fx_rate=1_350)
    buy_ok, buy = build_buy_order(settings, "7203.T", {"market": "JP"}, 2_000, 30_000)
    sell_ok, sell = build_sell_order(
        settings,
        "7203.T",
        {"market": "JP"},
        2_000,
        {"_quantity": 3},
        1.0,
    )
    assert buy_ok is False
    assert sell_ok is False
    assert buy["reason"] == sell["reason"] == "unsupported_market_for_current_toss_execution"


def test_local_market_is_not_mislabeled_as_us_financial_data():
    result = FreeFinancialSources(SimpleNamespace()).build("7203.T", {"market": "JP"})
    assert result == {
        "source": "unsupported_market_for_current_execution",
        "symbol": "7203.T",
        "market": "JP",
    }


def test_enabled_non_kr_us_row_is_excluded_from_execution_universe(tmp_path):
    symbols_path = tmp_path / "symbols.csv"
    _write_rows(symbols_path, [_row("7203.T", "JP", enabled="true")])

    assert load_symbols(str(symbols_path)) == {}
