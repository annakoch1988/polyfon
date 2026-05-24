# Polyfon Review — Data Collection Robustness and WDM Dry Run

Date: 2026-05-23
Reviewer: AI coding agent
Scope: static review of collection and execution code, DB sanity checks, and observed dry-run behavior. No application code changed in this review.

## Executive summary

Short version:

- Data collection is partially valid, but not yet robust.
- Dry-run of WDM is wired up and starts correctly, but there are important logic defects that make the historical replay untrustworthy.
- The database contents show real collection happened, but also show coverage inconsistency and behavior suggesting correctness issues.
- Several bugs / correction-worthy issues were found. Prompts for a cheaper model to fix them were created separately in `/home/user/MyProjects/polyfon/prompts/`.

## Evidence reviewed

### Code reviewed
- `/home/user/MyProjects/polyfon/polyfon/collector/market_discovery.py`
- `/home/user/MyProjects/polyfon/polyfon/collector/spot_collector.py`
- `/home/user/MyProjects/polyfon/polyfon/collector/book_collector.py`
- `/home/user/MyProjects/polyfon/polyfon/collector/orchestrator.py`
- `/home/user/MyProjects/polyfon/polyfon/execution/engine.py`
- `/home/user/MyProjects/polyfon/polyfon/strategies/wdm.py`
- `/home/user/MyProjects/polyfon/polyfon/models.py`
- `/home/user/MyProjects/polyfon/polyfon/utils/fees.py`
- `/home/user/MyProjects/polyfon/scripts/run.py`

### Runtime / DB observations
- `python3 -m scripts.run list-strategies` works and lists `SLA`, `WDM`
- `python3 -m scripts.run dry --strategy=WDM` starts and prints expected parameters, but did not complete within the short timeout used in this environment
- DB summary observed:
  - windows: `27 resolved`, `5 pending`
  - order_books: `7,803,104` rows, `27` distinct `window_id`, `84` distinct `token_id`
  - spot_prices: `10,405` rows, `1` distinct symbol
  - trade_signals: `4`
  - positions: `4`

## Review of data collection

## What looks valid

### 1. Overall architecture is sane
The collection flow is conceptually good:
- discovery via Gamma API
- spot collector via Binance websocket
- order book collector via Polymarket websocket
- timer-driven 5-minute boundary management
- DB persistence via async SQLAlchemy
- orphan resolution via API polling

This is a sensible baseline for the product goal.

### 2. Window lifecycle design is reasonable
In `polyfon/collector/orchestrator.py`:
- `_window_manager()` opens/closes windows on precise ET 5-minute boundaries
- `_sync_windows()` discovers future windows
- `_resolve_orphans()` resolves closed unresolved windows

That is a good architecture for deterministic 5-minute market windows.

### 3. Discovery logic is pragmatic
`market_discovery.py` paginates through Gamma results because stale events may be returned first. That matches project notes and is a reasonable workaround.

### 4. Binance spot collector is acceptable for a first version
`BinanceSpotCollector`:
- reconnects after exceptions
- supports multiple ticker streams in one URI
- normalizes symbol and close price

Simple, but acceptable.

### 5. Polymarket book collector handles the right event families
`PolymarketBookCollector` handles:
- `book`
- `price_change`
- `best_bid_ask`
- `last_trade_price`
- `market_resolved`

This aligns with the stated websocket usage.

## What is not robust / likely problematic

### 1. Spot collection coverage is suspicious for multi-coin operation
The DB has only `1` distinct symbol in `spot_prices`, while the configured/default coin list is `BTC,ETH` and dry mode printed `coins=BTC, ETH`.

This means one of:
- only one coin was ever actually collected, or
- multi-coin collection is not working reliably.

This is a red flag for collection robustness.

### 2. Order-book token count looks inconsistent
Observed:
- `27` windows with books
- `84` distinct token IDs

For a clean YES/NO pair per window, a rough expectation would be about `54` distinct tokens for `27` windows. `84` suggests some combination of:
- token churn not reflected cleanly in window linkage,
- subscription over-collection,
- mismatched book/window linkage,
- or retained rows from earlier inconsistent state.

This is not definitive proof of corruption, but it is not confidence-inspiring.

