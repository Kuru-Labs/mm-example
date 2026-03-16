"""
InfluxDB 3 metrics writer for the market making bot.

All write methods are no-ops when INFLUX_URL is not configured.
Points are buffered in-memory and flushed every 250 ms (or eagerly at 100 points)
by a background asyncio task. The InfluxDB3 client write call is synchronous and
is offloaded to a thread executor to avoid blocking the event loop.
"""
import asyncio
import time
from typing import Optional

from loguru import logger

try:
    from influxdb_client_3 import InfluxDBClient3, Point
    _INFLUX_AVAILABLE = True
except ImportError:
    _INFLUX_AVAILABLE = False


def _extract_quoter_id(cloid: str) -> str:
    """Parse quoter_id from cloid format '{side}-{quoter_id}-{timestamp_ms}'."""
    parts = cloid.split("-", 2)
    return parts[1] if len(parts) >= 2 else "unknown"


class InfluxWriter:
    """
    Buffers InfluxDB data points and flushes them asynchronously.

    Usage:
        writer = InfluxWriter(url, token, database, market, oracle_source)
        await writer.start()
        writer.write_state(...)   # non-blocking, queued
        await writer.stop()       # flushes remaining points
    """

    def __init__(
        self,
        url: str,
        token: str,
        database: str,
        market: str,
        oracle_source: str,
    ):
        self._url = url
        self._token = token
        self._database = database
        self._market = market
        self._oracle_source = oracle_source
        self._enabled = _INFLUX_AVAILABLE
        self._client: Optional[object] = None
        self._buffer: list = []
        self._flush_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        if not _INFLUX_AVAILABLE:
            logger.warning("influxdb3-python not installed — metrics disabled")

    async def start(self) -> None:
        if not self._enabled:
            return
        self._loop = asyncio.get_running_loop()
        self._client = InfluxDBClient3(
            host=self._url,
            token=self._token,
            database=self._database,
        )
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info(f"InfluxDB metrics enabled → {self._url} db={self._database}")

    async def query_last_cumulative_edge_pnl(self, market: str) -> float:
        """
        Query InfluxDB for the last written cumulative_edge_pnl for this market.
        Used on startup to resume from where the previous run left off.
        Returns 0.0 if no data found or InfluxDB is unavailable.
        """
        if not self._enabled:
            return 0.0
        sql = (
            f"SELECT cumulative_edge_pnl FROM mm_state "
            f"WHERE market = '{market}' "
            f"ORDER BY time DESC LIMIT 1"
        )
        try:
            loop = self._loop or asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._client.query(sql, language="sql"),
            )
            # result is a pyarrow Table
            if result is not None and result.num_rows > 0:
                value = result.column("cumulative_edge_pnl")[0].as_py()
                if value is not None:
                    logger.info(f"Resumed cumulative_edge_pnl from InfluxDB: {value:.6f}")
                    return float(value)
        except Exception as e:
            logger.warning(f"Could not restore cumulative_edge_pnl from InfluxDB: {e}")
        return 0.0

    async def stop(self) -> None:
        if not self._enabled:
            return
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_now()

    # ------------------------------------------------------------------
    # Internal flush machinery
    # ------------------------------------------------------------------

    async def query_last_cumulative_edge_pnl_by_quoter(self, market: str) -> dict:
        """
        Query InfluxDB for the last written per-quoter cumulative edge PnL fields.
        Returns a dict of {quoter_id: float}, e.g. {"1.0": 0.42, "10.0": 1.83}.
        Returns {} if no data found or InfluxDB is unavailable.
        """
        if not self._enabled:
            return {}
        sql = (
            f"SELECT * FROM mm_state "
            f"WHERE market = '{market}' "
            f"ORDER BY time DESC LIMIT 1"
        )
        try:
            loop = self._loop or asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._client.query(sql, language="sql"),
            )
            if result is None or result.num_rows == 0:
                return {}
            by_quoter: dict = {}
            for col in result.schema.names:
                if col.startswith("edge_pnl_q"):
                    raw_id = col[len("edge_pnl_q"):].replace("_", ".", 1)
                    val = result.column(col)[0].as_py()
                    if val is not None:
                        by_quoter[raw_id] = float(val)
            if by_quoter:
                logger.info(f"Resumed per-quoter edge PnL from InfluxDB: {by_quoter}")
            return by_quoter
        except Exception as e:
            logger.warning(f"Could not restore per-quoter edge PnL from InfluxDB: {e}")
            return {}

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            await self._flush_now()

    async def _flush_now(self) -> None:
        if not self._buffer:
            return
        points, self._buffer = self._buffer, []
        try:
            loop = self._loop or asyncio.get_running_loop()
            await loop.run_in_executor(None, self._write_sync, points)
        except Exception as e:
            logger.warning(f"InfluxDB write error: {e}")

    def _write_sync(self, points: list) -> None:
        self._client.write(record=points)

    def _enqueue(self, point: "Point") -> None:
        self._buffer.append(point)
        if len(self._buffer) >= 100:
            asyncio.ensure_future(self._flush_now())

    @staticmethod
    def _ts() -> int:
        return int(time.time() * 1_000_000_000)

    # ------------------------------------------------------------------
    # Public write methods
    # ------------------------------------------------------------------

    def write_state(
        self,
        reference_price: float,
        position: float,
        pnl: Optional[float],
        num_active_orders: int,
        num_cancels: int,
        num_new_orders: int,
        stop_bids: bool,
        stop_asks: bool,
        tps: float,
        best_bid: Optional[float],
        best_ask: Optional[float],
        order_prices: dict,
        cumulative_edge_pnl: float,
        cumulative_edge_pnl_by_quoter: dict,
    ) -> None:
        """Write mm_state measurement (one per main loop iteration)."""
        if not self._enabled:
            return
        p = (
            Point("mm_state")
            .tag("market", self._market)
            .tag("oracle_source", self._oracle_source)
            .field("reference_price", float(reference_price))
            .field("position", float(position))
            .field("num_active_orders", int(num_active_orders))
            .field("num_cancels", int(num_cancels))
            .field("num_new_orders", int(num_new_orders))
            .field("stop_bids", 1 if stop_bids else 0)
            .field("stop_asks", 1 if stop_asks else 0)
            .field("tps", float(tps))
            .field("cumulative_edge_pnl", float(cumulative_edge_pnl))
            .time(self._ts())
        )
        for qid, val in cumulative_edge_pnl_by_quoter.items():
            field_name = f"edge_pnl_q{qid.replace('.', '_')}"
            p = p.field(field_name, float(val))
        if pnl is not None:
            p = p.field("pnl", float(pnl))
        if best_bid is not None:
            p = p.field("best_bid", float(best_bid))
        if best_ask is not None:
            p = p.field("best_ask", float(best_ask))
        for name, price in order_prices.items():
            p = p.field(name, float(price))
        self._enqueue(p)

    def write_fill(
        self,
        side: str,
        price: float,
        oracle_price: float,
        realized_edge_bps: float,
        edge_pnl: float,
        filled_size: float,
        remaining_size: float,
        fill_type: str,
        quoter_id: str,
    ) -> None:
        """Write mm_fill measurement (one per fill callback)."""
        if not self._enabled:
            return
        p = (
            Point("mm_fill")
            .tag("market", self._market)
            .tag("side", side)
            .tag("quoter_id", quoter_id)
            .tag("fill_type", fill_type)
            .field("price", float(price))
            .field("oracle_price", float(oracle_price))
            .field("realized_edge_bps", float(realized_edge_bps))
            .field("edge_pnl", float(edge_pnl))
            .field("filled_size", float(filled_size))
            .field("remaining_size", float(remaining_size))
            .time(self._ts())
        )
        self._enqueue(p)

    def write_order(
        self,
        side: str,
        price: float,
        size: float,
        quoter_id: str,
        event: str,
    ) -> None:
        """Write mm_order measurement (placed or cancelled)."""
        if not self._enabled:
            return
        p = (
            Point("mm_order")
            .tag("market", self._market)
            .tag("side", side)
            .tag("quoter_id", quoter_id)
            .tag("event", event)
            .field("price", float(price))
            .field("size", float(size))
            .time(self._ts())
        )
        self._enqueue(p)

    def write_reconcile(
        self,
        tracked_position: float,
        drift: float,
        free_base: float,
        locked_base: float,
        free_quote: float,
        locked_quote: float,
        num_active_orders: int,
        block_number: int,
    ) -> None:
        """Write mm_reconcile measurement (one per reconcile cycle)."""
        if not self._enabled:
            return
        p = (
            Point("mm_reconcile")
            .tag("market", self._market)
            .field("tracked_position", float(tracked_position))
            .field("drift", float(drift))
            .field("free_base", float(free_base))
            .field("locked_base", float(locked_base))
            .field("free_quote", float(free_quote))
            .field("locked_quote", float(locked_quote))
            .field("num_active_orders", int(num_active_orders))
            .field("block_number", int(block_number))
            .time(self._ts())
        )
        self._enqueue(p)
