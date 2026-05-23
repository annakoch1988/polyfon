# WDM — Window Delta Momentum

## Description

Window Delta Momentum is a threshold-based strategy that enters a binary prediction market in the final seconds before resolution. It exploits the fact that when the spot price has moved far from the window open price at T-10s, the outcome is usually locked — but the Polymarket token price has not always fully converged to 0 or 1 yet.

The core intuition: a 5-minute binary market pays \$1 if spot > open at expiry. If spot is already +0.15% from open with only 10 seconds left, the probability of a reversal is ~0.7% (assuming Brownian motion). Yet the YES token may still trade at \$0.55–0.85 instead of \$1.00, leaving a profitable edge.

Unlike SLA which trades on *latent mispricing* (fair prob vs market price) well before expiry, WDM trades on *near-certain outcome revelation* at the very end. The two strategies are complementary — their entry windows don't overlap.

---

## Decision Flow (step by step)

### Step 1 — Timing window

```
if tau > tau_max or tau < tau_min:
    return None
```

`tau` is the seconds remaining until the window closes (`window.end_et - current_time`). The strategy only evaluates when tau is in `[tau_min, tau_max]`.

| Param | What it controls |
|-------|------------------|
| `tau_max` | If tau is above this, the outcome is still uncertain → skip. Default 15s means "don't enter more than 15 seconds before resolution." |
| `tau_min` | If tau is below this, the token price has likely already converged to 1.0 and liquidity may vanish → skip. Default 5s means "don't enter with fewer than 5 seconds left." |

The sweet spot is around tau = 10s: reversal probability is tiny, but the token hasn't fully converged yet.

### Step 2 — Compute window delta

```
delta = (current_spot - window_open_price) / window_open_price
```

The displacement of the current spot price relative to the price at window open. This is the only signal. Positive = spot is above open (likely UP). Negative = spot is below open (likely DOWN).

### Step 3 — Entry threshold

```
if abs(delta) < theta_entry:
    return None
```

The displacement must be large enough to be decisive.

| Param | What it controls |
|-------|------------------|
| `theta_entry` | The minimum |delta| required to consider a trade. A lower value produces more trades but lower accuracy (more windows entered where the outcome flips in the final seconds). A higher value produces fewer, higher-conviction trades. Default 0.001 = 0.10%. |

### Step 4 — Confidence

```
confidence = min(abs(delta) / theta_sat, 1.0)
```

Linear ramp from 0 to 1 based on how far delta exceeds the threshold.

| Param | What it controls |
|-------|------------------|
| `theta_sat` | The |delta| at which confidence hits 1.0. If theta_sat = 0.003, a delta of 0.15% gives confidence = 0.5, while 0.30% gives 1.0. Lower values make the strategy reach full conviction faster. |

Confidence is stored in the signal and can be used downstream for position sizing or ranking.

### Step 5 — Resolve order size

The strategy calls `_resolve_order(price)` which interprets `q_max` based on `order_class`:

| `order_class` | `q_max` means | Shares = | Constraint | Valid `time_in_force` |
|-------------|---------------|----------|------------|----------------------|
| `market` (default) | USDC to spend | `q_max / price` | `q_max >= $1.00` | `FOK` (default) or `FAK` |
| `limit` | Number of shares | `q_max` | `q_max >= 5` AND `q_max * price >= $1.00` | `GTC` or `GTD` |

WDM defaults to **market/FOK** because at T-10s the priority is speed — you want immediate fill-or-kill execution without resting on the book. `q_max = 100` means "spend $100 USDC."

### Step 6 — Edge calculation

```
if delta > theta_entry and up_best_ask is not None:
    shares, notional = _resolve_order(up_best_ask)
    if shares is None: return None        # ← constraint check
    fee = taker_fee_usdc(shares, price, fee_rate)
    edge = (1.0 - price) - fee / shares
    if edge > theta_edge:
        return Signal(direction="BUY_YES", size=shares, ...)
```

If delta is positive and above threshold, the strategy expects UP → buys the YES token at the UP token's best ask.

Gross profit per share = `1.0 - price` (YES resolves to \$1).

Fee per share = `fee_rate * price * (1 - price)` (the Polymarket formula).

Net edge per share = gross profit - fee per share.

| Param | What it controls |
|-------|------------------|
| `order_type` | `"market"` → `q_max` = USDC to spend (get shares = q/price). `"limit"` → `q_max` = shares to buy. Default `"market"`. |
| `q_max` | For market orders: USDC amount to spend (min $1). For limit orders: shares to buy (min 5 shares, min $1 notional). |
| `fee_rate` | Polymarket taker fee rate (fixed at 0.07 for crypto markets). |
| `theta_edge` | Minimum net edge per share required to enter. If the token price is too close to 1.0 (e.g., best_ask = 0.99), gross profit is tiny and fees eat it → edge may be below `theta_edge` → skip. Default 0.01. |

