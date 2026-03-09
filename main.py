#!/usr/bin/env python3
"""
Short Squeeze Trading Bot - Main Entry Point

A fully automated trading bot that scans for heavily shorted stocks,
detects squeeze momentum in progress, and executes trades via Alpaca's API.

Usage:
    python main.py              # Run the bot
    python main.py --scan       # Run single scan (no trading)
    python main.py --status     # Show bot status
    python main.py --refresh    # Refresh watchlist
    python main.py --check TSLA # Quick check a ticker
"""

import argparse
import sys
import json
from datetime import datetime

from loguru import logger

from shortsqueeze import Config, ShortSqueezeBot
from shortsqueeze.timezone import LOCAL_TZ


def arizona_time(record):
    """Format time in Arizona timezone (MST, no DST)."""
    return record["time"].astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def setup_logging(level: str = "INFO") -> None:
    """Configure logging with Arizona timezone."""
    logger.remove()
    logger.add(
        sys.stderr,
        format=lambda r: f"<green>{arizona_time(r)}</green> | <level>{r['level'].name: <8}</level> | <level>{r['message']}</level>\n",
        level=level,
    )
    logger.add(
        "logs/bot.log",
        rotation="10 MB",
        retention="7 days",
        format=lambda r: f"{arizona_time(r)} | {r['level'].name: <8} | {r['name']}:{r['function']} - {r['message']}\n",
        level="DEBUG",
    )


def run_bot(config: Config) -> None:
    """Run the trading bot."""
    bot = ShortSqueezeBot(config)

    try:
        bot.start()

        logger.info("Bot is running. Press Ctrl+C to stop.")

        # Keep running
        import time
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        bot.stop()


def run_scan(config: Config) -> None:
    """Run a single scan without trading."""
    bot = ShortSqueezeBot(config)
    result = bot.run_once()

    print("\n" + "=" * 60)
    print("SCAN RESULTS")
    print("=" * 60)
    print(f"Timestamp: {result['timestamp']}")
    print(f"Market Open: {result['market_open']}")

    if 'account' in result:
        print(f"\nAccount:")
        print(f"  Equity: ${result['account']['equity']:,.2f}")
        print(f"  Buying Power: ${result['account']['buying_power']:,.2f}")

    print(f"\nPositions: {result.get('positions', 0)}")
    print(f"Stocks Scanned: {result.get('stocks_scanned', 0)}")

    if result.get('signals'):
        print(f"\nSignals Found: {len(result['signals'])}")
        for sig in result['signals']:
            print(f"  - {sig['ticker']}: {sig['strength']} ({sig['reason']})")
    else:
        print("\nNo signals found")

    if result.get('errors'):
        print(f"\nErrors: {len(result['errors'])}")
        for err in result['errors']:
            print(f"  - {err}")


def show_status(config: Config) -> None:
    """Show bot status."""
    bot = ShortSqueezeBot(config)
    status = bot.get_status()

    print("\n" + "=" * 60)
    print("BOT STATUS")
    print("=" * 60)
    print(f"Market Open: {status['market_open']}")

    if status.get('account'):
        print(f"\nAccount:")
        print(f"  Equity: ${status['account']['equity']:,.2f}")
        print(f"  Cash: ${status['account']['cash']:,.2f}")
        print(f"  Buying Power: ${status['account']['buying_power']:,.2f}")

    print(f"\nPositions: {status['position_count']} / {status['max_positions']}")
    if status.get('positions'):
        for pos in status['positions']:
            pnl = pos['unrealized_pl']
            pnl_pct = pos['unrealized_plpc'] * 100
            print(f"  - {pos['symbol']}: {pos['qty']} shares @ ${pos['avg_entry_price']:.2f} "
                  f"(P/L: ${pnl:+,.2f} / {pnl_pct:+.2f}%)")

    print(f"\nWatchlist:")
    wl = status.get('watchlist', {})
    print(f"  Stocks: {wl.get('count', 0)}")
    print(f"  Last Refresh: {wl.get('last_refresh', 'Never')}")
    print(f"  Needs Refresh: {wl.get('needs_refresh', True)}")

    print(f"\nConfiguration:")
    print(f"  Scan Interval: {status['scan_interval_minutes']} minutes")
    print(f"  Risk per Trade: {config.risk.risk_per_trade * 100:.1f}%")


def refresh_watchlist(config: Config) -> None:
    """Refresh the watchlist."""
    from shortsqueeze.universe import WatchlistManager

    logger.info("Refreshing watchlist...")

    manager = WatchlistManager(config)
    df = manager.refresh_watchlist(force=True)

    print("\n" + "=" * 60)
    print("WATCHLIST REFRESH")
    print("=" * 60)
    print(f"Found {len(df)} squeeze candidates")

    if not df.empty:
        print("\nTop 10 by Short Interest:")
        top = df.nlargest(10, 'short_float_pct')[['ticker', 'company', 'short_float_pct', 'price']]
        for _, row in top.iterrows():
            print(f"  {row['ticker']:6} | {row['short_float_pct']:5.1f}% SI | ${row['price']:8.2f} | {row['company'][:30]}")


def check_ticker(config: Config, ticker: str) -> None:
    """Quick check a specific ticker."""
    bot = ShortSqueezeBot(config)
    result = bot.manual_entry(ticker)

    print("\n" + "=" * 60)
    print(f"TICKER CHECK: {ticker}")
    print("=" * 60)

    if result.get('signal'):
        print(f"Signal: YES - {result['signal_strength']}")
        print(f"Reason: {result['reason']}")

        if result.get('position_size'):
            ps = result['position_size']
            print(f"\nProposed Position:")
            print(f"  Shares: {ps['shares']}")
            print(f"  Entry: ${ps['entry_price']:.2f}")
            print(f"  Stop Loss: ${ps['stop_loss']:.2f}")
            print(f"  Risk: ${ps['risk_amount']:.2f}")
    else:
        print(f"Signal: NO")
        print(f"Reason: {result.get('message', 'No entry conditions met')}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Short Squeeze Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                  Run the bot continuously
  python main.py --scan           Run a single market scan
  python main.py --status         Show current status
  python main.py --refresh        Refresh the watchlist
  python main.py --check TSLA     Check if TSLA has an entry signal

Environment Variables:
  ALPACA_API_KEY          Your Alpaca API key
  ALPACA_SECRET_KEY       Your Alpaca secret key
  ALPACA_TRADING_MODE     'paper' or 'live' (default: paper)
        """
    )

    parser.add_argument(
        "--scan",
        action="store_true",
        help="Run single scan cycle (no continuous trading)"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show bot and account status"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh the watchlist"
    )
    parser.add_argument(
        "--check",
        metavar="TICKER",
        help="Quick check a specific ticker for entry signal"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        help="Path to .env configuration file"
    )

    args = parser.parse_args()

    # Setup logging
    log_level = "DEBUG" if args.debug else "INFO"
    setup_logging(log_level)

    # Load configuration
    config = Config.load(args.config)

    # Execute requested action
    if args.scan:
        run_scan(config)
    elif args.status:
        show_status(config)
    elif args.refresh:
        refresh_watchlist(config)
    elif args.check:
        check_ticker(config, args.check.upper())
    else:
        run_bot(config)


if __name__ == "__main__":
    main()
