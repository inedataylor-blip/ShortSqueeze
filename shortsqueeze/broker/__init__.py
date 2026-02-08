"""
Broker abstraction layer.

Provides abstract interfaces and factory functions for creating
broker-specific data clients and trade executors.

To add a new broker:
1. Create a new module (e.g., broker/ibkr_data.py, broker/ibkr_trader.py)
2. Implement BrokerDataClient and BrokerTrader interfaces
3. Add the broker to the factory functions below
4. Set BROKER_TYPE=ibkr in your .env file
"""

from .base import BrokerDataClient, BrokerTrader
from .alpaca_data import AlpacaDataClient
from .alpaca_trader import AlpacaTradeExecutor
from .finnhub_data import FinnhubDataClient

__all__ = [
    "BrokerDataClient",
    "BrokerTrader",
    "AlpacaDataClient",
    "AlpacaTradeExecutor",
    "FinnhubDataClient",
    "create_data_client",
    "create_trader",
]


def create_data_client(config) -> BrokerDataClient:
    """
    Factory: create the appropriate broker data client based on config.

    Args:
        config: Config object with broker_type and broker credentials

    Returns:
        BrokerDataClient implementation
    """
    broker_type = getattr(config, "broker_type", "alpaca").lower()

    if broker_type == "alpaca":
        return AlpacaDataClient(config)
    else:
        raise ValueError(
            f"Unsupported broker type: '{broker_type}'. "
            f"Supported brokers: alpaca"
        )


def create_trader(config) -> BrokerTrader:
    """
    Factory: create the appropriate broker trader based on config.

    Args:
        config: Config object with broker_type and broker credentials

    Returns:
        BrokerTrader implementation
    """
    broker_type = getattr(config, "broker_type", "alpaca").lower()

    if broker_type == "alpaca":
        return AlpacaTradeExecutor(config)
    else:
        raise ValueError(
            f"Unsupported broker type: '{broker_type}'. "
            f"Supported brokers: alpaca"
        )
