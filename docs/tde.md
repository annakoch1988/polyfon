# TDE — Time Decay Effect

## Description

TDE exploits the accelerating convergence of the fair probability as resolution approaches. In the final seconds of a 5-minute window, the probability that "spot > open" must converge toward either 0 or 1 — but the Polymarket token price often lags behind this deterministic drift.

The strategy computes the **theta** (rate of probability change per second) directly from the Black-Scholes binary call formula:

$$\Theta = \frac{\partial \hat{\pi}}{\partial \tau} = \phi(d_1) \cdot \frac{\partial d_1}{\partial \tau}$$

where:
- `π̂` = fair probability of UP (from the diffusion model)
- `τ` = seconds remaining to resolution
- `d₁` = standard Brownian bridge distance from strike
- `φ` = standard normal PDF

When `Θ < 0`, probability is drifting **up** toward 1 (spot is above strike); when `Θ > 0`, probability is drifting **down** toward 0 (spot is below strike). TDE enters only when:
1. The market is already mispriced relative to the fair model (`|fair_prob - market_price| > threshold`), and
2. The theta direction agrees — meaning the mispricing is *widening*, not converging.

This "double condition" distinguishes TDE from SLA, which trades any level mispricing regardless of the rate-of-change direction.

---

## Decision Flow (step by step)

### Step 1 — Timing window

```
if tau < tau_min or tau > tau_max:
    return None
```

`tau_max` prevents entry when there is too much time remaining (theta is too small). `tau_min` avoids the illiquid final seconds where fills are unreliable.

| Param | What it controls |
|-------|------------------|
| `tau_max` | If τ exceeds this, the probability convergence is still slow and the edge is too small. Default 90s. |
| `tau_min` | If τ is below this, liquidity may vanish and the token has already converged. Default 15s. |

The sweet spot is `[15, 90]` seconds before resolution — theta is large enough to matter, but fills are still available.

### Step 2 — Theta computation

```
theta = _theta(spot, strike, tau, sigma_per_minute)
```

The module-level `_theta()` function computes the analytical derivative ∂π̂/∂τ, matching the fair probability formula exactly.

### Step 3 — Direction check

```
if theta < 0:         # probability rising → BUY_YES
    price = up_best_ask
    mispricing = fair_prob - price
elif theta > 0:       # probability falling → BUY_NO
    price = down_best_ask
    mispricing = (1 - fair_prob) - price
else:
    return None       # theta too close to zero
```

The sign of theta determines which side to trade. When the probability mass is drifting toward one outcome, the corresponding token is the one to buy.

### Step 4 — Level check and order sizing

```
if mispricing < theta_entry:
    return None
```

The level mispricing must exceed the entry threshold — same concept as SLA's `theta_entry`.

```
shares, notional = _resolve_order(price)
```

| `order_class` | `q_max` means | Shares = | Constraint | Valid `time_in_force` |
|-------------|---------------|----------|------------|----------------------|
| `market` (default) | USDC to spend | `q_max / price` | `q_max >= $1.00` | `FOK` (default) or `FAK` |
| `limit` | Number of shares | `q_max` | `q_max >= 5` AND `q_max * price >= $1.00` | `GTC` or `GTD` |

TDE defaults to **market/FOK** because near-resolution trade speed dominates price improvement.

### Step 5 — Edge check

```
fee = taker_fee_usdc(shares, price, fee_rate)
edge = mispricing - fee / shares
if edge < theta_edge:
    return None
```

For BUY_YES: `edge = fair_prob - ask - fee/share`. For BUY_NO: `edge = (1 - fair_prob) - ask_NO - fee/share`.

### Step 6 — Confidence model

```
confidence = min(mispricing / epsilon_sat, 1.0) * min(abs(theta) / theta_sat, 1.0)
```

Two factors:
- **Level confidence:** how far the mispricing exceeds its saturation threshold
- **Rate confidence:** how large theta is relative to its saturation threshold

Both must be non-zero for the signal to fire. The product naturally down-weights signals where one factor is near zero.

| Param | What it controls |
|-------|------------------|
| `epsilon_sat` | Mispricing magnitude that saturates level-confidence. Lower → full conviction at smaller gaps. Default 0.15. |
| `theta_sat` | Theta magnitude that saturates rate-confidence. Lower → full conviction with less acceleration. Default 0.005 prob/sec. |

---

## Parameters Reference

| Param | Default | Used in | Effect | Tuning |
|-------|---------|---------|--------|--------|
| `theta_entry` | 0.05 | Step 4 | Minimum |fair_prob - market_price| to allow entry. Lower → more trades, lower average edge. | Grid search [0.03, 0.10]. |
| `tau_max` | 90.0 s | Step 1 | Upper bound on entry timing: τ <= tau_max. Higher → more entry windows, but theta is smaller → lower edge. | Test 60, 90, 120. |
| `tau_min` | 15.0 s | Step 1 | Lower bound on entry timing: τ >= tau_min. Lower → risk of vanishing liquidity. | Test 10, 15, 20. |
| `epsilon_sat` | 0.15 | Step 6 | Mispricing at which level-confidence saturates. | Set based on historical mispricing distribution. |
| `theta_sat` | 0.005 | Step 6 | Theta magnitude at which rate-confidence saturates. | Set based on typical theta values at entry: plot theta across windows. |
| `q_max` | 1.0 | Step 4 | Market: USDC to spend. Limit: shares to buy. | Fraction of bankroll. Conservative: $1. |
| `theta_edge` | 0.01 | Step 5 | Minimum net edge per share to enter. | Raise to skip marginal trades; set to 0.0 to disable. |
| `order_class` | `"market"` | Step 4 | `"market"` = spend q_max USDC. `"limit"` = buy q_max shares. | TDE near resolution should use `"market"`. |
| `time_in_force` | `"FOK"` | Step 4 | `"FOK"` = all-or-nothing market. `"FAK"` = partial fill. `"GTC"` = limit. | Market → FOK/FAK. Limit → GTC/GTD. |
| `fee_rate` | 0.07 | Step 5 | Taker fee multiplier | Fixed by Polymarket. |

---

## How to Run

```bash
# Default params ($1 per trade)
python -m scripts.run dry --strategy=TDE

# Shorter entry window, lower threshold
python -m scripts.run dry --strategy=TDE \
  --param tau_max=60 \
  --param theta_entry=0.04

# Higher conviction, larger size
python -m scripts.run dry --strategy=TDE \
  --param epsilon_sat=0.10 \
  --param theta_sat=0.003 \
  --param q_max=50

# Shadow mode
python -m scripts.run shadow --strategy=TDE --collect
```
