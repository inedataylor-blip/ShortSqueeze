"""
Data fetching module for Short Squeeze Trading Bot.

Handles data retrieval from:
- Finviz: Stock screening and short interest data
- Yahoo Finance: Short interest verification and fundamentals
- Alpaca: Real-time price/volume data and trading
"""

import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import numpy as np
import requests
from bs4 import BeautifulSoup
import yfinance as yf
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from loguru import logger
from ratelimit import limits, sleep_and_retry

from .config import Config, ScreeningConfig


# Rate limiting: 5 requests per second for Finviz
@sleep_and_retry
@limits(calls=5, period=1)
def _rate_limited_request(url: str, headers: dict) -> requests.Response:
    """Make a rate-limited HTTP request."""
    return requests.get(url, headers=headers, timeout=30)


class FinvizScraper:
    """Scraper for Finviz stock screener."""

    BASE_URL = "https://finviz.com/screener.ashx"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    def __init__(self, config: ScreeningConfig):
        self.config = config

    def get_high_short_interest_stocks(self) -> pd.DataFrame:
        """
        Scrape Finviz for stocks with high short interest.

        Returns:
            DataFrame with columns: ticker, company, sector, industry,
                                   market_cap, price, short_float
        """
        logger.info("Scraping Finviz for high short interest stocks...")

        # Build filter parameters
        # f: filters
        # v: view (111 = overview with custom columns)
        # Short Float Over 20%: sh_short_o20
        # Price Over 5: sh_price_o5
        # Average Volume Over 1M: sh_avgvol_o1000
        # Market Cap Over 300M: cap_smallover (> $300M)

        filters = [
            "sh_short_o20",  # Short float over 20%
            "sh_price_o5",   # Price over $5
            "sh_avgvol_o1000",  # Avg volume over 1M
            "cap_smallover",  # Market cap > $300M
        ]

        params = {
            "v": "152",  # Custom view with short float
            "f": ",".join(filters),
            "o": "-shortinterestshare",  # Sort by short interest descending
        }

        all_stocks = []
        page = 1
        rows_per_page = 20

        while True:
            params["r"] = (page - 1) * rows_per_page + 1
            url = f"{self.BASE_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

            try:
                response = _rate_limited_request(url, self.HEADERS)
                response.raise_for_status()
            except requests.RequestException as e:
                logger.error(f"Failed to fetch Finviz page {page}: {e}")
                break

            soup = BeautifulSoup(response.text, "lxml")
            table = soup.find("table", {"class": "table-light"})

            if not table:
                logger.debug(f"No more data found on page {page}")
                break

            rows = table.find_all("tr")[1:]  # Skip header row

            if not rows:
                break

            for row in rows:
                cols = row.find_all("td")
                if len(cols) >= 10:
                    try:
                        ticker = cols[1].text.strip()
                        company = cols[2].text.strip()
                        sector = cols[3].text.strip()
                        industry = cols[4].text.strip()
                        market_cap = self._parse_market_cap(cols[6].text.strip())
                        price = self._parse_float(cols[8].text.strip())
                        short_float = self._parse_percentage(cols[9].text.strip())

                        if all([ticker, market_cap, price, short_float is not None]):
                            all_stocks.append({
                                "ticker": ticker,
                                "company": company,
                                "sector": sector,
                                "industry": industry,
                                "market_cap": market_cap,
                                "price": price,
                                "short_float_pct": short_float,
                            })
                    except (IndexError, ValueError) as e:
                        logger.debug(f"Error parsing row: {e}")
                        continue

            logger.debug(f"Page {page}: Found {len(rows)} stocks")
            page += 1

            # Safety limit
            if page > 50:
                break

            time.sleep(0.5)  # Be nice to Finviz

        df = pd.DataFrame(all_stocks)
        logger.info(f"Found {len(df)} stocks from Finviz with short float > 20%")
        return df

    @staticmethod
    def _parse_market_cap(value: str) -> Optional[float]:
        """Parse market cap string (e.g., '1.5B', '500M') to float."""
        if not value or value == "-":
            return None
        value = value.upper().replace(",", "")
        multipliers = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
        for suffix, mult in multipliers.items():
            if value.endswith(suffix):
                try:
                    return float(value[:-1]) * mult
                except ValueError:
                    return None
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def _parse_float(value: str) -> Optional[float]:
        """Parse float from string."""
        if not value or value == "-":
            return None
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def _parse_percentage(value: str) -> Optional[float]:
        """Parse percentage string (e.g., '25.5%') to float."""
        if not value or value == "-":
            return None
        try:
            return float(value.replace("%", ""))
        except ValueError:
            return None


