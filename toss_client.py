import os
import random
import threading
import time
import uuid
from typing import Any, Dict, Optional, List

import requests

from auth import TossTokenManager


class TossAPIError(RuntimeError):
    """An API error with enough metadata to make a safe retry decision."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429 or self.code in {"rate-limit-exceeded", "edge-rate-limit-exceeded"}


class _RequestPacer:
    """Process-local, per-rate-group pacing for the Toss REST API.

    Toss applies limits per client and API group.  A fixed conservative spacing
    is intentionally simpler than a bursty token bucket here: the bot is a
    low-frequency daily-bar strategy, so avoiding a 429 is more valuable than
    shaving a few seconds from the initial cache warm-up.
    """

    def __init__(self, safety_factor: float = 0.8) -> None:
        self.safety_factor = min(1.0, max(0.1, float(safety_factor)))
        self._lock = threading.Lock()
        self._next_allowed: Dict[str, float] = {}

    def wait(self, group: str, limit_per_second: float) -> None:
        interval = 1.0 / max(0.1, float(limit_per_second) * self.safety_factor)
        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_allowed.get(group, now))
            self._next_allowed[group] = scheduled + interval
        delay = scheduled - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def defer(self, group: str, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._next_allowed[group] = max(
                self._next_allowed.get(group, 0.0),
                time.monotonic() + float(seconds),
            )


class TossClient:
    """
    Toss Invest API wrapper.

    개선점:
    - TOSS_ACCESS_TOKEN을 수동으로 매일 갱신하지 않아도 됨.
    - TOSS_CLIENT_ID / TOSS_CLIENT_SECRET으로 24시간 토큰 자동 발급/갱신.
    - 401 인증 실패 시 토큰 refresh 후 1회 재시도.
    - accountSeq를 수동으로 고정하지 않아도 accounts()에서 첫 BROKERAGE 계좌를 자동 선택 가능.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        access_token: Optional[str] = None,
        default_account_seq: Optional[str] = None,
        dry_run: bool = True,
        token_manager: Optional[TossTokenManager] = None,
        auto_account: bool = True,
        rate_limit_safety_factor: float | None = None,
        max_get_retries: int | None = None,
        retry_backoff_max_seconds: float | None = None,
    ):
        self.base_url = (base_url or os.getenv("TOSS_BASE_URL") or "https://openapi.tossinvest.com").rstrip("/")
        self.manual_access_token = access_token or os.getenv("TOSS_ACCESS_TOKEN", "")
        self.default_account_seq = default_account_seq or os.getenv("TOSS_ACCOUNT_SEQ", "")
        self.dry_run = dry_run
        self.auto_account = auto_account
        self.rate_limit_safety_factor = float(
            rate_limit_safety_factor
            if rate_limit_safety_factor is not None
            else os.getenv("TOSS_RATE_LIMIT_SAFETY_FACTOR", "0.80")
        )
        self.max_get_retries = max(
            0,
            int(max_get_retries if max_get_retries is not None else os.getenv("TOSS_MAX_GET_RETRIES", "2")),
        )
        self.retry_backoff_max_seconds = max(
            0.1,
            float(
                retry_backoff_max_seconds
                if retry_backoff_max_seconds is not None
                else os.getenv("TOSS_RETRY_BACKOFF_MAX_SECONDS", "15")
            ),
        )
        self._pacer = _RequestPacer(self.rate_limit_safety_factor)

        write_env = (os.getenv("TOSS_WRITE_TOKEN_TO_ENV", "true").lower() in {"1", "true", "yes", "y"})
        self.token_manager = token_manager or TossTokenManager(write_token_to_env=write_env)

        self.ep_accounts = os.getenv("TOSS_ENDPOINT_ACCOUNTS", "/api/v1/accounts")
        self.ep_orders = os.getenv("TOSS_ENDPOINT_ORDERS", "/api/v1/orders")
        self.ep_buying_power = os.getenv("TOSS_ENDPOINT_BUYING_POWER", "/api/v1/buying-power")
        self.ep_positions = os.getenv("TOSS_ENDPOINT_POSITIONS", "/api/v1/holdings")
        self.ep_stocks = os.getenv("TOSS_ENDPOINT_STOCKS", "/api/v1/stocks")
        self.ep_prices = os.getenv("TOSS_ENDPOINT_PRICES", "/api/v1/prices")
        self.ep_orderbook = os.getenv("TOSS_ENDPOINT_ORDERBOOK", "/api/v1/orderbook")
        self.ep_candles = os.getenv("TOSS_ENDPOINT_CANDLES", "/api/v1/candles")

        # Additional endpoint configurations used in client methods
        self.endpoint_order_status = os.getenv("TOSS_ENDPOINT_ORDER_STATUS", "/api/v1/orders/{orderId}")
        self.endpoint_stock_info = os.getenv("TOSS_ENDPOINT_STOCK_INFO", "/api/v1/stocks")
        self.endpoint_market_status = os.getenv("TOSS_ENDPOINT_MARKET_STATUS", "/api/v1/market-calendar/{market}")
        self.endpoint_sellable_quantity = os.getenv("TOSS_ENDPOINT_SELLABLE_QUANTITY", "/api/v1/sellable-quantity")
        self.endpoint_commissions = os.getenv("TOSS_ENDPOINT_COMMISSIONS", "/api/v1/commissions")
        self.endpoint_exchange_rate = os.getenv("TOSS_ENDPOINT_EXCHANGE_RATE", "/api/v1/exchange-rate")

    _RATE_LIMITS: Dict[str, float] = {
        "AUTH": 5,
        "ACCOUNT": 1,
        "ASSET": 5,
        "STOCK": 5,
        "MARKET_INFO": 3,
        "MARKET_DATA": 10,
        "MARKET_DATA_CHART": 5,
        "RANKING": 5,
        "ORDER": 6,
        "ORDER_HISTORY": 5,
        "ORDER_INFO": 6,
        "CONDITIONAL_ORDER": 5,
        "CONDITIONAL_ORDER_HISTORY": 10,
    }

    @classmethod
    def _rate_limit_group(cls, method: str, path: str) -> str:
        normalized = path.lower().split("?", 1)[0].rstrip("/")
        if normalized.endswith("/candles") or "/candles/" in normalized:
            return "MARKET_DATA_CHART"
        if normalized.endswith("/prices") or normalized.endswith("/orderbook") or normalized.endswith("/trades") or normalized.endswith("/price-limits"):
            return "MARKET_DATA"
        if normalized.endswith("/accounts"):
            return "ACCOUNT"
        if normalized.endswith("/holdings"):
            return "ASSET"
        if normalized.endswith("/buying-power") or normalized.endswith("/sellable-quantity") or normalized.endswith("/commissions"):
            return "ORDER_INFO"
        if "/market-calendar/" in normalized or normalized.endswith("/exchange-rate"):
            return "MARKET_INFO"
        if "/stocks" in normalized:
            return "STOCK"
        if "/conditional-orders" in normalized:
            return "CONDITIONAL_ORDER" if method.upper() != "GET" else "CONDITIONAL_ORDER_HISTORY"
        if "/orders" in normalized:
            return "ORDER" if method.upper() != "GET" else "ORDER_HISTORY"
        if normalized.endswith("/oauth2/token"):
            return "AUTH"
        return "MARKET_DATA"

    @staticmethod
    def _retry_after_seconds(response: requests.Response, attempt: int, maximum: float) -> float:
        for header in ("Retry-After", "X-RateLimit-Reset"):
            raw = response.headers.get(header)
            try:
                if raw is not None:
                    return min(maximum, max(0.0, float(raw)))
            except (TypeError, ValueError):
                continue
        # Exponential backoff with a small jitter follows Toss's documented
        # 429 guidance while staying bounded for an unattended bot process.
        return min(maximum, (2**attempt) + random.uniform(0.0, 0.25))

    @staticmethod
    def _error_details(response: requests.Response) -> tuple[Any, str | None, str | None]:
        try:
            detail: Any = response.json()
        except Exception:
            detail = response.text
        error = detail.get("error", {}) if isinstance(detail, dict) else {}
        code = error.get("code") if isinstance(error, dict) else None
        request_id = (
            error.get("requestId") if isinstance(error, dict) else None
        ) or response.headers.get("X-Request-Id") or response.headers.get("x-amz-cf-id")
        return detail, str(code) if code else None, str(request_id) if request_id else None

    def get_access_token(self, force_refresh: bool = False) -> str:
        if self.manual_access_token and not force_refresh:
            return self.manual_access_token
        return self.token_manager.get_access_token(force_refresh=force_refresh)

    def _headers(self, account_seq: Optional[str] = None, include_account: bool = True, force_refresh: bool = False) -> Dict[str, str]:
        token = self.get_access_token(force_refresh=force_refresh)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        if include_account:
            acc = account_seq or self.default_account_seq
            if acc:
                headers["X-Tossinvest-Account"] = str(acc)
        return headers

    def _url(self, path: str) -> str:
        return self.base_url + "/" + path.lstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        *,
        account_seq: Optional[str] = None,
        include_account: bool = True,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        retry_on_401: bool = True,
    ) -> Dict[str, Any]:
        url = self._url(path)
        normalized_method = method.upper()
        group = self._rate_limit_group(normalized_method, path)
        # Retrying a GET is safe.  Retrying an order POST after an ambiguous
        # response is not: the broker may already have accepted it.  Orders
        # therefore rely on clientOrderId idempotency and explicit status
        # reconciliation instead of automatic transport retries.
        retries = self.max_get_retries if normalized_method == "GET" else 0
        force_refresh = False
        auth_retry_used = False
        attempt = 0
        while True:
            self._pacer.wait(group, self._RATE_LIMITS.get(group, 3))
            resp = requests.request(
                method=normalized_method,
                url=url,
                headers=self._headers(account_seq, include_account=include_account, force_refresh=force_refresh),
                params=params,
                json=json_body,
                timeout=30,
            )

            if resp.status_code == 401 and retry_on_401 and not auth_retry_used:
                # Existing token expired/invalid: refresh once.  This is a
                # credential recovery path, not an order replay.
                self.manual_access_token = ""
                force_refresh = True
                auth_retry_used = True
                continue

            retryable = resp.status_code in {429, 500, 502, 503, 504}
            if retryable and attempt < retries:
                delay = self._retry_after_seconds(resp, attempt, self.retry_backoff_max_seconds)
                self._pacer.defer(group, delay)
                time.sleep(delay)
                attempt += 1
                continue

            if resp.status_code >= 400:
                detail, code, request_id = self._error_details(resp)
                retry_after = self._retry_after_seconds(resp, attempt, self.retry_backoff_max_seconds) if resp.status_code == 429 else None
                if retry_after:
                    # Even when the safe GET retry budget is exhausted, share
                    # the broker's cooldown with the next request instead of
                    # immediately repeating the same rate-limit failure.
                    self._pacer.defer(group, retry_after)
                raise TossAPIError(
                    f"Toss API error {resp.status_code}: {detail}",
                    status_code=resp.status_code,
                    code=code,
                    request_id=request_id,
                    retry_after_seconds=retry_after,
                )

            try:
                return resp.json()
            except Exception:
                return {"raw": resp.text}

    # ------------------------------------------------------------
    # Auth / account
    # ------------------------------------------------------------

    def issue_token(self) -> Dict[str, Any]:
        token = self.token_manager.refresh_access_token()
        return {
            "access_token": token,
            "message": "token issued/refreshed and cached",
        }

    def accounts(self) -> Dict[str, Any]:
        # 계좌 목록 조회는 X-Tossinvest-Account가 필요 없음
        return self._request("GET", self.ep_accounts, include_account=False)

    def resolve_account_seq(self) -> str:
        """
        계좌번호(accountNo)가 아니라 accountSeq를 찾아 X-Tossinvest-Account에 사용한다.
        .env에 TOSS_ACCOUNT_SEQ가 있으면 그걸 우선 사용.
        없으면 GET /api/v1/accounts 응답의 첫 BROKERAGE 계좌를 자동 선택.
        """
        if self.default_account_seq:
            return str(self.default_account_seq)

        if not self.auto_account:
            raise TossAPIError("TOSS_ACCOUNT_SEQ missing and auto_account is disabled")

        data = self.accounts()
        accounts = data.get("result", data)
        if not isinstance(accounts, list):
            raise TossAPIError(f"Unexpected accounts response: {data}")

        if not accounts:
            raise TossAPIError("No active Toss brokerage account found")

        brokerage = None
        for acc in accounts:
            if acc.get("accountType") == "BROKERAGE":
                brokerage = acc
                break
        if brokerage is None:
            brokerage = accounts[0]

        seq = brokerage.get("accountSeq")
        if seq is None:
            raise TossAPIError(f"accountSeq not found in accounts response: {brokerage}")

        self.default_account_seq = str(seq)
        return self.default_account_seq

    # ------------------------------------------------------------
    # Account / balance / holdings
    # ------------------------------------------------------------

    def buying_power(self, account_seq: Optional[str] = None, currency: Optional[str] = "KRW") -> Dict[str, Any]:
        acc = account_seq or self.resolve_account_seq()
        params = {
            "currency": currency or "KRW"
        }
        return self._request("GET", self.ep_buying_power, account_seq=acc, params=params)

    def positions(self, account_seq: Optional[str] = None) -> Dict[str, Any]:
        acc = account_seq or self.resolve_account_seq()
        return self._request("GET", self.ep_positions, account_seq=acc)

    def orders(self, account_seq: Optional[str] = None, status: Optional[str] = "OPEN") -> Dict[str, Any]:
        """List broker orders; used to prevent duplicate opposite/pending orders."""

        acc = account_seq or self.resolve_account_seq()
        params = {"status": status} if status else None
        return self._request("GET", self.ep_orders, account_seq=acc, params=params)

    def sellable_quantity(self, symbol: str, account_seq: Optional[str] = None) -> Dict[str, Any]:
        acc = account_seq or self.resolve_account_seq()
        return self._request(
            "GET",
            self.endpoint_sellable_quantity,
            account_seq=acc,
            params={"symbol": symbol},
        )

    def commissions(self, account_seq: Optional[str] = None) -> Dict[str, Any]:
        acc = account_seq or self.resolve_account_seq()
        return self._request("GET", self.endpoint_commissions, account_seq=acc)

    # ------------------------------------------------------------
    # Market info
    # ------------------------------------------------------------

    def stocks(self, symbols: List[str]) -> Dict[str, Any]:
        return self._request("GET", self.ep_stocks, include_account=False, params={"symbols": ",".join(symbols)})

    def prices(self, symbols: List[str]) -> Dict[str, Any]:
        return self._request("GET", self.ep_prices, include_account=False, params={"symbols": ",".join(symbols)})

    def orderbook(self, symbol: str) -> Dict[str, Any]:
        """Read the current book for one symbol immediately before an order.

        This endpoint is deliberately uncached: it is only called after all
        signal and account gates have passed, just before a market-order POST.
        """

        return self._request(
            "GET",
            self.ep_orderbook,
            include_account=False,
            params={"symbol": symbol},
        )

    def candles(
        self,
        symbol: str,
        interval: str = "1d",
        count: int = 120,
        before: Optional[str] = None,
    ) -> Dict[str, Any]:
        mapped_interval = str(interval).lower()
        if mapped_interval in {"1d", "d", "daily"}:
            mapped_interval = "1d"
        elif mapped_interval in {"1m", "m", "minute"}:
            mapped_interval = "1m"
        else:
            mapped_interval = "1d"  # Default fallback to day candle

        params: Dict[str, Any] = {"symbol": symbol, "interval": mapped_interval, "count": count}
        if before:
            params["before"] = str(before)

        return self._request(
            "GET",
            self.ep_candles,
            include_account=False,
            params=params,
        )

    def exchange_rate(self, base_currency: str = "USD", quote_currency: str = "KRW") -> Dict[str, Any]:
        return self._request(
            "GET",
            self.endpoint_exchange_rate,
            include_account=False,
            params={"baseCurrency": base_currency, "quoteCurrency": quote_currency},
        )

    # ------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------

    @staticmethod
    def _validate_order_create(
        side: str,
        order_type: str,
        symbol: str,
        quantity: Optional[str],
        order_amount: Optional[str],
        price: Optional[str],
    ) -> None:
        side = side.upper()
        order_type = order_type.upper()

        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")

        if order_type not in {"LIMIT", "MARKET"}:
            raise ValueError("orderType must be LIMIT or MARKET")

        if not symbol:
            raise ValueError("symbol is required")

        has_qty = quantity not in (None, "")
        has_amount = order_amount not in (None, "")

        if has_qty == has_amount:
            raise ValueError("quantity and orderAmount: exactly one must be provided")

        if order_type == "LIMIT" and not price:
            raise ValueError("LIMIT order requires price")

        if order_type == "MARKET" and price:
            raise ValueError("MARKET order must not include price")


    def stock_info(self, symbol: str) -> Dict[str, Any]:
        endpoint = self.endpoint_stock_info
        if "{symbol}" in endpoint:
            endpoint = endpoint.format(symbol=symbol)
            return self._request("GET", endpoint, include_account=False)
        return self._request("GET", endpoint, include_account=False, params={"symbols": symbol})

    def market_status(self, market: str | None = None) -> Dict[str, Any]:
        normalized_market = str(market or "KR").upper()
        endpoint = self.endpoint_market_status
        if "{market}" in endpoint:
            endpoint = endpoint.format(market=normalized_market)
            return self._request("GET", endpoint, include_account=False)
        params = {"market": normalized_market} if normalized_market else None
        return self._request("GET", endpoint, params=params, include_account=False)

    def order_status(self, order_id: str, account_seq: Optional[Any] = None) -> Dict[str, Any]:
        endpoint = self.endpoint_order_status.format(orderId=order_id, order_id=order_id)
        return self._request("GET", endpoint, account_seq=account_seq, include_account=True)

    def create_order(
        self,
        *,
        account_seq: Optional[str],
        symbol: str,
        side: str,
        order_type: str,
        quantity: Optional[str] = None,
        order_amount: Optional[str] = None,
        price: Optional[str] = None,
        client_order_id: Optional[str] = None,
        confirm_high_value_order: bool = False,
        time_in_force: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._validate_order_create(side, order_type, symbol, quantity, order_amount, price)

        acc = account_seq or self.resolve_account_seq()

        payload: Dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "orderType": order_type.upper(),
        }

        if quantity not in (None, ""):
            payload["quantity"] = str(quantity)
        if order_amount not in (None, ""):
            payload["orderAmount"] = str(order_amount)
        if price not in (None, ""):
            payload["price"] = str(price)
        if client_order_id:
            payload["clientOrderId"] = client_order_id
        if confirm_high_value_order:
            payload["confirmHighValueOrder"] = True
        if time_in_force:
            payload["timeInForce"] = time_in_force

        if self.dry_run:
            return {
                "dry_run": True,
                "endpoint": self.ep_orders,
                "accountSeq": acc,
                "payload": payload,
                "result": {
                    "orderId": f"dry_{uuid.uuid4().hex[:24]}",
                    "clientOrderId": client_order_id,
                },
            }

        return self._request("POST", self.ep_orders, account_seq=acc, json_body=payload)

    @staticmethod
    def _validate_modify_order(order_type: str, quantity: Optional[str], price: Optional[str]) -> None:
        order_type = order_type.upper()
        if order_type not in {"LIMIT", "MARKET"}:
            raise ValueError("orderType must be LIMIT or MARKET")

        if order_type == "LIMIT" and not price:
            raise ValueError("LIMIT modify requires price")

        if order_type == "MARKET" and price:
            raise ValueError("MARKET modify must not include price")

        if quantity is not None and not str(quantity).isdigit():
            raise ValueError("modify quantity must be positive integer string")

    def modify_order(
        self,
        *,
        account_seq: Optional[str],
        order_id: str,
        order_type: str,
        quantity: Optional[str] = None,
        price: Optional[str] = None,
        confirm_high_value_order: bool = False,
    ) -> Dict[str, Any]:
        if not order_id:
            raise ValueError("order_id is required")

        self._validate_modify_order(order_type, quantity, price)
        acc = account_seq or self.resolve_account_seq()

        payload: Dict[str, Any] = {
            "orderType": order_type.upper(),
        }

        if quantity not in (None, ""):
            payload["quantity"] = str(quantity)
        if price not in (None, ""):
            payload["price"] = str(price)
        if confirm_high_value_order:
            payload["confirmHighValueOrder"] = True

        path = f"{self.ep_orders.rstrip('/')}/{order_id}/modify"

        if self.dry_run:
            return {
                "dry_run": True,
                "endpoint": path,
                "accountSeq": acc,
                "payload": payload,
                "result": {
                    "orderId": f"dry_modify_{uuid.uuid4().hex[:24]}",
                },
            }

        return self._request("POST", path, account_seq=acc, json_body=payload)

    def cancel_order(
        self,
        *,
        account_seq: Optional[str],
        order_id: str,
    ) -> Dict[str, Any]:
        if not order_id:
            raise ValueError("order_id is required")

        acc = account_seq or self.resolve_account_seq()
        path = f"{self.ep_orders.rstrip('/')}/{order_id}/cancel"

        if self.dry_run:
            return {
                "dry_run": True,
                "endpoint": path,
                "accountSeq": acc,
                "payload": {},
                "result": {
                    "orderId": f"dry_cancel_{uuid.uuid4().hex[:24]}",
                },
            }

        return self._request("POST", path, account_seq=acc, json_body={})

    # ------------------------------------------------------------
    # Profit estimation helper
    # ------------------------------------------------------------

    @staticmethod
    def estimate_profit_fields(position: Dict[str, Any]) -> Dict[str, Any]:
        def pick(*names):
            for name in names:
                if name in position and position[name] not in (None, ""):
                    return position[name]
            return None

        quantity = pick("quantity", "qty", "holdingQuantity", "balanceQuantity")
        avg_price = pick("averagePrice", "avgPrice", "purchasePrice", "averagePurchasePrice")
        current_price = pick("currentPrice", "price", "marketPrice")
        purchase_amount = pick("purchaseAmount", "buyAmount", "totalPurchaseAmount")
        evaluation_amount = pick("evaluationAmount", "evalAmount", "marketValue")
        pnl = pick("profitAndLoss", "pnl", "valuationProfitLoss")
        profit_rate = pick("profitRate", "pnlRate", "returnRate")

        result = dict(position)

        try:
            if purchase_amount is None and quantity is not None and avg_price is not None:
                purchase_amount = float(quantity) * float(avg_price)
            if evaluation_amount is None and quantity is not None and current_price is not None:
                evaluation_amount = float(quantity) * float(current_price)
            if pnl is None and purchase_amount is not None and evaluation_amount is not None:
                pnl = float(evaluation_amount) - float(purchase_amount)
            if profit_rate is None and purchase_amount not in (None, 0, "0"):
                profit_rate = float(pnl) / float(purchase_amount) * 100.0 if pnl is not None else None
        except Exception:
            pass

        result["_estimated"] = {
            "quantity": quantity,
            "averagePrice": avg_price,
            "currentPrice": current_price,
            "purchaseAmount": purchase_amount,
            "evaluationAmount": evaluation_amount,
            "profitAndLoss": pnl,
            "profitRatePercent": profit_rate,
        }
        return result
