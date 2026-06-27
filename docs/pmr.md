# PMR — Price Momentum Reversal

## Description

PMR detects when an intra-window price extreme has been reached and the spot has already pulled back by a meaningful fraction of the range. This signals that the initial momentum has exhausted and a reversal toward the mean is underway.

PMR is the complement of **ROM (Range Oscillation Momentum)**:
- ROM enters when price is **at** an extreme, expecting **continuation**.
- PMR enters when price has **pulled back** from an extreme, betting on **full reversion** toward the opposite bound.

The pullback is measured as a fraction of the established intra-window range width. A pullback of 30%+ from the extreme suggests the momentum phase is over and a reversal leg is in progress.

---

## Decision Flow (step by step)

### Step 1 — Timing window

```
if tau < tau_min or tau > tau_max:
    return None
```

PMR needs enough range to have formed (`tau_min`) but enough remaining time for the reversal to play out (`tau_max`).

| Param | What it controls |
|-------|------------------|
| `tau_max` | Upper bound: if τ is too large, insufficient range has formed. Default 90s. |
| `tau_min` | Lower bound: if τ is too small, not enough time for reversal to pay out. Default 15s. |

### Step 2 — Range computation

The engine computes `range_high` and `range_low` from all SpotPrice ticks between window open and evaluation time. The strategy receives these as:

```
r_high = context.range_high
r_low  = context.range_low
r_width = r_high - r_low
```

If `r_width <= 0` (degenerate range), no signal is possible — exit.

### Step 3 — Pullback measurement

```
pullback_from_high = (r_high - spot) / r_width   # 0 = at high, 1 = at low
bounce_from_low    = (spot - r_low) / r_width    # 0 = at low, 1 = at high
```

- `pullback_from_high > theta_reversal` → price has reversed down from the high → reversal confirmed → BUY_NO
- `bounce_from_low > theta_reversal` → price has reversed up from the low → reversal confirmed → BUY_YES

| Param | What it controls |
|-------|------------------|
| `theta_reversal` | Minimum pullback from extreme (as fraction of range width) to confirm a reversal. Default 0.30 (30%). Lower → more signals at smaller pullbacks. Higher → more conviction but fewer entries. |

### Step 4 — Order sizing

```
shares, notional = _resolve_order(price)
```

| `order_class` | `q_max` means | Shares = | Constraint | Valid `time_in_force` |
|-------------|---------------|----------|------------|----------------------|
| `market` (default) | USDC to spend | `q_max / price` | `q_max >= $1.00` | `FOK` (default) or `FAK` |
| `limit` | Number of shares | `q_max` | `q_max >= 5` AND `q_max * price >= $1.00` | `GTC` or `GTD` |

### Step 5 — Edge check

```
fee = taker_fee_usdc(shares, price, fee_rate)
edge = (1.0 - price) - fee / shares
if edge < theta_edge:
    return None
```

For BUY_NO (when buying the DOWN token): same formula using `down_best_ask`.

### Step 6 — Confidence

```
confidence = min(pullback / theta_sat, 1.0)
```

The pullback/bounce fraction is linearly ramped against the saturation threshold. A pullback of `theta_sat` (default 0.50 = 50% of range width) gives full confidence.

| Param | What it controls |
|-------|------------------|
| `theta_sat` | Pullback fraction at which confidence saturates. Default 0.50 (50% of range width). |

---

## Parameters Reference

| Param | Default | Used in | Effect | Tuning |
|-------|---------|---------|--------|--------|
| `theta_reversal` | 0.30 | Step 3 | Min pullback from extreme (fraction of range width) to confirm reversal. | Grid search [0.20, 0.50]. |
| `theta_sat` | 0.50 | Step 6 | Pullback fraction at which confidence saturates. | Test 0.30–0.70. |
| `tau_max` | 90.0 s | Step 1 | Upper bound on entry timing: τ ≤ tau_max. Higher → more range formed, but less time for reversal. | Test 60–120s. |
| `tau_min` | 15.0 s | Step 1 | Lower bound on entry timing: τ ≥ tau_min. Lower → less time for reversal. | Test 10–30s. |
| `replay_cadence_seconds` | 1.0 s | Dry replay | Historical evaluation spacing inside the `[tau_min, tau_max]` band. | Test 1, 2, 5. |
| `q_max` | 1.0 | Step 4 | Market: USDC to spend. Limit: shares to buy. | Fraction of bankroll. |
| `theta_edge` | 0.01 | Step 5 | Minimum net edge per share. | Raise to skip marginal trades. |
| `order_class` | `"market"` | Step 4 | `"market"` = spend q_max USDC. `"limit"` = buy q_max shares. | PMR mid-window can use either. |
| `time_in_force` | `"FOK"` | Step 4 | Market → FOK/FAK. Limit → GTC/GTD. | Market → FOK/FAK. Limit → GTC/GTD. |
| `fee_rate` | 0.07 | Step 5 | Taker fee multiplier. | Fixed by Polymarket. |

---

## How to Run

```bash
# Default params ($1 per trade)
python -m scripts.run dry --strategy=PMR

# Tighter pullback threshold, wider timing
python -m scripts.run dry --strategy=PMR \
  --param theta_reversal=0.25 \
  --param tau_max=120

# Faster replay scan in dry mode
python -m scripts.run dry --strategy=PMR \
  --param replay_cadence_seconds=5

# Higher conviction only
python -m scripts.run dry --strategy=PMR \
  --param theta_reversal=0.40 \
  --param theta_edge=0.02

# Shadow mode
python -m scripts.run shadow --strategy=PMR
```