```
if delta < -theta_entry and down_best_ask is not None:
    shares, notional = _resolve_order(down_best_ask)
    if shares is None: return None
    fee = taker_fee_usdc(shares, price, fee_rate)
    edge = (1.0 - price) - fee / shares
    if edge > theta_edge:
        return Signal(direction="BUY_NO", ...)
```

If delta is negative and below the negative threshold, the strategy expects DOWN → buys the NO token at the DOWN token's best ask. Same edge logic.

---

### Polymarket order constraints

Before returning a signal, `_resolve_order(price)` enforces Polymarket's minimum order rules:

| `order_type` | Check | Reason |
|-------------|-------|--------|
| `market` | `q_max >= $1.00` | Minimum notional; you can't spend less than $1 USDC |
| `limit` | `q_max >= 5` | `INVALID_ORDER_MIN_SIZE` — Polymarket rejects < 5 shares |
| `limit` | `q_max * price >= $1.00` | Minimum notional for limit orders |

The fee always follows the Polymarket formula: `fee = shares * fee_rate * price * (1 - price)`.

At default `q_max=100` (market order, $100 USDC) and typical price range $0.50–$1.00, all checks pass automatically.

---

## Parameters Reference

| Param | Default | Used in | Effect | Tuning |
|-------|---------|---------|--------|--------|
| `theta_entry` | 0.001 (0.10%) | Step 3, Step 5 | Gates entry: `abs(delta) >= theta_entry` | Lower → more trades, lower win rate. Grid search over [0.0005, 0.002] on historical data. |
| `theta_sat` | 0.003 (0.30%) | Step 4 | Confidence ramp: `min(abs(delta) / theta_sat, 1.0)` | Set where historical hit rate plateaus. |
| `tau_max` | 15.0 s | Step 1 | Upper bound on entry timing: `tau <= tau_max` | Higher → more time for reversal. Test 10–20s. |
| `tau_min` | 5.0 s | Step 1 | Lower bound on entry timing: `tau >= tau_min` | Lower → risk of vanishing liquidity. Test 3–8s. |
| `q_max` | 100 | Step 5–6 | Market: USDC to spend. Limit: shares to buy. | For market (default): fraction of bankroll in USDC. Conservative: $50–100. Aggressive: $200–500. |
| `order_class` | `"market"` | Step 5 | `"market"` = spend q_max USDC, get shares = q/price. `"limit"` = buy q_max shares at price. | WDM at T-10s should use `"market"` (speed > price improvement). SLA with 30s+ runway can use `"limit"`. |
| `time_in_force` | `"FOK"` | Step 5 | `"FOK"` = fill-or-kill (all-or-nothing market). `"FAK"` = fill-and-kill (partial fill). `"GTC"` = good-til-cancelled (limit). `"GTD"` = good-til-date. | Market class → FOK/FAK. Limit class → GTC/GTD. Invalid combos raise at init. |
| `theta_edge` | 0.01 | Step 6 | Filters low-edge trades: `edge >= theta_edge` | Raise to skip marginal trades, lower to catch more. Set to 0 to skip filter entirely. |
| `fee_rate` | 0.07 | Step 6 | Taker fee multiplier | Fixed by Polymarket for crypto markets. Do not change unless Polymarket changes it. |

---

## Edge Sensitivity

The edge depends critically on `up_best_ask` (or `down_best_ask`) at entry time. Example with `q_max = 100`:

| Ask price | Gross profit/share | Fee/share | Net edge/share | Tradable? |
|-----------|-------------------|-----------|----------------|-----------|
| 0.55 | 0.45 | 0.0173 | 0.4327 | Yes (theta_edge=0.01) |
| 0.85 | 0.15 | 0.0089 | 0.1411 | Yes |
| 0.95 | 0.05 | 0.0033 | 0.0467 | Yes |
| 0.99 | 0.01 | 0.0007 | 0.0093 | No (below theta_edge) |
| 1.00 | 0.00 | 0.0000 | 0.0000 | No |

The strategy only enters when the market has not fully adjusted to the delta. If the YES token already trades at 0.99+ when delta is +0.15%, there is no edge — the strategy correctly abstains.

---

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

# Conservative: high conviction only, small size
python -m scripts.run dry --strategy=WDM \
  --param theta_entry=0.0015 \
  --param tau_min=8 \
  --param theta_edge=0.03 \
  --param q_max=50

# Shadow mode with default params
python -m scripts.run shadow --strategy=WDM --collect
```
