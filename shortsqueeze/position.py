"""
Position sizing and risk management for Short Squeeze Trading Bot.

Handles:
- Position size calculation based on account risk
- Stop loss and take profit levels
- Portfolio-level risk management
"""

from dataclasses import dataclass
from typing import Optional

from loguru import logger

from .config import Config, RiskConfig
from .signals import EntrySignal


@dataclass
class PositionSize:
    """Calculated position size and risk parameters."""

    ticker: str
    shares: int
    entry_price: float
    position_value: float
    stop_loss_price: float
    stop_loss_pct: float
    risk_amount: float  # Dollar risk per trade
    risk_pct: float  # Percentage of account risked

    # Take profit targets (optional)
    take_profit_1: Optional[float] = None  # 50% profit target
    take_profit_2: Optional[float] = None  # 100% profit target

    @property
    def is_valid(self) -> bool:
        """Check if position size is valid."""
        return self.shares > 0 and self.position_value > 0


class PositionSizer:
    """Calculates position sizes based on risk management rules."""

    def __init__(self, config: Config):
        self.config = config
        self.risk_config = config.risk

    def calculate_position_size(
        self,
        signal: EntrySignal,
        account_equity: float,
        current_positions: int = 0,
    ) -> Optional[PositionSize]:
        """
        Calculate position size for a trade signal.

        Uses the formula:
        Position Size = (Account Risk $) / (Entry Price - Stop Loss Price)

        Args:
            signal: EntrySignal from signal detection
            account_equity: Current account equity
            current_positions: Number of current open positions

        Returns:
            PositionSize object or None if trade not viable
        """
        # Check if we can take more positions
        if current_positions >= self.risk_config.max_positions:
            logger.info(
                f"Max positions ({self.risk_config.max_positions}) reached, "
                f"skipping {signal.ticker}"
            )
            return None

        entry_price = signal.current_price
        if entry_price <= 0:
            return None

        # Calculate stop loss price
        stop_loss_pct = self._calculate_stop_loss_pct(signal)
        stop_loss_price = entry_price * (1 - stop_loss_pct)

        # Calculate risk per share
        risk_per_share = entry_price - stop_loss_price

        if risk_per_share <= 0:
            logger.warning(f"Invalid risk per share for {signal.ticker}")
            return None

        # Calculate dollar risk for this trade
        risk_amount = account_equity * self.risk_config.risk_per_trade

        # Calculate number of shares
        shares = int(risk_amount / risk_per_share)

        if shares <= 0:
            logger.info(f"Position size too small for {signal.ticker}")
            return None

        # Calculate position value
        position_value = shares * entry_price

        # Check max position size constraint
        max_position_value = account_equity * self.risk_config.max_position_size
        if position_value > max_position_value:
            shares = int(max_position_value / entry_price)
            position_value = shares * entry_price
            logger.debug(
                f"Position size for {signal.ticker} reduced to {shares} shares "
                f"due to max position size limit"
            )

        if shares <= 0:
            return None

        # Recalculate actual risk after adjustment
        actual_risk = shares * risk_per_share
        actual_risk_pct = actual_risk / account_equity

        # Calculate take profit targets
        # TP1: Risk:Reward 1:1.5
        # TP2: Risk:Reward 1:3
        tp1 = entry_price + (risk_per_share * 1.5)
        tp2 = entry_price + (risk_per_share * 3)

        return PositionSize(
            ticker=signal.ticker,
            shares=shares,
            entry_price=entry_price,
            position_value=position_value,
            stop_loss_price=stop_loss_price,
            stop_loss_pct=stop_loss_pct,
            risk_amount=actual_risk,
            risk_pct=actual_risk_pct,
            take_profit_1=tp1,
            take_profit_2=tp2,
        )

    def _calculate_stop_loss_pct(self, signal: EntrySignal) -> float:
        """
        Calculate appropriate stop loss percentage based on volatility.

        For squeeze plays, we use a wider stop due to volatility.
        """
        base_stop = self.risk_config.default_stop_loss_pct

        # Adjust based on signal strength
        if signal.signal_strength == "strong":
            # Stronger signals get tighter stops (more confidence)
            return base_stop * 0.9
        elif signal.signal_strength == "weak":
            # Weaker signals get wider stops
            return base_stop * 1.2

        return base_stop

    def validate_position(
        self,
        position: PositionSize,
        account_equity: float,
        buying_power: float,
    ) -> tuple:
        """
        Validate a position against account constraints.

        Returns:
            Tuple of (is_valid, reason)
        """
        # Check buying power
        if position.position_value > buying_power:
            return False, f"Insufficient buying power (need ${position.position_value:.2f}, have ${buying_power:.2f})"

        # Check risk limits
        max_risk = account_equity * self.risk_config.risk_per_trade * 1.5  # Allow 50% buffer
        if position.risk_amount > max_risk:
            return False, f"Risk amount (${position.risk_amount:.2f}) exceeds limit"

        # Check position value
        max_value = account_equity * self.risk_config.max_position_size
        if position.position_value > max_value:
            return False, f"Position value exceeds max ({self.risk_config.max_position_size*100}% of equity)"

        return True, "Position validated"


