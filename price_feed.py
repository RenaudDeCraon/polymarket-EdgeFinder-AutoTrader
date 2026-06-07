"""
Binance WebSocket Price Feed
============================
Real-time BTC/USDT price via Binance WebSocket.
Also fetches historical klines for model training.
"""

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, List, Optional

import requests
import websocket


@dataclass
class PricePoint:
    timestamp: float
    price: float
    volume: float = 0.0

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)


@dataclass
class Kline:
    open_time: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: float
    trades: int = 0


class BinancePriceFeed:
    """Real-time BTC/USDT price from Binance WebSocket + historical kline fetcher."""

    WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    REST_URL = "https://api.binance.com/api/v3"

    def __init__(self, max_history: int = 10000):
        self.max_history = max_history
        self.price_history: Deque[PricePoint] = deque(maxlen=max_history)
        self.current_price: Optional[float] = None
        self.current_volume: float = 0.0
        self.last_update: float = 0.0
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._callbacks: List[Callable[[PricePoint], None]] = []
        self._lock = threading.Lock()
        self._connected = False
        self._reconnect_count = 0

    @property
    def connected(self) -> bool:
        return self._connected and self._running

    def on_price(self, callback: Callable[[PricePoint], None]):
        """Register a callback for each new price tick."""
        self._callbacks.append(callback)

    def start(self):
        """Start WebSocket connection in background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop WebSocket connection."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _run_ws(self):
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                pass

            if self._running:
                self._connected = False
                self._reconnect_count += 1
                wait = min(30, 2 ** min(self._reconnect_count, 5))
                time.sleep(wait)

    def _on_open(self, ws):
        self._connected = True
        self._reconnect_count = 0

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            price = float(data["p"])
            volume = float(data["q"])
            ts = data["T"] / 1000.0

            point = PricePoint(timestamp=ts, price=price, volume=volume)

            with self._lock:
                self.current_price = price
                self.current_volume = volume
                self.last_update = ts
                self.price_history.append(point)

            for cb in self._callbacks:
                try:
                    cb(point)
                except Exception:
                    pass
        except Exception:
            pass

    def _on_error(self, ws, error):
        self._connected = False

    def _on_close(self, ws, close_status_code=None, close_msg=None):
        self._connected = False

    def get_recent_prices(self, seconds: int = 300) -> List[PricePoint]:
        """Get price points from the last N seconds."""
        cutoff = time.time() - seconds
        with self._lock:
            return [p for p in self.price_history if p.timestamp >= cutoff]

    def get_price_at_intervals(self, interval_seconds: int = 1, count: int = 300) -> List[float]:
        """Get price sampled at regular intervals (most recent first)."""
        now = time.time()
        with self._lock:
            prices = list(self.price_history)

        if not prices:
            return []

        result = []
        for i in range(count):
            target_ts = now - i * interval_seconds
            closest = min(prices, key=lambda p: abs(p.timestamp - target_ts))
            if abs(closest.timestamp - target_ts) < interval_seconds * 2:
                result.append(closest.price)
            else:
                break

        result.reverse()
        return result

    def fetch_historical_klines(
        self, interval: str = "1m", limit: int = 1000, end_time: Optional[int] = None
    ) -> List[Kline]:
        """Fetch historical klines from Binance REST API for model training."""
        params = {"symbol": "BTCUSDT", "interval": interval, "limit": limit}
        if end_time:
            params["endTime"] = end_time

        try:
            resp = requests.get(f"{self.REST_URL}/klines", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            klines = []
            for k in data:
                klines.append(Kline(
                    open_time=k[0] / 1000.0,
                    open=float(k[1]),
                    high=float(k[2]),
                    low=float(k[3]),
                    close=float(k[4]),
                    volume=float(k[5]),
                    close_time=k[6] / 1000.0,
                    trades=int(k[8]),
                ))
            return klines
        except Exception as e:
            print(f"Error fetching klines: {e}")
            return []

    def fetch_bulk_klines(self, interval: str = "1m", total: int = 5000) -> List[Kline]:
        """Fetch multiple batches of klines for training data."""
        all_klines: List[Kline] = []
        end_time = None
        remaining = total

        while remaining > 0:
            batch_size = min(1000, remaining)
            klines = self.fetch_historical_klines(
                interval=interval, limit=batch_size, end_time=end_time
            )
            if not klines:
                break

            all_klines = klines + all_klines
            end_time = int(klines[0].open_time * 1000) - 1
            remaining -= len(klines)
            time.sleep(0.2)  # rate limit

        return all_klines
