"""
Configuration management for the Short Squeeze Trading Bot.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class AlpacaConfig:
    """Alpaca API configuration."""
    api_key: str
    secret_key: str
    base_url: str
    data_url: str = "https://data.alpaca.markets"

    @classmethod
    def from_env(cls) -> "AlpacaConfig":
        """Create config from environment variables."""
        trading_mode = os.getenv("ALPACA_TRADING_MODE", "paper").lower()

        if trading_mode == "live":
            base_url = "https://api.alpaca.markets"
        else:
            base_url = "https://paper-api.alpaca.markets"

        return cls(
            api_key=os.getenv("ALPACA_API_KEY", ""),
            secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
            base_url=base_url,
        )


@dataclass
class ScreeningConfig:
    """Universe screening criteria configuration."""
    min_short_float_pct: float = 20.0  # Minimum short % of float
    min_market_cap: float = 300_000_000  # $300M minimum
    min_avg_volume: int = 1_000_000  # 1M shares minimum
    # Dual watchlist price thresholds
    quality_min_price: float = 5.0  # Quality watchlist: $5+ (established companies)
    speculative_min_price: float = 1.0  # Speculative watchlist: $1-$5 (higher risk)
    exchanges: tuple = ("NYSE", "NASDAQ", "AMEX")


@dataclass
class IndicatorConfig:
    """Technical indicator parameters."""
    # Bollinger Bands
    bb_length: int = 20
    bb_mult: float = 2.0

    # Keltner Channels
    kc_length: int = 20
    kc_mult: float = 1.5

    # Use True Range for Keltner
    use_true_range: bool = True

    # Volume analysis
    volume_ma_period: int = 20
    volume_multiplier: float = 3.0  # 3x average volume threshold

    # Price triggers
    min_price_change_pct: float = 5.0  # 5% minimum move


@dataclass
class RiskConfig:
    """Risk management configuration."""
    risk_per_trade: float = 0.005  # 0.5% account risk per trade
    max_positions: int = 5
    max_position_size: float = 0.20  # 20% max allocation per position
    default_stop_loss_pct: float = 0.08  # 8% stop loss
    trailing_stop_pct: float = 0.05  # 5% trailing stop


@dataclass
class TradingConfig:
    """Trading execution configuration."""
    scan_interval_minutes: int = 15  # Scan every 15 minutes during market hours
    use_limit_orders: bool = True
    limit_offset_pct: float = 0.001  # 0.1% above current price for limits
    order_timeout_seconds: int = 30


@dataclass
class Config:
    """Main configuration container."""
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig.from_env)
    screening: ScreeningConfig = field(default_factory=ScreeningConfig)
    indicators: IndicatorConfig = field(default_factory=IndicatorConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)

    # Broker selection (currently: "alpaca")
    broker_type: str = "alpaca"

    # Finnhub API key for volume data fallback
    finnhub_api_key: str = ""

    # Paths
    data_dir: Path = field(default_factory=lambda: Path("data"))
    log_level: str = "INFO"

    def __post_init__(self):
        """Ensure data directory exists."""
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, env_file: Optional[str] = None) -> "Config":
        """Load configuration from environment."""
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()

        config = cls(
            alpaca=AlpacaConfig.from_env(),
            broker_type=os.getenv("BROKER_TYPE", "alpaca").lower(),
            finnhub_api_key=os.getenv("FINNHUB_API_KEY", ""),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )

        # Override from environment if set
        if risk_per_trade := os.getenv("RISK_PER_TRADE"):
            config.risk.risk_per_trade = float(risk_per_trade)

        if max_positions := os.getenv("MAX_POSITIONS"):
            config.risk.max_positions = int(max_positions)

        if max_position_size := os.getenv("MAX_POSITION_SIZE"):
            config.risk.max_position_size = float(max_position_size)

        if scan_interval := os.getenv("SCAN_INTERVAL_MINUTES"):
            config.trading.scan_interval_minutes = int(scan_interval)

        return config

    @property
    def watchlist_quality_path(self) -> Path:
        """Path to quality watchlist JSON file ($5+ stocks)."""
        return self.data_dir / "watchlist_quality.json"

    @property
    def watchlist_speculative_path(self) -> Path:
        """Path to speculative watchlist JSON file (<$5 stocks)."""
        return self.data_dir / "watchlist_speculative.json"

    @property
    def watchlist_path(self) -> Path:
        """Path to watchlist JSON file (legacy, points to quality)."""
        return self.watchlist_quality_path

    @property
    def trades_path(self) -> Path:
        """Path to trades log JSON file."""
        return self.data_dir / "trades.json"
