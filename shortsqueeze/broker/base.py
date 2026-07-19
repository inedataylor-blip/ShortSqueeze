"""
Abstract base classes for broker integrations.

These interfaces define the contract that any broker implementation must fulfill.
The rest of the bot interacts only with these abstractions, making it easy to
swap brokers without touching signal detection, position sizing, or watchlist logic.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import pandas as pd


class BrokerDataClient(ABC):
    """
    Abstract interface for broker market data and account information.

    Implementations must return data in the standardized dict/DataFrame
    formats documented on each method.
    """

    @abstractmethod
    def get_latest_quote(self, symbol: str) -> dict:
        """
        Get the latest quote for a symbol.

        Returns:
            dict with keys: symbol, bid, ask, bid_size, ask_size, timestamp
            Empty dict if unavailable.
        """

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        timeframe: str = "day",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 1000,
    ) -> pd.DataFrame:
        """
        Get historical OHLCV bar data.

        Args:
            symbol: Stock symbol
            timeframe: "minute", "hour", or "day"
            start: Start datetime
            end: End datetime
            limit: Maximum number of bars

        Returns:
            DataFrame with columns: open, high, low, close, volume, vwap (optional),
            indexed by timestamp.
        """

    @abstractmethod
    def get_intraday_bars(
        self,
        symbol: str,
        minutes: int = 5,
        days_back: int = 1,
    ) -> pd.DataFrame:
        """
        Get intraday minute bars.

        Returns:
            DataFrame with same format as get_bars().
        """

    @abstractmethod
    def get_daily_bars(self, symbol: str, days: int = 30) -> pd.DataFrame:
        """
        Get daily OHLCV bars.

        Returns:
            DataFrame with same format as get_bars().
        """

    @abstractmethod
    def get_account(self) -> dict:
        """
        Get account information.

        Returns:
            dict with keys: equity, cash, buying_power, portfolio_value,
            currency, pattern_day_trader, trading_blocked, account_blocked
            Empty dict if unavailable.
        """

    @abstractmethod
    def get_positions(self) -> Optional[list]:
        """
        Get all open positions.

        Returns:
            List of dicts, each with keys: symbol, qty, side, market_value,
            cost_basis, unrealized_pl, unrealized_plpc, current_price,
            avg_entry_price. Empty list when the book is genuinely flat.
            None on terminal fetch failure (distinct from [] so callers can
            skip the tick rather than treat a dropped fetch as "no positions").
        """

    @abstractmethod
    def get_position(self, symbol: str) -> Optional[dict]:
        """
        Get position for a specific symbol.

        Returns:
            dict with same keys as get_positions() items, or None if no position.
        """


class BrokerTrader(ABC):
    """
    Abstract interface for broker order execution.

    All methods return the bot's own Trade dataclass (from trader.py)
    or plain dicts — never broker-specific SDK types.
    """

    @abstractmethod
    def submit_market_order(self, ticker: str, quantity: int, side: str = "buy"):
        """
        Submit a market order.

        Args:
            ticker: Stock symbol
            quantity: Number of shares
            side: "buy" or "sell"

        Returns:
            Trade object or None if failed
        """

    @abstractmethod
    def submit_limit_order(self, ticker: str, quantity: int, limit_price: float, side: str = "buy"):
        """
        Submit a limit order.

        Returns:
            Trade object or None if failed
        """

    @abstractmethod
    def submit_stop_order(self, ticker: str, quantity: int, stop_price: float, side: str = "sell"):
        """
        Submit a stop order.

        Returns:
            Trade object or None if failed
        """

    @abstractmethod
    def submit_bracket_order(
        self,
        ticker: str,
        quantity: int,
        limit_price: float,
        stop_loss_price: float,
        take_profit_price: float,
    ):
        """
        Submit a bracket order (entry + stop loss + take profit).

        Returns:
            Trade object or None if failed
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by ID. Returns True if successful."""

    @abstractmethod
    def cancel_all_orders(self) -> int:
        """Cancel all open orders. Returns count of cancelled orders."""

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[dict]:
        """
        Get order details by ID.

        Returns:
            dict with keys: id, symbol, qty, filled_qty, side, type, status,
            limit_price, stop_price, filled_avg_price, created_at, filled_at
            or None if not found.
        """

    @abstractmethod
    def get_open_orders(self, ticker: Optional[str] = None) -> list:
        """
        Get open orders, optionally filtered by ticker.

        Returns:
            List of dicts with keys: id, symbol, qty, side, type, status,
            limit_price, stop_price
        """

    @abstractmethod
    def close_position(self, ticker: str):
        """
        Close an entire position for a ticker.

        Returns:
            Trade object or None if failed
        """

    @abstractmethod
    def close_all_positions(self) -> int:
        """Close all positions. Returns count of closed positions."""
