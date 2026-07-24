# v8 계산식 요약

## 운용금
```text
effective_bankroll = min(toss_buying_power, MAX_MANAGED_BANKROLL_KRW)
deployable_bankroll = effective_bankroll × (1 - CASH_RESERVE_FRACTION)
```

## LMSR
```text
C(q) = b × ln(exp(q_yes / b) + exp(q_no / b))
P_yes = exp(q_yes / b) / (exp(q_yes / b) + exp(q_no / b))
EventEdge = P_LLM - P_LMSR
```

## Kelly
```text
Kelly_YES = (p - C) / (1 - C)
Kelly_NO = (C - p) / C
PositionFraction = min(max(Kelly_YES × KELLY_MULTIPLIER, 0), MAX_POSITION_FRACTION)
OrderValue = deployable_bankroll × PositionFraction × LLM_Confidence
```

## Factor Score
```text
FactorScore = 0.16×Value + 0.20×Momentum + 0.16×Quality + 0.12×Risk + 0.12×Liquidity + 0.14×GrowthRevision + 0.10×NewsEvent
```

## SDE/GBM
```text
dS / S = μdt + σdW
μ = base_mu + 0.12×FactorScore + 0.15×EventEdge + 0.10×TrendScore + 0.06×NewsEventScore
σ = HistoricalVol × DisclosureMultiplier × NewsMultiplier × RiskMultiplier
SDEProbUp = P(S_T > S_0 × (1 + target_return))
SDEProbScore = 2 × (SDEProbUp - 0.5)
```

## Technical
```text
HL2=(High+Low)/2
TR=max(High-Low, |High-PrevClose|, |Low-PrevClose|)
ATR=rolling_mean(TR,N)
Supertrend Upper=HL2+mult×ATR
Supertrend Lower=HL2-mult×ATR
Dolpa-like Mid=EMA(Close,34), Upper=Mid+1.8×ATR, Lower=Mid-1.8×ATR
Bollinger Mid=SMA(Close,20), Upper=Mid+2σ, Lower=Mid-2σ
```

## FinalAlpha
```text
FinalAlpha = 0.25×FactorScore + 0.25×EventEdge + 0.20×SDEProbScore + 0.20×TrendScore + 0.10×NewsEventScore - 0.10×RiskPenalty
```

## BUY
```text
EventEdge>=MIN_EVENT_EDGE AND Kelly_YES>0 AND FinalAlpha>=MIN_FINAL_ALPHA AND SDEProbUp>=MIN_SDE_PROB_UP AND TrendScore>=MIN_TREND_SCORE AND TechnicalBuySignal AND SurvivalGate
```

## SELL
```text
PnL=(CurrentPrice-AvgPrice)/AvgPrice
SELL if PnL<=-STOP_LOSS_PCT or PnL>=TAKE_PROFIT_PCT or EventEdge<0 or FinalAlpha low or Trend/SDE low or TechnicalSellSignal
PARTIAL_SELL if PnL>0 and FastOppositeSignalCount>=2
```
