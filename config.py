import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw in (None, "") else float(raw)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw in (None, "") else int(raw)


def env_first(*names: str, default: str = "") -> str:
    """Return the first configured, non-empty environment variable.

    ``Gemini_Api2`` already exists in this project's .env file.  New code
    should use the conventional upper-case name (``GEMINI_API2_KEY``), but
    keeping this small compatibility layer means the existing key remains
    usable without copying a secret into another variable.
    """
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


@dataclass
class Settings:
    dry_run: bool = env_bool("DRY_RUN", True)
    loop_seconds: int = env_int("LOOP_SECONDS", 600)

    # aggressive bankroll/risk
    max_managed_bankroll_krw: float = env_float("MAX_MANAGED_BANKROLL_KRW", 300000)
    max_position_fraction: float = env_float("MAX_POSITION_FRACTION", 0.06)
    max_total_exposure_fraction: float = env_float("MAX_TOTAL_EXPOSURE_FRACTION", 0.80)
    max_open_positions: int = env_int("MAX_OPEN_POSITIONS", 12)

    min_event_edge: float = env_float("MIN_EVENT_EDGE", 0.08)
    # LMSR 내부가격이 LLM 확률로 수렴하는 속도. 클수록 event_edge가 빨리
    # 0으로 붕괴해 신규 매수 신호가 사라진다.
    lmsr_update_alpha: float = env_float("LMSR_UPDATE_ALPHA", 0.15)
    min_final_alpha: float = env_float("MIN_FINAL_ALPHA", 0.55)
    min_sde_prob_up: float = env_float("MIN_SDE_PROB_UP", 0.58)
    min_trend_score: float = env_float("MIN_TREND_SCORE", 0.20)
    kelly_multiplier: float = env_float("KELLY_MULTIPLIER", 1.0)

    max_daily_loss_pct: float = env_float("MAX_DAILY_LOSS_PCT", 0.05)
    max_total_drawdown_pct: float = env_float("MAX_TOTAL_DRAWDOWN_PCT", 0.15)
    max_api_errors: int = env_int("MAX_API_ERRORS", 5)
    min_llm_confidence: float = env_float("MIN_LLM_CONFIDENCE", 0.55)

    # llm
    llm_provider: str = os.getenv("LLM_PROVIDER", "mock")
    nvidia_api_key: str = os.getenv("NVIDIA_API_KEY", "")
    nvidia_base_url: str = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    nvidia_model: str = os.getenv("NVIDIA_MODEL", "deepseek-ai/deepseek-r1")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # Dedicated Gemini account/key for the multi-agent workflow.  Prefer the
    # conventional variables below; ``Gemini_Api2`` is the backwards-compatible
    # name already present in the local .env.
    # Do not introduce new paid model calls or influence an order path merely
    # because a compatible key already exists in .env.  Enable explicitly
    # after dry-run verification.
    enable_gemini_api2: bool = env_bool("ENABLE_GEMINI_API2", False)
    gemini_api2_key: str = env_first(
        "GEMINI_API2_KEY",
        "GEMINI_API2_API_KEY",
        "Gemini_Api2",
    )
    gemini_api2_model: str = env_first(
        "GEMINI_API2_MODEL",
        "GEMINI_MODEL",
        default="gemini-2.5-flash",
    )
    gemini_api2_daily_call_limit: int = env_int("GEMINI_API2_DAILY_CALL_LIMIT", 999999999999999999999999999999^9999999999999999999999999999999999999999999)
    # The free Gemini tier used by this project is commonly limited by requests
    # per minute long before the daily budget.  Pace turns rather than firing a
    # multi-agent chain into a predictable 429 response.
    gemini_api2_min_request_interval_seconds: float = env_float(
        "GEMINI_API2_MIN_REQUEST_INTERVAL_SECONDS", 12.5
    )
    gemini_api2_max_retries: int = env_int("GEMINI_API2_MAX_RETRIES", 1)
    gemini_api2_timeout_seconds: int = env_int("GEMINI_API2_TIMEOUT_SECONDS", 60)
    gemini_api2_temperature: float = env_float("GEMINI_API2_TEMPERATURE", 0.1)
    gemini_api2_max_output_tokens: int = env_int("GEMINI_API2_MAX_OUTPUT_TOKENS", 2048)
    gemini_api2_max_agents_per_run: int = env_int("GEMINI_API2_MAX_AGENTS_PER_RUN", 20)
    # A live run uses a compact pipeline containing the final proposal and both
    # risk reviewers.  The full research pipeline remains available for
    # slower/offline analysis.
    gemini_api2_pipeline: str = os.getenv("GEMINI_API2_PIPELINE", "live_event_research")
    gemini_api2_memory_top_k: int = env_int("GEMINI_API2_MEMORY_TOP_K", 5)
    gemini_api2_prompt_root: str = os.getenv("GEMINI_API2_PROMPT_ROOT", "agent")
    gemini_api2_audit_path: str = os.getenv("GEMINI_API2_AUDIT_PATH", "data/agentic/audit.jsonl")
    gemini_api2_memory_path: str = os.getenv("GEMINI_API2_MEMORY_PATH", "data/agentic/memory.jsonl")
    gemini_api2_hypergraph_path: str = os.getenv("GEMINI_API2_HYPERGRAPH_PATH", "data_cache/agentic/hyperedges.jsonl")
    gemini_api2_teacher_labels_path: str = os.getenv("GEMINI_API2_TEACHER_LABELS_PATH", "data_cache/agentic/teacher_labels.jsonl")
    gemini_api2_enable_response_schema: bool = env_bool("GEMINI_API2_ENABLE_RESPONSE_SCHEMA", False)
    # JSON structured output and Google Search grounding are only jointly
    # supported on selected Gemini models.  Individual research agents can
    # explicitly opt in once their configured model supports that combination.
    gemini_api2_enable_grounding: bool = env_bool("ENABLE_GEMINI_API2_GROUNDING", False)

    # The agent workflow uses three role-specific model groups on NVIDIA's
    # OpenAI-compatible endpoint. Each group has its own dedicated key/model;
    # generic NVIDIA_API_KEY/NVIDIA_MODEL remain reserved for the legacy path.
    enable_nvidia_nemotron_agents: bool = env_bool("ENABLE_NVIDIA_NEMOTRON_AGENTS", False)
    nvidia_nemotron_api_key: str = os.getenv("NVIDIA_NEMOTRON_API_KEY", "")
    nvidia_nemotron_base_url: str = os.getenv(
        "NVIDIA_NEMOTRON_BASE_URL", os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    )
    nvidia_nemotron_model: str = os.getenv("NVIDIA_NEMOTRON_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
    nvidia_nemotron_daily_call_limit: int = env_int("NVIDIA_NEMOTRON_DAILY_CALL_LIMIT", 30)
    nvidia_nemotron_timeout_seconds: int = env_int("NVIDIA_NEMOTRON_TIMEOUT_SECONDS", 180)
    nvidia_nemotron_temperature: float = env_float("NVIDIA_NEMOTRON_TEMPERATURE", 1.0)
    nvidia_nemotron_top_p: float = env_float("NVIDIA_NEMOTRON_TOP_P", 0.95)
    nvidia_nemotron_max_output_tokens: int = env_int("NVIDIA_NEMOTRON_MAX_OUTPUT_TOKENS", 16384)
    nvidia_nemotron_reasoning_budget: int = env_int("NVIDIA_NEMOTRON_REASONING_BUDGET", 16384)
    nvidia_nemotron_enable_thinking: bool = env_bool("NVIDIA_NEMOTRON_ENABLE_THINKING", True)
    nvidia_nemotron_stream: bool = env_bool("NVIDIA_NEMOTRON_STREAM", True)
    nvidia_nemotron_enable_response_schema: bool = False
    nvidia_nemotron_enable_grounding: bool = False
    nvidia_nemotron_min_request_interval_seconds: float = env_float(
        "NVIDIA_NEMOTRON_MIN_REQUEST_INTERVAL_SECONDS", 0.0
    )
    nvidia_nemotron_max_retries: int = env_int("NVIDIA_NEMOTRON_MAX_RETRIES", 0)
    nvidia_nemotron_max_agents_per_run: int = env_int("NVIDIA_NEMOTRON_MAX_AGENTS_PER_RUN", 20)
    nvidia_nemotron_pipeline: str = os.getenv("NVIDIA_NEMOTRON_PIPELINE", "live_event_research")
    nvidia_nemotron_memory_top_k: int = env_int("NVIDIA_NEMOTRON_MEMORY_TOP_K", 5)
    nvidia_nemotron_prompt_root: str = os.getenv("NVIDIA_NEMOTRON_PROMPT_ROOT", "agent")
    nvidia_nemotron_audit_path: str = os.getenv("NVIDIA_NEMOTRON_AUDIT_PATH", "data/agentic/audit.jsonl")
    nvidia_nemotron_memory_path: str = os.getenv("NVIDIA_NEMOTRON_MEMORY_PATH", "data/agentic/memory.jsonl")
    nvidia_nemotron_hypergraph_path: str = os.getenv(
        "NVIDIA_NEMOTRON_HYPERGRAPH_PATH", "data_cache/agentic/hyperedges.jsonl"
    )
    nvidia_nemotron_teacher_labels_path: str = os.getenv(
        "NVIDIA_NEMOTRON_TEACHER_LABELS_PATH", "data_cache/agentic/teacher_labels.jsonl"
    )

    # NVIDIA Super (Analysis Agents tier)
    nvidia_super_api_key: str = os.getenv("NVIDIA_SUPER_API_KEY", "")
    nvidia_super_base_url: str = os.getenv(
        "NVIDIA_SUPER_BASE_URL", os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    )
    nvidia_super_model: str = os.getenv("NVIDIA_SUPER_MODEL", "nvidia/nemotron-3-super-120b-a12b")
    nvidia_super_temperature: float = env_float("NVIDIA_SUPER_TEMPERATURE", 1.0)
    nvidia_super_top_p: float = env_float("NVIDIA_SUPER_TOP_P", 0.95)
    nvidia_super_max_output_tokens: int = env_int("NVIDIA_SUPER_MAX_OUTPUT_TOKENS", 16384)
    nvidia_super_reasoning_budget: int = env_int("NVIDIA_SUPER_REASONING_BUDGET", 16384)
    nvidia_super_enable_thinking: bool = env_bool("NVIDIA_SUPER_ENABLE_THINKING", True)
    nvidia_super_stream: bool = env_bool("NVIDIA_SUPER_STREAM", True)

    # NVIDIA-hosted independent final-comparison model (GLM by default).
    nvidia_final_api_key: str = os.getenv("NVIDIA_FINAL_API_KEY", "")
    nvidia_final_base_url: str = os.getenv(
        "NVIDIA_FINAL_BASE_URL", os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    )
    nvidia_final_model: str = os.getenv("NVIDIA_FINAL_MODEL", "z-ai/glm-5.2")
    nvidia_final_temperature: float = env_float("NVIDIA_FINAL_TEMPERATURE", 1.0)
    nvidia_final_top_p: float = env_float("NVIDIA_FINAL_TOP_P", 1.0)
    nvidia_final_max_output_tokens: int = env_int("NVIDIA_FINAL_MAX_OUTPUT_TOKENS", 16384)
    nvidia_final_seed: int = env_int("NVIDIA_FINAL_SEED", 42)
    nvidia_final_stream: bool = env_bool("NVIDIA_FINAL_STREAM", True)

    # Dual LLM API control
    enable_google_api: bool = env_bool("ENABLE_GOOGLE_API", True)
    enable_nvidia_api: bool = env_bool("ENABLE_NVIDIA_API", True)
    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")
    google_daily_call_limit: int = env_int("GOOGLE_DAILY_CALL_LIMIT", 10)
    nvidia_daily_call_limit: int = env_int("NVIDIA_DAILY_CALL_LIMIT", 10)
    enable_google_grounding: bool = env_bool("ENABLE_GOOGLE_GROUNDING", True)
    api_usage_path: str = os.getenv("API_USAGE_PATH", "api_usage_state.json")
    llm_log_path: str = os.getenv("LLM_LOG_PATH", "llm_calls.jsonl")

    # toss auth
    toss_client_id: str = os.getenv("TOSS_CLIENT_ID", "")
    toss_client_secret: str = os.getenv("TOSS_CLIENT_SECRET", "")
    toss_token_url: str = os.getenv("TOSS_TOKEN_URL", "https://openapi.tossinvest.com/oauth2/token")
    toss_write_token_to_env: bool = env_bool("TOSS_WRITE_TOKEN_TO_ENV", True)
    toss_auto_account: bool = env_bool("TOSS_AUTO_ACCOUNT", True)

    # toss
    toss_base_url: str = os.getenv("TOSS_BASE_URL", "https://openapi.tossinvest.com")
    toss_access_token: str = os.getenv("TOSS_ACCESS_TOKEN", "")
    toss_account_seq: str = os.getenv("TOSS_ACCOUNT_SEQ", "")
    toss_rate_limit_safety_factor: float = env_float("TOSS_RATE_LIMIT_SAFETY_FACTOR", 0.80)
    toss_max_get_retries: int = env_int("TOSS_MAX_GET_RETRIES", 2)
    toss_retry_backoff_max_seconds: float = env_float("TOSS_RETRY_BACKOFF_MAX_SECONDS", 15.0)
    toss_candle_cache_dir: str = os.getenv("TOSS_CANDLE_CACHE_DIR", "data_cache/toss_candles")
    # Daily-bar indicators do not need a second full-history download every
    # loop.  The current price is nevertheless refreshed immediately before a
    # live order.
    toss_candle_cache_ttl_seconds: int = env_int("TOSS_CANDLE_CACHE_TTL_SECONDS", 900)

    enable_dashboard: bool = env_bool("ENABLE_DASHBOARD", True)
    dashboard_host: str = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    dashboard_port: int = env_int("DASHBOARD_PORT", 5000)
    dashboard_open_browser: bool = env_bool("DASHBOARD_OPEN_BROWSER", True)

    rotate_event_questions: bool = env_bool("ROTATE_EVENT_QUESTIONS", True)
    # Country events include broker-verified ETFs and ADRs.  Keep the default
    # above that universe so an arbitrary first 50 does not silently become
    # the only automatic-trading candidates.
    max_symbols_per_event: int = env_int("MAX_SYMBOLS_PER_EVENT", 200)

    # Point-in-time market packet for the Gemini/Python research boundary.
    # These are project defaults recorded in each packet, not undocumented
    # paper parameters.
    # Toss returns a maximum of 200 bars per page; the client follows its
    # ``nextBefore`` cursor when more history is requested.
    research_ohlcv_bars: int = env_int("RESEARCH_OHLCV_BARS", 260)
    research_rsi_period: int = env_int("RESEARCH_RSI_PERIOD", 20)
    research_sma_short_period: int = env_int("RESEARCH_SMA_SHORT_PERIOD", 50)
    research_sma_long_period: int = env_int("RESEARCH_SMA_LONG_PERIOD", 200)
    research_macd_fast_period: int = env_int("RESEARCH_MACD_FAST_PERIOD", 12)
    research_macd_slow_period: int = env_int("RESEARCH_MACD_SLOW_PERIOD", 26)
    research_macd_signal_period: int = env_int("RESEARCH_MACD_SIGNAL_PERIOD", 9)
    research_vwma_period: int = env_int("RESEARCH_VWMA_PERIOD", 20)
    research_cci_period: int = env_int("RESEARCH_CCI_PERIOD", 20)
    research_adx_period: int = env_int("RESEARCH_ADX_PERIOD", 14)
    sde_horizon_days: int = env_int("SDE_HORIZON_DAYS", 20)
    sde_target_return: float = env_float("SDE_TARGET_RETURN", 0.0)
    forecast_ledger_path: str = os.getenv("FORECAST_LEDGER_PATH", "data/forecast_ledger.jsonl")
    order_lifecycle_ledger_path: str = os.getenv("ORDER_LIFECYCLE_LEDGER_PATH", "data/order_lifecycle.jsonl")

    # Statistical lead-lag research is opt-in.  It creates candidates for the
    # semantic risk filter and is never an order generator by itself.
    enable_lead_lag_research: bool = env_bool("ENABLE_LEAD_LAG_RESEARCH", False)
    lead_lag_max_lag: int = env_int("LEAD_LAG_MAX_LAG", 3)
    lead_lag_fdr_alpha: float = env_float("LEAD_LAG_FDR_ALPHA", 0.05)
    lead_lag_minimum_observations: int = env_int("LEAD_LAG_MINIMUM_OBSERVATIONS", 80)
    lead_lag_max_symbols: int = env_int("LEAD_LAG_MAX_SYMBOLS", 20)
    lead_lag_max_candidates: int = env_int("LEAD_LAG_MAX_CANDIDATES", 30)

    # small-account / order allocation
    cash_reserve_fraction: float = env_float("CASH_RESERVE_FRACTION", 0.15)
    min_order_krw: float = env_float("MIN_ORDER_KRW", 10000)
    min_order_usd: float = env_float("MIN_ORDER_USD", 5)
    usd_krw_fx_rate: float = env_float("USD_KRW_FX_RATE", 1350)
    max_new_buys_per_loop: int = env_int("MAX_NEW_BUYS_PER_LOOP", 2)
    # Hard live-buy caps. These bound the portfolio exposure even when a
    # model produces several valid BUY signals in successive bot cycles.
    auto_buy_total_cap_krw: float = env_float("AUTO_BUY_TOTAL_CAP_KRW", 30000)
    auto_buy_per_symbol_cap_krw: float = env_float("AUTO_BUY_PER_SYMBOL_CAP_KRW", 15000)
    max_live_price_move_pct: float = env_float("MAX_LIVE_PRICE_MOVE_PCT", 0.02)
    # Market orders are only submitted after a fresh order-book check.  These
    # are intentionally conservative hard caps, not alpha-model inputs.
    max_live_spread_bps: float = env_float("MAX_LIVE_SPREAD_BPS", 100.0)
    max_live_book_impact_bps: float = env_float("MAX_LIVE_BOOK_IMPACT_BPS", 50.0)
    max_live_orderbook_age_seconds: float = env_float("MAX_LIVE_ORDERBOOK_AGE_SECONDS", 15.0)
    live_llm_candidate_limit: int = env_int("LIVE_LLM_CANDIDATE_LIMIT", 20)
    # Live LLM reports are research inputs, not a high-frequency order feed.
    # A valid report is linked to its original audit run and expires explicitly.
    live_llm_decision_ttl_seconds: int = env_int("LIVE_LLM_DECISION_TTL_SECONDS", 3600)

    # v7 live data strictness
    require_real_data_for_live: bool = env_bool("REQUIRE_REAL_DATA_FOR_LIVE", True)
    block_live_on_price_fallback: bool = env_bool("BLOCK_LIVE_ON_PRICE_FALLBACK", True)
    block_live_on_ohlcv_fallback: bool = env_bool("BLOCK_LIVE_ON_OHLCV_FALLBACK", True)
    require_order_status_after_live_order: bool = env_bool("REQUIRE_ORDER_STATUS_AFTER_LIVE_ORDER", True)
    enable_market_status_check: bool = env_bool("ENABLE_MARKET_STATUS_CHECK", True)
    enable_stock_tradability_check: bool = env_bool("ENABLE_STOCK_TRADABILITY_CHECK", True)

    # v8 free financial data sources
    enable_free_financial_data: bool = env_bool("ENABLE_FREE_FINANCIAL_DATA", True)
    enable_yfinance: bool = env_bool("ENABLE_YFINANCE", True)
    enable_sec_edgar: bool = env_bool("ENABLE_SEC_EDGAR", True)
    enable_naver_finance: bool = env_bool("ENABLE_NAVER_FINANCE", True)
    sec_user_agent: str = os.getenv("SEC_USER_AGENT", "personal-research-bot email@example.com")
    sec_cache_dir: str = os.getenv("SEC_CACHE_DIR", "data_cache/sec")
    naver_cache_dir: str = os.getenv("NAVER_CACHE_DIR", "data_cache/naver")

    # v9 actual integrations that were previously partial
    enable_sapience_price: bool = env_bool("ENABLE_SAPIENCE_PRICE", False)
    sapience_base_url: str = os.getenv("SAPIENCE_BASE_URL", "")
    sapience_price_endpoint: str = os.getenv("SAPIENCE_PRICE_ENDPOINT", "/markets/{event_id}")
    sapience_api_key: str = os.getenv("SAPIENCE_API_KEY", "")

    enable_opendart: bool = env_bool("ENABLE_OPENDART", True)
    open_dart_api_key: str = os.getenv("OPEN_DART_API_KEY", "")
    opendart_cache_dir: str = os.getenv("OPENDART_CACHE_DIR", "data_cache/opendart")

    enable_pykrx: bool = env_bool("ENABLE_PYKRX", True)
    pykrx_cache_dir: str = os.getenv("PYKRX_CACHE_DIR", "data_cache/pykrx")

    # 네이버 검색 API (뉴스=사실 채널, 블로그=주관 채널 evidence)
    enable_naver_search: bool = env_bool("ENABLE_NAVER_SEARCH", True)
    naver_client_id: str = os.getenv("NAVER_CLIENT_ID", "")
    naver_client_secret: str = os.getenv("NAVER_CLIENT_SECRET", "")
    naver_search_daily_call_limit: int = env_int("NAVER_SEARCH_DAILY_CALL_LIMIT", 300)
    naver_search_cache_ttl_seconds: int = env_int("NAVER_SEARCH_CACHE_TTL_SECONDS", 1800)
    naver_search_cache_dir: str = os.getenv("NAVER_SEARCH_CACHE_DIR", "data_cache/naver_search")

    # technical entry/exit rules
    use_technical_entry_rules: bool = env_bool("USE_TECHNICAL_ENTRY_RULES", True)
    supertrend_period: int = env_int("SUPERTREND_PERIOD", 10)
    supertrend_fast_multiplier: float = env_float("SUPERTREND_FAST_MULTIPLIER", 2.0)
    supertrend_slow_multiplier: float = env_float("SUPERTREND_SLOW_MULTIPLIER", 4.0)
    min_trend_age_candles: int = env_int("MIN_TREND_AGE_CANDLES", 10)
    super_retest_atr_tolerance: float = env_float("SUPER_RETEST_ATR_TOLERANCE", 0.35)

    dolpa_ema_period: int = env_int("DOLPA_EMA_PERIOD", 34)
    dolpa_atr_multiplier: float = env_float("DOLPA_ATR_MULTIPLIER", 1.8)
    bb_period: int = env_int("BB_PERIOD", 20)
    bb_std: float = env_float("BB_STD", 2.0)
    bb_mid_tolerance_atr: float = env_float("BB_MID_TOLERANCE_ATR", 0.35)

    box_lookback: int = env_int("BOX_LOOKBACK", 30)
    box_max_atr_width: float = env_float("BOX_MAX_ATR_WIDTH", 6.0)
    box_edge_zone_fraction: float = env_float("BOX_EDGE_ZONE_FRACTION", 0.18)

    # exit rules
    stop_loss_pct: float = env_float("STOP_LOSS_PCT", 0.045)
    take_profit_pct: float = env_float("TAKE_PROFIT_PCT", 0.10)
    trailing_stop_pct: float = env_float("TRAILING_STOP_PCT", 0.04)
    trailing_activation_pct: float = env_float("TRAILING_ACTIVATION_PCT", 0.06)
    rr_target_multiple: float = env_float("RR_TARGET_MULTIPLE", 2.0)
    partial_take_profit_fraction: float = env_float("PARTIAL_TAKE_PROFIT_FRACTION", 0.50)
    sell_if_final_alpha_below: float = env_float("SELL_IF_FINAL_ALPHA_BELOW", 0.10)
    sell_if_event_edge_below: float = env_float("SELL_IF_EVENT_EDGE_BELOW", -0.05)
    sell_if_trend_score_below: float = env_float("SELL_IF_TREND_SCORE_BELOW", -0.30)
    sell_if_sde_prob_up_below: float = env_float("SELL_IF_SDE_PROB_UP_BELOW", 0.45)

    state_path: str = os.getenv("STATE_PATH", "state.json")
    log_path: str = os.getenv("TRADE_LOG_PATH", "trades.jsonl")
