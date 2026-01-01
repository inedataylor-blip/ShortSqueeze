"""
Universe generation and watchlist management for Short Squeeze Trading Bot.

Handles:
- Weekly watchlist refresh from Finviz/Yahoo
- Watchlist persistence to JSON
- Candidate filtering and validation
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from .config import Config
from .data import DataManager
from .timezone import now_eastern


class WatchlistManager:
    """Manages the squeeze candidate watchlist."""

    def __init__(self, config: Config):
        self.config = config
        self.data_manager = DataManager(config)
        self._watchlist: Optional[pd.DataFrame] = None
        self._last_refresh: Optional[datetime] = None

    @property
    def watchlist_path(self) -> Path:
        """Path to watchlist JSON file."""
        return self.config.watchlist_path

    def load_watchlist(self) -> pd.DataFrame:
        """
        Load watchlist from file.

        Returns:
            DataFrame of watchlist stocks
        """
        if not self.watchlist_path.exists():
            logger.info("No existing watchlist found, will need to refresh")
            return pd.DataFrame()

        try:
            with open(self.watchlist_path, "r") as f:
                data = json.load(f)

            self._last_refresh = datetime.fromisoformat(data.get("last_refresh", ""))
            stocks = data.get("stocks", [])

            self._watchlist = pd.DataFrame(stocks)
            logger.info(
                f"Loaded watchlist with {len(self._watchlist)} stocks, "
                f"last refresh: {self._last_refresh}"
            )
            return self._watchlist

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Error loading watchlist: {e}")
            return pd.DataFrame()

    def save_watchlist(self, df: pd.DataFrame) -> None:
        """
        Save watchlist to file.

        Args:
            df: DataFrame of watchlist stocks
        """
        self.config.data_dir.mkdir(parents=True, exist_ok=True)

        data = {
            "last_refresh": now_eastern().isoformat(),
            "stocks": df.to_dict(orient="records"),
        }

        with open(self.watchlist_path, "w") as f:
            json.dump(data, f, indent=2, default=str)

        self._watchlist = df
        self._last_refresh = now_eastern()
        logger.info(f"Saved watchlist with {len(df)} stocks")

    def needs_refresh(self, max_age_days: int = 7) -> bool:
        """
        Check if watchlist needs refreshing.

        Args:
            max_age_days: Maximum age in days before refresh needed

        Returns:
            True if refresh is needed
        """
        if self._watchlist is None or self._watchlist.empty:
            return True

        if self._last_refresh is None:
            return True

        age = now_eastern() - self._last_refresh
        return age > timedelta(days=max_age_days)

    def refresh_watchlist(self, force: bool = False) -> pd.DataFrame:
        """
        Refresh the watchlist from data sources.

        Args:
            force: Force refresh even if not needed

        Returns:
            Updated watchlist DataFrame
        """
        if not force and not self.needs_refresh():
            logger.info("Watchlist is current, skipping refresh")
            return self._watchlist

        logger.info("Refreshing watchlist from data sources...")

        try:
            # Get candidates from Finviz + Yahoo
            df = self.data_manager.get_squeeze_candidates()

            if df.empty:
                logger.warning("No candidates found during refresh")
                return df

            # Apply additional filters
            df = self._apply_filters(df)

            # Sort by short interest (highest first)
            df = df.sort_values("short_float_pct", ascending=False)

            # Save to file
            self.save_watchlist(df)

            logger.info(f"Watchlist refreshed with {len(df)} candidates")
            return df

        except Exception as e:
            logger.error(f"Error refreshing watchlist: {e}")
            # Return existing watchlist if available
            return self._watchlist if self._watchlist is not None else pd.DataFrame()

    def _apply_filters(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply screening filters to candidates."""
        initial_count = len(df)

        # Filter by short interest
        if "short_float_pct" in df.columns:
            df = df[df["short_float_pct"] >= self.config.screening.min_short_float_pct]

        # Filter by market cap
        if "market_cap" in df.columns:
            df = df[df["market_cap"] >= self.config.screening.min_market_cap]

        # Filter by average volume
        if "avg_volume" in df.columns:
            df = df[
                df["avg_volume"].fillna(float("inf")) >= self.config.screening.min_avg_volume
            ]

        # Filter by price
        if "price" in df.columns:
            df = df[df["price"] >= self.config.screening.min_price]

        logger.debug(f"Filtered from {initial_count} to {len(df)} candidates")
        return df

    def get_watchlist(self) -> pd.DataFrame:
        """
        Get current watchlist, loading from file if needed.

        Returns:
            DataFrame of watchlist stocks
        """
        if self._watchlist is None:
            self._watchlist = self.load_watchlist()
        return self._watchlist

    def get_tickers(self) -> list:
        """Get list of ticker symbols from watchlist."""
        df = self.get_watchlist()
        if df.empty or "ticker" not in df.columns:
            return []
        return df["ticker"].tolist()

    def is_in_watchlist(self, ticker: str) -> bool:
        """Check if a ticker is in the watchlist."""
        return ticker.upper() in [t.upper() for t in self.get_tickers()]

    def get_stock_info(self, ticker: str) -> Optional[dict]:
        """
        Get stored info for a specific ticker.

        Args:
            ticker: Stock symbol

        Returns:
            dict with stock info or None if not found
        """
        df = self.get_watchlist()
        if df.empty or "ticker" not in df.columns:
            return None

        matches = df[df["ticker"].str.upper() == ticker.upper()]
        if matches.empty:
            return None

        return matches.iloc[0].to_dict()

    def add_to_watchlist(self, stock_info: dict) -> None:
        """
        Add a stock to the watchlist.

        Args:
            stock_info: dict with stock information (must include 'ticker')
        """
        if "ticker" not in stock_info:
            raise ValueError("stock_info must include 'ticker'")

        df = self.get_watchlist()

        # Remove if already exists
        ticker = stock_info["ticker"].upper()
        if not df.empty and "ticker" in df.columns:
            df = df[df["ticker"].str.upper() != ticker]

        # Add new entry
        new_df = pd.concat([df, pd.DataFrame([stock_info])], ignore_index=True)
        self.save_watchlist(new_df)

    def remove_from_watchlist(self, ticker: str) -> bool:
        """
        Remove a stock from the watchlist.

        Args:
            ticker: Stock symbol to remove

        Returns:
            True if removed, False if not found
        """
        df = self.get_watchlist()
        if df.empty or "ticker" not in df.columns:
            return False

        initial_len = len(df)
        df = df[df["ticker"].str.upper() != ticker.upper()]

        if len(df) < initial_len:
            self.save_watchlist(df)
            return True
        return False

    def get_summary(self) -> dict:
        """Get summary statistics for the watchlist."""
        df = self.get_watchlist()

        if df.empty:
            return {
                "count": 0,
                "last_refresh": None,
                "needs_refresh": True,
            }

        summary = {
            "count": len(df),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "needs_refresh": self.needs_refresh(),
        }

        if "short_float_pct" in df.columns:
            summary["avg_short_float"] = df["short_float_pct"].mean()
            summary["max_short_float"] = df["short_float_pct"].max()

        if "sector" in df.columns:
            summary["sectors"] = df["sector"].value_counts().to_dict()

        return summary


def should_refresh_today() -> bool:
    """
    Check if today is a refresh day (Sunday) in US Eastern timezone.

    The watchlist is designed to be refreshed weekly on Sundays
    when the market is closed.
    """
    return now_eastern().weekday() == 6  # Sunday = 6
