"""
Signal detection module for Short Squeeze Trading Bot.

Handles intraday scanning and entry signal detection based on:
- Primary triggers (price, volume, VWAP)
- Secondary confirmation (Squeeze Momentum indicator)
"""

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional

import pandas as pd
from loguru import logger

from .config import Config
from .data import AlpacaDataClient, YahooFinanceData
from .indicators import TechnicalAnalyzer, SqueezeState
from .universe import WatchlistManager


@dataclass
class EntrySignal:
    """Represents an entry signal for a potential trade."""

    ticker: str
    timestamp: datetime
    signal_strength: str  # "strong", "moderate", "weak"

    # Price data
    current_price: float
    previous_close: float
    price_change_pct: float

    # Volume data
    current_volume: float
    avg_volume: float
    volume_ratio: float

    # Technical indicators
    vwap: float
    above_vwap: bool
    squeeze_state: dict

    # Signal components
    primary_triggers_met: bool
    squeeze_fired: bool
    momentum_bullish: bool

    # Additional context
    short_float_pct: Optional[float] = None
    sector: Optional[str] = None
    reason: str = ""

    @property
    def should_trade(self) -> bool:
        """Determine if this signal warrants a trade."""
        return self.primary_triggers_met and (self.squeeze_fired or self.momentum_bullish)


@dataclass
class ScanResult:
    """Result of scanning the watchlist for signals."""

    timestamp: datetime
    stocks_scanned: int
    signals: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def has_signals(self) -> bool:
        return len(self.signals) > 0

    @property
    def tradeable_signals(self) -> list:
        return [s for s in self.signals if s.should_trade]


