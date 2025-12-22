"""
Technical indicators for the Short Squeeze Trading Bot.

Implements the LazyBear Squeeze Momentum indicator and supporting calculations.
Based on: https://www.tradingview.com/script/nqQ1DT5a-Squeeze-Momentum-Indicator-LazyBear/

Indicator Logic:
- Squeeze State: Bollinger Bands inside Keltner Channels = volatility compressed
- Squeeze Firing: Bollinger Bands expand outside Keltner Channels
- Momentum: Linear regression of price deviation from midline
"""

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from .config import IndicatorConfig


@dataclass
class SqueezeState:
    """Represents the current state of the Squeeze Momentum indicator."""

    squeeze_on: bool  # BBs inside KCs (volatility compressed)
    squeeze_off: bool  # BBs outside KCs (volatility expanding)
    momentum_value: float  # Current momentum value
    momentum_rising: bool  # Momentum > previous momentum
    momentum_positive: bool  # Momentum > 0

    # Derived signals
    @property
    def bullish(self) -> bool:
        """Bullish signal: positive and rising momentum."""
        return self.momentum_positive and self.momentum_rising

    @property
    def bearish(self) -> bool:
        """Bearish signal: negative and falling momentum."""
        return not self.momentum_positive and not self.momentum_rising

    @property
    def squeeze_firing_bullish(self) -> bool:
        """Squeeze just fired with bullish momentum."""
        return self.squeeze_off and self.bullish


def calculate_sma(series: pd.Series, period: int) -> pd.Series:
    """Calculate Simple Moving Average."""
    return series.rolling(window=period, min_periods=period).mean()


def calculate_std(series: pd.Series, period: int) -> pd.Series:
    """Calculate Standard Deviation."""
    return series.rolling(window=period, min_periods=period).std()


def calculate_true_range(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series
) -> pd.Series:
    """
    Calculate True Range.

    TR = max(high - low, abs(high - prev_close), abs(low - prev_close))
    """
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = abs(high - prev_close)
    tr3 = abs(low - prev_close)
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def calculate_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int
) -> pd.Series:
    """Calculate Average True Range."""
    tr = calculate_true_range(high, low, close)
    return calculate_sma(tr, period)


