# CRV — Cross-Contract Relative Value

## Description

The original CRV proposal targets **multi-strike binary strips** on the same
underlying (e.g. "BTC > $68,000", "BTC > $68,500", "BTC > $69,000").
It exploits violations of monotonicity and convexity between those
parallel contracts.

In the single up/down market model used here we adapt the idea to a
**spot-price monotonicity check**: for a binary call on
"spot > window-open price" the fair probability must be monotonically
increasing in spot.  When the market price violates this relationship by
more than `theta_entry` the strategy enters in the direction of fair
value restoration.

## Decision Flow

### Step 1 — Timing window

```
if tau < tau_min or tau > tau_max:
    return None
```

| Param | What it controls |
|-------|------------------|
| `tau_max` | Upper bound on entry timing (default 240 s). |
| `tau_min` | Earliest entry before resolution (default 15 s). |

### Step 2 — Spot vs strike

```
if spot > strike:  # binary call is in the money
    mispricing = reference_level - up_ask
    if mispricing < theta_entry: return None
    direction = BUY_YES
elif spot < strike:  # binary call is out of the money
    mispricing = up_ask - reference_level
    if mispricing < theta_entry: return None
    direction = BUY_NO
else:
    return None  # exactly at the money, no edge
```

The `reference_level` (default 0.50) is the fair-value boundary that
separates the "above strike" region from the "below strike" region.
When the market disagrees with this boundary by more than `theta_entry`,
we trade the inconsistency.

| Param | What it controls |
|-------|------------------|
| `theta_entry` | Minimum distance from `reference_level` to trigger (default 0.05). |
| `reference_level` | Fair-value boundary (default 0.50). Must lie in (0,1). |

### Step 3 — Edge check

```
edge = mispricing - fee / shares
if edge < theta_edge: return None
```

### Step 4 — Confidence

```
confidence = min(mispricing / theta_entry, 1.0)
```

## Parameters Reference

| Param | Default | Used in | Effect | Tuning |
|-------|---------|---------|--------|--------|
| `theta_entry` | 0.05 | Step 2 | Minimum mispricing to enter. | Grid search [0.03, 0.10]. |
| `reference_level` | 0.50 | Step 2 | Boundary between ITM and OTM fair values. | 0.45–0.55 range. |
| `tau_max` | 240.0 s | Step 1 | Latest entry before resolution. | Test 120, 180, 240, 300. |
| `tau_min` | 15.0 s | Step 1 | Earliest entry before resolution. | Test 10, 15, 20. |
| `replay_cadence_seconds` | 1.0 s | Dry replay | Historical eval spacing. | Test 1, 2, 5. |
| `q_max` | 1.0 | Step 3 | Market: USDC to spend. Limit: shares to buy. | Fraction of bankroll. |
| `theta_edge` | 0.01 | Step 3 | Minimum net edge per share to enter. | Raise to skip marginal trades. |
| `order_class` | `"market"` | Step 3 | `"market"` = spend q_max USDC. `"limit"` = buy q_max shares. | Use `"market"` for fast fill. |
| `time_in_force` | `"FOK"` | Step 3 | `"FOK"` = all-or-nothing market. `"FAK"` = partial fill. | Market → FOK/FAK. |
| `fee_rate` | 0.07 | Step 3 | Taker fee multiplier | Fixed by Polymarket. |

## How to Run

```bash
# Default params
python -m scripts.run dry --strategy=CRV

# Tighter entry, higher reference level
python -m scripts.run dry --strategy=CRV \
  --param theta_entry=0.04 \
  --param reference_level=0.52

# Shadow mode
python -m scripts.run shadow --strategy=CRV --collect
```
