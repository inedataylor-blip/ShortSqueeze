"""
Alpaca implementation of BrokerTrader.

Handles order execution, management, and position closure via the Alpaca SDK.
"""

from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import (
    OrderSide,
    TimeInForce,
    QueryOrderStatus,
)
from loguru import logger

from .base import BrokerTrader
from ..timezone import now_eastern


class AlpacaTradeExecutor(BrokerTrader):
    """Alpaca implementation of broker trader."""

    def __init__(self, config):
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

    def submit_market_order(self, ticker: str, quantity: int, side: str = "buy"):
        """Submit a market order."""
        from ..trader import Trade

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
                timestamp=now_eastern(),
                order_id=str(order.id),
                client_order_id=order.client_order_id,
            )

            logger.info(f"Market order submitted: {side.upper()} {quantity} {ticker}")
            return trade

        except Exception as e:
            logger.error(f"Failed to submit market order for {ticker}: {e}")
            return None

    def submit_limit_order(self, ticker: str, quantity: int, limit_price: float, side: str = "buy"):
        """Submit a limit order."""
        from ..trader import Trade

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
                timestamp=now_eastern(),
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

    def submit_stop_order(self, ticker: str, quantity: int, stop_price: float, side: str = "sell"):
        """Submit a stop order."""
        from ..trader import Trade

        try:
            order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

            request = StopOrderRequest(
                symbol=ticker,
                qty=quantity,
                side=order_side,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop_price, 2),
            )

            order = self.client.submit_order(request)

            trade = Trade(
                ticker=ticker,
                side=side,
                quantity=quantity,
                order_type="stop",
                status=order.status.value,
                timestamp=now_eastern(),
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
    ):
        """Submit a bracket order (entry + stop loss + take profit)."""
        from ..trader import Trade

        try:
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
                timestamp=now_eastern(),
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

    def close_position(self, ticker: str):
        """Close an entire position for a ticker."""
        from ..trader import Trade

        try:
            order = self.client.close_position(ticker)

            trade = Trade(
                ticker=ticker,
                side="sell",
                quantity=int(float(order.qty)),
                order_type="market",
                status=order.status.value,
                timestamp=now_eastern(),
                order_id=str(order.id),
                reason="position_close",
            )

            # Order is submitted, not necessarily filled — the position only
            # actually closes once Alpaca reports a fill.
            logger.info(f"Submitted close: SELL {order.qty} {ticker}")
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
