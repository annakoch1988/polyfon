# VPX — CEX Toxicity as a Leading Volatility Indicator (VPIN-X adaptation)

## Description

VPX detects volatility regime shifts by comparing short-term realised
volatility to a longer-term baseline.  When short-term vol spikes above
the baseline, a volatility regime change is predicted — contracts are
repriced using the projected vol, and the strategy trades any discrepancy
with the market price.

The original VPIN-X (Strategy 8) required Binance trade-level data
to compute VPIN (Volume-synchronized Probability of Informed Trading).
This adaptation uses the ratio of short-window to long-window realised
volatility (both computed from Binance spot ticker data) as a proxy for
VPIN spike detection.

**Core mechanism:** higher volatility pushes binary option prices toward
0.50 (more uncertainty).  OTM contracts become more valuable; ITM contracts
become less valuable.  When the market hasn't yet priced in a volatility
regime shift, the strategy enters in the direction of the repricing.

## Decision Flow

### Step 1 — Timing window

```
if tau < tau_min or tau > tau_max:
    return None
```

| Param | What it controls |
|-------|------------------|
| `tau_max` | Upper bound on entry timing. Default 240 s. |
| `tau_min` | Lower bound — avoid illiquid final seconds. Default 15 s. |

### Step 2 — Volatility spike detection

```
vol_ratio = sigma_short / sigma_long
if vol_ratio < vpx_threshold:
    return None
```

| Param | What it controls |
|-------|------------------|
| `vpx_threshold` | Min ratio short/long vol to signal a spike. 1.5 = short vol 50 % above long vol. Default 1.5. |

### Step 3 — Volatility projection

```
projected_sigma = sigma_long + beta_vpx * (sigma_short - sigma_long)
```

| Param | What it controls |
|-------|------------------|
| `beta_vpx` | How much of the vol spike persists forward. 0.5 = half the elevation carries. Default 0.5. |

### Step 4 — Adjusted fair probability

```
d1 = ln(spot / strike) / (projected_sigma * sqrt(tau / 60))
adjusted_prob = Phi(d1)
```

### Step 5 — Mispricing check

```
mispricing = adjusted_prob - market_price
if abs(mispricing) < theta_entry:
    return None
```

| Param | What it controls |
|-------|------------------|
| `theta_entry` | Minimum |mispricing| to enter. Default 0.03. |

### Step 6 — Direction

```
if mispricing > 0:  BUY_YES  (market undervalues YES post-vol-spike)
else:                BUY_NO   (market overvalues YES)
```

## Parameters Reference

| Param | Default | Used in | Effect | Tuning |
|-------|---------|---------|--------|--------|
| `vpx_threshold` | 1.5 | Step 2 | Min vol ratio to detect spike. Higher → fewer signals. | Test 1.3, 1.5, 2.0. |
| `beta_vpx` | 0.5 | Step 3 | Vol spike persistence. | Test 0.3, 0.5, 0.8. |
| `theta_entry` | 0.03 | Step 5 | Min |mispricing| to enter. | Grid search. |
| `tau_max` | 240.0 s | Step 1 | Upper entry bound. | Test 180, 240, 300. |
| `tau_min` | 15.0 s | Step 1 | Lower entry bound. | Test 10, 15, 20. |
| `replay_cadence_seconds` | 1.0 s | Dry replay | Historical eval spacing. | Test 1, 2, 5. |
| `q_max` | 1.0 | Step 6 | Market: USDC to spend. | Fraction of bankroll. |
| `theta_edge` | 0.01 | Step 6 | Min net edge per share. | Raise to skip marginal trades. |
| `order_class` | `"market"` | Step 6 | Order type. | Market for speed. |
| `time_in_force` | `"FOK"` | Step 6 | TIF for market orders. | `FOK` or `FAK`. |
| `fee_rate` | 0.07 | Step 6 | Taker fee. | Fixed by Polymarket. |

## Concrete Example

**Setup:** BTC spot = $68,450. Contract: "BTC > $68,500 at 15:00?"
(4 min to resolution). sigma_long = 0.0008/min. sigma_short = 0.0016/min.
YES price = 0.35.

1. **Vol ratio:** 0.0016 / 0.0008 = 2.0 > 1.5 threshold → **spike detected**.

2. **Projected sigma:** 0.0008 + 0.5 × (0.0016 - 0.0008) = 0.0012/min.

3. **Adjusted fair prob:**
   - d = ln(68450/68500) / (0.0012 × √4) = -0.000730 / 0.0024 = -0.304
   - π̂ = Φ(-0.304) = 0.381

4. **Mispricing:** 0.381 - 0.35 = +0.031 > 0.03 theta_entry. **BUY YES.**

The market has not yet incorporated higher vol into the OTM contract —
the YES should be worth 0.381, not 0.35.

## Relationship to Other Strategies

VPX is orthogonal to spot-direction strategies (SLA, VIT, PMR, ROM, WDM):
it trades solely on volatility regime shifts, not directional spot
movements.  It complements TDE: TDE exploits deterministic theta decay;
VPX exploits stochastic volatility changes.

## No Future-Knowledge Compliance

- `build_replay_plan()` generates eval times from window fixed properties only.
- Volatility is computed from data strictly at or before `eval_time`.
- `stop_on_signal=True` ensures the FIRST valid signal wins.

## How to Run

```bash
# Default params
python -m scripts.run dry --strategy=VPX

# Higher spike threshold, lower persistence
python -m scripts.run dry --strategy=VPX \
  --param vpx_threshold=2.0 \
  --param beta_vpx=0.3

# Shadow mode
python -m scripts.run shadow --strategy=VPX
```
