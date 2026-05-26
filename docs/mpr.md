# MPR — Mean Price Reversion

## Description

MPR enters when the current spot price has deviated significantly from the intra-window mean spot price. The core assumption is that short-lived momentum spikes revert toward the mean within the 5-minute window, and the Polymarket token price lags the spot move.

MPR is distinct from ROM and PMR:
- **ROM** enters at the range extreme, expecting continuation.
- **PMR** enters after a pullback from the extreme, expecting full reversion.
- **MPR** enters on deviation from the mean, without requiring an extreme to have been reached first.

The deviation is measured as a fraction of the mean:

$$\text{deviation} = \frac{\text{spot} - \text{mean\_price}}{\text{mean\_price}}$$

Positive deviation → spot above mean → expect reversion down → BUY_NO.
Negative deviation → spot below mean → expect reversion up → BUY_YES.

---

## Decision Flow (step by step)

### Step 1 — Timing window

```
if tau < tau_min or tau > tau_max:
    return None
```

MPR needs enough ticks to form a meaningful mean (`tau_min`) but enough remaining time for reversion to play out (`tau_max`).

| Param | What it controls |
|-------|------------------|
| `tau_max` | Upper bound on entry timing. Default 120s. Higher → more ticks in the mean, but less time for reversion. |
| `tau_min` | Lower bound. Default 30s. Lower → less data for the mean, but more reversion time. |

### Step 2 — Mean computation

The engine computes `AVG(price)` over all SpotPrice ticks between `start_et` and the evaluation time. The strategy receives this as `context.mean_spot_price`.

### Step 3 — Deviation and entry

```
deviation = (spot - mean_price) / mean_price
if abs(deviation) < theta_entry:
    return None
```

| Param | What it controls |
|-------|------------------|
| `theta_entry` | Minimum |deviation| to consider entry. Default 0.001 (0.10%). |

### Step 4 — Direction

```
if deviation > theta_entry:        → spot above mean → BUY_NO
if deviation < -theta_entry:       → spot below mean → BUY_YES
```

Uses `down_best_ask` for BUY_NO and `up_best_ask` for BUY_YES.

### Step 5 — Order sizing and edge

```
shares, notional = _resolve_order(price)
fee = taker_fee_usdc(shares, price, fee_rate)
edge = (1.0 - price) - fee / shares
if edge < theta_edge:
    return None
```

### Step 6 — Confidence

```
confidence = min(abs(deviation) / theta_sat, 1.0)
```

Linear ramp of the deviation magnitude.

| Param | What it controls |
|-------|------------------|
| `theta_sat` | |deviation| at which confidence saturates. Default 0.003 (0.30%). |

---

## Parameters Reference

| Param | Default | Used in | Effect | Tuning |
|-------|---------|---------|--------|--------|
| `theta_entry` | 0.001 (0.10%) | Step 3 | Min |deviation| from mean. | Grid search [0.0005, 0.002]. |
| `theta_sat` | 0.003 (0.30%) | Step 6 | |deviation| at confidence saturation. | Test 0.002–0.005. |
| `tau_max` | 120.0 s | Step 1 | Upper bound on entry timing. | Test 90–180s. |
| `tau_min` | 30.0 s | Step 1 | Lower bound on entry timing. | Test 15–60s. |
| `replay_cadence_seconds` | 1.0 s | Dry replay | Eval spacing in `[tau_min, tau_max]`. | Test 1, 2, 5. |
| `q_max` | 1.0 | Step 5 | Market: USDC to spend. Limit: shares. | Fraction of bankroll. |
| `theta_edge` | 0.01 | Step 5 | Min edge per share. | Raise to skip marginal trades. |
| `order_class` | `"market"` | Step 5 | `"market"` or `"limit"`. | MPR mid-window can use either. |
| `time_in_force` | `"FOK"` | Step 5 | FOK/FAK for market, GTC/GTD for limit. | — |
| `fee_rate` | 0.07 | Step 5 | Taker fee. | Fixed by Polymarket. |

---

## How to Run

```bash
# Default params
python -m scripts.run dry --strategy=MPR

# Tighter deviation threshold, wider timing
python -m scripts.run dry --strategy=MPR \
  --param theta_entry=0.0005 \
  --param tau_max=150

# Faster replay
python -m scripts.run dry --strategy=MPR \
  --param replay_cadence_seconds=5

# Higher conviction
python -m scripts.run dry --strategy=MPR \
  --param theta_entry=0.002 \
  --param theta_edge=0.02

# Shadow mode
python -m scripts.run shadow --strategy=MPR --collect
```
