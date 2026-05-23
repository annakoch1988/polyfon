# Polyfon

**Quantitative Trading System for 5-Minute Crypto Prediction Markets on Polymarket**

Polyfon is a Python-based trading infrastructure designed to discover, collect, and
trade 5-minute crypto-resolution binary prediction markets on Polymarket. It supports
multiple trading strategies, real-time data collection via WebSocket, and three
execution modes (collect, dry, shadow) with wet mode planned for the future.

---

## Features

- **Real-Time Data Collection** via WebSocket
  - Binance spot price feeds (`wss://stream.binance.com:9443/ws`)
  - Polymarket order book feeds (`wss://ws-subscriptions-clob.polymarket.com/ws/market`)
- **SQLite Database** — single-file, fully portable, no external daemon
- **SQLAlchemy 2.0 Async ORM** — type-safe, async database operations
- **Flexible Strategy Framework** — plug-and-play strategy registration with `@register`
- **Accurate Fee Modeling** — Polymarket taker fees per official documentation
- **Fair Probability Pricing** — Black-Scholes binary call approximation for edge detection
- **Three Execution Modes**:
  - `collect` — data only, no trading
  - `dry` — backtest on saved historical data
  - `shadow` — real-time simulated trading

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| Database | SQLite |
| ORM | SQLAlchemy 2.0 (async) |
| HTTP Client | `httpx` |
| WebSocket | `websockets` |
| CLI | `click` + `rich` |
| Scientific | `numpy`, `pandas`, `scipy` |

---

## Quick Start

### Prerequisites

- Python 3.11 or newer
- Internet access (for WebSocket feeds and REST APIs)

### Installation

```bash
# Clone or navigate to the project directory
cd polyfon

# Install dependencies (system-wide or in a virtual environment)
pip install sqlalchemy aiosqlite alembic httpx websockets \
    python-dotenv click numpy pandas scipy pydantic \
    pydantic-settings rich tenacity typing-extensions
```

### Configuration

Copy the example environment file and adjust as needed:

```bash
cp .env.example .env
```

Edit `.env`:

```env
DATABASE_URL=sqlite+aiosqlite:///./polyfon.db
POLYMARKET_API_URL=https://clob.polymarket.com
BINANCE_WS_URL=wss://stream.binance.com:9443/ws
COINS=BTC,ETH
LOG_LEVEL=INFO
```

---

## Running Polyfon

All commands are executed via the CLI:

```bash
python -m scripts.run <command> [options]
```

### 1. Collect Mode — Data Only

Collects real-time spot prices and order books into the SQLite database. No strategies run.

```bash
# Collect for all configured coins (default: BTC, ETH)
python -m scripts.run collect

# Collect for specific coins only
python -m scripts.run collect --coins=BTC,ETH
```

What gets saved:
- Active 5-minute crypto markets discovered from Polymarket
- 5-minute trading windows (open/close tracking)
- Spot prices from Binance (1-second ticks via WebSocket)
- Order book snapshots from Polymarket (best bid/ask via WebSocket)

### 2. Dry Mode — Simulated Backtest

Runs a strategy on historical data already saved in the database. No real money, no live data feeds (unless `--collect` is added).

```bash
# Run SLA strategy on all historical windows in DB
python -m scripts.run dry --strategy=SLA

# Run on specific coins only
python -m scripts.run dry --strategy=SLA --coins=BTC,ETH

# Run while also collecting live data in parallel
python -m scripts.run dry --strategy=SLA --coins=BTC,ETH --collect
```

Output: trade signals and simulated positions written to the database. Inspect with SQLite or raw SQL.

### 3. Shadow Mode — Real-Time Simulation

Like wet mode but places no real orders. Tracks simulated PnL in real time as if trading live.

```bash
# Run SLA strategy in real-time simulation
python -m scripts.run shadow --strategy=SLA

# With live data collection
python -m scripts.run shadow --strategy=SLA --coins=BTC,ETH --collect
```

### 4. List Available Strategies

```bash
python -m scripts.run list-strategies
```

Currently registered: `SLA`

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  CLI Entry Point  (scripts/run.py)                           │
│  ├─ collect  →  CollectionOrchestrator                       │
│  ├─ dry      →  ExecutionEngine(mode="dry")                  │
│  ├─ shadow   →  ExecutionEngine(mode="shadow")              │
│  └─ wet      →  [POSTPONED]                                  │
└─────────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
  ┌──────────┐     ┌────────────────┐     ┌──────────┐
  │Collector │     │Strategy Engine │     │Execution │
  │(WebSocket│     │(BaseStrategy  │     │Engine    │
  │ + REST)  │     │ + Registry)   │     │(dry/shad)│
  └──────────┘     └────────────────┘     └──────────┘
        │                   │                   │
        └───────────────────┼───────────────────┘
                            ▼
              ┌──────────────────────┐
              │  SQLite Database     │
              │  (polyfon.db)        │
              └──────────────────────┘
