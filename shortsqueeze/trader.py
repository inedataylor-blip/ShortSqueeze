"""
Trading execution module for Short Squeeze Trading Bot.

Handles:
- Order execution via Alpaca API
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

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    StopLimitOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import (
    OrderSide,
    OrderType,
    TimeInForce,
    OrderStatus,
    QueryOrderStatus,
)
from loguru import logger

from .config import Config
from .position import PositionSize
from .signals import EntrySignal


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


class TradeExecutor:
    """Executes trades via Alpaca API."""

    def __init__(self, config: Config):
        self.config = config
        self._client = None

    @property
    def client(self) -> TradingClient:
        """Lazy-load trading client."""
        if self._client is None:
            self._client = TradingClient(
                api_key=self.config.alpaca.api_key,
                secret_key=self.config.alpaca.secret_key,
                paper="paper" in self.config.alpaca.base_url,
            )
        return self._client

    def submit_market_order(
        self,
        ticker: str,
        quantity: int,
        side: str = "buy",
    ) -> Optional[Trade]:
        """
        Submit a market order.

        Args:
            ticker: Stock symbol
            quantity: Number of shares
            side: "buy" or "sell"

        Returns:
            Trade object or None if failed
        """
        try:
            order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

            request = MarketOrderRequest(
                symbol=ticker,
                qty=quantity,
                side=order_side,
                time_in_force=TimeInForce.DAY,
            )

            order = self.client.submit_order(request)

            trade = Trade(
                ticker=ticker,
                side=side,
                quantity=quantity,
                order_type="market",
                status=order.status.value,
                timestamp=datetime.now(),
                order_id=str(order.id),
                client_order_id=order.client_order_id,
            )

            logger.info(f"Market order submitted: {side.upper()} {quantity} {ticker}")
            return trade

        except Exception as e:
            logger.error(f"Failed to submit market order for {ticker}: {e}")
            return None

    def submit_limit_order(
        self,
        ticker: str,
        quantity: int,
        limit_price: float,
        side: str = "buy",
    ) -> Optional[Trade]:
        """
        Submit a limit order.

        Args:
            ticker: Stock symbol
            quantity: Number of shares
            limit_price: Limit price
            side: "buy" or "sell"

        Returns:
            Trade object or None if failed
        """
        try:
            order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

            request = LimitOrderRequest(
                symbol=ticker,
                qty=quantity,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
            )

            order = self.client.submit_order(request)

            trade = Trade(
                ticker=ticker,
                side=side,
                quantity=quantity,
                order_type="limit",
                status=order.status.value,
                timestamp=datetime.now(),
                limit_price=limit_price,
                order_id=str(order.id),
                client_order_id=order.client_order_id,
            )

            logger.info(
                f"Limit order submitted: {side.upper()} {quantity} {ticker} @ ${limit_price:.2f}"
            )
            return trade

        except Exception as e:
            logger.error(f"Failed to submit limit order for {ticker}: {e}")
            return None

    def submit_stop_order(
        self,
        ticker: str,
        quantity: int,
        stop_price: float,
        side: str = "sell",
    ) -> Optional[Trade]:
        """
        Submit a stop order (typically for stop loss).

        Args:
            ticker: Stock symbol
            quantity: Number of shares
            stop_price: Stop trigger price
            side: "buy" or "sell"

        Returns:
            Trade object or None if failed
        """
        try:
            order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

            request = StopOrderRequest(
                symbol=ticker,
                qty=quantity,
                side=order_side,
                time_in_force=TimeInForce.GTC,  # Good til cancelled for stops
                stop_price=round(stop_price, 2),
            )

            order = self.client.submit_order(request)

            trade = Trade(
                ticker=ticker,
                side=side,
                quantity=quantity,
                order_type="stop",
                status=order.status.value,
                timestamp=datetime.now(),
                stop_price=stop_price,
                order_id=str(order.id),
                client_order_id=order.client_order_id,
            )

            logger.info(
                f"Stop order submitted: {side.upper()} {quantity} {ticker} @ ${stop_price:.2f}"
            )
            return trade

        except Exception as e:
            logger.error(f"Failed to submit stop order for {ticker}: {e}")
            return None

    def submit_bracket_order(
        self,
        ticker: str,
        quantity: int,
        limit_price: float,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> Optional[Trade]:
        """
        Submit a bracket order (entry + stop loss + take profit).

        Note: Alpaca's bracket orders use OCO (one-cancels-other) logic.
        """
        try:
            # Submit main entry order as limit
            request = LimitOrderRequest(
                symbol=ticker,
                qty=quantity,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                order_class="bracket",
                stop_loss={"stop_price": round(stop_loss_price, 2)},
                take_profit={"limit_price": round(take_profit_price, 2)},
            )

            order = self.client.submit_order(request)

            trade = Trade(
                ticker=ticker,
                side="buy",
                quantity=quantity,
                order_type="bracket",
                status=order.status.value,
                timestamp=datetime.now(),
                limit_price=limit_price,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                order_id=str(order.id),
                client_order_id=order.client_order_id,
            )

            logger.info(
                f"Bracket order submitted: BUY {quantity} {ticker} @ ${limit_price:.2f} "
                f"(SL: ${stop_loss_price:.2f}, TP: ${take_profit_price:.2f})"
            )
            return trade

        except Exception as e:
            logger.error(f"Failed to submit bracket order for {ticker}: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by ID."""
        try:
            self.client.cancel_order_by_id(order_id)
            logger.info(f"Order {order_id} cancelled")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def cancel_all_orders(self) -> int:
        """Cancel all open orders."""
        try:
            result = self.client.cancel_orders()
            logger.info(f"Cancelled {len(result)} orders")
            return len(result)
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return 0

    def get_order(self, order_id: str) -> Optional[dict]:
        """Get order details by ID."""
        try:
            order = self.client.get_order_by_id(order_id)
            return {
                "id": str(order.id),
                "symbol": order.symbol,
                "qty": float(order.qty),
                "filled_qty": float(order.filled_qty) if order.filled_qty else 0,
                "side": order.side.value,
                "type": order.order_type.value,
                "status": order.status.value,
                "limit_price": float(order.limit_price) if order.limit_price else None,
                "stop_price": float(order.stop_price) if order.stop_price else None,
                "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
                "created_at": order.created_at.isoformat() if order.created_at else None,
                "filled_at": order.filled_at.isoformat() if order.filled_at else None,
            }
        except Exception as e:
            logger.error(f"Failed to get order {order_id}: {e}")
            return None

    def get_open_orders(self, ticker: Optional[str] = None) -> list:
        """Get all open orders, optionally filtered by ticker."""
        try:
            request = GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                symbols=[ticker] if ticker else None,
            )
            orders = self.client.get_orders(request)

            return [{
                "id": str(o.id),
                "symbol": o.symbol,
                "qty": float(o.qty),
                "side": o.side.value,
                "type": o.order_type.value,
                "status": o.status.value,
                "limit_price": float(o.limit_price) if o.limit_price else None,
                "stop_price": float(o.stop_price) if o.stop_price else None,
            } for o in orders]

        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    def close_position(self, ticker: str) -> Optional[Trade]:
        """Close an entire position for a ticker."""
        try:
            order = self.client.close_position(ticker)

            trade = Trade(
                ticker=ticker,
                side="sell",
                quantity=int(float(order.qty)),
                order_type="market",
                status=order.status.value,
                timestamp=datetime.now(),
                order_id=str(order.id),
                reason="position_close",
            )

            logger.info(f"Position closed: SELL {order.qty} {ticker}")
            return trade

        except Exception as e:
            logger.error(f"Failed to close position for {ticker}: {e}")
            return None

    def close_all_positions(self) -> int:
        """Close all positions."""
        try:
            result = self.client.close_all_positions()
            logger.info(f"Closed {len(result)} positions")
            return len(result)
        except Exception as e:
            logger.error(f"Failed to close all positions: {e}")
            return 0


class TradeManager:
    """Manages trade execution and logging."""

    def __init__(self, config: Config):
        self.config = config
        self.executor = TradeExecutor(config)
        self.trades_file = config.trades_path

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

        # Decide between market and limit order
        if self.config.trading.use_limit_orders:
            # Set limit slightly above current price for buy
            limit_offset = entry_price * self.config.trading.limit_offset_pct
            limit_price = entry_price + limit_offset

            trade = self.executor.submit_limit_order(
                ticker=ticker,
                quantity=shares,
                limit_price=limit_price,
                side="buy",
            )
        else:
            trade = self.executor.submit_market_order(
                ticker=ticker,
                quantity=shares,
                side="buy",
            )

        if trade:
            trade.stop_loss_price = position_size.stop_loss_price
            trade.take_profit_price = position_size.take_profit_1
            trade.signal_strength = signal.signal_strength
            trade.reason = signal.reason

            # Submit stop loss order
            self._submit_stop_loss(ticker, shares, position_size.stop_loss_price)

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