class YahooFinanceData:
    """Yahoo Finance data fetcher for short interest verification."""

    def __init__(self):
        pass

    def get_short_interest(self, ticker: str) -> dict:
        """
        Get short interest data for a ticker from Yahoo Finance.

        Returns:
            dict with keys: short_percent_of_float, short_ratio, shares_short
        """
        try:
            stock = yf.Ticker(ticker)
            info = stock.info

            return {
                "ticker": ticker,
                "short_percent_of_float": info.get("shortPercentOfFloat", 0) * 100
                    if info.get("shortPercentOfFloat") else None,
                "short_ratio": info.get("shortRatio"),
                "shares_short": info.get("sharesShort"),
                "shares_outstanding": info.get("sharesOutstanding"),
                "float_shares": info.get("floatShares"),
            }
        except Exception as e:
            logger.debug(f"Error fetching Yahoo data for {ticker}: {e}")
            return {"ticker": ticker}

    def get_stock_info(self, ticker: str) -> dict:
        """Get comprehensive stock info."""
        try:
            stock = yf.Ticker(ticker)
            info = stock.info

            return {
                "ticker": ticker,
                "name": info.get("shortName", info.get("longName", "")),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "market_cap": info.get("marketCap"),
                "avg_volume": info.get("averageVolume"),
                "avg_volume_10d": info.get("averageVolume10days"),
                "price": info.get("currentPrice", info.get("regularMarketPrice")),
                "previous_close": info.get("previousClose", info.get("regularMarketPreviousClose")),
                "short_percent_of_float": (info.get("shortPercentOfFloat", 0) * 100
                    if info.get("shortPercentOfFloat") else None),
                "short_ratio": info.get("shortRatio"),
                "exchange": info.get("exchange"),
            }
        except Exception as e:
            logger.debug(f"Error fetching stock info for {ticker}: {e}")
            return {"ticker": ticker}

    def get_historical_data(
        self,
        ticker: str,
        period: str = "1mo",
        interval: str = "1d"
    ) -> pd.DataFrame:
        """
        Get historical OHLCV data.

        Args:
            ticker: Stock symbol
            period: Data period (1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max)
            interval: Data interval (1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo)
        """
        try:
            stock = yf.Ticker(ticker)
            df = stock.history(period=period, interval=interval)
            df.index = df.index.tz_localize(None) if df.index.tz else df.index
            return df
        except Exception as e:
            logger.error(f"Error fetching historical data for {ticker}: {e}")
            return pd.DataFrame()


