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
        "Referer": "https://finviz.com/screener.ashx",
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

        # Build filter parameters for short float > 20%
        # Using view 152 which includes short float column
        filters = [
            "sh_short_o20",  # Short float over 20%
            "sh_price_o5",   # Price over $5
            "sh_avgvol_o1000",  # Avg volume over 1M
            "cap_smallover",  # Market cap > $300M
        ]

        all_stocks = []
        page = 1
        rows_per_page = 20

        while True:
            row_start = (page - 1) * rows_per_page + 1
            url = f"{self.BASE_URL}?v=152&f={','.join(filters)}&r={row_start}"

            try:
                response = _rate_limited_request(url, self.HEADERS)
                response.raise_for_status()
                logger.debug(f"Fetched page {page}, status: {response.status_code}")
            except requests.RequestException as e:
                logger.error(f"Failed to fetch Finviz page {page}: {e}")
                break

            soup = BeautifulSoup(response.text, "lxml")

            # Try multiple table selectors (Finviz changes their layout frequently)
            table = None
            for selector in [
                {"class": "screener_table"},
                {"class": "table-light"},
                {"class": "screener-body-table-nw"},
                {"id": "screener-table"},
                {"id": "screener-content"},
            ]:
                table = soup.find("table", selector)
                if table:
                    logger.debug(f"Found table with selector: {selector}")
                    break

            # Fallback: find table containing stock tickers by looking for ticker links
            if not table:
                tables = soup.find_all("table")
                for t in tables:
                    # Look for ticker links with various class patterns
                    ticker_link = (
                        t.find("a", {"class": "screener-link-primary"}) or
                        t.find("a", {"class": "tab-link"}) or
                        t.find("a", href=lambda x: x and "quote.ashx" in x)
                    )
                    if ticker_link:
                        table = t
                        logger.debug("Found table via ticker link fallback")
                        break

            # Additional fallback: look for rows with table-light-row class
            if not table:
                rows_with_data = soup.find_all("tr", {"class": "table-light-row"})
                if rows_with_data:
                    table = rows_with_data[0].find_parent("table")
                    logger.debug("Found table via table-light-row fallback")

            if not table:
                # Check if we got a "no results" page
                if "No results found" in response.text or page > 1:
                    logger.debug(f"No more data found on page {page}")
                else:
                    logger.warning("Could not find stock table in Finviz response")
                break

            # Find all rows with stock data - try multiple row patterns
            rows = table.find_all("tr", {"class": ["table-light-row", "table-dark-row"]})
            if not rows:
                rows = table.find_all("tr")
            stocks_found = 0

            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 6:  # Reduced from 8 to handle different views
                    continue

                try:
                    # Find ticker - try multiple class patterns
                    ticker_elem = (
                        row.find("a", {"class": "screener-link-primary"}) or
                        row.find("a", {"class": "tab-link"}) or
                        row.find("a", href=lambda x: x and "quote.ashx?t=" in x)
                    )
                    if not ticker_elem:
                        # Try finding first anchor in row that looks like a ticker
                        for a in row.find_all("a"):
                            text = a.text.strip()
                            href = a.get("href", "")
                            if text and text.isupper() and 1 <= len(text) <= 5:
                                ticker_elem = a
                                break
                            elif "quote.ashx" in href:
                                ticker_elem = a
                                break

                    if not ticker_elem:
                        continue

                    ticker = ticker_elem.text.strip()
                    if not ticker or not ticker.isupper():
                        continue

                    # Parse other columns - index depends on view
                    # View 152 columns: No, Ticker, Company, Sector, Industry, Country, Market Cap, P/E, Price, Change, Volume, Short Float
                    col_texts = [c.text.strip() for c in cols]

                    # Find indices by looking for patterns
                    company = ""
                    sector = ""
                    industry = ""
                    market_cap = None
                    price = None
                    short_float = None

                    for i, text in enumerate(col_texts):
                        if text == ticker:
                            # Typically: ticker at i, company at i+1
                            if i + 1 < len(col_texts):
                                company = col_texts[i + 1]
                            continue
                        # Market cap pattern (ends with B, M, K)
                        if text and text[-1] in "BMK" and market_cap is None:
                            parsed = self._parse_market_cap(text)
                            if parsed and parsed > 1e6:
                                market_cap = parsed
                        # Percentage pattern (ends with %)
                        if text.endswith("%") and short_float is None:
                            parsed = self._parse_percentage(text)
                            if parsed is not None and parsed > 0:
                                short_float = parsed
                        # Price pattern (looks like a number)
                        if price is None:
                            parsed = self._parse_float(text)
                            if parsed is not None and 1 < parsed < 10000:
                                price = parsed

                    # If we found key data, add it
                    if ticker and short_float is not None and short_float >= 20:
                        all_stocks.append({
                            "ticker": ticker,
                            "company": company,
                            "sector": sector,
                            "industry": industry,
                            "market_cap": market_cap,
                            "price": price,
                            "short_float_pct": short_float,
                        })
                        stocks_found += 1

                except Exception as e:
                    logger.debug(f"Error parsing row: {e}")
                    continue

            logger.debug(f"Page {page}: Found {stocks_found} stocks")

            if stocks_found == 0:
                break

            page += 1
            if page > 50:
                break

            time.sleep(0.5)

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
        timeframe: TimeFrame = TimeFrame.Day,
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
            )

            bars = self.data_client.get_stock_bars(request)

            # Handle different response formats from Alpaca SDK
            data = []
            try:
                # Try accessing as dict-like object with .data attribute
                if hasattr(bars, 'data'):
                    bar_data = bars.data
                    if isinstance(bar_data, dict) and symbol in bar_data:
                        data = bar_data[symbol]
                    elif hasattr(bar_data, 'get'):
                        data = bar_data.get(symbol, [])
                # Try direct bracket access
                if not data:
                    try:
                        data = bars[symbol]
                    except (KeyError, TypeError):
                        pass
                # Try iterating directly
                if not data:
                    data = [b for b in bars if getattr(b, 'symbol', None) == symbol]
            except Exception as e:
                logger.debug(f"Error parsing Alpaca response for {symbol}: {e}")
                data = []

            if not data:
                return pd.DataFrame()

            df = pd.DataFrame([{
                "timestamp": bar.timestamp,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume) if bar.volume else 0,
                "vwap": float(bar.vwap) if hasattr(bar, 'vwap') and bar.vwap else None,
                "trade_count": getattr(bar, 'trade_count', 0),
            } for bar in data])

            df.set_index("timestamp", inplace=True)
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            return df

        except Exception as e:
            logger.debug(f"Alpaca bars error for {symbol}: {e}")
            return pd.DataFrame()

    def get_intraday_bars(
        self,
        symbol: str,
        minutes: int = 5,
        days_back: int = 1,
    ) -> pd.DataFrame:
        """Get intraday minute bars."""
        start = datetime.now() - timedelta(days=days_back)

        # Use simple TimeFrame constants to avoid SDK compatibility issues
        try:
            if minutes >= 60:
                timeframe = TimeFrame.Hour
            else:
                timeframe = TimeFrame.Minute
        except Exception:
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

    # Known high short interest stocks to check when Finviz fails
    # Updated December 2025 - removed delisted stocks, added current high SI names
    # Sources: Yahoo Finance, Benzinga, MarketBeat, Fintel
    FALLBACK_TICKERS = [
        # Current high short interest (20%+) - December 2025
        "HIMS", "APLD", "SOUN", "MP", "UPST", "CVNA", "BYND",
        "ZETA", "CPNG", "XPEV", "TMC", "AAOI", "RGTI", "ONDS",
        # EV/Clean energy shorts
        "PLUG", "FCEL", "BLNK", "QS", "LAZR", "NKLA", "GOEV", "HYLN",
        # Meme stocks still trading
        "GME", "AMC", "KOSS", "SNDL", "BB", "NOK", "SPCE",
        # Tech/Growth shorts
        "PLTR", "FUBO", "CLOV", "WKHS", "ATER",
        # Other notable shorts
        "VIR", "BKKT", "EVTL", "APRN", "TSLA", "CHPT", "LCID",
        "RIVN", "AFRM", "COIN", "MARA", "RIOT", "CLSK",
    ]

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
            logger.warning("No candidates found from Finviz, using Yahoo fallback")
            df = self._get_candidates_from_yahoo()
            if not df.empty:
                # Yahoo data already verified, just return it
                return df

        if df.empty:
            logger.warning("No candidates found from any source")
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

    def _get_candidates_from_yahoo(self) -> pd.DataFrame:
        """
        Fallback: Get candidates directly from Yahoo Finance.

        Checks a list of known high short interest stocks.
        """
        logger.info("Checking known high short interest stocks via Yahoo Finance...")

        candidates = []
        for ticker in self.FALLBACK_TICKERS:
            try:
                info = self.yahoo.get_stock_info(ticker)

                short_pct = info.get("short_percent_of_float")
                price = info.get("price")
                market_cap = info.get("market_cap")
                avg_volume = info.get("avg_volume")

                # Apply filters with logging
                if short_pct is None or short_pct < self.config.screening.min_short_float_pct:
                    if short_pct is not None:
                        logger.debug(f"{ticker}: short interest {short_pct:.1f}% < {self.config.screening.min_short_float_pct}% min")
                    continue
                if price is None or price < self.config.screening.min_price:
                    if price is not None:
                        logger.debug(f"{ticker}: price ${price:.2f} < ${self.config.screening.min_price} min")
                    continue
                if market_cap is None or market_cap < self.config.screening.min_market_cap:
                    if market_cap is not None:
                        logger.debug(f"{ticker}: market cap ${market_cap/1e6:.0f}M < ${self.config.screening.min_market_cap/1e6:.0f}M min")
                    continue

                candidates.append({
                    "ticker": ticker,
                    "company": info.get("name", ""),
                    "sector": info.get("sector", ""),
                    "industry": info.get("industry", ""),
                    "market_cap": market_cap,
                    "price": price,
                    "short_float_pct": short_pct,
                    "avg_volume": avg_volume,
                })

                logger.debug(f"{ticker}: {short_pct:.1f}% short interest")

            except Exception as e:
                logger.debug(f"Error checking {ticker}: {e}")
                continue

            time.sleep(0.1)  # Rate limiting

        df = pd.DataFrame(candidates)
        logger.info(f"Found {len(df)} candidates from Yahoo fallback")
        return df
