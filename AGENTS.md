# AGENTS.md — Polyfon Project Compass

**Polyfon** is a quantitative trading system for 5-minute crypto-resolution prediction markets on Polymarket.

## Technology Stack (finalized)
- **Language**: Python 3.11+
- **Database**: SQLite (single file, portable)
- **ORM**: SQLAlchemy 2.0 with async support (`aiosqlite` driver)
- **HTTP**: `httpx` for REST API calls (market discovery, orphan resolution)
- **WebSocket**: `websockets` for Binance spot feeds AND Polymarket order book feeds
- **CLI**: `click` + `rich` for terminal output
- **Scientific**: `numpy`, `pandas`, `scipy`

## Project Goals
1. Build a flexible, extensible Python trading infrastructure.
2. Support execution modes: `collect`, `dry`, `shadow`, `wet` (wet is postponed).
3. Persist all data in SQLite via SQLAlchemy async ORM.
4. Implement 18 strategies (SLA, PMR, MPR, VIT, TDE, CRV, OBI, VPX, CLL, HMM, ROM, MIP, PFR, RND, HPE, KLD, ARL, EVT) + 1 supplementary strategy WDM (Window Delta Momentum).
5. Every strategy fits a common interface via `BaseStrategy` + `@register` decorator.
6. Fee calculations strictly follow Polymarket docs: `fee = C * feeRate * p * (1 - p)`.
7. Windows are 5-minute intervals aligned to ET clock boundaries.
8. Best-bid/ask carry-forward implemented in both WebSocket collector and execution engine.
9. Outcomes resolved via Gamma API polling (`_resolve_orphans` runs every 60s in main loop) + WebSocket `market_resolved` events (best-effort; not emitted by Polymarket for automated 5-min markets).

## Architecture (single-process, asyncio-based)
```
scripts/run.py CLI (click)
    collect  →  CollectionOrchestrator
    dry      →  ExecutionEngine(mode="dry")
    shadow   →  ExecutionEngine(mode="shadow")
    wet      →  [POSTPONED]

CollectionOrchestrator:
    PolymarketDiscovery  →  REST API: find active 5-min crypto markets
    BinanceSpotCollector  →  WebSocket: spot prices → DB
    PolymarketBookCollector  →  WebSocket: best bid/ask → DB
    WindowManager  →  timer-driven open/close at 5-min boundaries
    _resolve_orphans  →  Gamma API resolution for past closed windows

ExecutionEngine:
    loads StrategyRegistry → instantiates chosen strategy
    builds Context (spot, book per token, window_open_price, fair prob, tau)
    calls strategy.on_tick(window, context) → Signal
    logs signal + simulates long-only Polymarket entry (BUY_YES / BUY_NO only) in dry/shadow

Database (SQLite):
    collect_run_sessions, dry_run_sessions, dry_run_window_results, dry_run_trade_results,
    windows, spot_prices, order_books, trade_signals, positions, config
```

## WebSocket Details

### Polymarket Market Channel
- **URL**: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **Subscription**:
  ```json
  {"assets_ids": ["<token_id_1>", "<token_id_2>"], "type": "market", "custom_feature_enabled": true}
  ```
- **Update subscription** (without reconnect):
  ```json
  {"operation": "subscribe", "assets_ids": ["<token_id>"]}
  ```
- **Ping**: send `{}` every 10 seconds
- **Handled events**:
  - `book` — full orderbook snapshot (emitted on subscribe + after trades)
  - `price_change` — delta updates with best_bid / best_ask per asset_id
  - `best_bid_ask` — direct best bid/ask update
  - `last_trade_price` — trade execution event
   - `market_resolved` — outcome notification (fields: `winning_asset_id`, `winning_outcome`)
- **Ignored events**: `tick_size_change`, `new_market`, `pong`

### Binance Spot
- **URL**: `wss://stream.binance.com:9443/ws/<coins>@ticker`
- Combined stream format: `wss://stream.binance.com:9443/ws/btcusdt@ticker/ethusdt@ticker`

