# Strategy: Perpetual Funding Rate Sentiment Arbitrage (PFR)

## Overview
Crypto perpetual funding rate extremes predict short-term spot reversals
that Polymarket retail flow absorbs with a lag.  PFR adjusts the
Black-Scholes fair probability using a funding-rate sentiment index and
enters when the market is mispriced relative to the funding-implied
probability.

## Signal Construction

1. **Z-score**: `Z_f = (f_t - mu_f) / sigma_f`
2. **Sentiment index**: `Psi = -tanh(Z_f / lambda_f)`
3. **Adjusted spot**: `S_hat = S * exp(beta_f * Psi * sigma * sqrt(tau))`
4. **Fair probability**: `pi_hat = Phi(ln(S_hat / K) / (sigma * sqrt(tau)))`
5. **Mispricing**: `epsilon = pi_hat - p_market`

Enter when `|Z_f| > z_min` AND `|epsilon| > theta_entry`.

## Entry Direction
- `mispricing > 0` (fair > market) → **BUY_YES**
- `mispricing < 0` (fair < market) → **BUY_NO**

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| mu_f | 0.0 | Rolling mean of funding rate (%) |
| sigma_f | 0.0025 | Rolling std of funding rate |
| lambda_f | 2.0 | Sentiment scaling |
| beta_f | 0.8 | Spot impact coefficient |
| z_min | 2.0 | Minimum funding Z-score |
| theta_entry | 0.05 | Minimum |mispricing| |
| tau_max | 120.0 | Max τ to enter (seconds) |
| tau_min | 15.0 | Min τ to enter (seconds) |
| funding_rate | 0.0 | Current predicted funding rate (%) |
| q_max | 1.0 | Position size (USDC for market) |
| theta_edge | 0.01 | Minimum expected edge per share |
| order_class | market | Order type |
| time_in_force | FOK | TIF for market orders |
| fee_rate | 0.07 | Taker fee rate |

## CLI Examples
```bash
python -m scripts.run dry --strategy=PFR
python -m scripts.run dry --strategy=PFR --param theta_entry=0.03 --param z_min=2.5
python -m scripts.run shadow --strategy=PFR --collect
```

## References
- Makarov & Schoar (2020) — perpetual funding autocorrelation
- Ludwig (2022) — extreme funding percentiles predict BTC returns
- Borri & Shakhnov (2023) — funding spikes lead crypto flash crashes