class SignalDetector:
    """Detects entry signals for short squeeze trades."""

    # Market hours (Eastern Time)
    MARKET_OPEN = time(9, 30)
    MARKET_CLOSE = time(16, 0)

    def __init__(self, config: Config):
        self.config = config
        self.alpaca = AlpacaDataClient(config)
        self.yahoo = YahooFinanceData()
        self.analyzer = TechnicalAnalyzer(config.indicators)
        self.watchlist_manager = WatchlistManager(config)

    def is_market_open(self) -> bool:
        """Check if the market is currently open."""
        now = datetime.now()

        # Check if it's a weekday
        if now.weekday() >= 5:  # Saturday = 5, Sunday = 6
            return False

        current_time = now.time()
        return self.MARKET_OPEN <= current_time <= self.MARKET_CLOSE

    def get_minutes_since_open(self) -> int:
        """Get number of minutes since market open."""
        now = datetime.now()
        market_open = datetime.combine(now.date(), self.MARKET_OPEN)

        if now < market_open:
            return 0

        delta = now - market_open
        return int(delta.total_seconds() / 60)

    def scan_stock(self, ticker: str, stock_info: Optional[dict] = None) -> Optional[EntrySignal]:
        """
        Scan a single stock for entry signals.

        Args:
            ticker: Stock symbol
            stock_info: Optional pre-loaded stock info from watchlist

        Returns:
            EntrySignal if conditions are met, None otherwise
        """
        try:
            # Get current data from Alpaca
            quote = self.alpaca.get_latest_quote(ticker)
            if not quote:
                logger.debug(f"{ticker}: No quote available")
                return None

            current_price = (quote.get("bid", 0) + quote.get("ask", 0)) / 2
            if current_price <= 0:
                return None

            # Get daily bars for indicators
            daily_bars = self.alpaca.get_daily_bars(ticker, days=30)
            if daily_bars.empty or len(daily_bars) < 21:
                logger.debug(f"{ticker}: Insufficient historical data")
                return None

            # Get previous close
            previous_close = daily_bars["close"].iloc[-2] if len(daily_bars) >= 2 else 0
            if previous_close <= 0:
                return None

            # Get intraday bars for VWAP
            intraday = self.alpaca.get_intraday_bars(ticker, minutes=5, days_back=1)

            # Calculate current day's volume
            today = datetime.now().date()
            if not intraday.empty:
                today_data = intraday[intraday.index.date == today]
                current_volume = today_data["volume"].sum() if not today_data.empty else 0
            else:
                current_volume = 0

            # Get average volume
            avg_volume = daily_bars["volume"].rolling(20).mean().iloc[-1]
            if pd.isna(avg_volume) or avg_volume <= 0:
                avg_volume = daily_bars["volume"].mean()

            # Get stock info from watchlist if not provided
            if stock_info is None:
                stock_info = self.watchlist_manager.get_stock_info(ticker) or {}

            # Check entry conditions using technical analyzer
            minutes_since_open = self.get_minutes_since_open()

            # Calculate indicators on daily data
            analyzed = self.analyzer.analyze(daily_bars)

            # Get current VWAP from intraday data if available
            vwap = 0
            above_vwap = False
            if not intraday.empty:
                intraday_analyzed = self.analyzer.analyze(intraday)
                if "vwap" in intraday_analyzed.columns:
                    vwap = intraday_analyzed["vwap"].iloc[-1]
                    above_vwap = current_price > vwap if not pd.isna(vwap) else False

            # Get squeeze state from daily data
            squeeze_state = self.analyzer.get_squeeze_signal(analyzed)

            # Calculate price change
            price_change_pct = ((current_price - previous_close) / previous_close) * 100

            # Calculate volume ratio (adjusted for time of day)
            if minutes_since_open > 0 and avg_volume > 0:
                expected_volume = avg_volume * (minutes_since_open / 390)
                volume_ratio = current_volume / expected_volume if expected_volume > 0 else 0
            else:
                volume_ratio = 0

            # Check primary triggers
            price_trigger = price_change_pct >= self.config.indicators.min_price_change_pct
            volume_trigger = volume_ratio >= self.config.indicators.volume_multiplier
            primary_triggers_met = price_trigger and volume_trigger and above_vwap

            # Check squeeze conditions
            squeeze_fired = squeeze_state.squeeze_off if squeeze_state else False
            momentum_bullish = squeeze_state.bullish if squeeze_state else False

            # Determine if we have a valid signal
            has_signal = primary_triggers_met and (squeeze_fired or momentum_bullish)

            if not has_signal:
                logger.debug(
                    f"{ticker}: No signal - price={price_trigger}, vol={volume_trigger}, "
                    f"vwap={above_vwap}, squeeze={squeeze_fired}, mom={momentum_bullish}"
                )
                return None

            # Determine signal strength
            if squeeze_fired and momentum_bullish and price_change_pct >= 10:
                strength = "strong"
            elif squeeze_fired or (momentum_bullish and price_change_pct >= 7):
                strength = "moderate"
            else:
                strength = "weak"

            # Build reason string
            reasons = []
            if squeeze_fired:
                reasons.append("Squeeze fired")
            if momentum_bullish:
                reasons.append("Bullish momentum")
            reasons.append(f"+{price_change_pct:.1f}%")
            reasons.append(f"{volume_ratio:.1f}x volume")

            signal = EntrySignal(
                ticker=ticker,
                timestamp=datetime.now(),
                signal_strength=strength,
                current_price=current_price,
                previous_close=previous_close,
                price_change_pct=price_change_pct,
                current_volume=current_volume,
                avg_volume=avg_volume,
                volume_ratio=volume_ratio,
                vwap=vwap,
                above_vwap=above_vwap,
                squeeze_state={
                    "squeeze_on": squeeze_state.squeeze_on if squeeze_state else False,
                    "squeeze_off": squeeze_state.squeeze_off if squeeze_state else False,
                    "momentum_value": squeeze_state.momentum_value if squeeze_state else 0,
                    "momentum_rising": squeeze_state.momentum_rising if squeeze_state else False,
                    "momentum_positive": squeeze_state.momentum_positive if squeeze_state else False,
                },
                primary_triggers_met=primary_triggers_met,
                squeeze_fired=squeeze_fired,
                momentum_bullish=momentum_bullish,
                short_float_pct=stock_info.get("short_float_pct"),
                sector=stock_info.get("sector"),
                reason=" | ".join(reasons),
            )

            logger.info(f"{ticker}: Entry signal detected - {strength} - {signal.reason}")
            return signal

        except Exception as e:
            logger.error(f"Error scanning {ticker}: {e}")
            return None

    def scan_watchlist(self) -> ScanResult:
        """
        Scan all stocks in the watchlist for entry signals.

        Returns:
            ScanResult with detected signals
        """
        result = ScanResult(
            timestamp=datetime.now(),
            stocks_scanned=0,
        )

        if not self.is_market_open():
            logger.info("Market is closed, skipping scan")
            return result

        watchlist = self.watchlist_manager.get_watchlist()
        if watchlist.empty:
            logger.warning("Watchlist is empty, nothing to scan")
            return result

        tickers = self.watchlist_manager.get_tickers()
        logger.info(f"Scanning {len(tickers)} stocks from watchlist...")

        for ticker in tickers:
            result.stocks_scanned += 1
            stock_info = self.watchlist_manager.get_stock_info(ticker)

            try:
                signal = self.scan_stock(ticker, stock_info)
                if signal:
                    result.signals.append(signal)
            except Exception as e:
                result.errors.append({"ticker": ticker, "error": str(e)})
                logger.error(f"Error scanning {ticker}: {e}")

        # Sort signals by strength
        strength_order = {"strong": 0, "moderate": 1, "weak": 2}
        result.signals.sort(key=lambda s: strength_order.get(s.signal_strength, 3))

        logger.info(
            f"Scan complete: {result.stocks_scanned} stocks scanned, "
            f"{len(result.signals)} signals detected, {len(result.errors)} errors"
        )

        return result

    def quick_check(self, ticker: str) -> dict:
        """
        Quick check of a single ticker without full analysis.

        Useful for real-time monitoring of specific stocks.
        """
        quote = self.alpaca.get_latest_quote(ticker)
        if not quote:
            return {"ticker": ticker, "status": "no_data"}

        current_price = (quote.get("bid", 0) + quote.get("ask", 0)) / 2

        # Get previous close from Yahoo (faster than Alpaca for this)
        info = self.yahoo.get_stock_info(ticker)
        previous_close = info.get("previous_close", 0)

        if previous_close > 0:
            change_pct = ((current_price - previous_close) / previous_close) * 100
        else:
            change_pct = 0

        return {
            "ticker": ticker,
            "price": current_price,
            "previous_close": previous_close,
            "change_pct": change_pct,
            "meets_price_trigger": change_pct >= self.config.indicators.min_price_change_pct,
            "timestamp": datetime.now().isoformat(),
        }


