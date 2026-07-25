"""
Main bot orchestration for Short Squeeze Trading Bot.

Handles:
- Bot lifecycle management
- Scheduled scanning and trading
- Position monitoring
"""

from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from .broker import create_data_client, create_trader
from .config import Config
from .log_manager import rotate_daily_log
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

        # Position tracking, updated on every get_positions() fetch by
        # _update_position_tracking:
        # - _held_tickers/_recent_exits: diff-based exit detection (catches
        #   server-side bracket fills the bot never logs) feeding the re-entry
        #   cooldown in _process_signal.
        # - _position_highs: per-ticker high-water mark so the trailing stop in
        #   should_exit_position has a real "highest_price" to trail from.
        # - _position_first_seen: when a ticker first appeared, feeding the
        #   max-hold-time exit.
        # In-memory only: highs and hold clocks reset on bot restart (re-seeded
        # from the current price / restart time), which is acceptable drift.
        self._held_tickers: set[str] = set()
        self._recent_exits: dict[str, datetime] = {}
        self._position_highs: dict[str, float] = {}
        self._position_first_seen: dict[str, datetime] = {}
        self._last_portfolio_log: Optional[datetime] = None
        # Stale-quote detection: per ticker, the last-seen unrealized P&L% and
        # how many consecutive monitor ticks (≈minutes) it has been unchanged.
        # A halted/stale feed shows a byte-identical plpc for many minutes;
        # _stale_warned dedupes the warning to once per stale streak.
        self._position_plpc_streak: dict[str, tuple[float, int]] = {}
        self._stale_warned: set[str] = set()
        # Per-ticker count of exits within the current session, so a chop magnet
        # can't be re-entered indefinitely. Reset at the daily watchlist refresh.
        self._daily_stopouts: dict[str, int] = {}

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

        # Daily log rotation - 16:15 ET (~15 min after the 16:00 close, DST-correct).
        # Renames logs/bot.log to logs/bot_<session-date>.log and starts a fresh file.
        self.scheduler.add_job(
            rotate_daily_log,
            CronTrigger(hour=16, minute=15, timezone=EASTERN_TZ),
            id="daily_log_rotation",
            name="Daily Log Rotation",
        )

        # End-of-day order cleanup - 15:58 ET, just before the close. Cancels
        # unfilled BUY limits so GTC bracket entries can't fill on a next-day
        # gap-down at a stale price.
        self.scheduler.add_job(
            self._cancel_stale_buy_orders,
            CronTrigger(hour=15, minute=58, timezone=EASTERN_TZ),
            id="eod_order_cleanup",
            name="EOD Order Cleanup",
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
            if positions is None:
                # Fetch failed (post-retry). Skip the scan rather than treat an
                # empty result as "0 positions held" — that would bypass the
                # max-positions guard below and risk over-buying.
                logger.warning("Position fetch failed; skipping scan tick")
                return
            self._update_position_tracking(positions)
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

    def _update_position_tracking(self, positions: list) -> None:
        """Refresh per-ticker tracking state from a position fetch.

        - Exits (ticker disappeared): stamp the re-entry cooldown. Catches exits
          via any path — the software stop in _monitor_positions or a server-side
          bracket stop/take-profit fill that the bot never logged directly.
        - Entries (ticker appeared): start the max-hold clock.
        - Held: update the high-water mark that the trailing stop trails from.
        """
        now_et = now_eastern()
        current = {p["symbol"].upper() for p in positions}

        for ticker in self._held_tickers - current:
            self._recent_exits[ticker] = now_et
            self._daily_stopouts[ticker] = self._daily_stopouts.get(ticker, 0) + 1
            self._position_highs.pop(ticker, None)
            self._position_first_seen.pop(ticker, None)
            self._position_plpc_streak.pop(ticker, None)
            self._stale_warned.discard(ticker)
            logger.debug(
                f"{ticker}: position closed; re-entry cooldown started "
                f"({self.config.risk.reentry_cooldown_minutes}min)"
            )

        for p in positions:
            ticker = p["symbol"].upper()
            price = p.get("current_price", 0) or 0
            if ticker not in self._held_tickers:
                self._position_first_seen.setdefault(ticker, now_et)
            if price > 0:
                self._position_highs[ticker] = max(
                    self._position_highs.get(ticker, 0), price
                )

        self._held_tickers = current

        # Prune stale cooldown entries so the dict can't grow unbounded.
        cooldown = self.config.risk.reentry_cooldown_minutes
        self._recent_exits = {
            t: ts
            for t, ts in self._recent_exits.items()
            if (now_et - ts).total_seconds() / 60 < cooldown
        }

    def _check_stale_quote(self, ticker: str, plpc: float) -> None:
        """Warn once when a position's unrealized P&L% is frozen too long.

        Called on each 1-minute monitor tick, so the consecutive-unchanged
        count is ~minutes. A live quote fluctuates; a byte-identical plpc for
        stale_quote_alert_minutes straight means a halted or stale feed
        (e.g. a position stuck at a constant +1.0% for days). Alert-only —
        force-selling on suspected-bad data is riskier than surfacing it; the
        max-hold-time exit remains the backstop.
        """
        key = ticker.upper()
        threshold = self.config.risk.stale_quote_alert_minutes
        last = self._position_plpc_streak.get(key)

        if last is not None and last[0] == plpc:
            count = last[1] + 1
        else:
            count = 1
            self._stale_warned.discard(key)  # value moved; re-arm the warning
        self._position_plpc_streak[key] = (plpc, count)

        if count >= threshold and key not in self._stale_warned:
            self._stale_warned.add(key)
            logger.warning(
                f"{ticker}: quote appears stale — unrealized P&L frozen at "
                f"{plpc * 100:+.1f}% for {count} min; check for a halt/data issue"
            )

    def _log_portfolio_snapshot(self, positions: list) -> None:
        """Log held positions (P&L, age) at most once per hour.

        Without this, a week where every scan is skipped at max positions leaves
        no trace of WHICH tickers are occupying the slots.
        """
        now_et = now_eastern()
        if (
            self._last_portfolio_log is not None
            and (now_et - self._last_portfolio_log).total_seconds() < 3600
        ):
            return
        self._last_portfolio_log = now_et

        parts = []
        for p in positions:
            ticker = p["symbol"].upper()
            plpc = p.get("unrealized_plpc", 0) * 100
            first_seen = self._position_first_seen.get(ticker)
            age = f"{(now_et - first_seen).days}d" if first_seen else "?d"
            parts.append(f"{ticker} {plpc:+.1f}% ({age})")

        logger.info(
            f"Portfolio {len(positions)}/{self.config.risk.max_positions}: "
            + " | ".join(parts)
        )

    def _cancel_stale_buy_orders(self) -> None:
        """Cancel unfilled BUY orders near the close.

        Bracket orders are GTC so their SL/TP legs survive overnight, but that
        also lets an unfilled entry limit persist — a stale BUY at yesterday's
        price could fill on a gap-down. Cancelling the parent of an unfilled
        bracket cancels its inactive children; SELL legs of filled positions are
        untouched.
        """
        try:
            open_orders = self.broker_trader.get_open_orders()
        except Exception as e:
            logger.warning(f"EOD order cleanup: could not fetch open orders ({e})")
            return

        for order in open_orders:
            if order.get("side", "").lower() != "buy":
                continue
            order_id = order.get("id", "")
            if self.broker_trader.cancel_order(order_id):
                logger.info(
                    f"EOD order cleanup: cancelled unfilled BUY "
                    f"{order.get('symbol', '?')} (order {order_id})"
                )

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

        # Re-entry cooldown: don't immediately re-buy a ticker we were just
        # stopped out of (e.g. chasing a faded extreme-mover back down).
        last_exit = self._recent_exits.get(ticker.upper())
        if last_exit is not None:
            elapsed_min = (now_eastern() - last_exit).total_seconds() / 60
            cooldown = self.config.risk.reentry_cooldown_minutes
            if elapsed_min < cooldown:
                logger.info(
                    f"Skipping {ticker}: in re-entry cooldown "
                    f"({elapsed_min:.0f}/{cooldown}min since exit)"
                )
                return

        # Same-day chop guard: once a ticker has stopped out this many times
        # today, stop re-entering it — the cooldown alone only delays another
        # bite at the same chop.
        stopouts = self._daily_stopouts.get(ticker.upper(), 0)
        max_stopouts = self.config.risk.max_daily_stopouts_per_ticker
        if stopouts >= max_stopouts:
            logger.info(
                f"Skipping {ticker}: {stopouts} stop-out(s) today "
                f"(max {max_stopouts}); no more re-entries this session"
            )
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
            if positions is None:
                # Fetch failed (post-retry). Skip this tick without touching
                # tracking state — treating it as an empty book would falsely
                # register every held position as closed, wiping high-water
                # marks, resetting max-hold clocks, and mis-stamping cooldowns.
                logger.warning("Position fetch failed; skipping monitor tick")
                return
            self._update_position_tracking(positions)

            if not positions:
                return

            self._log_portfolio_snapshot(positions)

            for position in positions:
                ticker = position["symbol"]
                current_price = position["current_price"]
                entry_price = position["avg_entry_price"]

                # Flag a halted/stale quote (P&L% frozen for many minutes).
                self._check_stale_quote(ticker, position.get("unrealized_plpc", 0))

                # Supply the tracked high-water mark so the trailing stop has a
                # real high to trail from (Alpaca's position payload has no such
                # field; without this the trailing stop can never fire).
                position["highest_price"] = self._position_highs.get(
                    ticker.upper(), current_price
                )

                # Check exit conditions
                should_exit, reason = self.risk_manager.should_exit_position(
                    position=position,
                    current_price=current_price,
                    entry_price=entry_price,
                    stop_loss_pct=self.config.risk.default_stop_loss_pct,
                    trailing_stop_pct=self.config.risk.trailing_stop_pct,
                )

                # Max-hold-time exit: a squeeze that hasn't resolved in
                # max_hold_days is dead money blocking one of the position
                # slots (a full week of "Max positions reached" lockout).
                if not should_exit:
                    first_seen = self._position_first_seen.get(ticker.upper())
                    max_days = self.config.risk.max_hold_days
                    if first_seen is not None and (now_eastern() - first_seen).days >= max_days:
                        should_exit = True
                        reason = f"Max hold time ({max_days}d) reached"

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
        # New session: clear the per-ticker daily stop-out counts so a name
        # that chopped out yesterday is eligible again today.
        self._daily_stopouts.clear()
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

            # Get positions ([] also covers a failed fetch here — reporting only)
            positions = self.broker_data.get_positions() or []
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
        positions = self.broker_data.get_positions() or []
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
        positions = self.broker_data.get_positions() or []

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
