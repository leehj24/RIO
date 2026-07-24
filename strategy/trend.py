import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, period: int = 10) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
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


def supertrend_direction(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> int:
    if len(df) < period + 5:
        return 0

    data = df.copy()
    data["atr"] = atr(data, period)
    hl2 = (data["high"] + data["low"]) / 2
    data["upper"] = hl2 + multiplier * data["atr"]
    data["lower"] = hl2 - multiplier * data["atr"]

    c = data["close"].iloc[-1]
    upper = data["upper"].iloc[-1]
    lower = data["lower"].iloc[-1]

    if np.isnan(upper) or np.isnan(lower):
        return 0

    if c > upper:
        return 1
    if c < lower:
        return -1

    mid = (upper + lower) / 2
    return 1 if c >= mid else -1


def bolpa_like_score(df: pd.DataFrame, ema_period: int = 34, atr_period: int = 14, k: float = 1.8) -> float:
    """
    돌파트렌드 비공개 지표 복제가 아니라
    EMA 중심선 + ATR 채널 + 리테스트 구조.
    """
    if len(df) < max(ema_period, atr_period) + 5:
        return 0.0

    data = df.copy()
    data["ema"] = data["close"].ewm(span=ema_period, adjust=False).mean()
    data["atr"] = atr(data, atr_period)
    data["upper"] = data["ema"] + k * data["atr"]
    data["lower"] = data["ema"] - k * data["atr"]

    c = data["close"].iloc[-1]
    prev_c = data["close"].iloc[-2]
    ema = data["ema"].iloc[-1]
    upper = data["upper"].iloc[-1]
    lower = data["lower"].iloc[-1]

    if np.isnan(upper) or np.isnan(lower):
        return 0.0

    if c > upper:
        return 1.0
    if c < lower:
        return -1.0

    if prev_c < ema and c > ema:
        return 0.50
    if prev_c > ema and c < ema:
        return -0.50

    if c > ema:
        return 0.25
    if c < ema:
        return -0.25

    return 0.0


def compute_trend_score(ohlcv: pd.DataFrame) -> float:
    fast = supertrend_direction(ohlcv, period=10, multiplier=2.0)
    slow = supertrend_direction(ohlcv, period=10, multiplier=4.0)

    if fast == 1 and slow == 1:
        double_score = 1.0
    elif fast == -1 and slow == -1:
        double_score = -1.0
    else:
        double_score = 0.0

    bolpa = bolpa_like_score(ohlcv)
    return float(np.clip(0.55 * double_score + 0.45 * bolpa, -1.0, 1.0))
