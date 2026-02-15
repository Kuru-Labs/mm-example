import requests
from typing import Optional, Dict
from abc import ABC, abstractmethod


class PriceSource(ABC):
    """Abstract base class for price sources"""

    @abstractmethod
    def get_price(self, market_id: str) -> Optional[float]:
        """Get price from the source"""
        pass


class CoinbasePriceSource(PriceSource):
    """Fetch price from Coinbase API"""

    def __init__(self, symbol: str = "MON-USD"):
        """
        Initialize Coinbase price source.

        Args:
            symbol: Trading pair symbol (e.g., "MON-USD", "BTC-USD")
        """
        self.symbol = symbol

    def get_price(self, market_id: str) -> Optional[float]:
        """
        Fetch the latest price from Coinbase API.

        Args:
            market_id (str): Not used for Coinbase, uses self.symbol instead
        """
        url = f"https://api.coinbase.com/v2/prices/{self.symbol}/spot"

        try:
            response = requests.get(url)
            response.raise_for_status()

            data = response.json()
            if not data.get("data"):
                return None

            amount = data["data"].get("amount")
            if not amount:
                return None

            return float(amount)

        except (requests.RequestException, KeyError, ValueError):
            return None


class KuruPriceSource(PriceSource):
    def get_price(self, market_id: str) -> Optional[float]:
        """
        Fetch the latest closing price from Kuru.io API for a given market address.

        Args:
            market_id (str): The market address on Kuru
        """
        url = (
            f"https://api.kuru.io/api/v2/markets/{market_id}/price"
        )

        try:
            response = requests.get(url)
            response.raise_for_status()

            data = response.json()
            if not data.get("success"):
                return None

            price = data["data"]["data"]
            if not price:
                return None

            return float(price)

        except (requests.RequestException, KeyError, IndexError, ValueError):
            return None


class OracleService:
    def __init__(self):
        self.price_sources: Dict[str, PriceSource] = {}

    def add_price_source(self, name: str, source: PriceSource) -> None:
        """Add a new price source to the service"""
        self.price_sources[name] = source

    def get_price(self, market_id: str, source_name: str) -> Optional[float]:
        """Get price from a specific source"""
        source = self.price_sources.get(source_name)
        if not source:
            return None
        return source.get_price(market_id)

    def get_average_price(self, market_id: str) -> Optional[float]:
        """Get average price across all available sources"""
        prices = []
        for source in self.price_sources.values():
            price = source.get_price(market_id)
            if price is not None:
                prices.append(price)

        if not prices:
            return None

        return sum(prices) / len(prices)
