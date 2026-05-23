# AGENTS.md — Polyfon Project Compass

**Polyfon** is a quantitative trading system for 5-minute crypto-resolution prediction markets on Polymarket.

## Technology Stack (finalized)
- **Language**: Python 3.11+
- **Database**: SQLite (single file, portable)
- **ORM**: SQLAlchemy 2.0 with async support (`aiosqlite` driver)
- **HTTP**: `httpx` for REST API calls (market discovery)
- **WebSocket**: `websockets` for Binance spot feeds AND Polymarket order book feeds
- **CLI**: `click` + `rich` for terminal output
- **Scientific**: `numpy`, `pandas`, `scipy`

## Project Goals
1. Build a flexible, extensible Python trading infrastructure.
2. Support execution modes: `collect`, `dry`, `shadow`, `wet` (wet is postponed).
3. Persist all data in SQLite via SQLAlchemy async ORM.
4. Implement 18 strategies (SLA, PMR, MPR, VIT, TDE, CRV, OBI, VPX, CLL, HMM, ROM, MIP, PFR, RND, HPE, KLD, ARL, EVT).
5. Every strategy fits a common interface via `BaseStrategy` + `@register` decorator.
6. Fee calculations strictly follow Polymarket docs: `fee = C * feeRate * p * (1 - p)`.
7. Windows are 5-minute intervals aligned to clock boundaries.
8. Best-bid/ask carry-forward implemented in both WebSocket collector and execution engine.
9. Outcomes resolved asynchronously via background resolver (planned).

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
    WindowManager  →  open/close 5-min windows

ExecutionEngine:
    loads StrategyRegistry → instantiates chosen strategy
    builds Context (spot, book, fair prob, tau)
    calls strategy.on_tick(window, context) → Signal
    logs signal + simulates position (dry/shadow)

Database (SQLite):
    markets, windows, spot_prices, order_books,
    trade_signals, positions, fee_params, config
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
- **Ignored events**: `tick_size_change`, `new_market`, `market_resolved`

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
        market_discovery.py   # Polymarket CLOB REST API discovery
        spot_collector.py     # Binance WebSocket spot feed
        book_collector.py     # Polymarket WebSocket book (best bid/ask + carry-forward)
        orchestrator.py       # Ties collectors + window manager together
    pricing/
        fair_probability.py   # BS binary call fair prob
        volatility.py         # Rolling realized vol
    strategies/
        base.py               # BaseStrategy, Context, Signal, StrategyRegistry
        sla.py                # Strategy 1: Spot-Led Latency Arbitrage
    execution/
        engine.py             # ExecutionEngine (dry/shadow mode)
    utils/
        fees.py               # taker_fee_usdc, effective_cost, net_pnl
scripts/
    run.py                 # CLI entry point
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
- **Market**: condition_id, token_id, title, category, fee_rate, tick_size, underlying, strike, status
- **Window**: market_id, start_time, end_time, strike, outcome, status
- **SpotPrice**: symbol, price, timestamp, source
- **OrderBook**: market_id, window_id, best_bid, best_ask, bid_size, ask_size, last_trade_price, stale, timestamp
- **TradeSignal**: strategy, window_id, direction, size, expected_edge, confidence, timestamp
- **Position**: mode, market_id, window_id, strategy, side, entry_price, size, exit_price, pnl, fees_paid, status
- **FeeParams**: market_id, fee_rate, maker_rate, rebate_rate

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
python -m scripts.run dry --strategy=SLA --coins=BTC,ETH --collect

# Shadow mode (real-time simulation)
python -m scripts.run shadow --strategy=SLA --coins=BTC,ETH --collect

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
2. **Phase 2**: Shadow mode refinement, async resolution resolver thread.
3. **Phase 3**: Wet mode (CLOB API orders, private key). **POSTPONED.**
4. **Phase 4**: Additional strategies (PMR, MPR, VIT, TDE, CRV, OBI, VPX, CLL, HMM, ROM, MIP, PFR, RND, HPE, KLD, ARL, EVT).
5. **Phase 5**: Python ML bridge (GARCH, EVT, HMM, Hawkes) for advanced strategies.

## Notes for Future Agents
- Always use `session_scope()` context manager for DB transactions.
- All timestamps are UTC.
- Carry-forward logic is in `PolymarketBookCollector`: if no message for >5s, emits stale record.
- Use `StrategyRegistry.instantiate(name)` to create strategy instances.
- Keep strategy logic stateless where possible; persist state via DB ConfigKV if needed.
