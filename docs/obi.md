# OBI — Order Book Imbalance

## Description

OBI exploits order-book micro-structure by measuring the ratio of bid to ask sizes at the top of the book for the UP token. A strong imbalance toward the bid side indicates aggressive buy pressure (market participants are lifting offers), while a strong imbalance toward the ask side indicates selling pressure.

The imbalance is computed on the UP token's Level 1 order book:

$$\text{imbalance} = \frac{\text{bid\_size} - \text{ask\_size}}{\text{bid\_size} + \text{ask\_size}}$$

Range: $[-1, +1]$
- $+1$: all bids, no asks → overwhelming buy pressure → BUY_YES
- $-1$: all asks, no bids → overwhelming sell pressure → BUY_NO
- $0$: balanced book → no signal

If the UP token's book lacks size data, the strategy falls back to the DOWN token's book.

---

## Decision Flow (step by step)

### Step 1 — Timing window

```
if tau < tau_min or tau > tau_max:
    return None
```

`tau_max` ensures enough book data has accumulated for a meaningful imbalance. `tau_min` avoids the illiquid final seconds.

| Param | What it controls |
|-------|------------------|
| `tau_max` | Upper bound on entry timing. Default 120s (enter no earlier than 2 minutes before expiry). Higher → more book data, but less time for pressure to resolve. |
| `tau_min` | Lower bound. Default 15s. Lower → risk of vanishing liquidity. |

### Step 2 — Imbalance computation

```
bid_size = up_bid_size, ask_size = up_ask_size
imbalance = (bid_size - ask_size) / (bid_size + ask_size)
```

If UP sizes are null/zero, fall back to DOWN token's book.

### Step 3 — Entry threshold

```
if abs(imbalance) < theta_entry:
    return None
```

| Param | What it controls |
|-------|------------------|
| `theta_entry` | Minimum |imbalance| to consider a trade. Default 0.40. Lower → more signals at weaker pressure. Higher → fewer, higher-conviction signals. |

### Step 4 — Direction and order sizing

```
if imbalance > theta_entry and up_best_ask is not None:
    → BUY_YES at up_best_ask
if imbalance < -theta_entry and down_best_ask is not None:
    → BUY_NO at down_best_ask
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

Same formula for BUY_NO using `down_best_ask`.

### Step 6 — Confidence

```
confidence = min(abs(imbalance) / theta_sat, 1.0)
```

Linear ramp: imbalance = theta_sat gives full confidence.

| Param | What it controls |
|-------|------------------|
| `theta_sat` | |imbalance| magnitude at which confidence saturates. Default 0.60. |

---

## Parameters Reference

| Param | Default | Used in | Effect | Tuning |
|-------|---------|---------|--------|--------|
| `theta_entry` | 0.40 | Step 3 | Min |imbalance| to allow entry. | Grid search [0.20, 0.60]. |
| `theta_sat` | 0.60 | Step 6 | |imbalance| magnitude at which confidence saturates. | Test 0.30–0.80. |
| `tau_max` | 120.0 s | Step 1 | Upper bound on entry timing. | Test 90–180s. |
| `tau_min` | 15.0 s | Step 1 | Lower bound on entry timing. | Test 10–30s. |
| `replay_cadence_seconds` | 1.0 s | Dry replay | Historical evaluation spacing inside the `[tau_min, tau_max]` band. | Test 1, 2, 5. |
| `q_max` | 1.0 | Step 4 | Market: USDC to spend. Limit: shares to buy. | Fraction of bankroll. |
| `theta_edge` | 0.01 | Step 5 | Minimum net edge per share. | Raise to skip marginal trades. |
| `order_class` | `"market"` | Step 4 | `"market"` = spend q_max USDC. `"limit"` = buy q_max shares. | OBI mid-window can use either. |
| `time_in_force` | `"FOK"` | Step 4 | Market → FOK/FAK. Limit → GTC/GTD. | Market → FOK/FAK. Limit → GTC/GTD. |
| `fee_rate` | 0.07 | Step 5 | Taker fee multiplier. | Fixed by Polymarket. |

---

## How to Run

```bash
# Default params ($1 per trade)
python -m scripts.run dry --strategy=OBI

# Tighter imbalance threshold
python -m scripts.run dry --strategy=OBI \
  --param theta_entry=0.30

# Faster replay scan in dry mode
python -m scripts.run dry --strategy=OBI \
  --param replay_cadence_seconds=5

# Higher conviction only
python -m scripts.run dry --strategy=OBI \
  --param theta_entry=0.50 \
  --param theta_edge=0.02

# Shadow mode
python -m scripts.run shadow --strategy=OBI --collect
```
