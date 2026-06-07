"""
Main bot orchestration for Short Squeeze Trading Bot.

Handles:
- Bot lifecycle management
- Scheduled scanning and trading
- Position monitoring
"""

import time
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from .broker import create_data_client, create_trader
from .config import Config
from .timezone import EASTERN_TZ, is_market_hours, now_eastern
from .universe import WatchlistManager
from .signals import SignalDetector, SignalFilter
from .position import PositionSizer, RiskManager
from .trader import TradeManager


class ShortSqueezeBot:
    """Main trading bot orchestrator."""

    def __init__(self, config: Optional[Config] = None):
        """Initialize the bot with configuration."""
        self.config = config or Config.load()

        # Initialize broker components
        self.broker_data = create_data_client(self.config)
        self.broker_trader = create_trader(self.config)

        # Initialize strategy components
        self.watchlist = WatchlistManager(self.config)
        self.signal_detector = SignalDetector(
            self.config,
            broker_data=self.broker_data,
            watchlist_manager=self.watchlist,
        )
        self.signal_filter = SignalFilter(self.config)
        self.position_sizer = PositionSizer(self.config)
        self.risk_manager = RiskManager(self.config)
        self.trade_manager = TradeManager(self.config, executor=self.broker_trader)

        # Scheduler
        self.scheduler = BackgroundScheduler()
        self._running = False

    def start(self) -> None:
        """Start the trading bot."""
        logger.info("Starting Short Squeeze Trading Bot...")

        # Validate configuration
        if not self._validate_config():
            logger.error("Configuration validation failed. Bot not started.")
            return

        # Check account status
        account = self.broker_data.get_account()
        if not account:
            logger.error("Failed to connect to Alpaca. Check your API keys.")
            return

        if account.get("trading_blocked") or account.get("account_blocked"):
            logger.error("Trading is blocked on this account.")
            return

        logger.info(
            f"Connected to Alpaca - Equity: ${account['equity']:,.2f}, "
            f"Buying Power: ${account['buying_power']:,.2f}"
        )

        # Load or refresh watchlist
        self._refresh_watchlist_if_needed()

        # Schedule jobs
        self._schedule_jobs()

        # Start scheduler
        self.scheduler.start()
        self._running = True

        logger.info(
            f"Bot started. Scanning every {self.config.trading.scan_interval_minutes} minutes "
            f"during market hours."
        )

    def stop(self) -> None:
        """Stop the trading bot."""
        logger.info("Stopping Short Squeeze Trading Bot...")

        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

        self._running = False
        logger.info("Bot stopped.")

    def _validate_config(self) -> bool:
        """Validate essential configuration."""
        if self.config.broker_type == "alpaca":
            if not self.config.alpaca.api_key or not self.config.alpaca.secret_key:
                logger.error("Alpaca API credentials not configured")
                return False
        return True

    def _schedule_jobs(self) -> None:
        """Schedule all bot jobs."""
        # Intraday scan - every N minutes during market hours
        self.scheduler.add_job(
            self._run_scan,
            IntervalTrigger(minutes=self.config.trading.scan_interval_minutes),
            id="intraday_scan",
            name="Intraday Signal Scan",
            max_instances=1,
        )

        # Daily watchlist refresh - Every day at 6:00 AM ET (before market open)
        self.scheduler.add_job(
            self._refresh_watchlist,
            CronTrigger(hour=6, minute=0, timezone=EASTERN_TZ),
            id="daily_refresh",
            name="Daily Watchlist Refresh",
        )

        # Position monitoring - every minute during market hours
        self.scheduler.add_job(
            self._monitor_positions,
            IntervalTrigger(minutes=1),
            id="position_monitor",
            name="Position Monitor",
            max_instances=1,
        )

        logger.info("Jobs scheduled")

    def _is_market_hours(self) -> bool:
        """Check if we're in market hours (US Eastern Time)."""
        return is_market_hours()

    def _run_scan(self) -> None:
        """Run the intraday scan for signals."""
        if not self._is_market_hours():
            logger.debug("Market closed, skipping scan")
            return

        logger.info("Running intraday scan...")

        try:
            # Get current account state
            account = self.broker_data.get_account()
            if not account:
                logger.error("Failed to get account info")
                return

            positions = self.broker_data.get_positions()
            current_position_count = len(positions)

            # Check if we can take more positions
            if current_position_count >= self.config.risk.max_positions:
                logger.info(
                    f"Max positions ({self.config.risk.max_positions}) reached. "
                    f"Skipping scan."
                )
                return

            # Run signal detection
            scan_result = self.signal_detector.scan_watchlist()

            if not scan_result.has_signals:
                logger.info("No signals detected")
                return

            # Filter signals
            filtered_signals = self.signal_filter.filter_signals(
                scan_result.signals,
                max_signals=self.config.risk.max_positions - current_position_count,
            )

            # Deduplicate by sector
            filtered_signals = self.signal_filter.deduplicate_by_sector(filtered_signals)

            logger.info(f"Processing {len(filtered_signals)} filtered signals")

            # Process each signal
            for signal in filtered_signals:
                self._process_signal(signal, account, positions)

        except Exception as e:
            logger.error(f"Error during scan: {e}", exc_info=True)

    def _process_signal(
        self,
        signal,
        account: dict,
        positions: list,
    ) -> None:
        """Process a single entry signal."""
        ticker = signal.ticker

        # Check concentration risk
        ok, warning = self.risk_manager.check_concentration_risk(
            positions, ticker, signal.sector
        )
        if not ok:
            logger.info(f"Skipping {ticker}: {warning}")
            return

        # Block re-entry when a BUY for this ticker is still pending. A limit
        # order from a prior scan may not have filled yet, so positions[] is
        # still empty even though we're effectively long.
        try:
            open_orders = self.broker_trader.get_open_orders(ticker)
        except Exception as e:
            logger.warning(f"{ticker}: could not fetch open orders ({e}); proceeding")
            open_orders = []
        pending_buys = [o for o in open_orders if o.get("side", "").lower() == "buy"]
        if pending_buys:
            order_ids = ", ".join(o.get("id", "?") for o in pending_buys)
            logger.info(f"Skipping {ticker}: pending BUY order(s) already open ({order_ids})")
            return

        # Calculate position size
        position_size = self.position_sizer.calculate_position_size(
            signal=signal,
            account_equity=account["equity"],
            current_positions=len(positions),
        )

        if not position_size or not position_size.is_valid:
            logger.info(f"Position size calculation failed for {ticker}")
            return

        # Validate position
        is_valid, reason = self.position_sizer.validate_position(
            position_size,
            account["equity"],
            account["buying_power"],
        )

        if not is_valid:
            logger.info(f"Position validation failed for {ticker}: {reason}")
            return

        # Execute trade
        logger.info(
            f"Executing entry for {ticker}: {position_size.shares} shares @ "
            f"${position_size.entry_price:.2f} (SL: ${position_size.stop_loss_price:.2f})"
        )

        trade = self.trade_manager.execute_entry(signal, position_size)

        if trade:
            logger.info(f"Trade submitted for {ticker}: Order ID {trade.order_id}")
        else:
            logger.warning(f"Trade execution failed for {ticker}")

    def _monitor_positions(self) -> None:
        """Monitor open positions for exit conditions."""
        if not self._is_market_hours():
            return

        try:
            positions = self.broker_data.get_positions()

            if not positions:
                return

            for position in positions:
                ticker = position["symbol"]
                current_price = position["current_price"]
                entry_price = position["avg_entry_price"]

                # Check exit conditions
                should_exit, reason = self.risk_manager.should_exit_position(
                    position=position,
                    current_price=current_price,
                    entry_price=entry_price,
                    stop_loss_pct=self.config.risk.default_stop_loss_pct,
                    trailing_stop_pct=self.config.risk.trailing_stop_pct,
                )

                if should_exit:
                    # Skip if a SELL for this ticker is already pending. Otherwise the
                    # 1-minute monitor tick will resubmit the same close every minute
                    # while the prior limit order is still working, which Alpaca
                    # rejects as "insufficient qty available" once the prior order
                    # holds the full position.
                    try:
                        open_orders = self.broker_trader.get_open_orders(ticker)
                    except Exception as e:
                        logger.warning(f"{ticker}: could not fetch open orders ({e}); proceeding")
                        open_orders = []
                    pending_sells = [o for o in open_orders if o.get("side", "").lower() == "sell"]
                    if pending_sells:
                        ids = ", ".join(o.get("id", "?") for o in pending_sells)
                        logger.debug(f"{ticker}: SELL already pending ({ids}); skipping monitor exit")
                        continue

                    logger.info(f"Exit signal for {ticker}: {reason}")
                    self.trade_manager.execute_exit(ticker, reason=reason)

        except Exception as e:
            logger.error(f"Error monitoring positions: {e}")

    def _refresh_watchlist_if_needed(self) -> None:
        """Refresh watchlist if needed."""
        if self.watchlist.needs_refresh():
            self._refresh_watchlist()
        else:
            watchlist = self.watchlist.get_watchlist()
            logger.info(f"Using existing watchlist with {len(watchlist)} stocks")

    def _refresh_watchlist(self) -> None:
        """Force refresh the watchlist."""
        logger.info("Refreshing watchlist...")
        try:
            df = self.watchlist.refresh_watchlist(force=True)
            logger.info(f"Watchlist refreshed: {len(df)} candidates")
        except Exception as e:
            logger.error(f"Error refreshing watchlist: {e}")

    def run_once(self) -> dict:
        """
        Run a single scan cycle (useful for testing or manual runs).

        Returns:
            dict with scan results
        """
        logger.info("Running single scan cycle...")

        result = {
            "timestamp": now_eastern().isoformat(),
            "market_open": self._is_market_hours(),
            "signals": [],
            "trades": [],
            "errors": [],
        }

        if not self._is_market_hours():
            result["message"] = "Market is closed"
            return result

        try:
            # Get account
            account = self.broker_data.get_account()
            if not account:
                result["errors"].append("Failed to get account info")
                return result

            result["account"] = {
                "equity": account["equity"],
                "buying_power": account["buying_power"],
            }

            # Get positions
            positions = self.broker_data.get_positions()
            result["positions"] = len(positions)

            # Run scan
            scan_result = self.signal_detector.scan_watchlist()
            result["stocks_scanned"] = scan_result.stocks_scanned

            for signal in scan_result.signals:
                result["signals"].append({
                    "ticker": signal.ticker,
                    "strength": signal.signal_strength,
                    "price_change": signal.price_change_pct,
                    "volume_ratio": signal.volume_ratio,
                    "reason": signal.reason,
                })

            if scan_result.errors:
                result["errors"].extend([e["error"] for e in scan_result.errors])

        except Exception as e:
            result["errors"].append(str(e))

        return result

    def get_status(self) -> dict:
        """Get current bot status."""
        account = self.broker_data.get_account()
        positions = self.broker_data.get_positions()
        watchlist_summary = self.watchlist.get_summary()

        return {
            "running": self._running,
            "market_open": self._is_market_hours(),
            "account": account,
            "positions": positions,
            "position_count": len(positions),
            "max_positions": self.config.risk.max_positions,
            "watchlist": watchlist_summary,
            "scan_interval_minutes": self.config.trading.scan_interval_minutes,
        }

    def manual_entry(self, ticker: str) -> Optional[dict]:
        """
        Manually trigger entry analysis for a specific ticker.

        Returns:
            dict with analysis results
        """
        logger.info(f"Manual entry analysis for {ticker}")

        # Check if in watchlist
        if not self.watchlist.is_in_watchlist(ticker):
            logger.warning(f"{ticker} not in watchlist")

        # Scan the stock
        signal = self.signal_detector.scan_stock(ticker)

        if not signal:
            return {"ticker": ticker, "signal": False, "message": "No entry signal"}

        # Get account
        account = self.broker_data.get_account()
        positions = self.broker_data.get_positions()

        # Calculate position size
        position_size = self.position_sizer.calculate_position_size(
            signal=signal,
            account_equity=account["equity"],
            current_positions=len(positions),
        )

        return {
            "ticker": ticker,
            "signal": True,
            "signal_strength": signal.signal_strength,
            "reason": signal.reason,
            "position_size": {
                "shares": position_size.shares if position_size else 0,
                "entry_price": position_size.entry_price if position_size else 0,
                "stop_loss": position_size.stop_loss_price if position_size else 0,
                "risk_amount": position_size.risk_amount if position_size else 0,
            } if position_size else None,
        }


def run_bot():
    """Run the bot in the foreground."""
    import sys
    from loguru import logger

    # Configure logging
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        level="INFO",
    )
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        rotation="16:00",  # Rotate at market close (4 PM ET)
        retention="30 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} - {message}",
        level="DEBUG",
    )

    config = Config.load()
    bot = ShortSqueezeBot(config)

    try:
        bot.start()

        # Keep running
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        bot.stop()


if __name__ == "__main__":
    run_bot()
