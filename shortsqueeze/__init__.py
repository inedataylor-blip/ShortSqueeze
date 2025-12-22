"""
Short Squeeze Trading Bot

A fully automated trading bot that scans for heavily shorted stocks,
detects squeeze momentum in progress, and executes trades via Alpaca's API.
"""

__version__ = "1.0.0"
__author__ = "Short Squeeze Bot"

from .config import Config
from .bot import ShortSqueezeBot

__all__ = ["Config", "ShortSqueezeBot", "__version__"]
