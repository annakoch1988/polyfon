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
    logs signal + simulates position (dry/shadow)

Database (SQLite):
    run_sessions, windows, spot_prices, order_books,
    trade_signals, positions, config
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
    execution/
        engine.py             # ExecutionEngine (dry/shadow mode)
    utils/
        fees.py               # taker_fee_usdc, effective_cost, net_pnl
scripts/
    run.py                 # CLI entry point
docs/
    sla.md                 # SLA strategy reference
    wdm.md                 # WDM strategy reference
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
- **RunSession**: id, started_at, finished_at (null = aborted/crashed)
- **Window**: id, slug, title, underlying, start_et, end_et, outcome, status (pending/open/closed/resolved), run_session_id, up_token_id, down_token_id, condition_id, fee_rate, tick_size
- **SpotPrice**: id, symbol, price, timestamp, source
- **OrderBook**: id, window_id, token_id, best_bid, best_ask, bid_size, ask_size, last_trade_price, stale, timestamp
- **TradeSignal**: id, strategy, window_id, direction, size, expected_edge, confidence, timestamp
- **Position**: id, mode, window_id, strategy, side, entry_price, size, exit_price, pnl, fees_paid, status, opened_at, closed_at
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
python -m scripts.run dry --strategy=SLA --coins=BTC,ETH --collect

# Shadow mode (real-time simulation)
python -m scripts.run shadow --strategy=WDM --coins=BTC,ETH --collect

# Dry/Shadow CLI now supports --param key=value (repeatable)
python -m scripts.run dry --strategy=WDM --param theta_entry=0.0005 --param tau_max=20

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
4. **Phase 4**: Additional strategies (PMR, MPR, VIT, TDE, CRV, OBI, VPX, CLL, HMM, ROM, MIP, PFR, RND, HPE, KLD, ARL, EVT).
5. **Phase 5**: Python ML bridge (GARCH, EVT, HMM, Hawkes) for advanced strategies.

## Agent Protocol
- After making any functional changes or achieving progress, update this file to reflect the current state. Do not wait to be asked.

## Notes for Future Agents
- Always use `session_scope()` context manager for DB transactions.
- All timestamps are naive UTC stored in the DB; ET boundary semantics kept in `start_et` / `end_et`.
- Carry-forward logic is in `PolymarketBookCollector`: if no message for >5s, emits stale record.
- Use `StrategyRegistry.instantiate(name)` to create strategy instances.
- Keep strategy logic stateless where possible; persist state via DB ConfigKV if needed.
- `RunSession` tracks each collector start; `finished_at` null = interrupted.
- Unfinished windows (open/pending from previous sessions) are DELETED at startup, not resolved.
- Unfinished windows (open/pending from previous sessions) are DELETED at startup (`Maintenance` section), not resolved.
- Resolution is done by `_resolve_orphans()` which polls the Gamma API (`fetch_resolution(slug)`) for closed-but-unresolved windows. It runs at startup during `Maintenance` and every 60s in the main loop.
- Gamma API `fetch_resolution(slug)` uses `outcomePrices` (stringified JSON array, e.g. `'["1","0"]'`) not the `outcome` field (always `None` for automated markets). Maps "Up" → "Yes", "Down" → "No".
- `_series_events` paginates through Gamma API results (skip=0,100,200,...) because the API returns stale events first; current events are typically at skip=600+.
- Unresolved window tokens stay subscribed even after close, so `market_resolved` WebSocket events can still be received. However, Polymarket does NOT emit `market_resolved` events via WebSocket for automated 5-min markets.
- Once a window is resolved (`outcome` set), its tokens are removed from the subscription on the next boundary update.
- The `_window_manager` is timer-driven: sleeps to each 5-min ET boundary, opens/closes deterministically. No polling loop.
- `_sync_windows` runs every 60s to discover new events; skips windows whose `start_et` is >1s in the past.
- Open windows are invalidated on confirmed live-data loss: spot disconnect, spot queue overflow, Polymarket book disconnect, Polymarket book queue overflow, and Binance spot silence beyond `binance_silence_threshold_sec`.
- Binance spot silence invalidation applies both after ticks have been flowing and when no first Binance tick arrives within `binance_silence_threshold_sec` after a window opens.
- Invalid windows keep `status="invalid"` with `invalid_reason` and `invalidated_at`; no backfill is attempted, and future pending windows may still open normally after reconnect.
- **WDM strategy** (`polyfon/strategies/wdm.py`): Supplementary strategy, not part of the 18. Entry at T-10s based on spot displacement from window open price. Uses `up_best_ask` for BUY YES and `down_best_ask` for BUY NO. Confidence = min(|delta| / theta_sat, 1.0). Documented in `articles/window_delta_momentum_wdm.md`.
- **Context fields**: `window_open_price` (earliest spot in window range), `up_best_bid`/`up_best_ask`/`down_best_bid`/`down_best_ask` (per-token OrderBook). Backward compat: `best_bid`/`best_ask` default to UP token values.
- **Book ambiguity fixed**: `_build_context` queries UP and DOWN OrderBooks separately by `token_id`. `_simulate_fill` uses `token_map` to look up the correct token's book based on signal direction.
- **SLA improvement**: `fair_probability` now uses `window_open_price` as strike (was hardcoded 0.5).
- **Dry mode fix**: `run_dry` now processes all `closed`/`resolved` windows (not just `open`). `_build_context` accepts `eval_time` parameter; historical windows are evaluated at `end_et - 10s` (T-10s entry point for WDM). Spot price query falls back to earliest record if no price exists before `eval_time`.
