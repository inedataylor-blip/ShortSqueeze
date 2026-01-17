"""
Universe generation and watchlist management for Short Squeeze Trading Bot.

Handles:
- Daily watchlist refresh from multiple data sources
- Dual watchlist management (quality $5+ and speculative <$5)
- Watchlist persistence to JSON
- Candidate filtering and validation
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
from loguru import logger

from .config import Config
from .data import DataManager
from .timezone import now_eastern


class WatchlistManager:
    """
    Manages dual squeeze candidate watchlists.

    - Quality watchlist: Stocks >= $5 (established companies, lower risk)
    - Speculative watchlist: Stocks $1-$5 (higher risk, higher squeeze potential)
    """

    def __init__(self, config: Config):
        self.config = config
        self.data_manager = DataManager(config)
        self._quality_watchlist: Optional[pd.DataFrame] = None
        self._speculative_watchlist: Optional[pd.DataFrame] = None
        self._last_refresh: Optional[datetime] = None

    @property
    def quality_path(self) -> Path:
        """Path to quality watchlist JSON file."""
        return self.config.watchlist_quality_path

    @property
    def speculative_path(self) -> Path:
        """Path to speculative watchlist JSON file."""
        return self.config.watchlist_speculative_path

    def _load_watchlist_file(self, path: Path) -> Tuple[pd.DataFrame, Optional[datetime]]:
        """
        Load a watchlist from a JSON file.

        Returns:
            Tuple of (DataFrame, last_refresh datetime)
        """
        if not path.exists():
            return pd.DataFrame(), None

        try:
            with open(path, "r") as f:
                data = json.load(f)

            last_refresh = datetime.fromisoformat(data.get("last_refresh", ""))
            stocks = data.get("stocks", [])
            df = pd.DataFrame(stocks)

            return df, last_refresh

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Error loading watchlist from {path}: {e}")
            return pd.DataFrame(), None

    def _save_watchlist_file(self, df: pd.DataFrame, path: Path) -> None:
        """Save a watchlist to a JSON file."""
        self.config.data_dir.mkdir(parents=True, exist_ok=True)

        data = {
            "last_refresh": now_eastern().isoformat(),
            "stocks": df.to_dict(orient="records"),
        }

        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def load_watchlists(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Load both watchlists from files.

        Returns:
            Tuple of (quality_df, speculative_df)
        """
        self._quality_watchlist, refresh_q = self._load_watchlist_file(self.quality_path)
        self._speculative_watchlist, refresh_s = self._load_watchlist_file(self.speculative_path)

        # Use the most recent refresh time
        if refresh_q and refresh_s:
            self._last_refresh = max(refresh_q, refresh_s)
        else:
            self._last_refresh = refresh_q or refresh_s

        q_count = len(self._quality_watchlist) if self._quality_watchlist is not None else 0
        s_count = len(self._speculative_watchlist) if self._speculative_watchlist is not None else 0

        logger.info(
            f"Loaded watchlists: {q_count} quality ($5+), {s_count} speculative (<$5), "
            f"last refresh: {self._last_refresh}"
        )

        return self._quality_watchlist, self._speculative_watchlist

    def save_watchlists(self, quality_df: pd.DataFrame, speculative_df: pd.DataFrame) -> None:
        """Save both watchlists to files."""
        self._save_watchlist_file(quality_df, self.quality_path)
        self._save_watchlist_file(speculative_df, self.speculative_path)

        self._quality_watchlist = quality_df
        self._speculative_watchlist = speculative_df
        self._last_refresh = now_eastern()

        logger.info(
            f"Saved watchlists: {len(quality_df)} quality ($5+), "
            f"{len(speculative_df)} speculative (<$5)"
        )

    def needs_refresh(self, max_age_days: int = 1) -> bool:
        """
        Check if watchlists need refreshing.

        Args:
            max_age_days: Maximum age in days before refresh needed (default: 1 for daily refresh)

        Returns:
            True if refresh is needed
        """
        # Check if watchlists are loaded
        if self._quality_watchlist is None and self._speculative_watchlist is None:
            return True

        # Check if both are empty
        q_empty = self._quality_watchlist is None or self._quality_watchlist.empty
        s_empty = self._speculative_watchlist is None or self._speculative_watchlist.empty
        if q_empty and s_empty:
            return True

        if self._last_refresh is None:
            return True

        age = now_eastern() - self._last_refresh
        return age > timedelta(days=max_age_days)

    def refresh_watchlist(self, force: bool = False) -> pd.DataFrame:
        """
        Refresh both watchlists from data sources.

        Fetches all candidates and splits them into:
        - Quality: price >= $5
        - Speculative: price >= $1 and < $5

        Args:
            force: Force refresh even if not needed

        Returns:
            Combined DataFrame of all candidates (for backwards compatibility)
        """
        if not force and not self.needs_refresh():
            logger.info("Watchlists are current, skipping refresh")
            return self.get_watchlist()

        logger.info("Refreshing watchlists from data sources...")

        try:
            # Get ALL candidates from scrapers (no price filtering)
            df = self.data_manager.get_squeeze_candidates()

            if df.empty:
                logger.warning("No candidates found during refresh")
                return df

            # Apply filters EXCEPT price
            df = self._apply_filters_no_price(df)

            if df.empty:
                logger.warning("No candidates passed filters")
                return df

            # Split by price into two watchlists
            quality_min = self.config.screening.quality_min_price
            speculative_min = self.config.screening.speculative_min_price

            quality_df = df[df["price"] >= quality_min].copy()
            speculative_df = df[
                (df["price"] >= speculative_min) & (df["price"] < quality_min)
            ].copy()

            # Sort both by short interest (highest first)
            if not quality_df.empty:
                quality_df = quality_df.sort_values("short_float_pct", ascending=False)
            if not speculative_df.empty:
                speculative_df = speculative_df.sort_values("short_float_pct", ascending=False)

            # Save both watchlists
            self.save_watchlists(quality_df, speculative_df)

            logger.info(
                f"Watchlists refreshed: {len(quality_df)} quality ($5+), "
                f"{len(speculative_df)} speculative (${speculative_min}-${quality_min})"
            )

            # Return combined for backwards compatibility
            return pd.concat([quality_df, speculative_df], ignore_index=True)

        except Exception as e:
            logger.error(f"Error refreshing watchlists: {e}")
            return self.get_watchlist()

    def _apply_filters_no_price(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply screening filters to candidates (excluding price filter)."""
        initial_count = len(df)

        # Filter by short interest
        if "short_float_pct" in df.columns:
            df = df[df["short_float_pct"] >= self.config.screening.min_short_float_pct]

        # Filter by market cap (use lower threshold for speculative)
        # Allow smaller market caps for speculative stocks
        if "market_cap" in df.columns:
            min_cap = self.config.screening.min_market_cap
            # Allow 1/3 of normal market cap for stocks under $5
            df = df[
                (df["market_cap"] >= min_cap) |
                ((df["price"] < self.config.screening.quality_min_price) &
                 (df["market_cap"] >= min_cap / 3))
            ]

        # Filter by average volume (more lenient)
        if "avg_volume" in df.columns:
            min_vol = self.config.screening.min_avg_volume
            df = df[df["avg_volume"].fillna(float("inf")) >= min_vol / 2]

        # NO price filter here - we split by price later

        logger.debug(f"Filtered from {initial_count} to {len(df)} candidates (before price split)")
        return df

    def get_quality_watchlist(self) -> pd.DataFrame:
        """Get quality watchlist ($5+ stocks)."""
        if self._quality_watchlist is None:
            self.load_watchlists()
        return self._quality_watchlist if self._quality_watchlist is not None else pd.DataFrame()

    def get_speculative_watchlist(self) -> pd.DataFrame:
        """Get speculative watchlist (<$5 stocks)."""
        if self._speculative_watchlist is None:
            self.load_watchlists()
        return self._speculative_watchlist if self._speculative_watchlist is not None else pd.DataFrame()

    def get_watchlist(self) -> pd.DataFrame:
        """
        Get combined watchlist (both quality and speculative).

        Returns:
            DataFrame of all watchlist stocks
        """
        quality = self.get_quality_watchlist()
        speculative = self.get_speculative_watchlist()

        if quality.empty and speculative.empty:
            return pd.DataFrame()
        elif quality.empty:
            return speculative
        elif speculative.empty:
            return quality
        else:
            return pd.concat([quality, speculative], ignore_index=True)

    def get_tickers(self, watchlist_type: str = "all") -> list:
        """
        Get list of ticker symbols from watchlist(s).

        Args:
            watchlist_type: "quality", "speculative", or "all"

        Returns:
            List of ticker symbols
        """
        if watchlist_type == "quality":
            df = self.get_quality_watchlist()
        elif watchlist_type == "speculative":
            df = self.get_speculative_watchlist()
        else:
            df = self.get_watchlist()

        if df.empty or "ticker" not in df.columns:
            return []
        return df["ticker"].tolist()

    def is_in_watchlist(self, ticker: str) -> bool:
        """Check if a ticker is in either watchlist."""
        return ticker.upper() in [t.upper() for t in self.get_tickers("all")]

    def get_stock_info(self, ticker: str) -> Optional[dict]:
        """
        Get stored info for a specific ticker from either watchlist.

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

    def get_summary(self) -> dict:
        """Get summary statistics for both watchlists."""
        quality = self.get_quality_watchlist()
        speculative = self.get_speculative_watchlist()

        summary = {
            "quality_count": len(quality) if not quality.empty else 0,
            "speculative_count": len(speculative) if not speculative.empty else 0,
            "total_count": (len(quality) if not quality.empty else 0) +
                          (len(speculative) if not speculative.empty else 0),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "needs_refresh": self.needs_refresh(),
        }

        combined = self.get_watchlist()
        if not combined.empty and "short_float_pct" in combined.columns:
            summary["avg_short_float"] = combined["short_float_pct"].mean()
            summary["max_short_float"] = combined["short_float_pct"].max()

        if not combined.empty and "sector" in combined.columns:
            summary["sectors"] = combined["sector"].value_counts().to_dict()

        return summary

    # Legacy method aliases for backwards compatibility
    def load_watchlist(self) -> pd.DataFrame:
        """Load watchlist (legacy, loads both and returns combined)."""
        self.load_watchlists()
        return self.get_watchlist()

    def save_watchlist(self, df: pd.DataFrame) -> None:
        """Save watchlist (legacy, splits and saves to both files)."""
        quality_min = self.config.screening.quality_min_price
        speculative_min = self.config.screening.speculative_min_price

        if "price" in df.columns:
            quality_df = df[df["price"] >= quality_min].copy()
            speculative_df = df[
                (df["price"] >= speculative_min) & (df["price"] < quality_min)
            ].copy()
        else:
            # Can't split without price, put everything in quality
            quality_df = df
            speculative_df = pd.DataFrame()

        self.save_watchlists(quality_df, speculative_df)