### 3. Stale carry-forward state is generated but not persisted correctly
In `book_collector.py` stale carry-forward records are created internally, but the callback path does not include a stale flag, and `orchestrator.py` persists all book rows with `stale=False`.

So the database field `order_books.stale` is effectively unreliable.

This is a confirmed bug.

### 4. Subscription lifecycle likely only adds subscriptions and does not remove them
`book_collector.py` has an update path that sends:
```json
{"operation": "subscribe", "assets_ids": [...]}
```
There is no explicit unsubscribe path.

Meanwhile the orchestrator comments imply the active token set can be reduced over time. Unless the websocket protocol treats a subscribe update as a full replacement, the implementation likely accumulates subscriptions.

Potential consequences:
- token subscription bloat
- excess websocket traffic
- excessive DB writes
- inflated order-book volume

This is a likely robustness defect.

### 5. Order-book volume is operationally extreme for SQLite
Observed: `7,803,104` order book rows for a relatively small window set.

Even if technically functioning, this is likely to become a serious problem for:
- DB size growth
- query latency
- dry-run speed
- long-term maintainability

This is an operational weakness even if not a correctness bug by itself.

### 6. Debug stderr printing remains inside the websocket hot path
`book_collector.py` still prints raw resolution and unknown events to stderr. This is not robust production behavior.

## Confirmed collection-side issues

### Issue A — stale flag persistence bug
- File(s): `polyfon/collector/book_collector.py`, `polyfon/collector/orchestrator.py`
- Problem: stale records are emitted internally but persisted as `stale=False`
- Severity: High

### Issue B — carry-forward task lifecycle is unmanaged
- File: `polyfon/collector/book_collector.py`
- Problem: `_carry_forward_loop()` is created without storing a task handle, so it is not explicitly managed during shutdown
- Severity: Medium

### Issue C — likely additive-only websocket subscription updates
- File(s): `polyfon/collector/book_collector.py`, `polyfon/collector/orchestrator.py`
- Problem: update path appears to subscribe repeatedly without explicit unsubscribe behavior
- Severity: Medium

## Review of WDM dry run

## What works

### 1. WDM is correctly registered and exposed in CLI
- `WDMStrategy` is decorated with `@register`
- imported in `polyfon/strategies/__init__.py`
- shown by `python3 -m scripts.run list-strategies`

### 2. Dry mode starts correctly for WDM
Observed startup output showed:
- expected strategy defaults
- `Dry run: 27 historical windows`
- per-window processing lines such as `SKIP BTC ... no signal`

So wiring, instantiation, and top-level execution are functioning.

## What is wrong in historical replay

### 1. Dry mode ignores the coin filter
In `execution/engine.py`, `run_dry()` selects all closed/resolved windows and does not filter by `self.coins`.

Consequence:
- `--coins=ETH` would still process BTC windows

This is a confirmed bug.

### 2. Historical fill uses the wrong book timestamp
In dry replay, `_check_dry_window()` evaluates the strategy at `window.end_et - 10s`, but `_simulate_fill()` then queries the latest order book row for the token/window, regardless of evaluation time.

That means the simulated fill can use information from after the decision point.

This is the most important dry-run correctness defect.

### 3. `window_open_price` query is not properly time-bounded
`_build_context()` chooses the earliest spot after `window.start_et`, but does not bound that query by `eval_time` or `window.end_et`.

If no spot exists during the actual window, it can incorrectly select a later price and still call it the window-open price.

This is a confirmed logic bug affecting WDM signal quality.

### 4. Dry replay is slower than expected for the small dataset
The run did not finish within the short timeout despite only `27` historical windows. Most likely causes:
- very large `order_books` table
- repeated DB round trips
- no visible indexing strategy in reviewed code
- separate context queries per window and token

This is more of a scaling issue than a correctness bug, but it matters.

## Severity summary

### High severity
1. Historical fill price is wrong in dry mode
2. `stale` carry-forward information is lost before persistence
3. Dry mode ignores `--coins` filter

### Medium severity
4. `window_open_price` may be selected from outside the intended historical window
5. Subscription lifecycle likely does not remove old tokens server-side
6. Carry-forward background task is not explicitly managed

### Low / operational severity
7. Very large order-book growth in SQLite
8. Debug prints remain in websocket collector hot path

## Final judgment

## Collection
The collection pipeline clearly runs, but I would not yet call it robust.