## Key Directories
```
polyfon/
    config.py          # Pydantic settings from .env
    database.py        # SQLAlchemy async engine + session_scope
    models.py          # SQLAlchemy ORM models
    collector/
        market_discovery.py   # Polymarket CLOB REST API discovery + fetch_resolution()
        spot_collector.py     # Binance WebSocket spot feed
        book_collector.py     # Polymarket WebSocket book (best bid/ask + carry-forward)
        orchestrator.py       # Ties collectors + window manager together
    pricing/
        fair_probability.py   # BS binary call fair prob
        volatility.py         # Rolling realized vol
    strategies/
        base.py               # BaseStrategy, Context, Signal, StrategyRegistry
        sla.py                # Strategy 1: Spot-Led Latency Arbitrage
        wdm.py                # Strategy: Window Delta Momentum
        tde.py                # Strategy: Time Decay Effect
        rom.py                # Strategy: Range Oscillation Momentum
        pmr.py                # Strategy: Price Momentum Reversal
        obi.py                # Strategy: Order Book Imbalance
        mpr.py                # Strategy: Mean Price Reversion
        vit.py                # Strategy: Volume-Spike Informed Trading
        crv.py               # Strategy: Cross-Contract Relative Value
        cll.py               # Strategy: Cross-Asset Correlation Lead-Lag
        vpx.py               # Strategy: CEX Toxicity Volatility Indicator
    execution/
        engine.py             # ExecutionEngine (dry/shadow mode)
    utils/
        fees.py               # taker_fee_usdc, effective_cost, net_pnl
scripts/
    run.py                 # CLI entry point
docs/
    sla.md                 # SLA strategy reference
    wdm.md                 # WDM strategy reference
    tde.md                 # TDE strategy reference
    rom.md                 # ROM strategy reference
    pmr.md                 # PMR strategy reference
    obi.md                 # OBI strategy reference
    mpr.md                 # MPR strategy reference
    vit.md                 # VIT strategy reference
    crv.md                 # CRV strategy reference
    cll.md                 # CLL strategy reference
    vpx.md                 # VPX strategy reference
```

## Execution Modes
- **collect**: Only data collection. `python -m scripts.run collect --coins=BTC,ETH`
- **dry**: Run strategy on historical windows saved in DB. No real money.
- **shadow**: Real-time simulated trading — like wet but no real orders. Tracks PnL.
- **wet**: Real orders via CLOB API. **POSTPONED.**

## Adding a New Strategy
1. Create `polyfon/strategies/<name>.py`
2. Inherit `BaseStrategy`, set `name` class attribute
3. Implement `on_tick()` and `on_window_close()`
4. Decorate the class with `@register`
5. Import it in `polyfon/strategies/__init__.py`

Example:
```python
from polyfon.strategies.base import BaseStrategy, Context, Signal, register

@register
class MyStrategy(BaseStrategy):
    name = "MY"
    def on_tick(self, window, context):
        ...
    def on_window_close(self, window, context):
        ...
```

## Database Schema (SQLAlchemy)
- **RunSession** (`collect_run_sessions`): id, started_at, finished_at (null = aborted/crashed)
- **DryRunSession**: id, mode, strategy, strategy_params_json, coins_csv, window_slugs_csv, replay_cadence_seconds, total_windows, processed_windows, signaled_windows, filled_windows, total_trades, total_realized_pnl, started_at, finished_at, status, notes
- **DryRunWindowResult**: id, dry_run_session_id, window_id, strategy, window_index, status, reason, signal_direction, signal_edge, signal_confidence, order_class, signal_time, resolution, realized_pnl, trade_count
- **DryRunTradeResult**: id, dry_run_window_result_id, position_id, side, order_class, shares, entry_price, notional, entry_fee, total_cost, opened_at, resolution, settlement_price, revenue, fees_paid, pnl, outcome
- **Window**: id, slug, title, underlying, start_et, end_et, outcome, status (pending/open/closed/resolved), run_session_id, up_token_id, down_token_id, condition_id, fee_rate, tick_size
- **SpotPrice**: id, symbol, price, timestamp, source
- **OrderBook**: id, window_id, token_id, best_bid, best_ask, bid_size, ask_size, last_trade_price, stale, timestamp
- **TradeSignal**: id, strategy, window_id, direction (`BUY_YES` or `BUY_NO` only for entry simulation), size, expected_edge, confidence, timestamp
- **Position**: id, mode, window_id, strategy, contract_side (`YES` or `NO` contract only), entry_price, size, exit_price, pnl, fees_paid, status, opened_at, closed_at
- **ConfigKV**: id, key, value

