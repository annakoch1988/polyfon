# HMM — Hidden Markov Model Regime-Switching

## Idea

HMM is the next strategy in the `polymarket-strategies` sequence: a regime-aware adaptive model inspired by the HMM-RS proposal.

In the research spec, HMM-RS is a **meta-strategy** that allocates capital across other strategies based on a latent market regime. Polyfon’s current execution engine runs one strategy instance at a time, so this implementation adapts the proposal into a **single strategy with online regime inference**.

It infers a soft posterior across four regimes using only information available at the evaluation timestamp:

1. **calm** — low vol, tight spreads, small displacement from strike
2. **trending** — persistent directional move away from strike
3. **volatile** — elevated short-vs-long vol ratio, wider PM spreads
4. **converging** — near strike late in the window, fair value dominates

## Features used

The strategy uses currently available `Context` fields:

- `spot_price`
- `window_open_price`
- `fair_probability`
- `tau_seconds`
- `sigma_per_minute`
- `sigma_short_per_minute`
- `up_best_bid`, `up_best_ask`
- `down_best_bid`, `down_best_ask`

Derived features:

- displacement from open: `(spot - open) / open`
- distance to strike: `abs(displacement)`
- average PM spread across YES/NO books
- short/long realized volatility ratio

## Regime logic

The implementation uses a lightweight online approximation rather than a fully trained Baum-Welch Gaussian HMM. Each regime gets a score from the current feature vector, then scores are softmax-normalized into posterior-like probabilities.

The dominant regime must exceed `gamma_min`, otherwise the strategy does not trade.

## Entry logic by regime

### Calm

Assume mean reversion around the open/strike.

- spot above open by enough → bias toward **BUY_NO**
- spot below open by enough → bias toward **BUY_YES**

Trade only when the corresponding contract is also underpriced relative to fair value.

### Trending

Assume continuation.

- strong positive displacement + YES underpriced vs fair → **BUY_YES**
- strong negative displacement + NO underpriced vs fair → **BUY_NO**

### Volatile

Require volatility stress and wider spreads as confirmation.

- positive displacement + YES underpriced → **BUY_YES**
- negative displacement + NO underpriced → **BUY_NO**

### Converging

Late-window, near-strike regime where fair-value mispricing dominates.

- YES underpriced vs fair → **BUY_YES**
- NO underpriced vs fair → **BUY_NO**

## Parameters

| Param | Meaning |
|---|---|
| `gamma_min` | Minimum dominant regime posterior required to trade. Default `0.55`. |
| `novelty_threshold` | Safety threshold for ambiguous regime inference. Default `0.35`. |
| `trend_threshold` | Minimum displacement from open to classify a trend. Default `0.0008`. |
| `vol_ratio_threshold` | Minimum short/long vol ratio associated with volatile regime. Default `1.35`. |
| `spread_wide_threshold` | Average PM spread level used as volatile/converging confirmation. Default `0.035`. |
| `theta_entry` | Minimum fair-vs-market mispricing before considering entry. Default `0.03`. |
| `calm_reversion_threshold` | Minimum displacement used for calm-regime mean reversion. Default `0.0010`. |
| `tau_max` | Latest evaluation distance from window end. Default `180`. |
| `tau_min` | Earliest entry cutoff near expiry. Default `15`. |
| `replay_cadence_seconds` | Dry-run replay cadence. Default `1.0`. |
| `q_max` | Position size. Market = USDC spend, limit = shares. |
| `theta_edge` | Minimum expected net edge per share. |
| `order_class` | `market` or `limit`. |
| `time_in_force` | Market: `FOK`/`FAK`; Limit: `GTC`/`GTD`. |
| `fee_rate` | Polymarket taker fee rate. |

## Replay behavior

HMM evaluates from `end_et - tau_max` through `end_et - tau_min` at the configured cadence and stops on the **first valid signal**.

This remains compliant with Polyfon’s no-future-knowledge rule because:

- replay times are generated from window timestamps only,
- context is built using records with `timestamp <= eval_time`,
- the first fillable signal wins.

## Notes

- This is a practical adaptation of the HMM-RS proposal, not a full multi-strategy allocation engine.
- Metadata includes posterior probabilities for all four regimes plus the selected regime.
- Once Polyfon supports true strategy ensembles, this implementation can be upgraded into a real capital-allocation overlay.