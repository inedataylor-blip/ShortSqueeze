"""
Data fetching module for Short Squeeze Trading Bot.

Handles data retrieval from:
- Finviz: Stock screening and short interest data (via finvizfinance package)
- Yahoo Finance: Short interest verification, fundamentals, and dynamic screening
- Alpaca: Real-time price/volume data and trading
"""

import time
from datetime import datetime, timedelta
from typing import Optional, List

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

# Try to import finvizfinance for more reliable Finviz access
try:
    from finvizfinance.screener.overview import Overview
    FINVIZFINANCE_AVAILABLE = True
except ImportError:
    FINVIZFINANCE_AVAILABLE = False
    logger.warning("finvizfinance not installed, using manual scraper only")

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


class FinvizFinanceScreener:
    """
    More reliable Finviz screener using the finvizfinance package.
    This handles Finviz's anti-scraping measures better than manual scraping.
    """

    def __init__(self, config: ScreeningConfig):
        self.config = config

    def get_high_short_interest_stocks(self) -> pd.DataFrame:
        """
        Get stocks with high short interest using finvizfinance package.

        Returns:
            DataFrame with columns: ticker, company, sector, market_cap, price, short_float_pct
        """
        if not FINVIZFINANCE_AVAILABLE:
            logger.warning("finvizfinance not available")
            return pd.DataFrame()

        logger.info("Fetching high short interest stocks via finvizfinance...")

        try:
            foverview = Overview()

            # Set filters for high short interest stocks
            # Note: Filter names must match exactly (e.g., 'Market Cap.' with period)
            filters_dict = {
                'Float Short': 'Over 20%',
                'Price': 'Over $5',
                'Average Volume': 'Over 1M',
                'Market Cap.': 'Small ($300mln to $2bln)',  # Start with small cap
            }
            foverview.set_filter(filters_dict=filters_dict)

            # Get the screener results
            df = foverview.screener_view()

            if df is None or df.empty:
                # Try with different market cap filter
                filters_dict['Market Cap.'] = 'Mid ($2bln to $10bln)'
                foverview.set_filter(filters_dict=filters_dict)
                df = foverview.screener_view()

            if df is None or df.empty:
                # Try without market cap filter
                filters_dict.pop('Market Cap.', None)
                foverview.set_filter(filters_dict=filters_dict)
                df = foverview.screener_view()

            if df is None or df.empty:
                logger.warning("finvizfinance returned no results")
                return pd.DataFrame()

            # Normalize column names and extract relevant data
            result = []
            for _, row in df.iterrows():
                ticker = row.get('Ticker', row.get('ticker', ''))
                if not ticker:
                    continue

                result.append({
                    'ticker': ticker,
                    'company': row.get('Company', row.get('company', '')),
                    'sector': row.get('Sector', row.get('sector', '')),
                    'industry': row.get('Industry', row.get('industry', '')),
                    'market_cap': self._parse_value(row.get('Market Cap', row.get('market_cap', ''))),
                    'price': self._parse_value(row.get('Price', row.get('price', ''))),
                    'short_float_pct': self._parse_percentage(row.get('Float Short', row.get('Short Float', row.get('short_float', '')))),
                })

            result_df = pd.DataFrame(result)
            logger.info(f"finvizfinance found {len(result_df)} high short interest stocks")
            return result_df

        except Exception as e:
            logger.error(f"Error using finvizfinance: {e}")
            return pd.DataFrame()

    @staticmethod
    def _parse_value(value) -> Optional[float]:
        """Parse numeric value from various formats."""
        if pd.isna(value) or value == '' or value == '-':
            return None
        if isinstance(value, (int, float)):
            return float(value)
        value = str(value).upper().replace(',', '').replace('$', '')
        multipliers = {'K': 1e3, 'M': 1e6, 'B': 1e9, 'T': 1e12}
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
    def _parse_percentage(value) -> Optional[float]:
        """Parse percentage from various formats."""
        if pd.isna(value) or value == '' or value == '-':
            return None
        if isinstance(value, (int, float)):
            return float(value) * 100 if value < 1 else float(value)
        try:
            return float(str(value).replace('%', ''))
        except ValueError:
            return None