```

### Data Flow

1. **CollectionOrchestrator** discovers active 5-min Polymarket markets via REST
2. For each active market, a **5-minute window** is opened in the database
3. **BinanceSpotCollector** streams spot prices via WebSocket into `spot_prices`
4. **PolymarketBookCollector** streams order book events via WebSocket into `order_books`
5. **ExecutionEngine** loads open windows, builds a `Context` (spot + book + fair prob), calls `strategy.on_tick()`
6. Strategy returns a `Signal` → logged to `trade_signals`, simulated position to `positions` (dry/shadow)

---

## Database Schema

| Table | Purpose |
|-------|---------|
| **markets** | Discovered Polymarket markets (condition_id, token_id, title, category, fee_rate, strike, underlying) |
| **windows** | 5-minute trading windows per market (start_time, end_time, status: open/closed/resolved) |
| **spot_prices** | CEX spot prices (symbol, price, timestamp, source) |
| **order_books** | Best bid/ask snapshots per market/window (best_bid, best_ask, bid_size, ask_size, stale) |
| **trade_signals** | Strategy entry signals (direction, size, expected_edge, confidence) |
| **positions** | Simulated/real positions (entry_price, size, pnl, fees_paid, status) |
| **fee_params** | Per-market fee parameters (fee_rate, maker_rate, rebate_rate) |
| **config** | Key-value settings storage |

---

## Strategy Framework

### Implemented Strategies

| Name | Description | Status |
|------|-------------|--------|
| **SLA** | Spot-Led Latency Arbitrage — exploits lag between CEX spot moves and Polymarket price updates | ✅ Active |

### Adding a New Strategy

1. Create a file in `polyfon/strategies/`:

```python
from polyfon.strategies.base import BaseStrategy, Context, Signal, register

@register
class MyStrategy(BaseStrategy):
    name = "MY"

    def on_tick(self, window, context: Context) -> Signal | None:
        # window: SQLAlchemy Window object (strike, start_time, end_time, ...)
        # context: spot_price, best_bid, best_ask, fair_probability, tau_seconds, sigma_per_minute
        ...

    def on_window_close(self, window, context: Context) -> Signal | None:
        ...
```

2. Import it in `polyfon/strategies/__init__.py`.
3. Run: `python -m scripts.run list-strategies` to verify registration.

The `@register` decorator automatically adds the strategy to `StrategyRegistry`, making it selectable via `--strategy=MY`.

---

## Fee Calculation

Taker fees follow the Polymarket documented formula:

```
fee = round(shares * feeRate * price * (1 - price), 5)
```

Crypto markets: `feeRate = 0.07` (7%).

Maker fee: `0`.

Validated against [Polymarket Fee Documentation](https://docs.polymarket.com/trading/fees).

---

## Fair Probability Model

Polyfon computes the theoretical fair probability of a binary outcome using a Black-Scholes-style binary call approximation:

```
π̂ = Φ( d1 )
d1 = (ln(S/K) + (μ - σ²/2) * τ) / (σ * √τ)
```

Where:
- `S` = current spot price
- `K` = contract strike / threshold
- `τ` = time to resolution (minutes)
- `σ` = rolling realized volatility per minute
- `Φ` = standard normal CDF (from `scipy.stats.norm`)

Strategies compare `π̂` against the market's best bid/ask to detect mispricing edges.

---

## About AGENTS.md

`AGENTS.md` is the project's internal compass. It is automatically read at the start of each Cline session and contains:
- Full architecture description
- Data collection wire protocols (WebSocket message formats, subscription JSON)
- Database schema details
- Implementation phase roadmap
- Notes for future development

Keep it updated as the project evolves.

---

## Project Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| Phase 1 | ✅ Complete | Bootstrap: schema, config, WebSocket collectors, fair pricing, SLA strategy, dry mode, CLI |
| Phase 2 | 🔄 Planned | Shadow mode refinement, async window outcome resolver |
| Phase 3 | ⏸️ Postponed | Wet mode: real CLOB API orders (requires private key, Legolas API key) |
| Phase 4 | 📝 Planned | Additional strategies: PMR, MPR, VIT, TDE, CRV, OBI, VPX, CLL, HMM, ROM, MIP, PFR, RND, HPE, KLD, ARL, EVT |
| Phase 5 | 📝 Planned | ML bridge: GARCH volatility, EVT tail estimation, HMM regime detection, Hawkes processes |

---

## License

Private / Proprietary — for personal trading research.

---

## Disclaimer

This software is for **research and simulation purposes only**. Trading financial
instruments carries substantial risk of loss. No guarantee of profitability is
expressed or implied. Wet mode trading involves real capital and should only be
activated after thorough backtesting and shadow mode validation.
