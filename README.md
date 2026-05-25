# Polyfon

**Quantitative Trading System for 5-Minute Crypto Prediction Markets on Polymarket**

Polyfon is a Python-based trading infrastructure designed to discover, collect, and trade 5-minute crypto-resolution binary prediction markets on Polymarket. It supports multiple trading strategies, real-time data collection via WebSocket, and three execution modes (collect, dry, shadow).

---

## Features

- **Real-time data collection** via WebSocket — Binance spot feeds + Polymarket order book
- **SQLite database** — single-file, portable, no external daemon
- **SQLAlchemy 2.0 async ORM** — type-safe async database operations
- **Pluggable strategy framework** — `@register` decorator + `BaseStrategy` interface
- **18+ strategy slots** — SLA, WDM, TDE, ROM implemented; 14 more planned
- **Replay plans** — each strategy defines its own dry-mode evaluation cadence
- **Accurate fee modeling** — Polymarket taker fees per official documentation
- **Realized PnL reporting** — dry mode fetches resolved Windows and computes net PnL
- **Window-slug filtering** — run strategies on specific windows by slug

---

## Quick Start

### Prerequisites

- Python 3.11+
- Internet access (WebSocket feeds, REST APIs)

### Installation

```bash
git clone <repo-url> && cd polyfon
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

### Configuration

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

## CLI Reference

All commands are run via:

```bash
python -m scripts.run <command> [options]
```

### `collect` — Data Collection Only

```bash
python -m scripts.run collect                    # default coins from .env
python -m scripts.run collect --coins=BTC,ETH
```

- Discovers active 5-minute crypto markets via Polymarket REST API
- Opens/closes 5-minute trading windows aligned to ET clock boundaries
- Streams Binance spot prices (1-second ticks) into `spot_prices`
- Streams Polymarket order book best bid/ask into `order_books`
- Resolves closed windows via Gamma API polling every 60s

### `dry` — Historical Simulation

Runs a strategy on historical windows already in the database. Computes signals and realized PnL using resolved outcomes.

```bash
python -m scripts.run dry --strategy=WDM
python -m scripts.run dry --strategy=TDE --coins=BTC
python -m scripts.run dry --strategy=ROM --param tau_max=90 --param tau_min=45
python -m scripts.run dry --strategy=SLA --window-slugs=BTC_20260524_0510,BTC_20260524_0515
```

Options:

| Flag | Description |
|------|-------------|
| `--strategy` | Strategy name (`SLA`, `WDM`, `TDE`, `ROM`) |
| `--coins` | Comma-separated coin filter |
| `--collect` | Also run live data collection in parallel |
| `--window-slugs` | Comma-separated window slugs to restrict execution |
| `--param` | Strategy parameter as `key=value` (repeatable) |
| `--replay-cadence-seconds` | Override strategy's replay cadence |

Output: per-window status (`SIGNAL` / `SKIP`), trade signals logged to DB, realized PnL summary.

### `shadow` — Real-Time Simulation

Like wet mode but no real orders. Tracks simulated PnL in real time.

```bash
python -m scripts.run shadow --strategy=WDM --collect
python -m scripts.run shadow --strategy=TDE --coins=BTC,ETH
```

### `list-strategies`

```bash
python -m scripts.run list-strategies
```

---

## Implemented Strategies

| Name | Description |
|------|-------------|
| **SLA** | Spot-Led Latency Arbitrage — exploits lag between CEX spot moves and Polymarket pricing |
| **WDM** | Window Delta Momentum — entry at T-10s based on spot displacement from open |
| **TDE** | Time Decay Effect — entry when fair-probability theta agrees mispricing is widening |
| **ROM** | Range Oscillation Momentum — entry when spot is in top/bottom 20% of intra-window range |

Each strategy has its own `ReplayPlan` that defines when `on_tick` is evaluated during dry mode:

| Strategy | Dry-mode evaluation |
|----------|-------------------|
| SLA | Scans from window open until `tau_min` |
| WDM | Evaluates at T-10s |
| TDE | Scans τ ∈ [15, 90]s |
| ROM | Scans τ ∈ [30, 120]s |

Add a new strategy by creating `polyfon/strategies/<name>.py`, inheriting `BaseStrategy`, decorating with `@register`, and importing in `polyfon/strategies/__init__.py`.

---

## Architecture

```
scripts/run.py (click CLI)
    collect   → CollectionOrchestrator
    dry       → ExecutionEngine(mode="dry")
    shadow    → ExecutionEngine(mode="shadow")

CollectionOrchestrator:
    PolymarketDiscovery  →  REST: discover 5-min crypto markets
    BinanceSpotCollector  →  WS: spot prices → DB
    PolymarketBookCollector  →  WS: best bid/ask → DB
    WindowManager  →  timer-driven open/close at 5-min ET boundaries
    _resolve_orphans  →  Gamma API resolution polling (60s)

ExecutionEngine:
    loads strategy via StrategyRegistry
    builds Context (spot, book, fair prob, tau, range_high/low)
    calls strategy.on_tick(window, context) → Signal
    simulates long-only Polymarket entries (`BUY_YES` / `BUY_NO` only), computes PnL at window resolution
```

Polymarket simulation note: this project does not model synthetic short instruments. Bearish exposure is expressed as `BUY_NO`, not `SELL_YES`.

### Database Schema

| Table | Purpose |
|-------|---------|
| **collect_run_sessions** | Tracks collector start/stop |
| **windows** | 5-minute trading windows (slug, underlying, start_et, end_et, status, outcome) |
| **spot_prices** | Binance spot ticks (symbol, price, timestamp) |
| **order_books** | Best bid/ask per token (token_id, best_bid, best_ask, stale flag) |
| **trade_signals** | Strategy entry signals (direction, size, edge, confidence) |
| **positions** | Simulated/real bought-contract records (YES / NO contract, entry_price, size, pnl, fees, status) |
| **config_kv** | Key-value settings storage |

---

## Fee Calculation

Taker fee follows Polymarket documentation:

```
fee = round(shares * feeRate * price * (1 - price), 5)
```

- Crypto market `feeRate`: **0.07** (7%)
- Maker fee: **0**

Implemented in `polyfon/utils/fees.py`: `taker_fee_usdc()`, `net_pnl()`.

---

## Project Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| Phase 1 | ✅ Complete | Bootstrap: schema, config, WebSocket collectors, fair pricing, SLA, dry mode, CLI |
| Phase 2 | ✅ Complete | Shadow mode refinement, session tracking, resolution engine, orphan cleanup |
| Phase 3 | ⏸️ Postponed | Wet mode: real CLOB API orders |
| Phase 4 | 🔄 In Progress | Additional strategies: TDE, ROM implemented; 14 remaining (PMR, MPR, VIT, CRV, OBI, VPX, CLL, HMM, MIP, PFR, RND, HPE, KLD, ARL, EVT) |
| Phase 5 | 📝 Planned | ML bridge: GARCH volatility, EVT, HMM, Hawkes processes |

---

## Disclaimer

This software is for **research and simulation purposes only**. Trading financial instruments carries substantial risk of loss. No guarantee of profitability is expressed or implied.
