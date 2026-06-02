# VIT — Volume-Spike Informed Trading

## Description

VIT detects informed trading activity by combining spot-price momentum with
order-book imbalance.  When the spot price moves rapidly (signalling unusual
market activity) and the Polymarket order book confirms the direction through
bid/ask imbalance, the strategy enters in the direction of the informed flow.

The original VIT proposal required per-trade Polymarket volume data (EMA of
trade volume, Z-score spike detection, trade-sign Order Flow Imbalance).
This adaptation uses spot displacement from the window-open price as the
activity proxy and order-book depth imbalance as the direction proxy.

The core insight is that informed traders on Polymarket reveal their
direction through aggressive order placement, which creates detectable
book imbalances, while their information source (typically a faster spot
feed) drives rapid spot-price moves.  When both signals align, the
probability of favourable resolution is elevated.

## Decision Flow

### Step 1 — Timing window

```
if tau < tau_min or tau > tau_max:
    return None
```

| Param | What it controls |
|-------|------------------|
| `tau_max` | If τ exceeds this, the activity may dissipate before resolution. Matches VIT's holding-time range (30–240 s). Default 240 s. |
| `tau_min` | If τ is below this, liquidity vanishes and fills become unreliable. Default 15 s. |

### Step 2 — Activity spike (spot displacement)

```
displacement = (spot - window_open_price) / window_open_price
if abs(displacement) < v_threshold:
    return None
```

Spot displacement from the window-open price stands in for the original
VIT's volume Z-score spike detection.  A large displacement implies that
significant market activity has occurred since the window opened.

| Param | What it controls |
|-------|------------------|
| `v_threshold` | Minimum |displacement| to consider the market "active". Lower → more trades, higher noise. Default 0.001 (0.1 %). |
| `v_sat` | Displacement magnitude at which activity-confidence saturates. Default 0.005 (0.5 %). |

### Step 3 — Informed direction (book imbalance)

```
imbalance = (bid_size - ask_size) / (bid_size + ask_size)
if abs(imbalance) < imb_threshold:
    return None
```

Order-book imbalance replaces the original VIT's trade-sign OFI.  Positive
imbalance (bids > asks) signals buying pressure; negative imbalance signals
selling pressure.

| Param | What it controls |
|-------|------------------|
| `imb_threshold` | Minimum |imbalance| to confirm direction. Default 0.10. |
| `imb_sat` | |imbalance| at which direction-confidence saturates. Default 0.30. |

### Step 4 — Cross-validation (direction agreement)

```
if displacement > 0 and imbalance > 0:  BUY_YES
elif displacement < 0 and imbalance < 0:  BUY_NO
else:  return None  # directions disagree
```

This is VIT's critical filter: the spot direction and book direction must
agree.  If the spot is rising but the book is ask-heavy (selling pressure),
the signals conflict and the strategy stays out.  This eliminates false
positives from noise traders and regime-ambiguous periods.

### Step 5 — Order sizing and edge check

```
price = up_best_ask (BUY_YES) or down_best_ask (BUY_NO)
edge = (1.0 - price) - fee/share
if edge < theta_edge: return None
```

| `order_class` | `q_max` means | Shares = | Constraint | Valid `time_in_force` |
|---------------|---------------|----------|------------|----------------------|
| `market` (default) | USDC to spend | `q_max / price` | `q_max >= $1.00` | `FOK` (default) or `FAK` |
| `limit` | Number of shares | `q_max` | `q_max >= 5` AND `q_max * price >= $1.00` | `GTC` or `GTD` |

### Step 6 — Confidence model

```
confidence = min(|displacement| / v_sat, 1.0) * min(|imbalance| / imb_sat, 1.0)
```

Two factors multiplied:
- **Activity confidence:** how large the spot displacement is relative to its saturation threshold.
- **Direction confidence:** how strong the book imbalance is relative to its saturation threshold.

## Parameters Reference

| Param | Default | Used in | Effect | Tuning |
|-------|---------|---------|--------|--------|
| `v_threshold` | 0.001 | Step 2 | Minimum |displacement| to trigger. Lower → more trades, lower avg edge. | Grid search [0.0005, 0.003]. |
| `v_sat` | 0.005 | Step 6 | Displacement at which activity-confidence saturates. | Set from historical displacement distribution. |
| `imb_threshold` | 0.10 | Step 3 | Minimum |imbalance| to confirm direction. Lower → more signals. | Test 0.05, 0.10, 0.15, 0.20. |
| `imb_sat` | 0.30 | Step 6 | |imbalance| at which direction-confidence saturates. | Test 0.20, 0.30, 0.40. |
| `tau_max` | 240.0 s | Step 1 | Upper bound on entry timing. Matches original VIT's 30–240 s holding range. | Test 120, 180, 240, 300. |
| `tau_min` | 15.0 s | Step 1 | Lower bound on entry timing. Avoids illiquid final seconds. | Test 10, 15, 20. |
| `replay_cadence_seconds` | 1.0 s | Dry replay | Historical eval spacing inside the [τ_min, τ_max] band. | Test 1, 2, 5. |
| `q_max` | 1.0 | Step 5 | Market: USDC to spend. Limit: shares to buy. | Fraction of bankroll. |
| `theta_edge` | 0.01 | Step 5 | Minimum net edge per share to enter. | Raise to skip marginal trades. |
| `order_class` | `"market"` | Step 5 | `"market"` = spend q_max USDC. `"limit"` = buy q_max shares. | VIT near activity spike should use `"market"`. |
| `time_in_force` | `"FOK"` | Step 5 | `"FOK"` = all-or-nothing market. `"FAK"` = partial fill. | Market → FOK/FAK. |
| `fee_rate` | 0.07 | Step 5 | Taker fee multiplier | Fixed by Polymarket. |

## Relationship to Other Strategies

VIT occupies a unique intersection: it combines activity-spike detection
(similar to the original volume-spike logic) with book-imbalance direction
(complementary to OBI).  Unlike SLA, VIT does not compute fair probability;
unlike WDM, VIT requires book confirmation; unlike OBI, VIT requires spot
momentum.  The strategy fires less frequently than any of those individually
but with higher conviction per signal.

## How to Run

```bash
# Default params
python -m scripts.run dry --strategy=VIT

# Tighter imbalance, higher activity threshold
python -m scripts.run dry --strategy=VIT \
  --param imb_threshold=0.15 \
  --param v_threshold=0.002

# Larger size, shorter entry window
python -m scripts.run dry --strategy=VIT \
  --param q_max=50 \
  --param tau_max=120

# Shadow mode
python -m scripts.run shadow --strategy=VIT --collect
```