class HighShortInterestScraper:
    """
    Scraper for highshortinterest.com - provides a simple list of most shorted stocks.

    This site lists stocks sorted by short interest percentage, making it a good
    supplementary source for finding squeeze candidates.
    """

    BASE_URL = "https://highshortinterest.com"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
    }

    def __init__(self, config: ScreeningConfig):
        self.config = config

    def get_high_short_interest_stocks(self) -> pd.DataFrame:
        """
        Scrape highshortinterest.com for stocks with high short interest.

        Returns:
            DataFrame with columns: ticker, company, exchange, short_float_pct, float_shares, outstd_shares, industry
        """
        logger.info("Scraping highshortinterest.com for high short interest stocks...")

        all_stocks = []

        try:
            response = requests.get(self.BASE_URL, headers=self.HEADERS, timeout=30)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "lxml")

            # Find the main data table
            tables = soup.find_all("table")

            for table in tables:
                rows = table.find_all("tr")

                for row in rows:
                    cols = row.find_all("td")
                    if len(cols) < 5:
                        continue

                    try:
                        # Typical structure: Ticker, Company, Exchange, ShortInt, Float, Outstd, Industry
                        ticker = cols[0].text.strip().upper()

                        # Validate ticker format
                        if not ticker or not ticker.isalpha() or len(ticker) > 5:
                            continue

                        company = cols[1].text.strip() if len(cols) > 1 else ""
                        exchange = cols[2].text.strip() if len(cols) > 2 else ""

                        # Parse short interest percentage
                        short_pct_text = cols[3].text.strip() if len(cols) > 3 else ""
                        short_float_pct = self._parse_percentage(short_pct_text)

                        if short_float_pct is None or short_float_pct < 10:
                            continue

                        # Parse float and outstanding shares
                        float_shares = self._parse_shares(cols[4].text.strip()) if len(cols) > 4 else None
                        outstd_shares = self._parse_shares(cols[5].text.strip()) if len(cols) > 5 else None
                        industry = cols[6].text.strip() if len(cols) > 6 else ""

                        all_stocks.append({
                            "ticker": ticker,
                            "company": company,
                            "exchange": exchange,
                            "short_float_pct": short_float_pct,
                            "float_shares": float_shares,
                            "shares_outstanding": outstd_shares,
                            "industry": industry,
                            "sector": "",  # Not provided by this source
                            "market_cap": None,
                            "price": None,
                        })

                    except Exception as e:
                        logger.debug(f"Error parsing row: {e}")
                        continue

            df = pd.DataFrame(all_stocks)
            logger.info(f"highshortinterest.com found {len(df)} stocks")
            return df

        except requests.RequestException as e:
            logger.warning(f"Failed to fetch highshortinterest.com: {e}")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Error scraping highshortinterest.com: {e}")
            return pd.DataFrame()

    @staticmethod
    def _parse_percentage(value: str) -> Optional[float]:
        """Parse percentage string to float."""
        if not value or value == "-" or value == "N/A":
            return None
        try:
            return float(value.replace("%", "").replace(",", "").strip())
        except ValueError:
            return None

    @staticmethod
    def _parse_shares(value: str) -> Optional[float]:
        """Parse shares count with M/B suffix."""
        if not value or value == "-" or value == "N/A":
            return None
        value = value.upper().replace(",", "").strip()
        try:
            if value.endswith("M"):
                return float(value[:-1]) * 1e6
            elif value.endswith("B"):
                return float(value[:-1]) * 1e9
            elif value.endswith("K"):
                return float(value[:-1]) * 1e3
            return float(value)
        except ValueError:
            return None


