"""
Alpaca implementation of BrokerDataClient.

Provides market data (quotes, bars) and account/position information
via the Alpaca SDK.
"""

import time
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional, Callable, TypeVar

import pandas as pd
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from loguru import logger

from .base import BrokerDataClient

T = TypeVar('T')


def retry_on_connection_error(max_retries: int = 3, base_delay: float = 1.0):
    """
    Decorator to retry API calls on connection errors with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds (doubles each retry)
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (ConnectionError, ConnectionResetError,
                        ConnectionAbortedError, BrokenPipeError) as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            f"Connection error in {func.__name__}, "
                            f"retrying in {delay}s ({attempt + 1}/{max_retries}): {e}"
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"Connection error in {func.__name__} after "
                            f"{max_retries} retries: {e}"
                        )
                except Exception as e:
                    # Check for remote disconnected errors in the exception chain
                    error_str = str(e).lower()
                    if "remote" in error_str or "disconnected" in error_str or "connection" in error_str:
                        last_exception = e
                        if attempt < max_retries:
                            delay = base_delay * (2 ** attempt)
                            logger.warning(
                                f"Remote connection error in {func.__name__}, "
                                f"retrying in {delay}s ({attempt + 1}/{max_retries}): {e}"
                            )
                            time.sleep(delay)
                        else:
                            logger.error(
                                f"Remote connection error in {func.__name__} after "
                                f"{max_retries} retries: {e}"
                            )
                    else:
                        raise
            # Return empty/default value on final failure
            raise last_exception
        return wrapper
    return decorator


class AlpacaDataClient(BrokerDataClient):
    """Alpaca implementation of broker data client."""

    def __init__(self, config):
        self.config = config
        self._data_client = None
        self._trading_client = None

    @property
    def data_client(self) -> StockHistoricalDataClient:
        """Lazy-load data client."""
        if self._data_client is None:
            self._data_client = StockHistoricalDataClient(
                api_key=self.config.alpaca.api_key,
                secret_key=self.config.alpaca.secret_key,
            )
        return self._data_client

    @property
    def trading_client(self) -> TradingClient:
        """Lazy-load trading client."""
        if self._trading_client is None:
            self._trading_client = TradingClient(
                api_key=self.config.alpaca.api_key,
                secret_key=self.config.alpaca.secret_key,
                paper="paper" in self.config.alpaca.base_url,
            )
        return self._trading_client

    def get_latest_quote(self, symbol: str) -> dict:
        """Get latest quote for a symbol."""
        try:
            request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self.data_client.get_stock_latest_quote(request)
            quote = quotes.get(symbol)

            if quote:
                return {
                    "symbol": symbol,
                    "bid": float(quote.bid_price),
                    "ask": float(quote.ask_price),
                    "bid_size": quote.bid_size,
                    "ask_size": quote.ask_size,
                    "timestamp": quote.timestamp,
                }
            return {}
        except Exception as e:
            logger.error(f"Error fetching quote for {symbol}: {e}")
            return {}

    def get_bars(
        self,
        symbol: str,
        timeframe: str = "day",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 1000,
    ) -> pd.DataFrame:
        """Get historical bar data from Alpaca."""
        try:
            if start is None:
                start = datetime.now() - timedelta(days=5)
            if end is None:
                end = datetime.now()

            alpaca_timeframe = self._to_alpaca_timeframe(timeframe)

            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=alpaca_timeframe,
                start=start,
                end=end,
            )

            bars = self.data_client.get_stock_bars(request)

            # Handle different response formats from Alpaca SDK
            data = []
            try:
                if hasattr(bars, 'data'):
                    bar_data = bars.data
                    if isinstance(bar_data, dict) and symbol in bar_data:
                        data = bar_data[symbol]
                    elif hasattr(bar_data, 'get'):
                        data = bar_data.get(symbol, [])
                if not data:
                    try:
                        data = bars[symbol]
                    except (KeyError, TypeError):
                        pass
                if not data:
                    data = [b for b in bars if getattr(b, 'symbol', None) == symbol]
            except Exception as e:
                logger.debug(f"Error parsing Alpaca response for {symbol}: {e}")
                data = []

            if not data:
                return pd.DataFrame()

            df = pd.DataFrame([{
                "timestamp": bar.timestamp,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume) if bar.volume else 0,
                "vwap": float(bar.vwap) if hasattr(bar, 'vwap') and bar.vwap else None,
                "trade_count": getattr(bar, 'trade_count', 0),
            } for bar in data])

            df.set_index("timestamp", inplace=True)
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            return df

        except Exception as e:
            logger.debug(f"Alpaca bars error for {symbol}: {e}")
            return pd.DataFrame()

    def get_intraday_bars(
        self,
        symbol: str,
        minutes: int = 5,
        days_back: int = 1,
    ) -> pd.DataFrame:
        """Get intraday minute bars."""
        start = datetime.now() - timedelta(days=days_back)

        if minutes >= 60:
            timeframe = "hour"
        else:
            timeframe = "minute"

        return self.get_bars(symbol, timeframe=timeframe, start=start)

    def get_daily_bars(self, symbol: str, days: int = 30) -> pd.DataFrame:
        """Get daily OHLCV bars."""
        start = datetime.now() - timedelta(days=days)
        return self.get_bars(symbol, timeframe="day", start=start)

    @retry_on_connection_error(max_retries=3, base_delay=2.0)
    def get_account(self) -> dict:
        """Get account information with automatic retry on connection errors."""
        try:
            account = self.trading_client.get_account()
            return {
                "equity": float(account.equity),
                "cash": float(account.cash),
                "buying_power": float(account.buying_power),
                "portfolio_value": float(account.portfolio_value),
                "currency": account.currency,
                "pattern_day_trader": account.pattern_day_trader,
                "trading_blocked": account.trading_blocked,
                "account_blocked": account.account_blocked,
            }
        except Exception as e:
            logger.error(f"Error fetching account: {e}")
            return {}

    @retry_on_connection_error(max_retries=3, base_delay=2.0)
    def get_positions(self) -> list:
        """Get all open positions with automatic retry on connection errors."""
        try:
            positions = self.trading_client.get_all_positions()
            return [{
                "symbol": pos.symbol,
                "qty": float(pos.qty),
                "side": pos.side.value,
                "market_value": float(pos.market_value),
                "cost_basis": float(pos.cost_basis),
                "unrealized_pl": float(pos.unrealized_pl),
                "unrealized_plpc": float(pos.unrealized_plpc),
                "current_price": float(pos.current_price),
                "avg_entry_price": float(pos.avg_entry_price),
            } for pos in positions]
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return []

    def get_position(self, symbol: str) -> Optional[dict]:
        """Get position for a specific symbol."""
        try:
            pos = self.trading_client.get_open_position(symbol)
            return {
                "symbol": pos.symbol,
                "qty": float(pos.qty),
                "side": pos.side.value,
                "market_value": float(pos.market_value),
                "cost_basis": float(pos.cost_basis),
                "unrealized_pl": float(pos.unrealized_pl),
                "unrealized_plpc": float(pos.unrealized_plpc),
                "current_price": float(pos.current_price),
                "avg_entry_price": float(pos.avg_entry_price),
            }
        except Exception:
            return None

    @staticmethod
    def _to_alpaca_timeframe(timeframe: str) -> TimeFrame:
        """Convert generic timeframe string to Alpaca TimeFrame."""
        mapping = {
            "minute": TimeFrame.Minute,
            "hour": TimeFrame.Hour,
            "day": TimeFrame.Day,
        }
        return mapping.get(timeframe.lower(), TimeFrame.Day)
