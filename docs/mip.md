# MIP — Market Maker Inventory Pressure Exploitation

## Description

MIP infers aggregate market-maker inventory skew from the **joint** behaviour of
the UP and DOWN token order books.  When size asymmetries persist in a direction
the MM is accumulating inventory that must eventually be shed — and the direction
of the forced quote migration is predictable.

The Avellaneda-Stoikov model predicts quote migration from inventory:

- **MM over-long YES**  → lowers both bid and ask to shed inventory  → mid drops  → **BUY_NO**
- **MM over-short YES** → raises both bid and ask to attract buying  → mid rises  → **BUY_YES**

Without per-entity L2 data or trade attribution, MIP proxies inventory pressure
via a **composite book imbalance** that examines both tokens simultaneously:

$$\begin{aligned}
\textup\_imbalance}   &= \frac{\textup\_bid\_size} - \textup\_ask\_size}}{\textup\_bid\_size} + \textup\_ask\_size}} \\[4pt]
\textdown\_imbalance}  &= \frac{\textdown\_bid\_size} - \textdown\_ask\_size}}{\textdown\_bid\_size} + \textdown\_ask\_size}} \\[4pt]
\textinventory\_pressure (ip)} &= \textup\_imbalance} - \textdown\_imbalance}
\end{aligned}$$

- `ip > 0`  → strong YES buying + weak NO buying → MM short YES → will raise quotes → **BUY_YES**
- `ip < 0`  → strong NO buying + weak YES buying → MM long YES  → will lower quotes → **BUY_NO**

This is complementary to OBI (which only uses the UP token).  MIP explicitly
models the **net** inventory exposure flowing through both sides of the MM's book.

---

## Decision Flow (step by step)

### Step 1 — Timing window

```
if tau < tau_min or tau > tau_max:
    return None
```

| Param | What it controls |
|-------|------------------|
| `tau_max` | Upper bound.  Default 120 s (enter no later than 2 min before expiry). |
| `tau_min` | Lower bound.  Default 60 s.  Avoid final minute where MMs often withdraw. |

### Step 2 — Composite inventory-pressure index

```
ip = up_imbalance - down_imbalance
if ip is None:
    return None
```

Requires **both** UP and DOWN token size data to be present.  Fails silently if
either token's bid/ask sizes are missing or zero.

### Step 3 — Entry threshold

```
if abs(ip) < ip_threshold:
    return None
```

| Param | What it controls |
|-------|------------------|
| `ip_threshold` | Minimum `|ip|` to consider a trade.  Default 0.30.  Mirrors the MIP specification range [0.3, 0.7]. |

### Step 4 — Direction and order sizing

```
if ip > ip_threshold and up_best_ask is not None:
    → BUY_YES at up_best_ask
if ip < -ip_threshold and down_best_ask is not None:
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
confidence = min(abs(ip) / theta_sat, 1.0)
```

Linear ramp: `|ip| = theta_sat` gives full confidence.

| Param | What it controls |
|-------|------------------|
| `theta_sat` | `|ip|` magnitude at which confidence saturates.  Default 0.60. |

---

## Parameters Reference

| Param | Default | Used in | Effect | Tuning |
|-------|---------|---------|--------|--------|
| `ip_threshold` | 0.30 | Step 3 | Min `|ip|` to allow entry. | Grid search [0.15, 0.70] per MIP spec. |
| `theta_sat` | 0.60 | Step 6 | `|ip|` magnitude at which confidence saturates. | Test 0.30–0.80. |
| `tau_max` | 120.0 s | Step 1 | Upper bound on entry timing. | Test 90–180 s. |
| `tau_min` | 60.0 s | Step 1 | Lower bound — avoid final-minute MM withdrawal. | Test 30–90 s. |
| `replay_cadence_seconds` | 1.0 s | Dry replay | Historical evaluation spacing inside `[tau_min, tau_max]`. | Test 1, 2, 5. |
| `q_max` | 1.0 | Step 4 | Market: USDC to spend. Limit: shares to buy. | Fraction of bankroll. |
| `theta_edge` | 0.01 | Step 5 | Minimum net edge per share. | Raise to skip marginal trades. |
| `order_class` | `"market"` | Step 4 | `"market"` = spend q_max USDC. `"limit"` = buy q_max shares. | MIP mid-window can use either. |
| `time_in_force` | `"FOK"` | Step 4 | Market → FOK/FAK. Limit → GTC/GTD. | Market → FOK/FAK. Limit → GTC/GTD. |
| `fee_rate` | 0.07 | Step 5 | Taker fee multiplier. | Fixed by Polymarket. |

---

## How to Run

```bash
# Default params ($1 per trade)
python -m scripts.run dry --strategy=MIP

# Tighter inventory-pressure threshold
python -m scripts.run dry --strategy=MIP \
  --param ip_threshold=0.40

# Faster replay scan in dry mode
python -m scripts.run dry --strategy=MIP \
  --param replay_cadence_seconds=5

# Earlier entry window with higher conviction
python -m scripts.run dry --strategy=MIP \
  --param ip_threshold=0.50 \
  --param tau_min=30 \
  --param theta_edge=0.02

# Shadow mode
python -m scripts.run shadow --strategy=MIP --collect
```
