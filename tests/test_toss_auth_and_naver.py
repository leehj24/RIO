import io
import os
import sys
from pathlib import Path

import pandas as pd
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth import TossAuthError, TossTokenManager
from strategy.live_data_guard import parse_buying_power
from strategy.naver_finance import NaverWiseReportClient


def test_toss_token_manager_uses_env_token_when_available(monkeypatch, tmp_path):
    monkeypatch.setenv("TOSS_ACCESS_TOKEN", "env-token")
    monkeypatch.setenv("TOSS_ACCESS_TOKEN_EXPIRES_AT", str(10_000_000_000))
    manager = TossTokenManager(client_id="", client_secret="", cache_path=str(tmp_path / "token-cache.json"))
    assert manager.get_access_token() == "env-token"


def test_parse_toss_cash_buying_power_field():
    assert parse_buying_power({"result": {"currency": "KRW", "cashBuyingPower": "105568"}}) == 105568.0


def test_toss_token_refresh_disables_zstd_and_caches_token(monkeypatch, tmp_path):
    sent = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "fresh-token", "expires_in": 3600}

    def fake_post(url, *, headers, data, timeout):
        sent.update(url=url, headers=headers, data=data, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr("auth.requests.post", fake_post)
    manager = TossTokenManager(
        client_id="client-id",
        client_secret="client-secret",
        token_url="https://example.test/oauth2/token",
        cache_path=str(tmp_path / "token-cache.json"),
    )

    assert manager.refresh_access_token() == "fresh-token"
    assert sent["headers"]["Accept-Encoding"] == "gzip, deflate"
    assert sent["data"]["grant_type"] == "client_credentials"


def test_toss_token_refresh_wraps_transport_and_decode_errors(monkeypatch, tmp_path):
    def fail_post(*args, **kwargs):
        raise requests.exceptions.ContentDecodingError("zstd decode failed")

    monkeypatch.setattr("auth.requests.post", fail_post)
    manager = TossTokenManager(
        client_id="client-id",
        client_secret="client-secret",
        cache_path=str(tmp_path / "token-cache.json"),
    )

    with pytest.raises(TossAuthError, match="Toss token issue request failed"):
        manager.refresh_access_token()


def test_naver_financial_summary_parses_literal_html(monkeypatch):
    html = """
    <html><body>
    <script>encparam: 'abc123'; id: 'XYZ';</script>
    <table>
      <tr><th>주요재무정보</th><th>2023</th></tr>
      <tr><td>매출액</td><td>100</td></tr>
    </table>
    </body></html>
    """

    class FakeResponse:
        def __init__(self, text):
            self.text = text

    monkeypatch.setattr("strategy.naver_finance.requests.get", lambda *args, **kwargs: FakeResponse(html))
    client = NaverWiseReportClient(cache_dir=str(Path("/tmp/toss-naver-test")), ttl_seconds=0)
    df = client.financial_summary("005930", freq_type="A")
    assert df.index[0] == "매출액"
    assert df.loc["매출액", "2023"] == 100
