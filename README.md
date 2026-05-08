# Short Squeeze Trading Bot

A fully automated trading bot that scans for heavily shorted stocks, detects squeeze momentum in progress, and executes trades via Alpaca's API. Uses free data sources only with multi-source dynamic discovery.

## Features

- **Multi-Source Discovery**: Daily scan across Finviz and HighShortInterest.com for high short interest stocks (>20% of float)
- **Dual Watchlists**: Separate quality ($5+) and speculative ($1-$5) watchlists for comprehensive coverage
- **Real-time Signal Detection**: Intraday scanning every 15 minutes during market hours
- **LazyBear Squeeze Momentum**: Implementation of the popular TradingView indicator
- **Risk Management**: Position sizing based on account risk, stop losses, and take profits
- **Automated Trading**: Executes trades via Alpaca API (paper or live)
- **Dynamic Discovery**: No hardcoded ticker lists — all candidates found by scrapers in real-time

## Data Sources

| Data Type | Source | Update Frequency | Library |
|-----------|--------|------------------|---------|
| Short Interest Screening | Finviz (finvizfinance) | Daily | finvizfinance |
| Short Interest Screening | Finviz (manual scraper) | Daily | BeautifulSoup |
| Short Interest Screening | HighShortInterest.com | Daily | BeautifulSoup |
| Short Interest Verification | Yahoo Finance | ~2x monthly (lagged) | yfinance |
| Price/Volume Data | Alpaca API | Real-time | alpaca-trade-api |
| Historical OHLCV | Alpaca/Yahoo | Daily/Intraday | alpaca-trade-api/yfinance |

## Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/ShortSqueeze.git
cd ShortSqueeze
```

2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Configure environment variables:
```bash
cp .env.example .env
# Edit .env with your Alpaca API credentials
```

## Configuration

Edit `.env` file with your settings:

```env
# Alpaca API (get keys at https://alpaca.markets/)
ALPACA_API_KEY=your_api_key
ALPACA_SECRET_KEY=your_secret_key
ALPACA_TRADING_MODE=paper  # or 'live'

# Risk Management
RISK_PER_TRADE=0.005  # 0.5% account risk per trade
MAX_POSITIONS=5
MAX_POSITION_SIZE=0.20  # 20% max per position

# Scanning
SCAN_INTERVAL_MINUTES=5
```

## Usage

### Run the Bot Continuously
```bash
python main.py
```

### Run a Single Scan (No Trading)
```bash
python main.py --scan
```

### Check Bot Status
```bash
python main.py --status
```

### Refresh Watchlist
```bash
python main.py --refresh
```

### Check a Specific Ticker
```bash
python main.py --check TSLA
```

### Enable Debug Logging
```bash
python main.py --debug
```

## Strategy Details

### Universe Generation (Daily Refresh)

**Goal**: Build a comprehensive watchlist of squeeze candidates using multiple data sources

**Discovery Sources** (all queried, results merged and deduplicated):
1. **Finviz** (finvizfinance package) — Most reliable screener access
2. **Finviz** (manual scraper) — Backup BeautifulSoup scraper
3. **HighShortInterest.com** — Curated list of top shorted stocks
4. **SEC EDGAR Fail-to-Deliver data** — High FTDs as a squeeze-pressure signal

If all scrapers fail, the bot returns an empty watchlist (no false positives from stale data).

**Screening Criteria**:
- Short % of float > 20%
- Market cap > $300M for quality stocks (>$100M for speculative)
- Average daily volume > 500K shares
- US exchange listed (NYSE, NASDAQ, AMEX)

**Dual Watchlists**:
| Watchlist | Price Range | Market Cap | File |
|-----------|-------------|------------|------|
| Quality | >= $5 | >= $300M | `data/watchlist_quality.json` |
| Speculative | $1 - $5 | >= $100M | `data/watchlist_speculative.json` |

**Refresh Schedule**: Daily at 6:00 AM ET (before market open)

### Entry Signal Detection (Intraday Scan)

**Run frequency**: Every 15 minutes during market hours

**Primary Triggers (ALL must be true)**:
- Stock is on either watchlist (high short interest)
- Current price up > 5% from previous close
- Current volume > 3x 20-day average volume (prorated for time of day)
- Price > VWAP (holding above volume-weighted average)

**Secondary Confirmation (LazyBear Squeeze Momentum)**:
- **Squeeze state**: Bollinger Bands (20, 2.0) inside Keltner Channels (20, 1.5)
- **Squeeze firing**: BBs expanding outside KCs
- **Momentum positive and rising**: Linear regression value > 0 and > previous bar

**Entry Logic**:
- If primary triggers met AND (squeeze just fired OR momentum strongly positive) → BUY

### Squeeze Momentum Indicator Parameters

```
Bollinger Bands:
- Length: 20
- MultFactor: 2.0
- Calculation: SMA(close, 20) ± 2.0 * StdDev(close, 20)

Keltner Channels:
- Length: 20
- MultFactor: 1.5
- Use True Range: True
- Calculation: SMA(close, 20) ± 1.5 * SMA(TrueRange, 20)

Squeeze States:
- sqzOn: lowerBB > lowerKC AND upperBB < upperKC (volatility compressed)
- sqzOff: lowerBB < lowerKC AND upperBB > upperKC (volatility expanding)

Momentum Value:
- midline = average of Donchian midline and SMA
- val = LinearRegression(close - midline, 20)

Signals:
- Bullish: val > 0 AND val > val[1] (positive and rising)
- Bearish: val < 0 AND val < val[1] (negative and falling)
```

### Position Sizing

**Account Risk**: 0.5% per trade (conservative due to volatility)

**Position Size Formula**:
```
Position Size = (Account Equity × Risk Per Trade) / (Entry Price - Stop Loss Price)
```

**Constraints**:
- Maximum 5 concurrent positions
- Maximum 20% of equity per position
- Default 8% stop loss
- 5% trailing stop (when in profit)

### Exit Strategy

- **Stop Loss**: 8% below entry (placed immediately)
- **Trailing Stop**: 5% from highest price (when in profit)
- **Take Profit 1**: 1.5R (risk:reward) - sell 50%
- **Take Profit 2**: 3R - sell remaining

## Project Structure

```
ShortSqueeze/
├── main.py                 # CLI entry point
├── requirements.txt        # Python dependencies
├── .env.example           # Configuration template
├── .gitignore             # Git ignore rules
├── README.md              # This file
├── shortsqueeze/
│   ├── __init__.py        # Package init
│   ├── config.py          # Configuration management
│   ├── data.py            # Data fetching (Finviz, HighShortInterest, SEC FTD, Yahoo, Alpaca)
│   ├── indicators.py      # Technical indicators (Squeeze Momentum)
│   ├── universe.py        # Dual watchlist management (quality + speculative)
│   ├── signals.py         # Entry signal detection
│   ├── position.py        # Position sizing & risk management
│   ├── trader.py          # Trade execution
│   ├── timezone.py        # Market hours & timezone utilities
│   └── bot.py             # Main bot orchestration
└── data/
    ├── watchlist_quality.json      # Quality watchlist - $5+ stocks (auto-created)
    ├── watchlist_speculative.json  # Speculative watchlist - $1-$5 stocks (auto-created)
    └── trades.json                 # Trade history (auto-created)
```

## Risk Disclaimer

**WARNING**: This software is for educational purposes only. Trading stocks involves significant risk of loss. Past performance does not guarantee future results.

- Always start with paper trading
- Never risk more than you can afford to lose
- The authors are not responsible for any financial losses
- Do your own research before trading

## License

MIT License - See LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