class RiskManager:
    """Portfolio-level risk management."""

    def __init__(self, config: Config):
        self.config = config
        self.risk_config = config.risk

    def calculate_portfolio_risk(
        self,
        positions: list,
        account_equity: float,
    ) -> dict:
        """
        Calculate current portfolio risk metrics.

        Args:
            positions: List of position dicts from Alpaca
            account_equity: Current account equity

        Returns:
            dict with risk metrics
        """
        if not positions:
            return {
                "total_exposure": 0,
                "exposure_pct": 0,
                "position_count": 0,
                "largest_position_pct": 0,
                "can_add_position": True,
            }

        total_exposure = sum(abs(p.get("market_value", 0)) for p in positions)
        exposure_pct = (total_exposure / account_equity) * 100 if account_equity > 0 else 0

        largest = max(abs(p.get("market_value", 0)) for p in positions)
        largest_pct = (largest / account_equity) * 100 if account_equity > 0 else 0

        can_add = (
            len(positions) < self.risk_config.max_positions and
            exposure_pct < 80  # Max 80% portfolio exposure
        )

        return {
            "total_exposure": total_exposure,
            "exposure_pct": exposure_pct,
            "position_count": len(positions),
            "largest_position_pct": largest_pct,
            "can_add_position": can_add,
            "remaining_slots": self.risk_config.max_positions - len(positions),
        }

    def check_concentration_risk(
        self,
        positions: list,
        new_ticker: str,
        new_sector: Optional[str] = None,
    ) -> tuple:
        """
        Check if adding a new position creates concentration risk.

        Returns:
            Tuple of (is_ok, warning_message)
        """
        # Check if we already have this ticker
        tickers = [p.get("symbol", "").upper() for p in positions]
        if new_ticker.upper() in tickers:
            return False, f"Already have position in {new_ticker}"

        # Future: Add sector concentration check if sector data available
        # For now, just check duplicate tickers
        return True, ""

    def should_exit_position(
        self,
        position: dict,
        current_price: float,
        entry_price: Optional[float] = None,
        stop_loss_pct: float = 0.08,
        trailing_stop_pct: float = 0.05,
    ) -> tuple:
        """
        Determine if a position should be exited.

        Returns:
            Tuple of (should_exit, reason)
        """
        if entry_price is None:
            entry_price = position.get("avg_entry_price", 0)

        if entry_price <= 0:
            return False, "Unknown entry price"

        unrealized_plpc = position.get("unrealized_plpc", 0)
        highest_price = position.get("highest_price", current_price)

        # Check stop loss
        change_from_entry = (current_price - entry_price) / entry_price
        if change_from_entry <= -stop_loss_pct:
            return True, f"Stop loss triggered ({change_from_entry*100:.1f}%)"

        # Check trailing stop (only if in profit)
        if unrealized_plpc > 0:
            change_from_high = (current_price - highest_price) / highest_price
            if change_from_high <= -trailing_stop_pct:
                return True, f"Trailing stop triggered ({change_from_high*100:.1f}% from high)"

        # Check take profit targets
        if change_from_entry >= 0.30:  # 30% profit
            return True, f"Take profit 2 reached (+{change_from_entry*100:.1f}%)"

        return False, ""

    def get_exit_orders(
        self,
        position: PositionSize,
    ) -> list:
        """
        Generate exit order parameters for a position.

        Returns:
            List of exit order specifications
        """
        orders = []

        # Stop loss order
        orders.append({
            "type": "stop_loss",
            "stop_price": position.stop_loss_price,
            "qty": position.shares,
        })

        # Take profit orders (scale out)
        if position.take_profit_1:
            orders.append({
                "type": "take_profit",
                "limit_price": position.take_profit_1,
                "qty": position.shares // 2,  # Sell half at TP1
            })

        if position.take_profit_2:
            orders.append({
                "type": "take_profit",
                "limit_price": position.take_profit_2,
                "qty": position.shares - (position.shares // 2),  # Sell rest at TP2
            })

        return orders