## Fee Rules
- Taker fee: `fee = round(shares * fee_rate * price * (1 - price), 5)`
- Maker fee: 0
- Crypto fee_rate: 0.07 (7%)
- Fee table validated against docs.polymarket.com/trading/fees

## CLI Reference
```bash
# Data collection only
python -m scripts.run collect
python -m scripts.run collect --coins=BTC,ETH

# Dry mode (replay from DB)
python -m scripts.run dry --strategy=SLA
python -m scripts.run dry --strategy=WDM
python -m scripts.run dry --strategy=TDE
python -m scripts.run dry --strategy=SLA --coins=BTC,ETH --collect

# Shadow mode (real-time simulation)
python -m scripts.run shadow --strategy=WDM --coins=BTC,ETH --collect

# Dry/Shadow CLI now supports --param key=value (repeatable)
python -m scripts.run dry --strategy=WDM --param theta_entry=0.0005 --param tau_max=20

# TDE examples
python -m scripts.run dry --strategy=TDE
python -m scripts.run dry --strategy=TDE --param tau_max=60 --param theta_entry=0.04
python -m scripts.run shadow --strategy=TDE --collect

# ROM examples
python -m scripts.run dry --strategy=ROM
python -m scripts.run dry --strategy=ROM --param tau_max=90 --param tau_min=45
python -m scripts.run shadow --strategy=ROM --collect

# VIT examples
python -m scripts.run dry --strategy=VIT
python -m scripts.run dry --strategy=VIT --param imb_threshold=0.15 --param v_threshold=0.002
python -m scripts.run shadow --strategy=VIT --collect

# CLL examples
python -m scripts.run dry --strategy=CLL
python -m scripts.run dry --strategy=CLL --param theta_entry=0.02 --param rho=0.80
python -m scripts.run shadow --strategy=CLL --collect

# VPX examples
python -m scripts.run dry --strategy=VPX
python -m scripts.run dry --strategy=VPX --param vpx_threshold=2.0 --param beta_vpx=0.3
python -m scripts.run shadow --strategy=VPX --collect

# List strategies
python -m scripts.run list-strategies
```

## Environment Variables (.env)
```
DATABASE_URL=sqlite+aiosqlite:///./polyfon.db
POLYMARKET_API_URL=https://clob.polymarket.com
BINANCE_WS_URL=wss://stream.binance.com:9443/ws
COINS=BTC,ETH
LOG_LEVEL=INFO
```

## Implementation Phases
1. **Phase 1 (COMPLETE)**: Bootstrap — schema, config, WebSocket collectors, fair pricing, SLA strategy, dry mode, CLI.
2. **Phase 2 (IN PROGRESS)**: Shadow mode refinement, session tracking, resolution engine (WS + API), orphan cleanup.
3. **Phase 3**: Wet mode (CLOB API orders, private key). **POSTPONED.**
4. **Phase 4 (IN PROGRESS)**: Additional strategies — TDE, ROM, PMR, OBI, MPR, VIT, CRV, CLL, VPX implemented. Remaining: HMM, MIP, PFR, RND, HPE, KLD, ARL, EVT.
5. **Phase 5**: Python ML bridge (GARCH, EVT, HMM, Hawkes) for advanced strategies.

## Known Issues Resolved
- **Gamma API pagination broken**: `/events?series_slug=...` capped at 100 events with broken skip pagination. Replaced by synthetic window generation from ET boundary + individual slug-based Gamma lookup (`/events?slug={slug}`).
- **Aware/naive datetime mismatch**: `_end_from_slug` returned aware UTC but `now_n` in `_sync_windows` was naive, causing `TypeError: can't compare offset-naive and offset-aware datetimes`. Fixed by stripping tzinfo after slug extraction.
- **ET endDate parsing**: Polymarket endDate strings carry "Z" but represent ET clock time. All paths now parse as ET then convert to UTC.
- **Spurious book_disconnect invalidations**: 3-second grace period after window open suppresses invalidations caused by subscription setup timing.
- **Discovery time normalization fix**: Gamma `eventStartTime` / `endDate` are now treated as true UTC instants for recurring 5-minute markets. Discovery normalization prefers these explicit fields over slug-derived timestamps, fixing mislabelled `DISCOVERED` windows such as `btc-updown-5m-1780479000` being shown as `5:25–5:30 AM ET` instead of the correct `5:30–5:35 AM ET`.