It is valid enough to show:
- windows are being discovered and persisted
- resolutions are happening
- spot data is being collected
- order book data is being collected

But it is not robust enough to fully trust because of:
- stale persistence bug
- likely subscription lifecycle defect
- suspicious multi-coin coverage evidence
- very high order-book row explosion

## WDM dry run
The WDM dry-run path is wired and operational at the CLI level, but the historical replay is not yet trustworthy due to:
- wrong fill timing logic
- ignored coin filter
- weak `window_open_price` time bounding

## Recommended next actions
See prompt files created in `/home/user/MyProjects/polyfon/prompts/`:
- `fix_dry_run_historical_fill_and_filters.md`
- `fix_book_stale_persistence_and_task_lifecycle.md`
- `review_and_fix_polymarket_subscription_lifecycle.md`


## Follow-up review: collection validity under websocket interruptions

User clarification: BTC-only collection was intentional, so BTC-only coverage in the current DB is not treated as a bug.

### Question addressed
How robust are underlying spot and quote collectors under websocket interruptions, and is there any mechanism that marks a window invalid when data coverage is poor?

## Executive answer

- The collectors are somewhat resilient operationally, but not robust enough to guarantee data validity.
- There is currently no mechanism that marks a window invalid when coverage is poor or interrupted.
- The system has reconnect loops, quote carry-forward with stale marking, run-session tracking, and startup cleanup of unfinished windows.
- The system does not have explicit coverage accounting, gap detection at the window level, completeness thresholds, invalid/partial window status, spot staleness marking, or post-interruption backfill.

So the correct conclusion is:
- the collectors can continue running through transient websocket problems,
- but the stored windows are not certified as fully covered or valid.

## Spot / underlying price collection robustness

Reviewed file:
- `/home/user/MyProjects/polyfon/polyfon/collector/spot_collector.py`

### What exists today

`BinanceSpotCollector` has a basic reconnect loop:
- `_consume()` runs while `_running`
- on any exception it sleeps 5 seconds and reconnects
- latest seen prices are cached in memory via `_latest`

This gives basic continuity and makes the collector reasonably tolerant of transient disconnections.

### What is missing

#### 1. No explicit gap detection
If the Binance websocket disconnects for several seconds:
- no spot rows are written during the gap
- no row is inserted saying the feed was interrupted
- there is no stale flag on `SpotPrice`
- no affected window is marked degraded or invalid

Historical consumers only see sparse or missing samples, not a feed-health annotation.

#### 2. No backfill after reconnect
When reconnect happens, collection resumes from the current moment only.
Missed underlying prices are not recovered.

This matters for:
- exact window-open price
- exact T-10 evaluation price
- volatility estimation
- strategy replay reliability

#### 3. Queue overflow drops ticks
In `polyfon/collector/orchestrator.py`, spot ticks are dropped when the spot queue is full:

```python
except asyncio.QueueFull:
    logger.warning("Spot queue full – dropping tick for %s", symbol)
```

This is a real data-loss path and does not trigger any window invalidation logic.

#### 4. No heartbeat / coverage SLA for spot
There is no policy like:
- if no spot sample arrives for more than N seconds during an open window, mark coverage degraded
- if no sample exists near window open, mark window invalid
- if no sample exists near T-10, mark strategy inputs unreliable

### Spot collection judgment
Operationally, spot collection is resilient enough to reconnect.
From a data-validity perspective, it is not robust enough to certify coverage.

## Quote / order-book collection robustness

Reviewed files:
- `/home/user/MyProjects/polyfon/polyfon/collector/book_collector.py`
- `/home/user/MyProjects/polyfon/polyfon/collector/orchestrator.py`

### What exists today

#### 1. Reconnect loop
`PolymarketBookCollector._consume()` reconnects after exceptions with a short sleep.

#### 2. Ping keepalive
The collector sends `{}` every 10 seconds as required by the Polymarket market websocket.

#### 3. Carry-forward with stale marking
If no message arrives for a token for more than `carry_timeout_sec`:
- `_carry_forward_loop()` emits a synthetic record
- that record is marked `stale=True`
- stale state is now persisted through the orchestrator into `OrderBook.stale`

This is useful and materially better than the spot collector in terms of observability.

