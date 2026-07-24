import numpy as np


def prediction_market_kelly_yes(p_yes: float, yes_price: float) -> float:
    """
    예측시장형 Kelly.

    YES를 가격 C에 산다고 가정:
    - 사건 발생 시 1로 정산
    - 사건 미발생 시 0
    - LLM 추정확률 p

    Kelly_YES = (p - C) / (1 - C)
    """
    p = float(np.clip(p_yes, 0.0, 1.0))
    c = float(np.clip(yes_price, 0.001, 0.999))
    return (p - c) / (1.0 - c)


def prediction_market_kelly_no(p_yes: float, yes_price: float) -> float:
    """
    NO side Kelly.
    Kelly_NO = (C - p) / C
    """
    p = float(np.clip(p_yes, 0.0, 1.0))
    c = float(np.clip(yes_price, 0.001, 0.999))
    return (c - p) / c


def aggressive_position_fraction(kelly: float, max_position_fraction: float, multiplier: float = 1.0) -> float:
    """
    공격형:
    - full Kelly 성향
    - 그래도 단일 포지션 cap 적용
    """
    return float(np.clip(kelly * multiplier, 0.0, max_position_fraction))
