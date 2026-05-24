# ROM — Range Oscillation Momentum

## Description

ROM exploits the information contained in the intra-window spot price range. Over a 5-minute window, the spot price oscillates within a local range. When the price is positioned near one extreme of that range at evaluation time (T-30s to T-120s), it signals that momentum has driven price to the boundary — and the directional pressure is likely to continue or break out.

The core idea: a tight, well-defined range followed by price at an extreme = high-conviction momentum signal. A wide, noisy range with price near the middle = no signal.

The engine pre-computes `range_high` and `range_low` from all SpotPrice ticks between window open and evaluation time, so ROM is stateless per tick.

---

## Decision Flow (step by step)

### Step 1 — Timing window

```
if tau < tau_min or tau > tau_max:
    return None
```

ROM needs enough time for a range to form (`tau_min`) but enough remaining time for momentum to play out (`tau_max`).

| Param | What it controls |
|-------|------------------|
| `tau_max` | Upper bound: if τ is too large, insufficient range has formed. Default 120s (enter no earlier than 2 minutes into the window). |
| `tau_min` | Lower bound: if τ is too small, not enough time for momentum to pay out. Default 30s. |

### Step 2 — Range computation

The engine queries `SELECT MAX(price), MIN(price) FROM spot_prices WHERE symbol=? AND timestamp BETWEEN start_et AND eval_time`. The strategy receives these as:

```
r_high = context.range_high
r_low  = context.range_low
r_width = r_high - r_low
```

If `r_width <= 0` (only one tick or degenerate range), no signal is possible — exit.

### Step 3 — Proximity and delta

```
prox_up   = (r_high - spot) / r_width   # 0 = at high, 1 = at low
prox_down = (spot - r_low) / r_width    # 0 = at low, 1 = at high
delta_up   = (spot - r_low) / r_low     # fractional displacement from low
delta_down = (r_high - spot) / r_high   # fractional displacement from high
```

- `prox_up < 0.2` → spot is in the top 20% of the range → momentum up
- `prox_down < 0.2` → spot is in the bottom 20% → momentum down
- `delta` gives the absolute displacement magnitude for confidence calculation

### Step 4 — Entry thresholds

```
if prox_up < 0.2 and delta_up > theta_entry and up_best_ask:
    → BUY_YES
if prox_down < 0.2 and delta_down > theta_entry and down_best_ask:
    → BUY_NO
```

| Param | What it controls |
|-------|------------------|
| `theta_entry` | Minimum fractional displacement from the opposite extreme. Default 0.001 (0.10%). Ensures the range has meaningful breadth. |

### Step 5 — Order sizing

```
shares, notional = _resolve_order(price)
```

| `order_class` | `q_max` means | Shares = | Constraint | Valid `time_in_force` |
|-------------|---------------|----------|------------|----------------------|
| `market` (default) | USDC to spend | `q_max / price` | `q_max >= $1.00` | `FOK` (default) or `FAK` |
| `limit` | Number of shares | `q_max` | `q_max >= 5` AND `q_max * price >= $1.00` | `GTC` or `GTD` |

### Step 6 — Edge check

```
fee = taker_fee_usdc(shares, price, fee_rate)
edge = (1.0 - price) - fee / shares
if edge < theta_edge:
    return None
```

For BUY_YES the gross profit per share = `1.0 - price` (resolves to $1). For BUY_NO: same formula using `down_best_ask`.

### Step 7 — Confidence

```
confidence = min(delta / theta_sat, 1.0) * min(r_width / (0.5 * strike), 1.0)
```

Two factors:
- **Displacement confidence:** how far the delta is from the opposite bound
- **Range quality:** how tight the range is relative to 50% of the strike price. Tight ranges = higher conviction (price coiling before a move)

---

## Parameters Reference

| Param | Default | Used in | Effect | Tuning |
|-------|---------|---------|--------|--------|
| `theta_entry` | 0.001 (0.10%) | Step 4 | Minimum fractional delta to enter. | Grid search [0.0005, 0.002]. |
| `theta_sat` | 0.003 (0.30%) | Step 7 | Delta magnitude at which displacement-confidence saturates. | Set where historical hit rate plateaus. |
| `tau_max` | 120.0 s | Step 1 | Upper bound on entry timing. | Longer → more range formed, but less time for momentum. Test 90–180s. |
| `tau_min` | 30.0 s | Step 1 | Lower bound. Shorter → less momentum time. | Test 20–60s. |
| `replay_cadence_seconds` | 1.0 s | Dry replay | Historical evaluation spacing inside the `[tau_min, tau_max]` band. Lower = finer replay resolution, higher = faster backtests. | Test 1, 2, 5. |
| `q_max` | 1.0 | Step 5 | Market: USDC to spend. Limit: shares to buy. | Fraction of bankroll. |
| `theta_edge` | 0.01 | Step 6 | Minimum net edge per share. | Raise to skip marginal trades. |
| `order_class` | `"market"` | Step 5 | `"market"` = spend q_max USDC. `"limit"` = buy q_max shares. | ROM mid-window can use either. |
| `time_in_force` | `"FOK"` | Step 5 | Market → FOK/FAK. Limit → GTC/GTD. | Market → FOK/FAK. Limit → GTC/GTD. |
| `fee_rate` | 0.07 | Step 6 | Taker fee multiplier. | Fixed by Polymarket. |

---

## How to Run

```bash
# Default params ($1 per trade)
python -m scripts.run dry --strategy=ROM

# Tighter timing
python -m scripts.run dry --strategy=ROM \
  --param tau_max=90 \
  --param tau_min=45

# Faster replay scan in dry mode
python -m scripts.run dry --strategy=ROM \
  --param replay_cadence_seconds=5

# Higher conviction only
python -m scripts.run dry --strategy=ROM \
  --param theta_entry=0.002 \
  --param theta_edge=0.02

# Shadow mode
python -m scripts.run shadow --strategy=ROM --collect
```
