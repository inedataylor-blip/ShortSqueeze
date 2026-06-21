"""
Trading execution module for Short Squeeze Trading Bot.

Handles:
- Order execution via broker abstraction layer
- Order management (submit, cancel, modify)
- Trade logging and history
"""

import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from enum import Enum

from loguru import logger

from .broker import BrokerTrader, create_trader
from .config import Config
from .position import PositionSize
from .signals import EntrySignal
from .timezone import now_eastern


class TradeStatus(Enum):
    """Status of a trade."""
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Trade:
    """Represents a trade execution."""

    ticker: str
    side: str  # "buy" or "sell"
    quantity: int
    order_type: str
    status: str
    timestamp: datetime

    # Prices
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    fill_price: Optional[float] = None

    # Order tracking
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None

    # Risk management
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None

    # Context
    signal_strength: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data


class TradeManager:
    """Manages trade execution and logging."""

    def __init__(self, config: Config, executor: Optional[BrokerTrader] = None):
        self.config = config
        self.executor = executor or create_trader(config)
        self.trades_file = config.trades_path
        # Track recently closed positions to prevent duplicate close attempts
        self._pending_closes: set[str] = set()

    def execute_entry(
        self,
        signal: EntrySignal,
        position_size: PositionSize,
    ) -> Optional[Trade]:
        """
        Execute an entry trade based on signal and position size.

        Args:
            signal: Entry signal from signal detection
            position_size: Calculated position size

        Returns:
            Trade object or None if failed
        """
        ticker = signal.ticker
        shares = position_size.shares
        entry_price = position_size.entry_price
        stop_loss_price = position_size.stop_loss_price
        take_profit_price = position_size.take_profit_1

        if self.config.trading.use_limit_orders and take_profit_price:
            # Preferred path: a single bracket order (entry + stop-loss +
            # take-profit submitted atomically). Submitting the entry and a
            # standalone opposing stop separately gets rejected by Alpaca as a
            # "potential wash trade" because the entry order is still open when
            # the stop is placed. A bracket failure does NOT fall through to a
            # market order — we don't want to silently convert a limit-intent
            # entry into a market fill (slippage risk on fast movers).
            limit_offset = entry_price * self.config.trading.limit_offset_pct
            limit_price = entry_price + limit_offset

            trade = self.executor.submit_bracket_order(
                ticker=ticker,
                quantity=shares,
                limit_price=limit_price,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
            )
        else:
            # Market entry (use_limit_orders disabled, or no take-profit target).
            # The standalone stop is best-effort and may be rejected while the
            # entry is unfilled; the in-process position monitor is the backstop.
            trade = self.executor.submit_market_order(
                ticker=ticker,
                quantity=shares,
                side="buy",
            )
            if trade:
                self._submit_stop_loss(ticker, shares, stop_loss_price)

        if trade:
            trade.stop_loss_price = stop_loss_price
            trade.take_profit_price = take_profit_price
            trade.signal_strength = signal.signal_strength
            trade.reason = signal.reason

            # Log trade
            self._log_trade(trade)

        return trade

    def _submit_stop_loss(
        self,
        ticker: str,
        quantity: int,
        stop_price: float,
    ) -> Optional[Trade]:
        """Submit a stop loss order for an entered position."""
        return self.executor.submit_stop_order(
            ticker=ticker,
            quantity=quantity,
            stop_price=stop_price,
            side="sell",
        )

    def execute_exit(
        self,
        ticker: str,
        reason: str = "manual",
    ) -> Optional[Trade]:
        """
        Execute an exit trade (close position).

        Args:
            ticker: Stock symbol
            reason: Reason for exit

        Returns:
            Trade object or None if failed
        """
        # Prevent duplicate close attempts
        ticker_upper = ticker.upper()
        if ticker_upper in self._pending_closes:
            logger.debug(f"Skipping duplicate close attempt for {ticker}")
            return None

        self._pending_closes.add(ticker_upper)

        try:
            # Cancel any open orders for this ticker first
            open_orders = self.executor.get_open_orders(ticker)
            for order in open_orders:
                self.executor.cancel_order(order["id"])

            # Close position
            trade = self.executor.close_position(ticker)

            if trade:
                trade.reason = reason
                self._log_trade(trade)

            return trade
        finally:
            # Remove from pending after a delay to prevent rapid retries
            # The position monitor runs every minute, so 30s is safe
            import threading
            def clear_pending():
                time.sleep(30)
                self._pending_closes.discard(ticker_upper)
            threading.Thread(target=clear_pending, daemon=True).start()

    def _log_trade(self, trade: Trade) -> None:
        """Log trade to JSON file."""
        try:
            self.config.data_dir.mkdir(parents=True, exist_ok=True)

            # Load existing trades
            trades = []
            if self.trades_file.exists():
                with open(self.trades_file, "r") as f:
                    trades = json.load(f)

            # Add new trade
            trades.append(trade.to_dict())

            # Save
            with open(self.trades_file, "w") as f:
                json.dump(trades, f, indent=2, default=str)

        except Exception as e:
            logger.error(f"Failed to log trade: {e}")

    def get_trade_history(self, limit: int = 100) -> list:
        """Get recent trade history."""
        try:
            if not self.trades_file.exists():
                return []

            with open(self.trades_file, "r") as f:
                trades = json.load(f)

            return trades[-limit:]

        except Exception as e:
            logger.error(f"Failed to get trade history: {e}")
            return []

    def wait_for_fill(
        self,
        order_id: str,
        timeout_seconds: int = 30,
        poll_interval: float = 0.5,
    ) -> Optional[dict]:
        """
        Wait for an order to be filled.

        Args:
            order_id: Order ID to monitor
            timeout_seconds: Maximum time to wait
            poll_interval: Time between status checks

        Returns:
            Order dict if filled, None if timeout or failed
        """
        start = time.time()

        while time.time() - start < timeout_seconds:
            order = self.executor.get_order(order_id)

            if not order:
                return None

            status = order.get("status", "")

            if status == "filled":
                return order
            elif status in ["cancelled", "expired", "rejected"]:
                logger.warning(f"Order {order_id} ended with status: {status}")
                return None

            time.sleep(poll_interval)

        logger.warning(f"Order {order_id} timed out after {timeout_seconds}s")
        return None
