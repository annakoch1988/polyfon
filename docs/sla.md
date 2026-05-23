# SLA — Spot-Led Latency Arbitrage

## Description

Compares fair probability (Black-Scholes binary call with window open price as strike) to the market price of the YES token. When mispricing exceeds a threshold, enters early in the window (≥30s to resolution) expecting the market to converge to fair value as the CEX spot move propagates to Polymarket.

## Parameters

| Param | Default | Effect | How to tune |
|-------|---------|--------|-------------|
| `theta_entry` | 0.05 | Minimum |fair - market| mispricing to enter. Lower = more trades, lower accuracy. | Grid search on history. |
| `tau_min` | 30.0 s | Don't trade if less than this many seconds remain. Prevents late entries with insufficient convergence time. | Test 20–60s. |
| `q_max` | 100 | Max shares per trade. | Fraction of bankroll. |
| `fee_rate` | 0.07 | Polymarket taker fee rate. | Fixed by Polymarket. |

## How to Run

```bash
# Default params
python -m scripts.run dry --strategy=SLA

# Tighter threshold, later entry
python -m scripts.run dry --strategy=SLA \
  --param theta_entry=0.03 \
  --param tau_min=45

# Larger position
python -m scripts.run dry --strategy=SLA \
  --param q_max=250

# Shadow mode
python -m scripts.run shadow --strategy=SLA --collect
```
