"""
Finnhub data client for market data fallback.

Provides real-time quotes and intraday volume data when primary
broker data is unavailable. Free tier: 60 calls/minute.

API docs: https://finnhub.io/docs/api
"""

import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests
from loguru import logger


class FinnhubDataClient:
    """
    Finnhub market data client.

    Used as a fallback when the primary broker (Alpaca) doesn't return
    intraday data. Particularly useful for volume confirmation.
    """

    BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._last_request_time = 0
        self._min_request_interval = 0.02  # 50 requests/sec max, we use 1/50

    def _rate_limit(self):
        """Simple rate limiting to stay under 60 calls/min."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def _request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make a rate-limited request to Finnhub API."""
        if not self.api_key:
            return None

        self._rate_limit()

        params = params or {}
        params["token"] = self.api_key

        try:
            response = requests.get(
                f"{self.BASE_URL}/{endpoint}",
                params=params,
                timeout=10,
            )

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                logger.warning("Finnhub rate limit hit, waiting...")
                time.sleep(1)
                return None
            else:
                logger.debug(f"Finnhub {endpoint}: HTTP {response.status_code}")
                return None

        except requests.RequestException as e:
            logger.debug(f"Finnhub request error: {e}")
            return None

    def get_quote(self, symbol: str) -> dict:
        """
        Get real-time quote for a symbol.

        Returns:
            dict with keys: current_price, change, percent_change,
            high, low, open, previous_close, volume, timestamp
        """
        data = self._request("quote", {"symbol": symbol})

        if not data or data.get("c", 0) == 0:
            return {}

        return {
            "symbol": symbol,
            "current_price": data.get("c", 0),  # Current price
            "change": data.get("d", 0),  # Change
            "percent_change": data.get("dp", 0),  # Percent change
            "high": data.get("h", 0),  # High of day
            "low": data.get("l", 0),  # Low of day
            "open": data.get("o", 0),  # Open
            "previous_close": data.get("pc", 0),  # Previous close
            "timestamp": data.get("t", 0),  # Timestamp
        }

    def get_candles(
        self,
        symbol: str,
        resolution: str = "D",
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Get OHLCV candle data.

        Args:
            symbol: Stock symbol
            resolution: Candle resolution - 1, 5, 15, 30, 60, D, W, M
            from_ts: Start timestamp (unix)
            to_ts: End timestamp (unix)

        Returns:
            DataFrame with columns: open, high, low, close, volume, timestamp
        """
        if to_ts is None:
            to_ts = int(datetime.now().timestamp())
        if from_ts is None:
            from_ts = to_ts - (30 * 24 * 60 * 60)  # 30 days back

        data = self._request("stock/candle", {
            "symbol": symbol,
            "resolution": resolution,
            "from": from_ts,
            "to": to_ts,
        })

        if not data or data.get("s") != "ok":
            return pd.DataFrame()

        try:
            df = pd.DataFrame({
                "timestamp": pd.to_datetime(data["t"], unit="s"),
                "open": data["o"],
                "high": data["h"],
                "low": data["l"],
                "close": data["c"],
                "volume": data["v"],
            })
            df.set_index("timestamp", inplace=True)
            return df

        except (KeyError, ValueError) as e:
            logger.debug(f"Error parsing Finnhub candles for {symbol}: {e}")
            return pd.DataFrame()

    def get_intraday_volume(self, symbol: str) -> dict:
        """
        Get today's volume and compare to average.

        This is the main method for volume confirmation fallback.

        Returns:
            dict with keys: current_volume, avg_volume, volume_ratio
        """
        # Get quote for current day's data
        quote = self.get_quote(symbol)
        if not quote:
            return {}

        # Get daily candles for average volume calculation
        to_ts = int(datetime.now().timestamp())
        from_ts = to_ts - (30 * 24 * 60 * 60)  # 30 days

        candles = self.get_candles(symbol, resolution="D", from_ts=from_ts, to_ts=to_ts)

        if candles.empty:
            return {}

        # Calculate 20-day average volume (excluding today)
        if len(candles) > 1:
            avg_volume = candles["volume"].iloc[:-1].tail(20).mean()
        else:
            avg_volume = candles["volume"].mean()

        # Today's volume isn't in the quote, but we can estimate from candles
        # If today's candle exists, use it; otherwise use quote data
        today = datetime.now().date()
        today_candle = candles[candles.index.date == today]

        if not today_candle.empty:
            current_volume = today_candle["volume"].iloc[0]
        else:
            # Finnhub quote doesn't include today's volume directly
            # We'll return what we have
            current_volume = 0

        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0

        return {
            "symbol": symbol,
            "current_volume": current_volume,
            "avg_volume": avg_volume,
            "volume_ratio": volume_ratio,
            "current_price": quote.get("current_price", 0),
            "previous_close": quote.get("previous_close", 0),
        }

    def get_daily_bars(self, symbol: str, days: int = 30) -> pd.DataFrame:
        """
        Get daily OHLCV bars.

        Returns:
            DataFrame with columns: open, high, low, close, volume
        """
        to_ts = int(datetime.now().timestamp())
        from_ts = to_ts - (days * 24 * 60 * 60)

        return self.get_candles(symbol, resolution="D", from_ts=from_ts, to_ts=to_ts)

    def get_intraday_bars(self, symbol: str, resolution: str = "5", days_back: int = 1) -> pd.DataFrame:
        """
        Get intraday bars.

        Args:
            symbol: Stock symbol
            resolution: "1", "5", "15", "30", "60" minutes
            days_back: Number of days to look back

        Returns:
            DataFrame with columns: open, high, low, close, volume
        """
        to_ts = int(datetime.now().timestamp())
        from_ts = to_ts - (days_back * 24 * 60 * 60)

        return self.get_candles(symbol, resolution=resolution, from_ts=from_ts, to_ts=to_ts)