class SignalFilter:
    """Filters and prioritizes signals based on various criteria."""

    def __init__(self, config: Config):
        self.config = config

    def filter_signals(
        self,
        signals: list,
        max_signals: int = 5,
        min_strength: str = "weak",
    ) -> list:
        """
        Filter and prioritize signals.

        Args:
            signals: List of EntrySignal objects
            max_signals: Maximum number of signals to return
            min_strength: Minimum signal strength to include

        Returns:
            Filtered and sorted list of signals
        """
        if not signals:
            return []

        strength_order = {"strong": 0, "moderate": 1, "weak": 2}
        min_strength_val = strength_order.get(min_strength, 2)

        # Filter by strength
        filtered = [
            s for s in signals
            if s.should_trade and strength_order.get(s.signal_strength, 3) <= min_strength_val
        ]

        # Sort by strength, then by volume ratio, then by price change
        filtered.sort(
            key=lambda s: (
                strength_order.get(s.signal_strength, 3),
                -s.volume_ratio,
                -s.price_change_pct,
            )
        )

        return filtered[:max_signals]

    def deduplicate_by_sector(self, signals: list, max_per_sector: int = 2) -> list:
        """
        Limit signals per sector to avoid concentration risk.

        Args:
            signals: List of EntrySignal objects
            max_per_sector: Maximum signals per sector

        Returns:
            Deduplicated list of signals
        """
        sector_counts = {}
        result = []

        for signal in signals:
            sector = signal.sector or "Unknown"
            count = sector_counts.get(sector, 0)

            if count < max_per_sector:
                result.append(signal)
                sector_counts[sector] = count + 1

        return result
