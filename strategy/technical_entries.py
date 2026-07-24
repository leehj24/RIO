import math
from typing import Dict, Any, List

import numpy as np
import pandas as pd


def _clip(v: float, low: float = -1.0, high: float = 1.0) -> float:
    return float(np.clip(float(v), low, high))


def _safe(v, default=0.0):
    try:
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def atr(df: pd.DataFrame, period: int = 10) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def supertrend_frame(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """
    실전 룰용 Supertrend series.
    direction: +1 상승, -1 하락
    line: 상승이면 final_lower, 하락이면 final_upper
    """
    data = df.copy().reset_index(drop=True)
    data["atr"] = atr(data, period)
    hl2 = (data["high"].astype(float) + data["low"].astype(float)) / 2.0
    basic_upper = hl2 + multiplier * data["atr"]
    basic_lower = hl2 - multiplier * data["atr"]

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    direction = pd.Series(index=data.index, dtype=float)
    line = pd.Series(index=data.index, dtype=float)

    for i in range(len(data)):
        if i == 0 or pd.isna(data.loc[i, "atr"]):
            direction.iloc[i] = 1
            line.iloc[i] = basic_lower.iloc[i]
            continue

        prev_close = float(data.loc[i - 1, "close"])
        prev_fu = final_upper.iloc[i - 1]
        prev_fl = final_lower.iloc[i - 1]

        if pd.isna(prev_fu):
            prev_fu = basic_upper.iloc[i]
        if pd.isna(prev_fl):
            prev_fl = basic_lower.iloc[i]

        if basic_upper.iloc[i] < prev_fu or prev_close > prev_fu:
            final_upper.iloc[i] = basic_upper.iloc[i]
        else:
            final_upper.iloc[i] = prev_fu

        if basic_lower.iloc[i] > prev_fl or prev_close < prev_fl:
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_lower.iloc[i] = prev_fl

        prev_dir = int(direction.iloc[i - 1]) if not pd.isna(direction.iloc[i - 1]) else 1
        close = float(data.loc[i, "close"])

        if prev_dir == -1 and close > final_upper.iloc[i]:
            cur_dir = 1
        elif prev_dir == 1 and close < final_lower.iloc[i]:
            cur_dir = -1
        else:
            cur_dir = prev_dir

        direction.iloc[i] = cur_dir
        line.iloc[i] = final_lower.iloc[i] if cur_dir == 1 else final_upper.iloc[i]

    data["st_upper"] = final_upper
    data["st_lower"] = final_lower
    data["st_direction"] = direction.fillna(1).astype(int)
    data["st_line"] = line
    return data


def bollinger_frame(df: pd.DataFrame, period: int = 20, std: float = 2.0) -> pd.DataFrame:
    data = df.copy().reset_index(drop=True)
    close = data["close"].astype(float)
    mid = close.rolling(period).mean()
    sigma = close.rolling(period).std(ddof=0)
    data["bb_mid"] = mid
    data["bb_upper"] = mid + std * sigma
    data["bb_lower"] = mid - std * sigma
    data["bb_width"] = (data["bb_upper"] - data["bb_lower"]) / mid.replace(0, np.nan)
    return data


def consecutive_age(series: pd.Series, value: int) -> int:
    age = 0
    for v in reversed(series.dropna().tolist()):
        if int(v) == int(value):
            age += 1
        else:
            break
    return age


def flip_count(series: pd.Series, lookback: int = 30) -> int:
    s = series.dropna().astype(int).tail(lookback)
    if len(s) < 2:
        return 0
    return int((s.diff().fillna(0) != 0).sum())


def count_flips_to(series: pd.Series, to_value: int, lookback: int = 30) -> int:
    s = series.dropna().astype(int).tail(lookback)
    if len(s) < 2:
        return 0
    prev = s.shift(1)
    return int(((prev != to_value) & (s == to_value)).sum())


def bullish_reversal(row) -> bool:
    high = _safe(row["high"])
    low = _safe(row["low"])
    open_ = _safe(row["open"])
    close = _safe(row["close"])
    rng = max(high - low, 1e-9)
    return close > open_ and (close - low) / rng >= 0.55


def bearish_reversal(row) -> bool:
    high = _safe(row["high"])
    low = _safe(row["low"])
    open_ = _safe(row["open"])
    close = _safe(row["close"])
    rng = max(high - low, 1e-9)
    return close < open_ and (high - close) / rng >= 0.55


def near(value: float, target: float, tol: float) -> bool:
    if any(pd.isna(x) for x in [value, target, tol]):
        return False
    return abs(float(value) - float(target)) <= abs(float(tol))


def double_bottom_top(df: pd.DataFrame, atr_value: float, lookback: int = 40) -> Dict[str, bool]:
    """
    아주 단순한 쌍바닥/쌍고점 감지.
    실제 차트 패턴 인식은 더 정교화 가능.
    """
    data = df.tail(lookback).reset_index(drop=True)
    if len(data) < 20:
        return {"double_bottom": False, "double_top": False}

    mid = len(data) // 2
    left_low_idx = int(data["low"].iloc[:mid].idxmin())
    right_low_idx = int(data["low"].iloc[mid:].idxmin())
    left_high_idx = int(data["high"].iloc[:mid].idxmax())
    right_high_idx = int(data["high"].iloc[mid:].idxmax())

    left_low = float(data.loc[left_low_idx, "low"])
    right_low = float(data.loc[right_low_idx, "low"])
    left_high = float(data.loc[left_high_idx, "high"])
    right_high = float(data.loc[right_high_idx, "high"])
    close = float(data["close"].iloc[-1])

    tol = max(atr_value * 0.8, close * 0.015)
    neckline = float(data["high"].iloc[min(left_low_idx, right_low_idx):max(left_low_idx, right_low_idx)+1].max())
    baseline = float(data["low"].iloc[min(left_high_idx, right_high_idx):max(left_high_idx, right_high_idx)+1].min())

    double_bottom = abs(left_low - right_low) <= tol and close > neckline
    double_top = abs(left_high - right_high) <= tol and close < baseline
    return {"double_bottom": bool(double_bottom), "double_top": bool(double_top)}




def round_bottom_top(df: pd.DataFrame, atr_value: float, lookback: int = 60) -> Dict[str, bool]:
    """
    라운드 바닥/라운드 탑 감지.

    Round bottom 조건:
    - 최근 lookback 가격을 2차식으로 근사했을 때 계수 a > 0
    - 저점이 중앙부에 위치
    - 왼쪽 평균 기울기 < 0, 오른쪽 평균 기울기 > 0
    - 종가가 중앙선/목선 위로 회복

    Round top은 반대.
    """
    data = df.tail(lookback).reset_index(drop=True)
    if len(data) < 30:
        return {"round_bottom": False, "round_top": False}

    close = data["close"].astype(float).to_numpy()
    high = data["high"].astype(float).to_numpy()
    low = data["low"].astype(float).to_numpy()

    if np.nanstd(close) <= 1e-9:
        return {"round_bottom": False, "round_top": False}

    x = np.linspace(-1.0, 1.0, len(close))
    y = (close - np.nanmean(close)) / max(np.nanstd(close), 1e-9)
    a, b, c = np.polyfit(x, y, 2)

    min_idx = int(np.nanargmin(close))
    max_idx = int(np.nanargmax(close))
    center_low = len(close) * 0.25 <= min_idx <= len(close) * 0.75
    center_high = len(close) * 0.25 <= max_idx <= len(close) * 0.75

    left = close[: len(close)//2]
    right = close[len(close)//2 :]

    left_slope = np.polyfit(np.arange(len(left)), left, 1)[0]
    right_slope = np.polyfit(np.arange(len(right)), right, 1)[0]

    current = close[-1]
    mid_ma = float(pd.Series(close).rolling(20).mean().iloc[-1])
    neckline_bottom = float(np.nanmax(high[max(0, min_idx - 5): min(len(high), min_idx + 20)]))
    neckline_top = float(np.nanmin(low[max(0, max_idx - 5): min(len(low), max_idx + 20)]))
    tol = max(float(atr_value) * 0.3, current * 0.005)

    round_bottom = (
        a > 0.15
        and center_low
        and left_slope < 0
        and right_slope > 0
        and current >= min(mid_ma, neckline_bottom + tol)
    )

    round_top = (
        a < -0.15
        and center_high
        and left_slope > 0
        and right_slope < 0
        and current <= max(mid_ma, neckline_top - tol)
    )

    return {"round_bottom": bool(round_bottom), "round_top": bool(round_top)}


def compute_technical_rules(df: pd.DataFrame, settings=None) -> Dict[str, Any]:
    """
    사용자가 말한 매매법을 실제 BUY/SELL/EXIT 신호로 분리한다.

    포함:
    - Supertrend 색상 전환
    - Supertrend retest
    - trend 유지 10캔들 조건
    - Double Trend fast/slow breakout
    - 횡보장 flip 3회 이상 감지
    - Dolpa-like EMA/ATR 채널, 중단선, 상/하단선
    - Bollinger Band 중단선 눌림/되돌림
    - 박스권 하단/상단 매매
    - 쌍바닥/쌍고점 단순 패턴
    - 반대 fast signal 2회 분할익절 신호
    """
    if df is None or len(df) < 60:
        return {
            "trend_score": 0.0,
            "buy_signal": False,
            "sell_signal": False,
            "exit_signal": False,
            "partial_exit_signal": False,
            "buy_triggers": [],
            "sell_triggers": [],
            "exit_triggers": ["insufficient_ohlcv"],
            "levels": {},
            "range_state": "unknown",
            "data_quality": "insufficient_ohlcv",
        }

    st_period = getattr(settings, "supertrend_period", 10)
    fast_mult = getattr(settings, "supertrend_fast_multiplier", 2.0)
    slow_mult = getattr(settings, "supertrend_slow_multiplier", 4.0)
    min_age = getattr(settings, "min_trend_age_candles", 10)
    retest_tol_mult = getattr(settings, "super_retest_atr_tolerance", 0.35)

    bb_period = getattr(settings, "bb_period", 20)
    bb_std = getattr(settings, "bb_std", 2.0)
    bb_tol_mult = getattr(settings, "bb_mid_tolerance_atr", 0.35)

    box_lookback = getattr(settings, "box_lookback", 30)
    box_edge_zone = getattr(settings, "box_edge_zone_fraction", 0.18)
    box_max_atr_width = getattr(settings, "box_max_atr_width", 6.0)

    fast = supertrend_frame(df, period=st_period, multiplier=fast_mult)
    slow = supertrend_frame(df, period=st_period, multiplier=slow_mult)
    bb = bollinger_frame(df, period=bb_period, std=bb_std)

    data = df.copy().reset_index(drop=True)
    data["atr"] = atr(data, st_period)
    data["ema_mid"] = data["close"].ewm(span=getattr(settings, "dolpa_ema_period", 34), adjust=False).mean()

    atr_now = _safe(data["atr"].iloc[-1], 0.0)
    close = _safe(data["close"].iloc[-1])
    low = _safe(data["low"].iloc[-1])
    high = _safe(data["high"].iloc[-1])
    last = data.iloc[-1]

    fast_dir = int(fast["st_direction"].iloc[-1])
    slow_dir = int(slow["st_direction"].iloc[-1])
    prev_fast_dir = int(fast["st_direction"].iloc[-2])
    prev_slow_dir = int(slow["st_direction"].iloc[-2])

    fast_line = _safe(fast["st_line"].iloc[-1])
    slow_line = _safe(slow["st_line"].iloc[-1])
    slow_age = consecutive_age(slow["st_direction"], slow_dir)
    fast_age = consecutive_age(fast["st_direction"], fast_dir)

    fast_flip_buy = prev_fast_dir == -1 and fast_dir == 1
    fast_flip_sell = prev_fast_dir == 1 and fast_dir == -1
    slow_flip_buy = prev_slow_dir == -1 and slow_dir == 1
    slow_flip_sell = prev_slow_dir == 1 and slow_dir == -1

    flips = flip_count(fast["st_direction"], lookback=box_lookback)
    is_choppy = (fast_dir != slow_dir) or (flips >= 3 and slow_age <= min_age)

    # 더블트렌드: fast/slow가 다른 구간은 횡보로 보고, slow까지 전환되며 둘이 일치하면 추세 시작.
    double_breakout_buy = (fast_dir == 1 and slow_dir == 1 and (prev_fast_dir != prev_slow_dir or slow_flip_buy))
    double_breakout_sell = (fast_dir == -1 and slow_dir == -1 and (prev_fast_dir != prev_slow_dir or slow_flip_sell))

    # 슈퍼트렌드 리테스트: 추세가 10캔들 이상 유지된 뒤 ST line 부근에서 지지/저항 캔들.
    retest_tol = atr_now * retest_tol_mult
    super_retest_buy = slow_dir == 1 and slow_age >= min_age and low <= slow_line + retest_tol and bullish_reversal(last)
    super_retest_sell = slow_dir == -1 and slow_age >= min_age and high >= slow_line - retest_tol and bearish_reversal(last)

    # Dolpa-like 채널: 비공개 돌파트렌드 정확 복제 아님. EMA 중단선 + ATR 채널 응용.
    dolpa_k = getattr(settings, "dolpa_atr_multiplier", 1.8)
    ema_mid = _safe(data["ema_mid"].iloc[-1])
    prev_close = _safe(data["close"].iloc[-2])
    prev_ema_mid = _safe(data["ema_mid"].iloc[-2])
    dolpa_upper = ema_mid + dolpa_k * atr_now
    dolpa_lower = ema_mid - dolpa_k * atr_now
    prev_upper = _safe(data["ema_mid"].iloc[-2]) + dolpa_k * _safe(data["atr"].iloc[-2])
    prev_lower = _safe(data["ema_mid"].iloc[-2]) - dolpa_k * _safe(data["atr"].iloc[-2])

    dolpa_color = "yellow" if close >= ema_mid else "blue"
    dolpa_arrow_buy = prev_close <= prev_upper and close > dolpa_upper
    dolpa_arrow_sell = prev_close >= prev_lower and close < dolpa_lower
    dolpa_mid_retest_buy = dolpa_color == "yellow" and slow_age >= min_age and near(low, ema_mid, atr_now * retest_tol_mult) and bullish_reversal(last)
    dolpa_mid_retest_sell = dolpa_color == "blue" and slow_age >= min_age and near(high, ema_mid, atr_now * retest_tol_mult) and bearish_reversal(last)
    dolpa_mid_stop_long = close < ema_mid and prev_close >= prev_ema_mid
    dolpa_mid_stop_short = close > ema_mid and prev_close <= prev_ema_mid

    # Bollinger 중단선 눌림/되돌림
    bb_mid = _safe(bb["bb_mid"].iloc[-1])
    bb_upper = _safe(bb["bb_upper"].iloc[-1])
    bb_lower = _safe(bb["bb_lower"].iloc[-1])
    bb_mid_prev = _safe(bb["bb_mid"].iloc[-2])
    bb_uptrend = close > bb_mid and bb_mid > bb_mid_prev
    bb_downtrend = close < bb_mid and bb_mid < bb_mid_prev
    bb_mid_pullback_buy = bb_uptrend and near(low, bb_mid, atr_now * bb_tol_mult) and bullish_reversal(last)
    bb_mid_pullback_sell = bb_downtrend and near(high, bb_mid, atr_now * bb_tol_mult) and bearish_reversal(last)

    # 박스권 하단/상단 매매
    box_data = data.tail(box_lookback)
    box_high = _safe(box_data["high"].max())
    box_low = _safe(box_data["low"].min())
    box_width = max(box_high - box_low, 1e-9)
    box_is_valid = box_width <= max(atr_now * box_max_atr_width, close * 0.10)
    lower_zone = box_low + box_width * box_edge_zone
    upper_zone = box_high - box_width * box_edge_zone

    box_lower_buy = box_is_valid and close <= lower_zone and bullish_reversal(last)
    box_upper_sell = box_is_valid and close >= upper_zone and bearish_reversal(last)
    box_breakout_up = box_is_valid and close > box_high
    box_breakout_down = box_is_valid and close < box_low

    # 단순 패턴
    pattern = double_bottom_top(data, atr_now)
    double_bottom = pattern["double_bottom"]
    double_top = pattern["double_top"]

    round_pattern = round_bottom_top(data, atr_now)
    round_bottom = round_pattern["round_bottom"]
    round_top = round_pattern["round_top"]

    # 추세선 중첩은 자동작도 대신 ST line과 EMA mid가 가까운 구간으로 근사.
    trendline_overlap_buy = slow_dir == 1 and near(slow_line, ema_mid, atr_now * 0.55) and bullish_reversal(last)
    trendline_overlap_sell = slow_dir == -1 and near(slow_line, ema_mid, atr_now * 0.55) and bearish_reversal(last)

    opposite_fast_sell_count = count_flips_to(fast["st_direction"], -1, lookback=30)
    opposite_fast_buy_count = count_flips_to(fast["st_direction"], 1, lookback=30)
    partial_exit_long = opposite_fast_sell_count >= 2
    partial_exit_short = opposite_fast_buy_count >= 2

    buy_triggers: List[str] = []
    sell_triggers: List[str] = []
    exit_triggers: List[str] = []

    checks = [
        ("supertrend_flip_buy", fast_flip_buy),
        ("double_trend_breakout_buy", double_breakout_buy),
        ("supertrend_retest_buy", super_retest_buy),
        ("dolpa_arrow_buy", dolpa_arrow_buy),
        ("dolpa_midline_retest_buy", dolpa_mid_retest_buy),
        ("bollinger_midline_pullback_buy", bb_mid_pullback_buy),
        ("box_lower_support_buy", box_lower_buy),
        ("double_bottom_pattern_buy", double_bottom),
        ("round_bottom_pattern_buy", round_bottom),
        ("trendline_supertrend_overlap_buy", trendline_overlap_buy),
    ]
    for name, ok in checks:
        if ok:
            buy_triggers.append(name)

    checks = [
        ("supertrend_flip_sell", fast_flip_sell),
        ("double_trend_breakout_sell", double_breakout_sell),
        ("supertrend_retest_sell", super_retest_sell),
        ("dolpa_arrow_sell", dolpa_arrow_sell),
        ("dolpa_midline_retest_sell", dolpa_mid_retest_sell),
        ("bollinger_midline_pullback_sell", bb_mid_pullback_sell),
        ("box_upper_rejection_sell", box_upper_sell),
        ("double_top_pattern_sell", double_top),
        ("round_top_pattern_sell", round_top),
        ("trendline_supertrend_overlap_sell", trendline_overlap_sell),
    ]
    for name, ok in checks:
        if ok:
            sell_triggers.append(name)

    if dolpa_mid_stop_long:
        exit_triggers.append("dolpa_midline_close_break_long")
    if dolpa_mid_stop_short:
        exit_triggers.append("dolpa_midline_close_break_short")
    if box_breakout_up:
        exit_triggers.append("box_breakout_up")
    if box_breakout_down:
        exit_triggers.append("box_breakout_down")
    if partial_exit_long:
        exit_triggers.append("fast_opposite_sell_signal_twice_partial_long")
    if partial_exit_short:
        exit_triggers.append("fast_opposite_buy_signal_twice_partial_short")

    # 점수화
    double_score = 1.0 if fast_dir == 1 and slow_dir == 1 else -1.0 if fast_dir == -1 and slow_dir == -1 else 0.0
    retest_score = 0.0
    if super_retest_buy or dolpa_mid_retest_buy or bb_mid_pullback_buy:
        retest_score += 0.55
    if super_retest_sell or dolpa_mid_retest_sell or bb_mid_pullback_sell:
        retest_score -= 0.55

    breakout_score = 0.0
    if double_breakout_buy or dolpa_arrow_buy or box_breakout_up:
        breakout_score += 0.50
    if double_breakout_sell or dolpa_arrow_sell or box_breakout_down:
        breakout_score -= 0.50

    pattern_score = 0.0
    if double_bottom:
        pattern_score += 0.20
    if double_top:
        pattern_score -= 0.20
    if round_bottom:
        pattern_score += 0.25
    if round_top:
        pattern_score -= 0.25
    chop_penalty = -0.25 if is_choppy else 0.0

    trend_score = _clip(0.45 * double_score + 0.25 * retest_score + 0.20 * breakout_score + 0.10 * pattern_score + chop_penalty)

    # 횡보장은 아무 매수 신호나 허용하지 않고, 박스권 하단/상단 룰 또는 double trend breakout만 허용.
    buy_signal = bool(
        double_breakout_buy
        or super_retest_buy
        or dolpa_arrow_buy
        or dolpa_mid_retest_buy
        or bb_mid_pullback_buy
        or box_lower_buy
        or double_bottom
        or round_bottom
        or trendline_overlap_buy
    )
    sell_signal = bool(
        double_breakout_sell
        or super_retest_sell
        or dolpa_arrow_sell
        or dolpa_mid_retest_sell
        or bb_mid_pullback_sell
        or box_upper_sell
        or double_top
        or round_top
        or trendline_overlap_sell
    )

    if is_choppy and not (box_lower_buy or box_upper_sell or double_breakout_buy or double_breakout_sell):
        buy_signal = False
        sell_signal = False

    entry_price = close
    long_stop = min(slow_line if slow_dir == 1 else dolpa_lower, dolpa_lower, bb_lower if bb_lower else dolpa_lower)
    short_stop = max(slow_line if slow_dir == -1 else dolpa_upper, dolpa_upper, bb_upper if bb_upper else dolpa_upper)
    rr = getattr(settings, "rr_target_multiple", 2.0)

    long_risk = max(entry_price - long_stop, atr_now * 0.3, 1e-9)
    short_risk = max(short_stop - entry_price, atr_now * 0.3, 1e-9)

    return {
        "trend_score": float(trend_score),
        "buy_signal": bool(buy_signal),
        "sell_signal": bool(sell_signal),
        "exit_signal": bool(len(exit_triggers) > 0),
        "partial_exit_signal": bool(partial_exit_long or partial_exit_short),
        "buy_triggers": buy_triggers,
        "sell_triggers": sell_triggers,
        "exit_triggers": exit_triggers,
        "range_state": "choppy_or_box" if is_choppy else "trending",
        "data_quality": "ok",
        "states": {
            "fast_dir": fast_dir,
            "slow_dir": slow_dir,
            "fast_age": fast_age,
            "slow_age": slow_age,
            "flip_count": flips,
            "dolpa_color": dolpa_color,
            "box_is_valid": bool(box_is_valid),
            "round_bottom": bool(round_bottom),
            "round_top": bool(round_top),
        },
        "levels": {
            "fast_supertrend_line": float(fast_line),
            "slow_supertrend_line": float(slow_line),
            "dolpa_midline": float(ema_mid),
            "dolpa_upper": float(dolpa_upper),
            "dolpa_lower": float(dolpa_lower),
            "bb_mid": float(bb_mid),
            "bb_upper": float(bb_upper),
            "bb_lower": float(bb_lower),
            "box_high": float(box_high),
            "box_low": float(box_low),
            "long_stop": float(long_stop),
            "long_take_profit_rr": float(entry_price + rr * long_risk),
            "short_stop": float(short_stop),
            "short_take_profit_rr": float(entry_price - rr * short_risk),
        },
    }
