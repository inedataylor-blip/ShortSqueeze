"""
Data fetching module for Short Squeeze Trading Bot.

Handles data retrieval from:
- Finviz: Stock screening and short interest data (via finvizfinance package)
- Yahoo Finance: Short interest verification, fundamentals, and dynamic screening
- HighShortInterest.com: Supplementary short interest data
- Fintel: Short Squeeze Leaderboard
- SEC EDGAR: Fail-to-Deliver data (high FTDs = hard to borrow = squeeze pressure)

Broker-specific market data is handled by the broker/ package.
"""

import io
import time
import zipfile
from datetime import datetime, timedelta
from typing import Optional, List

import pandas as pd
import numpy as np
import requests
from bs4 import BeautifulSoup
import yfinance as yf
from loguru import logger

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


class SECFailToDeliverScraper:
    """
    Fetches Fail-to-Deliver (FTD) data from SEC EDGAR.

    High FTD counts indicate shares are hard to borrow, which creates
    additional squeeze pressure. Stocks with persistently high FTDs
    relative to their float are strong squeeze candidates.

    Data source: https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data
    Updated twice monthly (1st half ~end of month, 2nd half ~15th of next month).
    """

    BASE_URL = "https://www.sec.gov/files/data/fails-deliver-data"
    HEADERS = {
        "User-Agent": "ShortSqueezeBot/1.0 (research@example.com)",
        "Accept": "application/zip, */*",
        "Accept-Encoding": "gzip, deflate",
    }

    # Minimum FTD shares to consider significant
    MIN_FTD_SHARES = 50_000

    def __init__(self, config: ScreeningConfig):
        self.config = config

    def get_high_ftd_stocks(self) -> pd.DataFrame:
        """
        Fetch recent FTD data from SEC and identify stocks with high fail-to-deliver counts.

        Downloads the most recent available FTD file, aggregates by ticker,
        and returns stocks with significant FTD levels.

        Returns:
            DataFrame with columns: ticker, company, total_ftd_shares, avg_ftd_shares,
            max_ftd_shares, ftd_days, latest_price, short_float_pct (None)
        """
        logger.info("Fetching SEC Fail-to-Deliver data...")

        raw_df = self._download_latest_ftd()

        if raw_df.empty:
            return pd.DataFrame()

        # Aggregate FTD data by ticker
        aggregated = self._aggregate_ftd(raw_df)

        if aggregated.empty:
            return pd.DataFrame()

        logger.info(f"SEC FTD data: {len(aggregated)} stocks with significant fail-to-deliver counts")
        return aggregated

    def _download_latest_ftd(self) -> pd.DataFrame:
        """
        Download the most recent FTD data file from SEC.

        Tries the most recent half-month periods, falling back to older ones.

        Returns:
            Raw DataFrame of FTD records
        """
        now = datetime.now()

        # Build list of candidate file names to try (most recent first)
        candidates = []
        for months_back in range(0, 4):
            dt = now - timedelta(days=months_back * 30)
            year = dt.year
            month = dt.month
            candidates.append(f"cnsfails{year}{month:02d}b")
            candidates.append(f"cnsfails{year}{month:02d}a")

        for filename in candidates:
            url = f"{self.BASE_URL}/{filename}.zip"
            try:
                logger.debug(f"Trying SEC FTD file: {filename}.zip")
                response = requests.get(url, headers=self.HEADERS, timeout=30)

                if response.status_code == 200:
                    logger.info(f"Downloaded SEC FTD file: {filename}.zip")
                    return self._parse_ftd_zip(response.content, filename)
                elif response.status_code == 404:
                    continue
                else:
                    logger.debug(f"SEC FTD {filename}: HTTP {response.status_code}")

            except requests.RequestException as e:
                logger.debug(f"Failed to download {filename}: {e}")
                continue

        logger.warning("Could not download any SEC FTD data files")
        return pd.DataFrame()

    def _parse_ftd_zip(self, zip_content: bytes, filename: str) -> pd.DataFrame:
        """
        Parse a downloaded FTD ZIP file.

        The ZIP contains a pipe-delimited text file with columns:
        SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE
        """
        try:
            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                # Get the first (usually only) file in the ZIP
                file_list = zf.namelist()
                if not file_list:
                    logger.warning(f"Empty ZIP file: {filename}")
                    return pd.DataFrame()

                txt_filename = file_list[0]
                with zf.open(txt_filename) as f:
                    content = f.read().decode("utf-8", errors="ignore")

            # Parse pipe-delimited content
            lines = content.strip().split("\n")
            if len(lines) < 2:
                return pd.DataFrame()

            records = []
            for line in lines[1:]:  # Skip header
                parts = line.strip().split("|")
                if len(parts) < 6:
                    continue

                try:
                    settlement_date = parts[0].strip()
                    symbol = parts[2].strip().upper()
                    quantity = parts[3].strip()
                    description = parts[4].strip()
                    price = parts[5].strip()

                    # Validate symbol
                    if not symbol or not symbol.isalpha() or len(symbol) > 5:
                        continue

                    # Parse quantity
                    ftd_shares = int(quantity) if quantity else 0
                    if ftd_shares < self.MIN_FTD_SHARES:
                        continue

                    # Parse price
                    try:
                        price_val = float(price) if price else None
                    except ValueError:
                        price_val = None

                    records.append({
                        "date": settlement_date,
                        "ticker": symbol,
                        "ftd_shares": ftd_shares,
                        "company": description,
                        "price": price_val,
                    })

                except (ValueError, IndexError):
                    continue

            df = pd.DataFrame(records)
            logger.info(f"Parsed {len(df)} FTD records from {txt_filename}")
            return df

        except zipfile.BadZipFile:
            logger.warning(f"Bad ZIP file: {filename}")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Error parsing FTD file {filename}: {e}")
            return pd.DataFrame()

    def _aggregate_ftd(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Aggregate raw FTD records by ticker.

        Computes total, average, and max FTD shares, plus the number of days
        with FTDs. Stocks with more days of high FTDs are stronger candidates.
        """
        if df.empty or "ticker" not in df.columns:
            return pd.DataFrame()

        agg = df.groupby("ticker").agg(
            total_ftd_shares=("ftd_shares", "sum"),
            avg_ftd_shares=("ftd_shares", "mean"),
            max_ftd_shares=("ftd_shares", "max"),
            ftd_days=("ftd_shares", "count"),
            company=("company", "first"),
            price=("price", "last"),
        ).reset_index()

        # Filter: at least 3 days of significant FTDs
        agg = agg[agg["ftd_days"] >= 3]

        # Sort by total FTD shares (highest first)
        agg = agg.sort_values("total_ftd_shares", ascending=False)

        # Add placeholder columns for compatibility with other sources
        agg["short_float_pct"] = None
        agg["sector"] = ""
        agg["industry"] = ""
        agg["market_cap"] = None

        return agg


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


class DataManager:
    """Unified data manager combining all data sources."""

    def __init__(self, config: Config):
        self.config = config
        self.finviz_package = FinvizFinanceScreener(config.screening) if FINVIZFINANCE_AVAILABLE else None
        self.finviz_manual = FinvizScraper(config.screening)
        self.highshortinterest = HighShortInterestScraper(config.screening)
        self.fintel = FintelScraper(config.screening)
        self.sec_ftd = SECFailToDeliverScraper(config.screening)
        self.yahoo = YahooFinanceData()

    def get_squeeze_candidates(self) -> pd.DataFrame:
        """
        Get screened squeeze candidates using multiple data sources.

        Dynamic discovery from scrapers (no hardcoded tickers):
        1. finvizfinance package (most reliable Finviz access)
        2. Manual Finviz scraper (custom BeautifulSoup)
        3. HighShortInterest.com scraper
        4. Fintel Short Squeeze Leaderboard
        5. SEC EDGAR Fail-to-Deliver data (high FTDs = squeeze pressure)

        All results are merged and deduplicated to maximize coverage.
        If all scrapers fail, returns empty DataFrame (no false positives).

        Returns:
            DataFrame of stocks meeting all screening criteria
        """
        all_candidates = []
        sources_tried = 0
        sources_succeeded = 0

        # SOURCE 1: Try finvizfinance package (most reliable)
        if self.finviz_package is not None:
            sources_tried += 1
            logger.info("Fetching from finvizfinance package...")
            try:
                df = self.finviz_package.get_high_short_interest_stocks()
                if not df.empty:
                    logger.info(f"finvizfinance found {len(df)} candidates")
                    all_candidates.append(df)
                    sources_succeeded += 1
            except Exception as e:
                logger.warning(f"finvizfinance error: {e}")

        # SOURCE 2: Try manual Finviz scraper
        sources_tried += 1
        logger.info("Fetching from manual Finviz scraper...")
        try:
            df = self.finviz_manual.get_high_short_interest_stocks()
            if not df.empty:
                logger.info(f"Manual Finviz scraper found {len(df)} candidates")
                all_candidates.append(df)
                sources_succeeded += 1
        except Exception as e:
            logger.warning(f"Manual Finviz error: {e}")

        # SOURCE 3: Try HighShortInterest.com
        sources_tried += 1
        logger.info("Fetching from highshortinterest.com...")
        try:
            df = self.highshortinterest.get_high_short_interest_stocks()
            if not df.empty:
                logger.info(f"highshortinterest.com found {len(df)} candidates")
                all_candidates.append(df)
                sources_succeeded += 1
        except Exception as e:
            logger.warning(f"highshortinterest.com error: {e}")

        # SOURCE 4: Try Fintel Short Squeeze Leaderboard
        sources_tried += 1
        logger.info("Fetching from Fintel Short Squeeze Leaderboard...")
        try:
            df = self.fintel.get_short_squeeze_candidates()
            if not df.empty:
                logger.info(f"Fintel found {len(df)} candidates")
                all_candidates.append(df)
                sources_succeeded += 1
        except Exception as e:
            logger.warning(f"Fintel error: {e}")

        # SOURCE 5: SEC EDGAR Fail-to-Deliver data
        sources_tried += 1
        logger.info("Fetching SEC Fail-to-Deliver data...")
        try:
            df = self.sec_ftd.get_high_ftd_stocks()
            if not df.empty:
                logger.info(f"SEC FTD data found {len(df)} stocks with high fail-to-deliver counts")
                all_candidates.append(df)
                sources_succeeded += 1
        except Exception as e:
            logger.warning(f"SEC FTD error: {e}")

        # Check if all scrapers failed
        if not all_candidates:
            logger.error(
                f"All {sources_tried} data sources failed to return candidates. "
                "Check network connectivity and scraper configurations."
            )
            return pd.DataFrame()

        # Combine all dataframes
        combined = pd.concat(all_candidates, ignore_index=True)

        # Deduplicate by ticker, keeping first occurrence (usually has better data)
        combined = combined.drop_duplicates(subset=["ticker"], keep="first")

        logger.info(
            f"Combined {len(combined)} unique candidates from "
            f"{sources_succeeded}/{sources_tried} sources"
        )

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