## Agent Protocol
- After making any functional changes or achieving progress, update this file to reflect the current state. Do not wait to be asked.

## ABSOLUTE REQUIREMENT: No Future-Knowledge in Simulation
The dry run simulation MUST NOT look ahead. At every evaluation point the simulator may only use information available at or before that timestamp.
- `build_replay_plan()` MUST generate eval times from window fixed properties (`start_et`, `end_et`) only — never from market data.
- `_build_context()` queries spot/order book with `timestamp <= eval_time` (strictly past-and-present).
- `_check_dry_window()` iterates eval times chronologically; `stop_on_signal=True` MUST be set so the FIRST valid signal wins (not the "best" across the window).
- `_simulate_fill()` queries order books with `timestamp <= eval_time`.
- No strategy or engine code may scan a time band and retroactively select the optimal entry point.

**Any new strategy must be audited for compliance before merging. Any refactor of the engine must preserve these guarantees.**

## Notes for Future Agents
- Always use `session_scope()` context manager for DB transactions.
- All timestamps are naive UTC stored in the DB; ET boundary semantics kept in `start_et` / `end_et`.
- Carry-forward logic is in `PolymarketBookCollector`: if no message for >5s, emits stale record.
- Use `StrategyRegistry.instantiate(name)` to create strategy instances.
- Keep strategy logic stateless where possible; persist state via DB ConfigKV if needed.
- Polymarket entry simulation is long-only. There is no `SHORT_YES` or `SHORT_NO` concept in this project. Bearish exposure must be expressed as `BUY_NO`, not `SELL_YES`.
- `RunSession` tracks each collector start; `finished_at` null = interrupted.
- `DryRunSession` tracks each historical replay run; window/trade detail is stored in `dry_run_window_results` and `dry_run_trade_results` for later analysis.
- Unfinished/invalid windows (open/pending/invalid from previous sessions) are DELETED at startup, and any previous-run window with zero `order_books` rows is also deleted as analytically unusable.
- Unfinished/invalid windows (open/pending/invalid from previous sessions) are DELETED at startup (`Maintenance` section), and any previous-run window with zero `order_books` rows is also deleted as analytically unusable.
- Resolution is done by `_resolve_orphans()` which polls the Gamma API (`fetch_resolution(slug)`) for closed-but-unresolved windows. It runs at startup during `Maintenance` and every 60s in the main loop.
- Gamma API `fetch_resolution(slug)` uses `outcomePrices` (stringified JSON array, e.g. `'["1","0"]'`) not the `outcome` field (always `None` for automated markets). Maps "Up" → "Yes", "Down" → "No".
- **Gamma API discovery fix**: The `/events?series_slug=...` endpoint caps at 100 results and pagination (skip) wraps instead of returning fresh pages. Discovery now generates window slugs synthetically from ET clock boundaries and queries Gamma individually by slug (`/events?slug={slug}`). This reliably returns the correct window with token IDs, condition ID, and title.
- `_series_events` and `_is_relevant_now` are no longer used for discovery; only `_fetch_event_by_slug` + `_normalise` are called.
- **Window invalidation at open**: A 3-second grace period suppresses book_disconnect invalidations that fire within 3s of window open (prevents spurious invalidations caused by subscription setup lag).
- **ET endDate parsing**: Polymarket's `endDate` field carries a trailing "Z" but actually represents America/New_York clock time. All endDate parsing interprets as ET then converts to UTC. Slug epoch (Unix timestamp in slug suffix) is preferred as the primary source of end_utc because it is always correct UTC.
- **Datetime tz consistency**: All `end_utc` values stored in the DB are naive UTC. `_end_from_slug` returns aware UTC → must `.replace(tzinfo=None)` before storage. Comparisons with `now` in `_series_events` and `_is_relevant_now` must use consistent awareness (both aware or both naive).
- **Collection startup/boundary fix**: Discovery now anchors on the current ET 5-minute slot boundary so the currently opening window is included instead of being skipped until the next sync. Orchestrator startup no longer re-logs all discovered windows (avoids duplicate `DISCOVERED` spam), and startup/boundary watchdog timing now consistently uses the configured clock source.
- **Boundary open timing fix**: `_window_manager` now performs OPEN/CLOSED transitions immediately at the ET boundary using already-persisted pending windows, and only runs `_sync_windows()` afterward. This prevents Gamma API latency during boundary discovery from delaying window opens by several seconds.
- **Spot initial tick invalidation refinement**: At window open, `spot_initial_tick_timeout` is only armed for an underlying if no recent Binance tick has been observed within `binance_silence_threshold_sec`; this prevents false invalidations immediately after boundary transitions when live spot is already flowing.
- **Expected book reconnect fix**: `PolymarketBookCollector.update_assets()` may intentionally close the WebSocket when token removals require a clean resubscribe. These expected `ConnectionClosed(1000 OK)` events are no longer reported as `book_disconnect:*`, so newly opened windows are not falsely invalidated for self-induced reconnects.
- **Book payload compatibility hardening**: `PolymarketBookCollector._handle_message()` now accepts both dict and list-shaped WebSocket frames, unwraps nested `message` payloads, and logs the first few unrecognized payload shapes. This prevents silent dropping when Polymarket sends batched or nested market events.
- Unresolved window tokens stay subscribed even after close, so `market_resolved` WebSocket events can still be received. However, Polymarket does NOT emit `market_resolved` events via WebSocket for automated 5-min markets.
- Once a window is resolved (`outcome` set), its tokens are removed from the subscription on the next boundary update.
- The `_window_manager` is timer-driven: sleeps to each 5-min ET boundary, opens/closes deterministically. No polling loop.
- `_sync_windows` runs at startup and at every 5-min ET boundary; skips windows whose `start_et` is >1s in the past.
- Open windows are invalidated on confirmed live-data loss: spot disconnect, spot queue overflow, Polymarket book disconnect, Polymarket book queue overflow, and Binance spot silence beyond `binance_silence_threshold_sec`.
- Binance spot silence invalidation applies both after ticks have been flowing and when no first Binance tick arrives within `binance_silence_threshold_sec` after a window opens.
- Invalid windows keep `status="invalid"` with `invalid_reason` and `invalidated_at`; no backfill is attempted, and future pending windows may still open normally after reconnect.
- **TDE strategy** (`polyfon/strategies/tde.py`): Time Decay Effect. Entry at τ ∈ [15, 90]s when `|fair_prob - market_price| > theta_entry` AND the theta direction (∂π̂/∂τ) agrees that mispricing is widening. Stateless per-tick. Uses `up_best_ask` for BUY_YES, `down_best_ask` for BUY_NO. Computes theta analytically via `_theta()` — a closed-form derivative of the binary call formula. Documented in `docs/tde.md`.
- **ROM strategy** (`polyfon/strategies/rom.py`): Range Oscillation Momentum. Entry at τ ∈ [30, 120]s when spot is in the top/bottom 20% of its intra-window range. Uses `context.range_high` and `context.range_low` (computed in `_build_context` via `func.max`/`func.min` on SpotPrice). Entry direction: near range-high → BUY_YES, near range-low → BUY_NO. Confidence = displacement confidence × range-quality factor. Documented in `docs/rom.md`.
- **WDM strategy** (`polyfon/strategies/wdm.py`): Supplementary strategy, not part of the 18. Entry at T-10s based on spot displacement from window open price. Uses `up_best_ask` for BUY YES and `down_best_ask` for BUY NO. Confidence = min(|delta| / theta_sat, 1.0). Documented in `polymarket-strategies/window_delta_momentum_wdm.md`.
- **CLL strategy** (`polyfon/strategies/cll.py`): Cross-Asset Correlation Lead-Lag. Entry when leader asset (e.g. BTC) moves significantly and the lagger's Polymarket contract has not repriced. Uses `lookback_seconds` return of leader to predict lagger spot via `beta_lead` coefficient. Fair probability adjusted using conditional volatility `σ_B|A = σ_B × √(1 - ρ²)`. Entry direction: `adjusted_prob > market_price → BUY_YES`, else `BUY_NO`. Documented in `docs/cll.md`.
- **VPX strategy** (`polyfon/strategies/vpx.py`): CEX Toxicity Volatility Indicator. Detects volatility regime shifts by comparing short-term vs long-term realized vol. When `sigma_short / sigma_long > vpx_threshold`, projects vol forward using `beta_vpx` persistence and reprices contracts. Trades any discrepancy with market price. Documented in `docs/vpx.md`.
- **HMM strategy** (`polyfon/strategies/hmm.py`): Hidden Markov Model Regime-Switching adaptation. Because the current engine runs one strategy instance at a time, HMM-RS is implemented as a self-contained regime-aware selector rather than a full ensemble allocator. It infers soft posteriors over `calm`, `trending`, `volatile`, and `converging` regimes using only currently available context features (volatility, short/long vol ratio, PM spreads, spot displacement, distance to strike), then applies regime-consistent entry logic. Documented in `docs/hmm.md`.
- **Book collector diagnostics hardening**: `PolymarketBookCollector` now logs `on_book` callback failures instead of swallowing them silently, handles nested list payloads under `message`, and warns when `book`, `price_change`, `best_bid_ask`, or `last_trade_price` payloads are missing expected structure such as `asset_id`. This is specifically to diagnose live cases where windows are opening but `order_books` are not being persisted.
- **Book startup sequencing fix**: `CollectionOrchestrator.run()` now starts the DB worker tasks before starting live collectors, and the initial Polymarket book subscription goes through `_refresh_book_subscription()` instead of a separate startup path. This ensures incoming order-book updates are not racing ahead of the persistence worker during fresh startup and adds explicit logs for initial book-subscription size / asset updates.
- **CLI logging initialization**: `scripts/run.py` now configures stdlib logging from `settings.log_level` at CLI startup. Without this, collector `logger.info(...)` / `logger.warning(...)` messages could be invisible on some machines, making Polymarket book-subscription failures appear silent even after instrumentation.
- **SOCKS5 proxy support**: Config now supports `SOCKS5_PROXY_URL` as a global fallback plus service-specific overrides `POLYMARKET_WS_PROXY_URL`, `POLYMARKET_HTTP_PROXY_URL`, and `BINANCE_WS_PROXY_URL`. `PolymarketBookCollector` passes the proxy to `websockets.asyncio.client.connect`, `BinanceSpotCollector` passes the proxy to `websockets.connect`, and `PolymarketDiscovery` passes the proxy to `httpx.AsyncClient`. This is intended for deployments where Polymarket WS access is region-blocked and traffic must egress via a proxy.
- **Spot WS rejection handling**: `BinanceSpotCollector` now handles websocket handshake rejections such as HTTP 451 with structured logging and a concise rich console line instead of dumping raw tracebacks to stderr. This is particularly relevant when a global proxy is configured but Binance should not be routed through that egress.
- **SOCKS runtime dependency**: WebSocket proxying via `websockets` requires `python-socks` at runtime. The project dependencies now include `python-socks>=2.5.0`, and `BinanceSpotCollector` prints a concise operator-facing message if a SOCKS proxy is configured but that dependency is missing in the environment.
- **Context fields**: `window_open_price` (earliest spot in window range), `up_best_bid`/`up_best_ask`/`down_best_bid`/`down_best_ask` (per-token OrderBook), `up_bid_size`/`up_ask_size`/`down_bid_size`/`down_ask_size` (book sizes for OBI), `mean_spot_price` (intra-window mean for MPR). Backward compat: `best_bid`/`best_ask` default to UP token values.
- **Book ambiguity fixed**: `_build_context` queries UP and DOWN OrderBooks separately by `token_id`. `_simulate_fill` uses `token_map` to look up the correct token's book based on signal direction.
- **SLA improvement**: `fair_probability` now uses `window_open_price` as strike (was hardcoded 0.5).
- **Dry mode fix**: `run_dry` now processes all `closed`/`resolved` windows (not just `open`). `_build_context` accepts `eval_time` parameter and aligns spot/book/range context to that timestamp. Dry replay is now strategy-driven via `BaseStrategy.build_replay_plan()`: WDM evaluates at T-10s, TDE scans τ ∈ [15, 90]s, ROM scans τ ∈ [30, 120]s, and SLA scans from window open until `tau_min`. Strategy replay cadence defaults to 1 second and is configurable per strategy via `replay_cadence_seconds`. Spot price query falls back to earliest record if no price exists before `eval_time`.