class FintelScraper:
    """
    Scraper for Fintel.io Short Squeeze Leaderboard.

    Fintel provides a proprietary Short Squeeze Score based on:
    - Short Interest % Float
    - Short Borrow Fee Rates
    - Float utilization
    - Other factors

    Note: Full access requires subscription, but basic data may be available.
    """

    BASE_URL = "https://fintel.io/shortSqueeze"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Referer": "https://fintel.io/",
    }

    def __init__(self, config: ScreeningConfig):
        self.config = config

    def get_short_squeeze_candidates(self) -> pd.DataFrame:
        """
        Scrape Fintel Short Squeeze Leaderboard.

        Returns:
            DataFrame with columns: ticker, company, squeeze_score, short_float_pct, etc.
        """
        logger.info("Scraping Fintel Short Squeeze Leaderboard...")

        all_stocks = []

        try:
            response = requests.get(self.BASE_URL, headers=self.HEADERS, timeout=30)

            if response.status_code == 403:
                logger.warning("Fintel access denied (may require subscription)")
                return pd.DataFrame()

            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")

            # Find the leaderboard table
            table = soup.find("table", {"class": ["table", "leaderboard"]})
            if not table:
                # Try alternative selectors
                table = soup.find("table")

            if not table:
                logger.warning("Could not find Fintel leaderboard table")
                return pd.DataFrame()

            rows = table.find_all("tr")

            for row in rows[1:]:  # Skip header row
                cols = row.find_all("td")
                if len(cols) < 3:
                    continue

                try:
                    # Find ticker - usually in a link
                    ticker_elem = row.find("a", href=lambda x: x and "/ss/us/" in x)
                    if not ticker_elem:
                        # Try finding by text pattern
                        for col in cols:
                            text = col.text.strip().upper()
                            if text and text.isalpha() and 1 <= len(text) <= 5:
                                ticker = text
                                break
                        else:
                            continue
                    else:
                        ticker = ticker_elem.text.strip().upper()

                    if not ticker:
                        continue

                    # Parse other columns
                    col_texts = [c.text.strip() for c in cols]

                    squeeze_score = None
                    short_float_pct = None
                    company = ""

                    for i, text in enumerate(col_texts):
                        # Squeeze score (0-100)
                        if squeeze_score is None:
                            try:
                                val = float(text)
                                if 0 <= val <= 100:
                                    squeeze_score = val
                                    continue
                            except ValueError:
                                pass

                        # Short interest percentage
                        if "%" in text and short_float_pct is None:
                            try:
                                short_float_pct = float(text.replace("%", "").strip())
                            except ValueError:
                                pass

                        # Company name (longer text without numbers)
                        if len(text) > 10 and not any(c.isdigit() for c in text[:5]):
                            company = text

                    all_stocks.append({
                        "ticker": ticker,
                        "company": company,
                        "squeeze_score": squeeze_score,
                        "short_float_pct": short_float_pct or 20.0,  # Default if not found
                        "sector": "",
                        "industry": "",
                        "market_cap": None,
                        "price": None,
                    })

                except Exception as e:
                    logger.debug(f"Error parsing Fintel row: {e}")
                    continue

            df = pd.DataFrame(all_stocks)

            # Sort by squeeze score if available
            if not df.empty and "squeeze_score" in df.columns:
                df = df.sort_values("squeeze_score", ascending=False)

            logger.info(f"Fintel found {len(df)} short squeeze candidates")
            return df

        except requests.RequestException as e:
            logger.warning(f"Failed to fetch Fintel: {e}")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Error scraping Fintel: {e}")
            return pd.DataFrame()


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

    def screen_high_short_interest(
        self,
        seed_tickers: List[str],
        min_short_pct: float = 20.0,
        min_price: float = 5.0,
        min_market_cap: float = 300e6,
    ) -> pd.DataFrame:
        """
        Dynamically screen stocks for high short interest.

        This method checks a seed list of tickers and finds those with
        high short interest, then expands by checking related/similar stocks.

        Args:
            seed_tickers: List of tickers to check
            min_short_pct: Minimum short interest percentage
            min_price: Minimum stock price
            min_market_cap: Minimum market cap

        Returns:
            DataFrame of qualifying stocks
        """
        logger.info(f"Screening {len(seed_tickers)} tickers for high short interest...")

        candidates = []
        checked = set()

        for ticker in seed_tickers:
            if ticker in checked:
                continue
            checked.add(ticker)

            try:
                info = self.get_stock_info(ticker)

                short_pct = info.get("short_percent_of_float")
                price = info.get("price")
                market_cap = info.get("market_cap")

                # Skip if missing critical data
                if short_pct is None:
                    continue

                # Apply filters
                if short_pct < min_short_pct:
                    continue
                if price is None or price < min_price:
                    continue
                if market_cap is None or market_cap < min_market_cap:
                    continue

                candidates.append({
                    "ticker": ticker,
                    "company": info.get("name", ""),
                    "sector": info.get("sector", ""),
                    "industry": info.get("industry", ""),
                    "market_cap": market_cap,
                    "price": price,
                    "short_float_pct": short_pct,
                    "avg_volume": info.get("avg_volume"),
                    "short_ratio": info.get("short_ratio"),
                })

                logger.debug(f"{ticker}: {short_pct:.1f}% short interest - ADDED")

            except Exception as e:
                logger.debug(f"Error screening {ticker}: {e}")
                continue

            time.sleep(0.15)  # Rate limiting

        df = pd.DataFrame(candidates)
        logger.info(f"Found {len(df)} stocks with >{min_short_pct}% short interest")
        return df


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

    # Extended ticker list for dynamic screening
    # This is checked via Yahoo Finance to find current high short interest stocks
    # Much larger than before to increase chances of finding new squeeze candidates
    SCREENING_UNIVERSE = [
        # Current high short interest (20%+) - January 2026
        "HIMS", "APLD", "SOUN", "MP", "UPST", "CVNA", "BYND",
        "ZETA", "CPNG", "XPEV", "TMC", "AAOI", "RGTI", "ONDS",
        # EV/Clean energy - frequently shorted sector
        "PLUG", "FCEL", "BLNK", "QS", "LAZR", "NKLA", "GOEV", "HYLN",
        "CHPT", "LCID", "RIVN", "PTRA", "FSR", "WKHS", "RIDE",
        # Meme stocks / retail favorites
        "GME", "AMC", "KOSS", "SNDL", "BB", "NOK", "SPCE", "BBAI",
        # Tech/Growth - volatile, often shorted
        "PLTR", "FUBO", "CLOV", "ATER", "OPEN", "SOFI", "HOOD",
        "AFRM", "COIN", "MARA", "RIOT", "CLSK", "BTBT", "HUT",
        # Biotech/Healthcare - HIGH PRIORITY for squeeze potential
        "IBRX", "VIR", "SRNE", "NVAX", "MRNA", "BNTX", "INO", "OCGN",
        "SAVA", "AGEN", "IMVT", "APLS", "FATE", "BEAM", "CRSP", "EDIT",
        "NTLA", "VERV", "PRAX", "AKRO", "ARWR", "ALNY", "IONS", "REGN",
        "VRTX", "BMRN", "EXEL", "HALO", "LGND", "RARE", "RCKT", "RLAY",
        "SGEN", "TVTX", "XNCR", "ZLAB", "DCPH", "PTGX", "KYMR", "GTHX",
        "ORIC", "TGTX", "ACAD", "ALKS", "FOLD", "GILD", "INCY", "JAZZ",
        # SPACs and recent IPOs - often heavily shorted
        "BKKT", "EVTL", "DNA", "IONQ", "JOBY", "LILM", "ACHR",
        # Consumer/Retail - cyclical shorts
        "APRN", "W", "CHWY", "PRPL", "BGFV", "EXPR", "BBWI",
        # Additional frequently shorted names
        "TSLA", "NFLX", "SQ", "SNAP", "PINS", "ROKU", "ZM",
        "DOCU", "PTON", "DASH", "U", "RBLX", "PATH", "CRWD",
        # Small caps with high short interest potential
        "FFIE", "MULN", "BNGO", "SENS", "GEVO", "CLNE",
        "RMO", "ARVL", "REE", "PSNY", "VFS",
        # Solar/Energy - cyclical shorts
        "SEDG", "ENPH", "RUN", "NOVA", "ARRY", "MAXN", "JKS", "CSIQ",
        # Semiconductor/Tech hardware
        "WOLF", "AEHR", "LSCC", "SITM", "PLAB", "AOSL", "CRUS",
        # Airlines/Travel - high short interest
        "SAVE", "JBLU", "AAL", "UAL", "DAL", "LUV", "ALK", "ULCC",
        # Additional healthcare/pharma
        "TEVA", "ENDP", "PRGO", "CTLT", "ZTS", "VTRS", "OGN",
    ]

    # Last resort fallback - verified high SI stocks (subset of above)
    FALLBACK_TICKERS = [
        "HIMS", "APLD", "SOUN", "MP", "UPST", "CVNA", "BYND",
        "PLUG", "FCEL", "GME", "AMC", "PLTR", "FUBO", "MARA", "RIOT",
        "IBRX", "NVAX", "IONQ", "SEDG", "WOLF", "SAVE",
    ]

    def __init__(self, config: Config):
        self.config = config
        self.finviz_package = FinvizFinanceScreener(config.screening) if FINVIZFINANCE_AVAILABLE else None
        self.finviz_manual = FinvizScraper(config.screening)
        self.highshortinterest = HighShortInterestScraper(config.screening)
        self.fintel = FintelScraper(config.screening)
        self.yahoo = YahooFinanceData()
        self.alpaca = AlpacaDataClient(config)

    def get_squeeze_candidates(self) -> pd.DataFrame:
        """
        Get screened squeeze candidates using multiple data sources.

        Multi-source aggregation (collects from ALL available sources):
        1. finvizfinance package (most reliable Finviz access)
        2. Manual Finviz scraper (custom BeautifulSoup)
        3. HighShortInterest.com scraper
        4. Fintel Short Squeeze Leaderboard
        5. Yahoo Finance dynamic screening (checks SCREENING_UNIVERSE)

        All results are merged and deduplicated to maximize coverage.

        Returns:
            DataFrame of stocks meeting all screening criteria
        """
        all_candidates = []

        # SOURCE 1: Try finvizfinance package (most reliable)
        if self.finviz_package is not None:
            logger.info("Fetching from finvizfinance package...")
            try:
                df = self.finviz_package.get_high_short_interest_stocks()
                if not df.empty:
                    logger.info(f"finvizfinance found {len(df)} candidates")
                    all_candidates.append(df)
            except Exception as e:
                logger.warning(f"finvizfinance error: {e}")

        # SOURCE 2: Try manual Finviz scraper
        logger.info("Fetching from manual Finviz scraper...")
        try:
            df = self.finviz_manual.get_high_short_interest_stocks()
            if not df.empty:
                logger.info(f"Manual Finviz scraper found {len(df)} candidates")
                all_candidates.append(df)
        except Exception as e:
            logger.warning(f"Manual Finviz error: {e}")

        # SOURCE 3: Try HighShortInterest.com
        logger.info("Fetching from highshortinterest.com...")
        try:
            df = self.highshortinterest.get_high_short_interest_stocks()
            if not df.empty:
                logger.info(f"highshortinterest.com found {len(df)} candidates")
                all_candidates.append(df)
        except Exception as e:
            logger.warning(f"highshortinterest.com error: {e}")

        # SOURCE 4: Try Fintel Short Squeeze Leaderboard
        logger.info("Fetching from Fintel Short Squeeze Leaderboard...")
        try:
            df = self.fintel.get_short_squeeze_candidates()
            if not df.empty:
                logger.info(f"Fintel found {len(df)} candidates")
                all_candidates.append(df)
        except Exception as e:
            logger.warning(f"Fintel error: {e}")

        # SOURCE 5: Yahoo Finance dynamic screening (always run to catch SCREENING_UNIVERSE)
        logger.info(f"Screening {len(self.SCREENING_UNIVERSE)} tickers via Yahoo Finance...")
        try:
            df = self.yahoo.screen_high_short_interest(
                seed_tickers=self.SCREENING_UNIVERSE,
                min_short_pct=self.config.screening.min_short_float_pct,
                min_price=self.config.screening.min_price,
                min_market_cap=self.config.screening.min_market_cap,
            )
            if not df.empty:
                logger.info(f"Yahoo screening found {len(df)} candidates")
                all_candidates.append(df)
        except Exception as e:
            logger.warning(f"Yahoo screening error: {e}")

        # Merge all candidates
        if not all_candidates:
            # Last resort - check verified fallback tickers
            logger.warning("All sources failed, using fallback ticker list...")
            df = self._get_candidates_from_yahoo()
            if not df.empty:
                return df
            logger.error("No candidates found from ANY source")
            return pd.DataFrame()

        # Combine all dataframes
        combined = pd.concat(all_candidates, ignore_index=True)

        # Deduplicate by ticker, keeping first occurrence (usually has better data)
        combined = combined.drop_duplicates(subset=["ticker"], keep="first")

        logger.info(f"Combined {len(combined)} unique candidates from {len(all_candidates)} sources")

        # Verify and enrich with Yahoo data
        verified = self._verify_with_yahoo(combined)
        result = pd.DataFrame(verified)

        # Sort by short interest (highest first)
        if not result.empty and "short_float_pct" in result.columns:
            result = result.sort_values("short_float_pct", ascending=False)

        logger.info(f"Final watchlist: {len(result)} verified squeeze candidates")
        return result

    def _verify_with_yahoo(self, df: pd.DataFrame) -> list:
        """Verify and enrich candidates with Yahoo Finance data."""
        verified = []
        skipped = 0
        for _, row in df.iterrows():
            ticker = row["ticker"]
            yahoo_info = self.yahoo.get_stock_info(ticker)

            # Get short interest from Yahoo or Finviz
            yahoo_short = yahoo_info.get("short_percent_of_float")
            finviz_short = row.get("short_float_pct")

            # Use Yahoo data if available, otherwise trust Finviz
            if yahoo_short and yahoo_short >= self.config.screening.min_short_float_pct:
                verified_short = yahoo_short
            elif finviz_short and finviz_short >= self.config.screening.min_short_float_pct:
                verified_short = finviz_short
            else:
                # Trust Finviz filter results even without exact value
                # The filter was 'Float Short': 'Over 20%', so stocks should qualify
                verified_short = self.config.screening.min_short_float_pct
                logger.debug(f"{ticker}: No short float data from Yahoo or Finviz, using filter default")

            # Get price - need at least this to be useful
            price = yahoo_info.get("price") or row.get("price")
            if not price or price <= 0:
                skipped += 1
                logger.debug(f"{ticker}: Skipped - no valid price data")
                continue

            verified.append({
                "ticker": ticker,
                "company": row.get("company") or yahoo_info.get("name"),
                "sector": yahoo_info.get("sector") or row.get("sector"),
                "industry": yahoo_info.get("industry") or row.get("industry"),
                "market_cap": yahoo_info.get("market_cap") or row.get("market_cap"),
                "price": price,
                "avg_volume": yahoo_info.get("avg_volume"),
                "short_float_pct": verified_short,
                "short_ratio": yahoo_info.get("short_ratio"),
                "previous_close": yahoo_info.get("previous_close"),
                "exchange": yahoo_info.get("exchange"),
            })

            time.sleep(0.2)  # Rate limiting for Yahoo

        if skipped > 0:
            logger.info(f"Skipped {skipped} stocks due to missing price data")
        return verified

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
