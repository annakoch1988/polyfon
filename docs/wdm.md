# WDM — Window Delta Momentum

## Description

Enters at T-10s when spot price displacement from window open exceeds a threshold. At that horizon, the outcome is near-deterministic — the only thing needed is a market that hasn't fully priced it yet.

## Parameters

| Param | Default | Effect | How to tune |
|-------|---------|--------|-------------|
| `theta_entry` | 0.001 (0.10%) | Minimum spot displacement to enter. Lower = more trades, lower win rate. | Grid search on history; target max Sharpe net of fees. |
| `theta_sat` | 0.003 (0.30%) | Displacement at which confidence = 1.0. Higher = confidence grows more slowly with delta. | Set where historical hit rate plateaus. |
| `tau_max` | 15.0 s | Latest entry before resolution. Higher = more trades, more reversal risk. | Test 10–20s. |
| `tau_min` | 5.0 s | Earliest entry before resolution. Lower = more slippage risk. | Test 3–8s. |
| `q_max` | 100 | Max shares per trade. | Fraction of bankroll. |
| `theta_edge` | 0.01 | Minimum edge per share to enter. Filters out marginal trades eaten by fees. | Raise until trade count stabilizes. |
| `fee_rate` | 0.07 | Polymarket taker fee rate. | Fixed by Polymarket (0.07 for crypto). |

## How to Run

```bash
# Default params
python -m scripts.run dry --strategy=WDM

# Tuned: lower entry threshold, shorter entry window
python -m scripts.run dry --strategy=WDM \
  --param theta_entry=0.0005 \
  --param tau_max=10

# Aggressive: larger position, no edge filter
python -m scripts.run dry --strategy=WDM \
  --param theta_entry=0.0005 \
  --param theta_edge=0.0 \
  --param q_max=500

# Shadow mode with default params
python -m scripts.run shadow --strategy=WDM --collect
```