def calculate_bollinger_bands(
    close: pd.Series,
    period: int = 20,
    mult: float = 2.0
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Calculate Bollinger Bands.

    Returns:
        Tuple of (upper_band, middle_band, lower_band)
    """
    middle = calculate_sma(close, period)
    std = calculate_std(close, period)

    upper = middle + mult * std
    lower = middle - mult * std

    return upper, middle, lower


def calculate_keltner_channels(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
    mult: float = 1.5,
    use_true_range: bool = True
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Calculate Keltner Channels.

    Returns:
        Tuple of (upper_channel, middle_line, lower_channel)
    """
    middle = calculate_sma(close, period)

    if use_true_range:
        range_val = calculate_atr(high, low, close, period)
    else:
        range_val = calculate_sma(high - low, period)

    upper = middle + mult * range_val
    lower = middle - mult * range_val

    return upper, middle, lower


def calculate_donchian_midline(
    high: pd.Series,
    low: pd.Series,
    period: int = 20
) -> pd.Series:
    """
    Calculate Donchian Channel midline.

    Midline = (highest high + lowest low) / 2
    """
    highest_high = high.rolling(window=period, min_periods=period).max()
    lowest_low = low.rolling(window=period, min_periods=period).min()
    return (highest_high + lowest_low) / 2


def calculate_linear_regression(series: pd.Series, period: int) -> pd.Series:
    """
    Calculate linear regression value (last point on regression line).

    This uses the least squares method to fit a line and returns
    the y-value at the last point (which represents the current "smoothed" value).
    """
    def linreg(values):
        if len(values) < period or np.isnan(values).any():
            return np.nan
        x = np.arange(period)
        y = values[-period:]
        # Fit linear regression: y = mx + b
        # The value at the last point is what we want
        try:
            coeffs = np.polyfit(x, y, 1)
            return coeffs[0] * (period - 1) + coeffs[1]
        except (np.linalg.LinAlgError, ValueError):
            return np.nan

    return series.rolling(window=period, min_periods=period).apply(linreg, raw=True)


def calculate_squeeze_momentum(
    df: pd.DataFrame,
    config: Optional[IndicatorConfig] = None
) -> pd.DataFrame:
    """
    Calculate the full Squeeze Momentum indicator.

    Args:
        df: DataFrame with 'open', 'high', 'low', 'close', 'volume' columns
        config: Indicator configuration parameters

    Returns:
        DataFrame with added indicator columns:
        - bb_upper, bb_middle, bb_lower: Bollinger Bands
        - kc_upper, kc_middle, kc_lower: Keltner Channels
        - squeeze_on: Boolean, True when in squeeze (BBs inside KCs)
        - squeeze_off: Boolean, True when squeeze fired (BBs outside KCs)
        - momentum: The momentum oscillator value
        - momentum_rising: Boolean, True when momentum is rising
        - momentum_positive: Boolean, True when momentum > 0
    """
    if config is None:
        config = IndicatorConfig()

    result = df.copy()

    # Ensure we have the required columns
    required_cols = ["high", "low", "close"]
    for col in required_cols:
        if col not in result.columns:
            raise ValueError(f"DataFrame missing required column: {col}")

    high = result["high"]
    low = result["low"]
    close = result["close"]

    # Calculate Bollinger Bands
    bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(
        close, config.bb_length, config.bb_mult
    )
    result["bb_upper"] = bb_upper
    result["bb_middle"] = bb_middle
    result["bb_lower"] = bb_lower

    # Calculate Keltner Channels
    kc_upper, kc_middle, kc_lower = calculate_keltner_channels(
        high, low, close,
        config.kc_length, config.kc_mult, config.use_true_range
    )
    result["kc_upper"] = kc_upper
    result["kc_middle"] = kc_middle
    result["kc_lower"] = kc_lower

    # Determine squeeze state
    # sqzOn: BBs inside KCs (volatility compressed)
    result["squeeze_on"] = (bb_lower > kc_lower) & (bb_upper < kc_upper)
    # sqzOff: BBs outside KCs (volatility expanding)
    result["squeeze_off"] = (bb_lower < kc_lower) & (bb_upper > kc_upper)
    # No squeeze (neutral state)
    result["no_squeeze"] = ~result["squeeze_on"] & ~result["squeeze_off"]

    # Calculate momentum value
    # Midline = average of Donchian midline and SMA
    donchian_mid = calculate_donchian_midline(high, low, config.kc_length)
    sma_mid = calculate_sma(close, config.kc_length)
    midline = (donchian_mid + sma_mid) / 2

    # Momentum = LinReg(close - midline, length)
    deviation = close - midline
    result["momentum"] = calculate_linear_regression(deviation, config.kc_length)

    # Momentum direction
    result["momentum_rising"] = result["momentum"] > result["momentum"].shift(1)
    result["momentum_positive"] = result["momentum"] > 0

    # Bullish: positive and rising
    result["bullish"] = result["momentum_positive"] & result["momentum_rising"]
    # Bearish: negative and falling
    result["bearish"] = ~result["momentum_positive"] & ~result["momentum_rising"]

    return result


def get_current_squeeze_state(df: pd.DataFrame) -> Optional[SqueezeState]:
    """
    Get the current squeeze state from a calculated DataFrame.

    Args:
        df: DataFrame with squeeze momentum indicators calculated

    Returns:
        SqueezeState object or None if insufficient data
    """
    required_cols = ["squeeze_on", "squeeze_off", "momentum", "momentum_rising", "momentum_positive"]
    for col in required_cols:
        if col not in df.columns:
            logger.warning(f"DataFrame missing column for squeeze state: {col}")
            return None

    if len(df) < 2:
        return None

    last = df.iloc[-1]

    # Handle NaN values
    if pd.isna(last["momentum"]):
        return None

    return SqueezeState(
        squeeze_on=bool(last["squeeze_on"]),
        squeeze_off=bool(last["squeeze_off"]),
        momentum_value=float(last["momentum"]),
        momentum_rising=bool(last["momentum_rising"]),
        momentum_positive=bool(last["momentum_positive"]),
    )


def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Calculate Volume Weighted Average Price (VWAP).

    VWAP = cumsum(typical_price * volume) / cumsum(volume)
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    return (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()


def calculate_volume_ratio(
    current_volume: float,
    avg_volume: float,
    minutes_since_open: int,
    market_minutes: int = 390  # 6.5 hours
) -> float:
    """
    Calculate volume ratio adjusted for time of day.

    This prorates the average daily volume to compare fairly
    with intraday cumulative volume.

    Args:
        current_volume: Current day's cumulative volume
        avg_volume: Average daily volume (20-day)
        minutes_since_open: Minutes since market open
        market_minutes: Total trading minutes in a day (default 390)

    Returns:
        Volume ratio (current vs expected at this time of day)
    """
    if avg_volume == 0 or minutes_since_open <= 0:
        return 0.0

    # Expected volume by now = avg_volume * (time_elapsed / total_time)
    # But volume is typically front-loaded, so we use a simple linear model
    expected_volume = avg_volume * (minutes_since_open / market_minutes)

    if expected_volume == 0:
        return 0.0

    return current_volume / expected_volume


def calculate_price_change_percent(
    current_price: float,
    previous_close: float
) -> float:
    """Calculate percentage price change from previous close."""
    if previous_close == 0:
        return 0.0
    return ((current_price - previous_close) / previous_close) * 100


class TechnicalAnalyzer:
    """High-level technical analysis interface."""

    def __init__(self, config: IndicatorConfig):
        self.config = config

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Perform full technical analysis on price data.

        Args:
            df: DataFrame with OHLCV data

        Returns:
            DataFrame with all technical indicators
        """
        # Calculate squeeze momentum indicators
        result = calculate_squeeze_momentum(df, self.config)

        # Add VWAP if volume is available
        if "volume" in result.columns:
            result["vwap"] = calculate_vwap(result)
            result["above_vwap"] = result["close"] > result["vwap"]

            # Calculate volume moving average
            result["volume_sma"] = calculate_sma(
                result["volume"], self.config.volume_ma_period
            )

        return result

    def get_squeeze_signal(self, df: pd.DataFrame) -> Optional[SqueezeState]:
        """
        Get current squeeze signal.

        Args:
            df: DataFrame with OHLCV data (will calculate indicators if needed)

        Returns:
            SqueezeState object
        """
        if "squeeze_on" not in df.columns:
            df = self.analyze(df)

        return get_current_squeeze_state(df)

    def check_entry_conditions(
        self,
        df: pd.DataFrame,
        current_price: float,
        previous_close: float,
        current_volume: float,
        avg_volume: float,
        minutes_since_open: int = 60,
    ) -> dict:
        """
        Check all entry conditions for a squeeze trade.

        Returns:
            dict with condition results and overall signal
        """
        # Ensure indicators are calculated
        if "squeeze_on" not in df.columns:
            df = self.analyze(df)

        squeeze_state = get_current_squeeze_state(df)

        if squeeze_state is None:
            return {"signal": False, "reason": "Insufficient data for squeeze calculation"}

        # Check price change
        price_change_pct = calculate_price_change_percent(current_price, previous_close)
        price_trigger = price_change_pct >= self.config.min_price_change_pct

        # Check volume
        volume_ratio = calculate_volume_ratio(
            current_volume, avg_volume, minutes_since_open
        )
        volume_trigger = volume_ratio >= self.config.volume_multiplier

        # Check VWAP
        above_vwap = False
        if "vwap" in df.columns and len(df) > 0:
            vwap = df["vwap"].iloc[-1]
            above_vwap = current_price > vwap if not pd.isna(vwap) else False

        # Primary triggers
        primary_met = price_trigger and volume_trigger and above_vwap

        # Squeeze conditions
        squeeze_fired = squeeze_state.squeeze_off
        strong_momentum = squeeze_state.bullish

        # Final signal
        entry_signal = primary_met and (squeeze_fired or strong_momentum)

        return {
            "signal": entry_signal,
            "primary_triggers_met": primary_met,
            "price_change_pct": price_change_pct,
            "price_trigger": price_trigger,
            "volume_ratio": volume_ratio,
            "volume_trigger": volume_trigger,
            "above_vwap": above_vwap,
            "squeeze_state": {
                "squeeze_on": squeeze_state.squeeze_on,
                "squeeze_off": squeeze_state.squeeze_off,
                "momentum_value": squeeze_state.momentum_value,
                "momentum_rising": squeeze_state.momentum_rising,
                "momentum_positive": squeeze_state.momentum_positive,
                "bullish": squeeze_state.bullish,
            },
            "reason": self._get_signal_reason(
                entry_signal, price_trigger, volume_trigger,
                above_vwap, squeeze_state
            ),
        }

    @staticmethod
    def _get_signal_reason(
        signal: bool,
        price_trigger: bool,
        volume_trigger: bool,
        above_vwap: bool,
        squeeze_state: SqueezeState,
    ) -> str:
        """Generate human-readable reason for signal decision."""
        if signal:
            reasons = ["Entry signal triggered:"]
            if squeeze_state.squeeze_off:
                reasons.append("- Squeeze fired (volatility expanding)")
            if squeeze_state.bullish:
                reasons.append("- Bullish momentum (positive and rising)")
            return " ".join(reasons)

        missing = []
        if not price_trigger:
            missing.append("price move < 5%")
        if not volume_trigger:
            missing.append("volume < 3x average")
        if not above_vwap:
            missing.append("below VWAP")
        if not squeeze_state.squeeze_off and not squeeze_state.bullish:
            missing.append("no squeeze/momentum signal")

        return f"No signal: {', '.join(missing)}"
