import requests
import asyncio
import websockets
import json
from typing import Optional, Dict
from abc import ABC, abstractmethod
from loguru import logger
import threading


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
    """
    Fetch real-time price from Kuru WebSocket orderbook.

    Maintains a WebSocket connection to wss://ws.kuru.io and subscribes to
    the frontendOrderbook channel. Calculates mid-price from best bid/ask.
    """

    def __init__(self):
        self._best_bid: Optional[float] = None
        self._best_ask: Optional[float] = None
        self._market_id: Optional[str] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready_event = threading.Event()

    def start(self, market_id: str) -> None:
        """
        Start WebSocket connection in background.

        Args:
            market_id: Market address to subscribe to
        """
        self._market_id = market_id

        # Start WebSocket in background thread with its own event loop
        def run_ws():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._run_websocket())

        ws_thread = threading.Thread(target=run_ws, daemon=True)
        ws_thread.start()

        # Wait for initial orderbook data (timeout 5s)
        if not self._ready_event.wait(timeout=5.0):
            logger.warning("Kuru WebSocket connection timed out waiting for initial data")
        else:
            logger.success(f"✓ Kuru WebSocket connected (bid: {self._best_bid}, ask: {self._best_ask})")

    async def _run_websocket(self) -> None:
        """Run WebSocket connection (internal)"""
        uri = "wss://ws.kuru.io"

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(uri) as websocket:
                    # Subscribe to orderbook
                    subscribe_msg = {
                        "type": "subscribe",
                        "channel": "frontendOrderbook",
                        "market": self._market_id
                    }
                    await websocket.send(json.dumps(subscribe_msg))
                    logger.debug(f"Subscribed to Kuru orderbook for {self._market_id}")

                    # Process messages
                    while not self._stop_event.is_set():
                        try:
                            message = await asyncio.wait_for(
                                websocket.recv(),
                                timeout=1.0
                            )
                            self._process_message(json.loads(message))
                        except asyncio.TimeoutError:
                            continue

            except Exception as e:
                logger.error(f"Kuru WebSocket error: {e}")
                if not self._stop_event.is_set():
                    await asyncio.sleep(5)  # Retry after 5s

    def _process_message(self, data: dict) -> None:
        """Process WebSocket message and update prices"""
        try:
            # Check for orderbook data
            if "b" in data and "a" in data:
                bids = data["b"]
                asks = data["a"]

                if bids and asks:
                    # Best bid/ask are first entries
                    best_bid_raw = bids[0][0]  # [price, size]
                    best_ask_raw = asks[0][0]

                    # Convert from 10^18 to actual price
                    self._best_bid = best_bid_raw / (10 ** 18)
                    self._best_ask = best_ask_raw / (10 ** 18)

                    # Signal ready on first valid data
                    if not self._ready_event.is_set():
                        self._ready_event.set()

                    logger.debug(f"Kuru orderbook updated: bid={self._best_bid:.6f}, ask={self._best_ask:.6f}")

        except (KeyError, IndexError, TypeError) as e:
            logger.warning(f"Failed to parse Kuru orderbook: {e}")

    def get_price(self, market_id: str) -> Optional[float]:
        """
        Get latest mid-price from cached orderbook.

        Args:
            market_id: Market address (not used, set via start())

        Returns:
            Mid-price or None if not available
        """
        if self._best_bid is None or self._best_ask is None:
            return None

        mid_price = (self._best_bid + self._best_ask) / 2
        return mid_price

    def stop(self) -> None:
        """Stop WebSocket connection"""
        if self._loop:
            self._loop.call_soon_threadsafe(self._stop_event.set)


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
