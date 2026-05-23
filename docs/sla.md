# SLA — Spot-Led Latency Arbitrage

## Description

SLA exploits the latency between spot price moves on centralized exchanges (Binance) and price adjustments on Polymarket's prediction market tokens. When the CEX spot moves, the fair probability of "spot > open at expiry" changes immediately, but Polymarket's token price may lag by seconds or tens of seconds — especially in volatile conditions.

The strategy computes the fair probability using a Black-Scholes binary call approximation:

$$P(\text{UP}) = \Phi\left(\frac{\ln(S / K) + (\mu - \sigma^2/2) \tau}{\sigma \sqrt{\tau}}\right)$$

where:
- `S` = current spot price (CEX)
- `K` = window open price (the strike — the spot at the start of the 5-minute window)
- `τ` = time to resolution in minutes
- `σ` = rolling realized volatility per minute

When `fair_prob - market_price` exceeds a threshold, the strategy buys the undervalued token and holds until the market converges — either through automatic market-maker repricing or other arbitrageurs.

---

## Decision Flow (step by step)

### Step 1 — Timing check

```
if tau_seconds is None or tau_seconds < tau_min:
    return None
```

The strategy only enters when enough time remains for convergence.

| Param | What it controls |
|-------|------------------|
| `tau_min` | Minimum seconds before resolution. If too close to expiry, there may not be enough time for the market to converge. Default 30s. |

### Step 2 — Fair probability computation

This is done by the execution engine (`_build_context`), not the strategy itself. `context.fair_probability` uses `fair_probability(spot=current_spot, strike=window_open_price, tau_seconds=tau, sigma_per_minute=sigma)`.

### Step 3 — Get market price

```
market_price = best_ask if best_ask else best_bid
```

The strategy uses whichever side is available: ask for the token if present, otherwise bid. This is the current price that the market is offering.

### Step 4 — Mispricing calculation

```
mispricing = fair_prob - market_price
```

If `fair_prob > market_price` → the token is undervalued → buy YES.
If `fair_prob < market_price` → the token is overvalued → sell YES (i.e., buy NO).

### Step 5 — Resolve order size

`_resolve_order(price)` interprets `q_max` based on `order_class`:

| `order_class` | `q_max` means | Shares = | Constraint | Valid `time_in_force` |
|-------------|---------------|----------|------------|----------------------|
| `limit` (default) | Number of shares | `q_max` | `q_max >= 5` AND `q_max * price >= $1.00` | `GTC` (default) or `GTD` |
| `market` | USDC to spend | `q_max / price` | `q_max >= $1.00` | `FOK` or `FAK` |

SLA defaults to **limit** orders because it enters with 30s+ runway — plenty of time for a limit order to rest on the book and get a price improvement. Market orders are also supported for scenarios where you want immediate execution.

### Step 6 — Entry threshold

```
shares, notional = _resolve_order(price)
if shares is None:
    return None                           # ← constraint check
fee = taker_fee_usdc(shares, price, fee_rate)
edge = mispricing - fee / shares
```

| Param | What it controls |
|-------|------------------|
| `theta_entry` | Minimum |mispricing| required to enter. Lower → more trades, many at slim edge. Default 0.05. |
| `order_class` | `"limit"` → `q_max` = shares. `"market"` → `q_max` = USDC to spend. Default `"limit"`. |
| `time_in_force` | `"GTC"` → limit rests on book. `"FOK"` → market fill-or-kill. Default `"GTC"`. |
| `q_max` | For limit: shares to buy (min 5, min $1 notional). For market: USDC to spend (min $1). |
| `fee_rate` | Taker fee rate (0.07 for crypto). |

### Polymarket order constraints

`_resolve_order(price)` enforces Polymarket's minimum order rules:

| `order_class` | Check | Reason |
|-------------|-------|--------|
| `limit` | `q_max >= 5` | `INVALID_ORDER_MIN_SIZE` — rejects < 5 shares |
| `limit` | `q_max * price >= $1.00` | Minimum notional for limit orders |
| `market` | `q_max >= $1.00` | Minimum notional; can't spend less than $1 USDC |

Fee: `fee = shares * fee_rate * price * (1 - price)` per Polymarket docs.

---

## Parameters Reference

## Parameters Reference

| Param | Default | Used in | Effect |
|-------|---------|---------|--------|
| `theta_entry` | 0.05 | Step 6 | Minimum `|fair_prob - market_price|` to enter. Lower = more frequent trades but lower average edge. |
| `tau_min` | 30.0 s | Step 1 | Minimum seconds before resolution to allow entry. Higher = more convergence time, but fewer entry windows. |
| `q_max` | 100 | Step 5–6 | Market: USDC to spend. Limit: shares to buy. |
| `order_class` | `"limit"` | Step 5 | `"limit"` → rest on book (price improvement). `"market"` → immediate fill. |
| `time_in_force` | `"GTC"` | Step 5 | `"GTC"` = good-til-cancelled. `"GTD"` = good-til-date. `"FOK"`/`"FAK"` = market. |
| `fee_rate` | 0.07 | Step 6 | Polymarket taker fee multiplier (fixed by platform). |

---

## How to Run

```bash
# Default params
python -m scripts.run dry --strategy=SLA

# Lower threshold, longer convergence time
python -m scripts.run dry --strategy=SLA \
  --param theta_entry=0.03 \
  --param tau_min=45

# Larger position
python -m scripts.run dry --strategy=SLA \
  --param q_max=250

# Shadow mode
python -m scripts.run shadow --strategy=SLA --collect
```
