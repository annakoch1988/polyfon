# CLL — Cross-Asset Correlation Lead-Lag

## Description

CLL exploits persistent asymmetric lead-lag dynamics between crypto assets.
BTC often leads ETH by 10–60 seconds (Caporale & Plastuni 2019; Bouri et al.
2019).  When the leader asset (e.g. BTC) moves significantly, the lagger's
(e.g. ETH) Polymarket contract has not yet repriced.  The strategy enters
in the direction of the predicted catch-up move.

The strategy computes a lead-lag adjusted fair probability:

1. **Leader return**: compute the leader's return over the `lookback_seconds` window.
2. **Skip if leader is the window being evaluated** (e.g. skip BTC windows if BTC is the leader).
3. **Predict lagger return**: `predicted_return = beta_lead × leader_return`.
4. **Predicted spot**: `Ŝ = S × exp(predicted_return)`.
5. **Conditional volatility**: `σ_B|A = σ_B × √(1 - ρ²)` — the leader explains part of the lagger's variance, narrowing the probability distribution.
6. **CLL-adjusted fair prob**: `π̂_CLL = Φ(ln(Ŝ/K) / (σ_B|A × √(τ/60)))`.
7. **Trade when** `|π̂_CLL - market_price| > theta_entry`.

## Decision Flow

### Step 1 — Leader check

```
if window.underlying == leader:
    return None
```

The strategy only generates signals for lagger assets (e.g. ETH windows when
leader=BTC).  The leader itself has no leader to follow.

### Step 2 — Timing window

```
if tau < tau_min or tau > tau_max:
    return None
```

| Param | What it controls |
|-------|------------------|
| `tau_max` | Upper bound on entry timing. Default 240 s. |
| `tau_min` | Lower bound — avoid illiquid final seconds. Default 15 s. |

### Step 3 — Leader move check

```
if abs(leader_return) < leader_min_return:
    return None
```

The leader must have moved meaningfully.  A small leader return does not
provide enough signal to predict the lagger.

| Param | What it controls |
|-------|------------------|
| `leader_min_return` | Minimum |leader return| to act. Default 0.001 (0.1 %). |

### Step 4 — Predict lagger spot

```
predicted_return = beta_lead × leader_return
predicted_spot = spot × exp(predicted_return)
```

| Param | What it controls |
|-------|------------------|
| `beta_lead` | How much the lagger moves per unit leader move. Default 0.35. |
| `lookback_seconds` | Return computation window for the leader. Default 60 s. |

### Step 5 — Compute CLL-adjusted fair probability

```
cond_sigma = sigma × sqrt(1 - rho²)
d = ln(predicted_spot / strike) / (cond_sigma × sqrt(τ/60))
adjusted_prob = Φ(d)
```

| Param | What it controls |
|-------|------------------|
| `rho` | Cross-correlation between leader and lagger. Higher → narrower conditional vol → larger adjusted prob swing. Default 0.72. |

### Step 6 — Mispricing check

```
mispricing = adjusted_prob - market_price
if abs(mispricing) < theta_entry:
    return None
```

| Param | What it controls |
|-------|------------------|
| `theta_entry` | Minimum |mispricing| to enter. Default 0.03. |

### Step 7 — Direction

```
if mispricing > 0:  BUY_YES  (market undervalues YES)
else:                BUY_NO   (market overvalues YES)
```

## Parameters Reference

| Param | Default | Used in | Effect | Tuning |
|-------|---------|---------|--------|--------|
| `leader` | `"BTC"` | Step 1 | Leader asset symbol. | Set to the dominant leader for the lagger being traded. |
| `lookback_seconds` | 60.0 | Step 4 | Window for leader return computation. | Test 30, 60, 120. |
| `beta_lead` | 0.35 | Step 4 | Regression coefficient. Higher → more aggressive lagger prediction. | Estimate via OLS on historical data. |
| `rho` | 0.72 | Step 5 | Cross-correlation. Higher → narrower conditional vol. | Compute rolling CCF on 4h of data. |
| `leader_min_return` | 0.001 | Step 3 | Minimum |leader return| to act. | Test 0.0005, 0.001, 0.002. |
| `theta_entry` | 0.03 | Step 6 | Minimum |mispricing| to enter. | Grid search; optimize Sharpe. |
| `tau_max` | 240.0 s | Step 2 | Upper entry bound. | Test 180, 240, 300. |
| `tau_min` | 15.0 s | Step 2 | Lower entry bound. | Test 10, 15, 20. |
| `replay_cadence_seconds` | 1.0 s | Dry replay | Historical eval spacing. | Test 1, 2, 5. |
| `q_max` | 1.0 | Step 7 | Market: USDC to spend. | Fraction of bankroll. |
| `theta_edge` | 0.01 | Step 7 | Minimum net edge per share. | Raise to skip marginal trades. |
| `order_class` | `"market"` | Step 7 | Order type. | Market for speed. |
| `time_in_force` | `"FOK"` | Step 7 | TIF for market orders. | `FOK` or `FAK`. |
| `fee_rate` | 0.07 | Step 7 | Taker fee. | Fixed by Polymarket. |

## Concrete Example

**Setup:** BTC = $68,500 (+0.4% in 60s). ETH = $3,415 (+0.05%).
Contract: "ETH > $3,420 at 16:00?" (3 min to resolution). ETH YES = $0.30.

1. **Lead-lag parameters** (from empirical estimation):
   - BTC leads ETH by ~25 s.
   - ρ = 0.72, β_lead = 0.35.

2. **Leader move:** BTC return over last 60s = +0.004. Above `leader_min_return`.

3. **Predicted ETH return:** 0.35 × 0.004 = 0.0014.

4. **Predicted ETH spot:** $3,415 × exp(0.0014) = $3,419.78.

5. **Conditional vol:** σ_B = 0.0012/min. σ_B|A = 0.0012 × √(1-0.72²) = 0.000833/min.

6. **CLL fair prob:** σ_B|A × √(3) = 0.00144. d = ln(3419.78/3420) / 0.00144 = -0.000064 / 0.00144 = -0.045. π̂ = Φ(-0.045) = 0.482.

7. **Mispricing:** 0.482 - 0.30 = +0.182 > theta_entry. **BUY YES.**

## No Future-Knowledge Compliance

- `build_replay_plan()` generates eval times from window fixed properties only.
- Leader return is computed from data strictly at or before `eval_time`.
- `stop_on_signal=True` ensures the FIRST valid signal wins.

## How to Run

```bash
# Default params (leader=BTC, lagger=ETH)
python -m scripts.run dry --strategy=CLL

# Require both coins in the DB
python -m scripts.run dry --strategy=CLL --coins=BTC,ETH

# Lower mispricing threshold, tighter correlation
python -m scripts.run dry --strategy=CLL \
  --param theta_entry=0.02 \
  --param rho=0.80

# Shadow mode
python -m scripts.run shadow --strategy=CLL --collect
```