class AlpacaDataClient:
    """Alpaca data and trading client wrapper."""

    def __init__(self, config: Config):
        self.config = config
        self._data_client = None
        self._trading_client = None

    @property
    def data_client(self) -> StockHistoricalDataClient:
        """Lazy-load data client."""
        if self._data_client is None:
            self._data_client = StockHistoricalDataClient(
                api_key=self.config.alpaca.api_key,
                secret_key=self.config.alpaca.secret_key,
            )
        return self._data_client

    @property
    def trading_client(self) -> TradingClient:
        """Lazy-load trading client."""
        if self._trading_client is None:
            self._trading_client = TradingClient(
                api_key=self.config.alpaca.api_key,
                secret_key=self.config.alpaca.secret_key,
                paper="paper" in self.config.alpaca.base_url,
            )
        return self._trading_client

    def get_latest_quote(self, symbol: str) -> dict:
        """Get latest quote for a symbol."""
        try:
            request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self.data_client.get_stock_latest_quote(request)
            quote = quotes.get(symbol)

            if quote:
                return {
                    "symbol": symbol,
                    "bid": float(quote.bid_price),
                    "ask": float(quote.ask_price),
                    "bid_size": quote.bid_size,
                    "ask_size": quote.ask_size,
                    "timestamp": quote.timestamp,
                }
            return {}
        except Exception as e:
            logger.error(f"Error fetching quote for {symbol}: {e}")
            return {}

    def get_bars(
        self,
        symbol: str,
        timeframe: TimeFrame = TimeFrame.Minute,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 1000,
    ) -> pd.DataFrame:
        """
        Get historical bar data from Alpaca.

        Args:
            symbol: Stock symbol
            timeframe: Bar timeframe (Minute, Hour, Day)
            start: Start datetime
            end: End datetime
            limit: Maximum number of bars
        """
        try:
            if start is None:
                start = datetime.now() - timedelta(days=5)
            if end is None:
                end = datetime.now()

            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                limit=limit,
            )

            bars = self.data_client.get_stock_bars(request)
            data = bars.get(symbol, [])

            if not data:
                return pd.DataFrame()

            df = pd.DataFrame([{
                "timestamp": bar.timestamp,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": bar.volume,
                "vwap": float(bar.vwap) if bar.vwap else None,
                "trade_count": bar.trade_count,
            } for bar in data])

            df.set_index("timestamp", inplace=True)
            df.index = df.index.tz_localize(None) if df.index.tz else df.index
            return df

        except Exception as e:
            logger.error(f"Error fetching bars for {symbol}: {e}")
            return pd.DataFrame()

    def get_intraday_bars(
        self,
        symbol: str,
        minutes: int = 5,
        days_back: int = 1,
    ) -> pd.DataFrame:
        """Get intraday minute bars."""
        start = datetime.now() - timedelta(days=days_back)

        if minutes == 1:
            timeframe = TimeFrame.Minute
        elif minutes == 5:
            timeframe = TimeFrame(5, "Min")
        elif minutes == 15:
            timeframe = TimeFrame(15, "Min")
        elif minutes == 60:
            timeframe = TimeFrame.Hour
        else:
            timeframe = TimeFrame.Minute

        return self.get_bars(symbol, timeframe=timeframe, start=start)

    def get_daily_bars(
        self,
        symbol: str,
        days: int = 30,
    ) -> pd.DataFrame:
        """Get daily OHLCV bars."""
        start = datetime.now() - timedelta(days=days)
        return self.get_bars(symbol, timeframe=TimeFrame.Day, start=start)

    def get_account(self) -> dict:
        """Get account information."""
        try:
            account = self.trading_client.get_account()
            return {
                "equity": float(account.equity),
                "cash": float(account.cash),
                "buying_power": float(account.buying_power),
                "portfolio_value": float(account.portfolio_value),
                "currency": account.currency,
                "pattern_day_trader": account.pattern_day_trader,
                "trading_blocked": account.trading_blocked,
                "account_blocked": account.account_blocked,
            }
        except Exception as e:
            logger.error(f"Error fetching account: {e}")
            return {}

    def get_positions(self) -> list:
        """Get all open positions."""
        try:
            positions = self.trading_client.get_all_positions()
            return [{
                "symbol": pos.symbol,
                "qty": float(pos.qty),
                "side": pos.side.value,
                "market_value": float(pos.market_value),
                "cost_basis": float(pos.cost_basis),
                "unrealized_pl": float(pos.unrealized_pl),
                "unrealized_plpc": float(pos.unrealized_plpc),
                "current_price": float(pos.current_price),
                "avg_entry_price": float(pos.avg_entry_price),
            } for pos in positions]
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return []

    def get_position(self, symbol: str) -> Optional[dict]:
        """Get position for a specific symbol."""
        try:
            pos = self.trading_client.get_open_position(symbol)
            return {
                "symbol": pos.symbol,
                "qty": float(pos.qty),
                "side": pos.side.value,
                "market_value": float(pos.market_value),
                "cost_basis": float(pos.cost_basis),
                "unrealized_pl": float(pos.unrealized_pl),
                "unrealized_plpc": float(pos.unrealized_plpc),
                "current_price": float(pos.current_price),
                "avg_entry_price": float(pos.avg_entry_price),
            }
        except Exception:
            return None


class DataManager:
    """Unified data manager combining all data sources."""

    def __init__(self, config: Config):
        self.config = config
        self.finviz = FinvizScraper(config.screening)
        self.yahoo = YahooFinanceData()
        self.alpaca = AlpacaDataClient(config)

    def get_squeeze_candidates(self) -> pd.DataFrame:
        """
        Get screened squeeze candidates combining Finviz and Yahoo data.

        Returns:
            DataFrame of stocks meeting all screening criteria
        """
        # Get initial candidates from Finviz
        df = self.finviz.get_high_short_interest_stocks()

        if df.empty:
            logger.warning("No candidates found from Finviz")
            return df

        # Verify and enrich with Yahoo data
        verified = []
        for _, row in df.iterrows():
            ticker = row["ticker"]
            yahoo_info = self.yahoo.get_stock_info(ticker)

            # Verify short interest
            yahoo_short = yahoo_info.get("short_percent_of_float")
            if yahoo_short and yahoo_short >= self.config.screening.min_short_float_pct:
                verified_short = yahoo_short
            else:
                verified_short = row.get("short_float_pct")

            # Only include if still meets criteria
            if verified_short and verified_short >= self.config.screening.min_short_float_pct:
                verified.append({
                    "ticker": ticker,
                    "company": row.get("company") or yahoo_info.get("name"),
                    "sector": yahoo_info.get("sector") or row.get("sector"),
                    "industry": yahoo_info.get("industry") or row.get("industry"),
                    "market_cap": yahoo_info.get("market_cap") or row.get("market_cap"),
                    "price": yahoo_info.get("price") or row.get("price"),
                    "avg_volume": yahoo_info.get("avg_volume"),
                    "short_float_pct": verified_short,
                    "short_ratio": yahoo_info.get("short_ratio"),
                    "previous_close": yahoo_info.get("previous_close"),
                    "exchange": yahoo_info.get("exchange"),
                })

            time.sleep(0.2)  # Rate limiting for Yahoo

        result = pd.DataFrame(verified)
        logger.info(f"Verified {len(result)} squeeze candidates after Yahoo verification")
        return result