#### 4. Subscription lifecycle handling
Because the protocol is additive-only for subscribe updates, the collector now forces reconnect when assets must be removed.
That is a reasonable operational workaround.

### What is still missing

#### 1. No outage annotation at the window level
Carry-forward provides continuity of the quote series, but it does not certify validity.

If the quote feed dies near expiry:
- the DB may still contain quote rows
- many of them may be stale carry-forward rows
- the window is still not marked degraded or invalid

#### 2. Carry-forward can mask outages
This is a key point.

Stale quote continuity is synthetic continuity.
It may be useful for execution logic, but it should not be mistaken for proof of fresh market coverage.

A window can appear to have quote continuity while actually being mostly stale during a critical segment.

#### 3. Queue overflow drops book updates
In `orchestrator.py`:

```python
except asyncio.QueueFull:
    logger.warning("Book queue full – dropping update for %s", token_id)
```

So quote loss can happen during bursts, and there is no automatic quality downgrade for the affected window.

#### 4. No minimum freshness requirement for a window
There is no rule such as:
- must have at least one non-stale quote near T-10
- must have at least one non-stale quote near close
- must have both UP and DOWN token coverage during the window
- stale ratio above X% makes the window invalid

### Quote collection judgment
Quote collection is more instrumented than spot collection because it preserves stale state.
However, it is still not robust enough to guarantee window validity, because stale continuity is not turned into a quality policy.

## Is there a mechanism that marks a window invalid for poor coverage?

### Direct answer
No.

### Evidence

#### 1. Window model has no invalid/degraded status
In `polyfon/models.py`, window status is only documented as:
- `pending`
- `open`
- `closed`
- `resolved`

There is no status like:
- `invalid`
- `partial`
- `degraded`
- `incomplete`
- `interrupted`

#### 2. No coverage metadata on windows
The `Window` model has no fields for:
- spot coverage health
- quote coverage health
- missing-open flag
- missing-eval flag
- stale ratio
- invalid reason
- quality score

#### 3. Lifecycle code does not validate coverage
`_sync_windows()`, `_window_manager()`, and `_resolve_orphans()` manage discovery, open/close timing, and resolution.
They do not assess whether collected data for a window is complete enough to trust.

#### 4. Spot data has no stale concept
Unlike `OrderBook`, `SpotPrice` has no stale or degraded marker at all.

## What protections exist today, and what they actually mean

### 1. RunSession tracking
A `RunSession` whose `finished_at` is null indicates the collector run was interrupted.

This is useful for operational diagnosis.
It does not identify:
- which windows suffered data gaps
- which timestamps were missed
- whether any resolved window is still safe to use

### 2. Startup cleanup of unfinished windows
At startup, unfinished `pending` and `open` windows from prior runs are removed.

This prevents stale partial windows from remaining active, which is good.
But it does not repair or invalidate windows that were already closed/resolved with poor coverage.

### 3. Persisted stale quote rows
Persisting `OrderBook.stale` is useful because later analysis can, in principle, detect synthetic carry-forward periods.

However, there is currently no policy in code that interprets these stale rows into a window quality decision.

## Practical validity assessment

### Underlying price validity
Spot collection is only moderately robust:
- reconnects after interruption
- but no backfill
- no gap marker
- no stale marker
- no invalidation of affected windows

Therefore, if the Binance feed drops near the window open, T-10, or close, the window may remain in the DB as a normal window without any indication that underlying coverage was compromised.

### Quote validity
Quote collection is stronger operationally than spot collection because it records stale carry-forward rows.
But it still does not guarantee validity because:
- stale continuity can hide true feed loss
- there is no threshold-based invalidation
- there is no requirement for fresh quotes near critical times
- no window is marked degraded or invalid based on quote health

## Bottom-line conclusion

Current state:
- Operational resilience: fair
- Data validity assurance: weak
- Window-level coverage validation: absent

The collectors are serviceable for gathering data and can survive transient websocket interruptions.
But they are not yet robust enough to guarantee that each stored window is valid for research or trading evaluation.

The most important missing capability is this:
- the system does not convert data-quality problems into explicit window validity decisions.

So the answer to the user's core question is:
- yes, there is basic reconnect behavior and quote carry-forward,
- no, there is no mechanism today that marks a window invalid when coverage is poor.
