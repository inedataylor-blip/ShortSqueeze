# Short Squeeze Trading Bot

A fully automated trading bot that scans for heavily shorted stocks, detects squeeze momentum in progress, and executes trades via Alpaca's API. Uses free data sources only.

## Features

- **Universe Generation**: Weekly scan of Finviz for high short interest stocks (>20% of float)
- **Real-time Signal Detection**: Intraday scanning every 5-15 minutes during market hours
- **LazyBear Squeeze Momentum**: Implementation of the popular TradingView indicator
- **Risk Management**: Position sizing based on account risk, stop losses, and take profits
- **Automated Trading**: Executes trades via Alpaca API (paper or live)

## Data Sources

| Data Type | Source | Update Frequency | Library |
|-----------|--------|------------------|---------|
| Short Interest (% of float) | Yahoo Finance | ~2x monthly (lagged) | yfinance |
| Price/Volume Data | Alpaca API | Real-time | alpaca-trade-api |
| Screening Candidates | Finviz | Daily | BeautifulSoup |
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

### Universe Generation (Weekly Refresh)

**Goal**: Build a watchlist of squeeze candidates

**Criteria**:
- Short % of float > 20%
- Market cap > $300M (liquidity filter)
- Average daily volume > 1M shares
- Price > $5 (avoid penny stocks)
- US exchange listed (NYSE, NASDAQ)

**Process**:
1. Scrape Finviz for stocks with short float > 20%
2. Verify short interest data from Yahoo Finance
3. Filter by market cap and volume
4. Store as `data/watchlist.json` — refresh every Sunday

### Entry Signal Detection (Intraday Scan)

**Run frequency**: Every 5-15 minutes during market hours

**Primary Triggers (ALL must be true)**:
- Stock is on the watchlist (high short interest)
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
│   ├── data.py            # Data fetching (Finviz, Yahoo, Alpaca)
│   ├── indicators.py      # Technical indicators (Squeeze Momentum)
│   ├── universe.py        # Watchlist management
│   ├── signals.py         # Entry signal detection
│   ├── position.py        # Position sizing & risk management
│   ├── trader.py          # Trade execution
│   └── bot.py             # Main bot orchestration
└── data/
    ├── watchlist.json     # Generated watchlist (auto-created)
    └── trades.json        # Trade history (auto-created)
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
