# Plan: Make Bot Broker-Agnostic

## Problem
Alpaca SDK is tightly coupled across 4 files. Switching brokers would require rewriting trader.py, data.py, config.py, bot.py, and signals.py simultaneously.

## Approach
Introduce abstract base classes (Python ABCs) that define broker interfaces. Move all Alpaca-specific code into dedicated implementation modules. The rest of the bot only talks to the abstractions.

## Alpaca Coupling Today

| File | What it uses Alpaca for |
|------|------------------------|
| `trader.py` | TradingClient, MarketOrderRequest, LimitOrderRequest, StopOrderRequest, OrderSide, TimeInForce, etc. — all order execution |
| `data.py` | StockHistoricalDataClient, StockBarsRequest, StockLatestQuoteRequest, TimeFrame — market data + TradingClient for account/positions |
| `config.py` | AlpacaConfig dataclass with API keys, base_url, paper mode |
| `bot.py` | Imports AlpacaDataClient, uses get_account(), get_positions() |
| `signals.py` | Imports AlpacaDataClient, uses get_latest_quote(), get_daily_bars(), get_intraday_bars() |

## New File Structure

```
shortsqueeze/
  broker/
    __init__.py          # Exports base classes + factory function
    base.py              # Abstract base classes (BrokerDataClient, BrokerTrader)
    alpaca_data.py       # AlpacaDataClient implementation (moved from data.py)
    alpaca_trader.py     # AlpacaTradeExecutor implementation (moved from trader.py)
  config.py              # BrokerConfig replaces AlpacaConfig, keeps AlpacaConfig as subclass
  data.py                # DataManager no longer imports Alpaca directly
  trader.py              # TradeExecutor/TradeManager use abstract BrokerTrader
  signals.py             # Uses BrokerDataClient instead of AlpacaDataClient
  bot.py                 # Uses BrokerDataClient instead of AlpacaDataClient
```

## Step-by-Step Changes

### 1. Create `shortsqueeze/broker/base.py` — Abstract interfaces

Two ABCs:

**BrokerDataClient** (market data + account):
- `get_latest_quote(symbol) -> dict`
- `get_bars(symbol, timeframe, start, end, limit) -> DataFrame`
- `get_intraday_bars(symbol, minutes, days_back) -> DataFrame`
- `get_daily_bars(symbol, days) -> DataFrame`
- `get_account() -> dict`
- `get_positions() -> list`
- `get_position(symbol) -> Optional[dict]`

**BrokerTrader** (order execution):
- `submit_market_order(ticker, quantity, side) -> Optional[Trade]`
- `submit_limit_order(ticker, quantity, limit_price, side) -> Optional[Trade]`
- `submit_stop_order(ticker, quantity, stop_price, side) -> Optional[Trade]`
- `submit_bracket_order(ticker, quantity, limit_price, stop_loss_price, take_profit_price) -> Optional[Trade]`
- `cancel_order(order_id) -> bool`
- `cancel_all_orders() -> int`
- `get_order(order_id) -> Optional[dict]`
- `get_open_orders(ticker) -> list`
- `close_position(ticker) -> Optional[Trade]`
- `close_all_positions() -> int`

Return types are the same plain dicts/dataclasses already used (Trade, dict). No Alpaca-specific types leak out.

### 2. Create `shortsqueeze/broker/alpaca_data.py`

Move the existing `AlpacaDataClient` class from `data.py` into this file, and have it extend `BrokerDataClient`. No logic changes — just move + inherit.

### 3. Create `shortsqueeze/broker/alpaca_trader.py`

Move the existing `TradeExecutor` class from `trader.py` into this file, and have it extend `BrokerTrader`. No logic changes — just move + inherit.

### 4. Create `shortsqueeze/broker/__init__.py` — Factory function

```python
from .base import BrokerDataClient, BrokerTrader
from .alpaca_data import AlpacaDataClient
from .alpaca_trader import AlpacaTradeExecutor

def create_data_client(config) -> BrokerDataClient:
    """Factory: create the broker data client based on config."""
    return AlpacaDataClient(config)

def create_trader(config) -> BrokerTrader:
    """Factory: create the broker trader based on config."""
    return AlpacaTradeExecutor(config)
```

### 5. Update `config.py`

- Rename `AlpacaConfig` to `BrokerConfig` (keep `AlpacaConfig` as alias for backwards compatibility)
- Add a `broker_type: str = "alpaca"` field to Config for future use
- Keep existing AlpacaConfig logic intact — it still works, just generalized

### 6. Update `data.py`

- Remove the `AlpacaDataClient` class entirely (now in broker/alpaca_data.py)
- Remove Alpaca SDK imports
- `DataManager.__init__` takes a `BrokerDataClient` instead of creating `AlpacaDataClient` internally
- Keep all scraper classes (FinvizScraper, etc.) unchanged — they don't use Alpaca

### 7. Update `trader.py`

- Remove `TradeExecutor` class (now in broker/alpaca_trader.py)
- Remove Alpaca SDK imports
- `TradeManager.__init__` takes a `BrokerTrader` instead of creating `TradeExecutor` internally
- `Trade` dataclass and `TradeManager` stay in this file — they're broker-agnostic already

### 8. Update `signals.py`

- Import `BrokerDataClient` from `broker` instead of `AlpacaDataClient` from `data`
- `SignalDetector.__init__` takes `BrokerDataClient` parameter
- No logic changes — it already uses the dict-based return values

### 9. Update `bot.py`

- Import from `broker` package instead of `data`
- Use factory functions `create_data_client()` and `create_trader()`
- Pass broker instances to SignalDetector, TradeManager, etc.
- Replace `self.alpaca` with `self.broker_data` (naming)

## What Stays The Same
- All signal detection logic (signals.py inner logic)
- All position sizing logic (position.py)
- All watchlist/universe logic (universe.py)
- All indicator logic (indicators.py)
- All scraper logic (data.py scrapers)
- The Trade dataclass
- Config structure (just adds broker_type field)

## To Add a New Broker Later
1. Create `broker/ibkr_data.py` implementing `BrokerDataClient`
2. Create `broker/ibkr_trader.py` implementing `BrokerTrader`
3. Add `IBKRConfig` to config.py
4. Update factory functions in `broker/__init__.py`
5. Set `BROKER_TYPE=ibkr` in .env

No changes needed to signals, position sizing, watchlist management, or the bot orchestration.
