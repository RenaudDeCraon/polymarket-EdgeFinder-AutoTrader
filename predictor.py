"""
BTC Direction Predictor v2
==========================
Improved feature engineering + XGBoost for predicting BTC price direction.

Improvements over v1:
- Noise filter: only trains on moves > min_move_pct, skips coin-flip labels
- Multi-timeframe: 1m, 5m, 15m candle features
- Time features: hour of day, day of week, session (Asia/EU/US)
- Market-window features: distance to target and seconds left
- 20,000 klines for training (vs 5,000)
- Hyperparameter tuning via grid search
- Confidence calibration: "NO_TRADE" when model is uncertain
"""

import json
import math
import os
import pickle
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

try:
    import xgboost as xgb
    HAS_XGB = True
except ImportError:
    HAS_XGB = False

from price_feed import BinancePriceFeed, Kline


# ─── Minimum movement threshold ───
# Moves smaller than this (in %) are labeled as "SKIP" during training
MIN_MOVE_PCT = 0.03  # 0.03% of price ~ $22 on $74k BTC


@dataclass
class Prediction:
    timestamp: float
    direction_5m: str      # "UP", "DOWN", or "NO_TRADE"
    confidence_5m: float   # 0.0 - 1.0
    direction_15m: str
    confidence_15m: float
    current_price: float
    target_price: Optional[float] = None
    seconds_left: Optional[float] = None
    features: Dict[str, float] = field(default_factory=dict)

    @property
    def dt(self) -> str:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).strftime("%H:%M:%S")


class FeatureEngine:
    """Compute technical indicators from price series."""

    @staticmethod
    def sma(prices: np.ndarray, period: int) -> np.ndarray:
        if len(prices) < period:
            return np.full(len(prices), np.nan)
        result = np.full(len(prices), np.nan)
        cumsum = np.cumsum(prices)
        result[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
        return result

    @staticmethod
    def ema(prices: np.ndarray, period: int) -> np.ndarray:
        if len(prices) < period:
            return np.full(len(prices), np.nan)
        result = np.full(len(prices), np.nan)
        multiplier = 2.0 / (period + 1)
        result[period - 1] = np.mean(prices[:period])
        for i in range(period, len(prices)):
            result[i] = (prices[i] - result[i - 1]) * multiplier + result[i - 1]
        return result

    @staticmethod
    def rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
        if len(prices) < period + 1:
            return np.full(len(prices), np.nan)
        deltas = np.diff(prices)
        result = np.full(len(prices), np.nan)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])
        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            if avg_loss == 0:
                result[i + 1] = 100.0
            else:
                rs = avg_gain / avg_loss
                result[i + 1] = 100.0 - (100.0 / (1.0 + rs))
        return result

    @staticmethod
    def macd(prices: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
             ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        ema_fast = FeatureEngine.ema(prices, fast)
        ema_slow = FeatureEngine.ema(prices, slow)
        macd_line = ema_fast - ema_slow
        valid = macd_line[~np.isnan(macd_line)]
        signal_line = FeatureEngine.ema(valid, signal) if len(valid) >= signal else np.array([])
        full_signal = np.full(len(prices), np.nan)
        if len(signal_line) > 0:
            offset = len(prices) - len(signal_line)
            full_signal[offset:] = signal_line
        histogram = macd_line - full_signal
        return macd_line, full_signal, histogram

    @staticmethod
    def bollinger_bands(prices: np.ndarray, period: int = 20, std_dev: float = 2.0
                        ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        middle = FeatureEngine.sma(prices, period)
        upper = np.full(len(prices), np.nan)
        lower = np.full(len(prices), np.nan)
        for i in range(period - 1, len(prices)):
            std = np.std(prices[i - period + 1:i + 1])
            upper[i] = middle[i] + std_dev * std
            lower[i] = middle[i] - std_dev * std
        return upper, middle, lower

    @staticmethod
    def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
        if len(closes) < period + 1:
            return np.full(len(closes), np.nan)
        tr = np.full(len(closes), np.nan)
        tr[0] = highs[0] - lows[0]
        for i in range(1, len(closes)):
            tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        atr_vals = np.full(len(closes), np.nan)
        atr_vals[period] = np.mean(tr[1:period + 1])
        for i in range(period + 1, len(closes)):
            atr_vals[i] = (atr_vals[i - 1] * (period - 1) + tr[i]) / period
        return atr_vals

    @staticmethod
    def resample_klines(klines: List[Kline], factor: int) -> List[Kline]:
        """Resample 1m klines into larger timeframes (e.g., factor=5 → 5m candles)."""
        resampled = []
        for i in range(0, len(klines) - factor + 1, factor):
            chunk = klines[i:i + factor]
            resampled.append(Kline(
                open_time=chunk[0].open_time,
                open=chunk[0].open,
                high=max(k.high for k in chunk),
                low=min(k.low for k in chunk),
                close=chunk[-1].close,
                volume=sum(k.volume for k in chunk),
                close_time=chunk[-1].close_time,
                trades=sum(k.trades for k in chunk),
            ))
        return resampled

    @staticmethod
    def compute_features(
        klines: List[Kline],
        target_price: Optional[float] = None,
        seconds_left: Optional[float] = None,
    ) -> Optional[Dict[str, float]]:
        """Compute all features from klines. Returns features for the last bar."""
        if len(klines) < 60:
            return None

        closes = np.array([k.close for k in klines])
        highs = np.array([k.high for k in klines])
        lows = np.array([k.low for k in klines])
        volumes = np.array([k.volume for k in klines])
        opens = np.array([k.open for k in klines])

        # Pre-compute indicators
        rsi_14 = FeatureEngine.rsi(closes, 14)
        rsi_7 = FeatureEngine.rsi(closes, 7)
        macd_line, macd_signal, macd_hist = FeatureEngine.macd(closes)
        bb_upper, bb_middle, bb_lower = FeatureEngine.bollinger_bands(closes)
        atr_14 = FeatureEngine.atr(highs, lows, closes, 14)
        sma_5 = FeatureEngine.sma(closes, 5)
        sma_10 = FeatureEngine.sma(closes, 10)
        sma_20 = FeatureEngine.sma(closes, 20)
        sma_50 = FeatureEngine.sma(closes, 50)
        ema_9 = FeatureEngine.ema(closes, 9)
        ema_21 = FeatureEngine.ema(closes, 21)

        i = len(closes) - 1
        price = closes[i]

        def safe(val):
            return float(val) if not np.isnan(val) else 0.0

        features = {}

        # ── Core indicators ──
        features["rsi_14"] = safe(rsi_14[i])
        features["rsi_7"] = safe(rsi_7[i])
        features["macd"] = safe(macd_line[i])
        features["macd_signal"] = safe(macd_signal[i])
        features["macd_hist"] = safe(macd_hist[i])

        # Bollinger
        if not np.isnan(bb_upper[i]) and not np.isnan(bb_lower[i]) and bb_upper[i] != bb_lower[i]:
            features["bb_position"] = (price - bb_lower[i]) / (bb_upper[i] - bb_lower[i])
            features["bb_width"] = (bb_upper[i] - bb_lower[i]) / bb_middle[i]
        else:
            features["bb_position"] = 0.5
            features["bb_width"] = 0.0

        features["atr_pct"] = safe(atr_14[i]) / price * 100 if price > 0 else 0.0

        # MA crossovers
        features["sma_5_10_cross"] = (safe(sma_5[i]) - safe(sma_10[i])) / price * 100
        features["ema_9_21_cross"] = (safe(ema_9[i]) - safe(ema_21[i])) / price * 100
        features["price_vs_sma20"] = (price - safe(sma_20[i])) / price * 100
        features["price_vs_sma50"] = (price - safe(sma_50[i])) / price * 100 if not np.isnan(sma_50[i]) else 0.0

        # Rate of change
        for lb in [3, 5, 10, 15, 20, 30]:
            features[f"roc_{lb}"] = (price - closes[i - lb]) / closes[i - lb] * 100 if i >= lb else 0.0

        # Candle patterns
        body = closes[i] - opens[i]
        full_range = highs[i] - lows[i] if highs[i] != lows[i] else 0.0001
        features["candle_body_ratio"] = body / full_range
        features["upper_shadow"] = (highs[i] - max(opens[i], closes[i])) / full_range
        features["lower_shadow"] = (min(opens[i], closes[i]) - lows[i]) / full_range

        # Volume
        vol_sma = np.mean(volumes[max(0, i - 20):i + 1])
        features["volume_ratio"] = volumes[i] / vol_sma if vol_sma > 0 else 1.0
        if i >= 5:
            recent_vol = np.mean(volumes[i - 4:i + 1])
            older_vol = np.mean(volumes[max(0, i - 10):max(1, i - 4)])
            features["volume_trend"] = (recent_vol - older_vol) / older_vol if older_vol > 0 else 0.0
        else:
            features["volume_trend"] = 0.0

        # Volatility
        if i >= 20:
            returns = np.diff(closes[i - 20:i + 1]) / closes[i - 20:i]
            features["volatility_20"] = float(np.std(returns)) * 100
        else:
            features["volatility_20"] = 0.0

        # Range position
        if i >= 20:
            rh = np.max(highs[i - 20:i + 1])
            rl = np.min(lows[i - 20:i + 1])
            features["range_position"] = (price - rl) / (rh - rl) if rh != rl else 0.5
        else:
            features["range_position"] = 0.5

        # Consecutive direction
        cu = 0
        for j in range(i, max(i - 10, 0), -1):
            if closes[j] > closes[j - 1]:
                cu += 1
            else:
                break
        cd = 0
        for j in range(i, max(i - 10, 0), -1):
            if closes[j] < closes[j - 1]:
                cd += 1
            else:
                break
        features["consec_up"] = float(cu)
        features["consec_down"] = float(cd)

        if i >= 5:
            features["hl_ratio_5"] = (np.max(highs[i-4:i+1]) - np.min(lows[i-4:i+1])) / price * 100
        else:
            features["hl_ratio_5"] = 0.0

        # ── NEW: Time features ──
        last_kline = klines[-1]
        dt = datetime.fromtimestamp(last_kline.close_time, tz=timezone.utc)
        hour = dt.hour
        features["hour_sin"] = float(np.sin(2 * np.pi * hour / 24))
        features["hour_cos"] = float(np.cos(2 * np.pi * hour / 24))
        features["day_of_week"] = float(dt.weekday())
        # Trading session: 0=Asia(0-8 UTC), 1=Europe(8-14), 2=US(14-22), 3=late(22-24)
        if hour < 8:
            features["session"] = 0.0
        elif hour < 14:
            features["session"] = 1.0
        elif hour < 22:
            features["session"] = 2.0
        else:
            features["session"] = 3.0

        # ── NEW: Multi-timeframe (5m and 15m from 1m data) ──
        klines_5m = FeatureEngine.resample_klines(klines, 5)
        klines_15m = FeatureEngine.resample_klines(klines, 15)

        for label, kl in [("5m", klines_5m), ("15m", klines_15m)]:
            if len(kl) >= 20:
                c = np.array([k.close for k in kl])
                r14 = FeatureEngine.rsi(c, 14)
                features[f"rsi_14_{label}"] = safe(r14[-1])
                ml, ms, mh = FeatureEngine.macd(c)
                features[f"macd_hist_{label}"] = safe(mh[-1])
                s5 = FeatureEngine.sma(c, 5)
                s10 = FeatureEngine.sma(c, 10)
                features[f"sma_cross_{label}"] = (safe(s5[-1]) - safe(s10[-1])) / c[-1] * 100 if c[-1] > 0 else 0.0
                features[f"roc_5_{label}"] = (c[-1] - c[-6]) / c[-6] * 100 if len(c) > 5 else 0.0
            else:
                features[f"rsi_14_{label}"] = 50.0
                features[f"macd_hist_{label}"] = 0.0
                features[f"sma_cross_{label}"] = 0.0
                features[f"roc_5_{label}"] = 0.0

        # ── Market-window features ──
        if target_price and target_price > 0:
            features["distance_to_target_pct"] = (price - target_price) / target_price * 100
        else:
            features["distance_to_target_pct"] = 0.0
        features["seconds_left"] = float(seconds_left if seconds_left is not None else 0.0)

        # ── NEW: Momentum acceleration (2nd derivative) ──
        if i >= 10:
            roc_now = (closes[i] - closes[i - 5]) / closes[i - 5]
            roc_prev = (closes[i - 5] - closes[i - 10]) / closes[i - 10]
            features["momentum_accel"] = (roc_now - roc_prev) * 100
        else:
            features["momentum_accel"] = 0.0

        # ── NEW: Price vs VWAP-like ──
        if i >= 20:
            typical = (highs[i-19:i+1] + lows[i-19:i+1] + closes[i-19:i+1]) / 3
            vwap = np.sum(typical * volumes[i-19:i+1]) / np.sum(volumes[i-19:i+1]) if np.sum(volumes[i-19:i+1]) > 0 else price
            features["price_vs_vwap"] = (price - vwap) / price * 100
        else:
            features["price_vs_vwap"] = 0.0

        return features


# Feature list — must match compute_features output
FEATURE_NAMES = [
    "rsi_14", "rsi_7", "macd", "macd_signal", "macd_hist",
    "bb_position", "bb_width", "atr_pct",
    "sma_5_10_cross", "ema_9_21_cross", "price_vs_sma20", "price_vs_sma50",
    "roc_3", "roc_5", "roc_10", "roc_15", "roc_20", "roc_30",
    "candle_body_ratio", "upper_shadow", "lower_shadow",
    "volume_ratio", "volume_trend", "volatility_20",
    "range_position", "consec_up", "consec_down", "hl_ratio_5",
    # Time
    "hour_sin", "hour_cos", "day_of_week", "session",
    # Multi-timeframe
    "rsi_14_5m", "macd_hist_5m", "sma_cross_5m", "roc_5_5m",
    "rsi_14_15m", "macd_hist_15m", "sma_cross_15m", "roc_5_15m",
    # Market window
    "distance_to_target_pct", "seconds_left",
    # Advanced
    "momentum_accel", "price_vs_vwap",
]


class BTCPredictor:
    """XGBoost-based BTC direction predictor v2."""

    MODEL_DIR = Path(__file__).resolve().parent / "models"

    def __init__(self):
        self.model_5m: Optional[xgb.XGBClassifier] = None
        self.model_15m: Optional[xgb.XGBClassifier] = None
        self.is_trained = False
        self.training_accuracy_5m = 0.0
        self.training_accuracy_15m = 0.0
        self.training_samples = 0
        self.features_used = len(FEATURE_NAMES)
        self.prediction_history: Deque[Prediction] = deque(maxlen=500)
        self._lock = threading.Lock()

        self.MODEL_DIR.mkdir(exist_ok=True)

    def _prepare_training_data(
        self, klines: List[Kline], horizon_bars: int
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Build feature matrix X and label vector y with noise filtering."""
        if len(klines) < 60 + horizon_bars:
            return np.array([]), np.array([])

        closes = np.array([k.close for k in klines])
        highs = np.array([k.high for k in klines])
        lows = np.array([k.low for k in klines])
        volumes = np.array([k.volume for k in klines])
        opens = np.array([k.open for k in klines])
        n = len(closes)

        # Pre-compute all indicators once
        rsi_14 = FeatureEngine.rsi(closes, 14)
        rsi_7 = FeatureEngine.rsi(closes, 7)
        macd_line, macd_signal_arr, macd_hist = FeatureEngine.macd(closes)
        bb_upper, bb_middle, bb_lower = FeatureEngine.bollinger_bands(closes)
        atr_14 = FeatureEngine.atr(highs, lows, closes, 14)
        sma_5 = FeatureEngine.sma(closes, 5)
        sma_10 = FeatureEngine.sma(closes, 10)
        sma_20 = FeatureEngine.sma(closes, 20)
        sma_50 = FeatureEngine.sma(closes, 50)
        ema_9 = FeatureEngine.ema(closes, 9)
        ema_21 = FeatureEngine.ema(closes, 21)

        # Pre-compute multi-timeframe
        klines_5m = FeatureEngine.resample_klines(klines, 5)
        klines_15m = FeatureEngine.resample_klines(klines, 15)
        c5 = np.array([k.close for k in klines_5m]) if len(klines_5m) >= 20 else np.array([])
        c15 = np.array([k.close for k in klines_15m]) if len(klines_15m) >= 20 else np.array([])

        rsi_5m = FeatureEngine.rsi(c5, 14) if len(c5) >= 20 else np.array([])
        ml5, ms5, mh5 = FeatureEngine.macd(c5) if len(c5) >= 30 else (np.array([]), np.array([]), np.array([]))
        sma5_5m = FeatureEngine.sma(c5, 5) if len(c5) >= 10 else np.array([])
        sma10_5m = FeatureEngine.sma(c5, 10) if len(c5) >= 10 else np.array([])

        rsi_15m = FeatureEngine.rsi(c15, 14) if len(c15) >= 20 else np.array([])
        ml15, ms15, mh15 = FeatureEngine.macd(c15) if len(c15) >= 30 else (np.array([]), np.array([]), np.array([]))
        sma5_15m = FeatureEngine.sma(c15, 5) if len(c15) >= 10 else np.array([])
        sma10_15m = FeatureEngine.sma(c15, 10) if len(c15) >= 10 else np.array([])

        def safe(arr, idx):
            if len(arr) == 0 or idx < 0 or idx >= len(arr):
                return 0.0
            v = arr[idx]
            return float(v) if not np.isnan(v) else 0.0

        X_list = []
        y_list = []
        skipped_noise = 0

        for i in range(60, n - horizon_bars):
            price = closes[i]
            if price == 0:
                continue

            window_size = max(1, horizon_bars)
            window_start_i = (i // window_size) * window_size
            window_end_i = window_start_i + window_size - 1
            if window_end_i >= n or i >= window_end_i:
                continue

            target_price = klines[window_start_i].open
            future_price = klines[window_end_i].close
            move_pct = abs(future_price - target_price) / target_price * 100

            # NOISE FILTER: skip tiny moves that are basically random
            if move_pct < MIN_MOVE_PCT:
                skipped_noise += 1
                continue

            label = 1 if future_price > target_price else 0
            seconds_left = float((window_end_i - i + 1) * 60)

            # Time features
            dt = datetime.fromtimestamp(klines[i].close_time, tz=timezone.utc)
            hour = dt.hour

            # Map 1m index to 5m/15m index
            idx_5m = min(i // 5, len(c5) - 1) if len(c5) > 0 else -1
            idx_15m = min(i // 15, len(c15) - 1) if len(c15) > 0 else -1

            f = {}
            f["rsi_14"] = safe(rsi_14, i)
            f["rsi_7"] = safe(rsi_7, i)
            f["macd"] = safe(macd_line, i)
            f["macd_signal"] = safe(macd_signal_arr, i)
            f["macd_hist"] = safe(macd_hist, i)

            if not np.isnan(bb_upper[i]) and not np.isnan(bb_lower[i]) and bb_upper[i] != bb_lower[i]:
                f["bb_position"] = (price - bb_lower[i]) / (bb_upper[i] - bb_lower[i])
                f["bb_width"] = (bb_upper[i] - bb_lower[i]) / bb_middle[i]
            else:
                f["bb_position"] = 0.5
                f["bb_width"] = 0.0

            f["atr_pct"] = safe(atr_14, i) / price * 100
            f["sma_5_10_cross"] = (safe(sma_5, i) - safe(sma_10, i)) / price * 100
            f["ema_9_21_cross"] = (safe(ema_9, i) - safe(ema_21, i)) / price * 100
            f["price_vs_sma20"] = (price - safe(sma_20, i)) / price * 100
            f["price_vs_sma50"] = (price - safe(sma_50, i)) / price * 100

            for lb in [3, 5, 10, 15, 20, 30]:
                f[f"roc_{lb}"] = (price - closes[i - lb]) / closes[i - lb] * 100 if i >= lb else 0.0

            body = closes[i] - opens[i]
            full_range = highs[i] - lows[i] if highs[i] != lows[i] else 0.0001
            f["candle_body_ratio"] = body / full_range
            f["upper_shadow"] = (highs[i] - max(opens[i], closes[i])) / full_range
            f["lower_shadow"] = (min(opens[i], closes[i]) - lows[i]) / full_range

            vol_sma = np.mean(volumes[max(0, i - 20):i + 1])
            f["volume_ratio"] = volumes[i] / vol_sma if vol_sma > 0 else 1.0
            if i >= 10:
                recent_vol = np.mean(volumes[i - 4:i + 1])
                older_vol = np.mean(volumes[max(0, i - 10):max(1, i - 4)])
                f["volume_trend"] = (recent_vol - older_vol) / older_vol if older_vol > 0 else 0.0
            else:
                f["volume_trend"] = 0.0

            if i >= 20:
                rets = np.diff(closes[i - 20:i + 1]) / closes[i - 20:i]
                f["volatility_20"] = float(np.std(rets)) * 100
            else:
                f["volatility_20"] = 0.0

            if i >= 20:
                rh = np.max(highs[i - 20:i + 1])
                rl = np.min(lows[i - 20:i + 1])
                f["range_position"] = (price - rl) / (rh - rl) if rh != rl else 0.5
            else:
                f["range_position"] = 0.5

            cu = 0
            for j in range(i, max(i - 10, 0), -1):
                if closes[j] > closes[j - 1]:
                    cu += 1
                else:
                    break
            cd = 0
            for j in range(i, max(i - 10, 0), -1):
                if closes[j] < closes[j - 1]:
                    cd += 1
                else:
                    break
            f["consec_up"] = float(cu)
            f["consec_down"] = float(cd)

            if i >= 5:
                f["hl_ratio_5"] = (np.max(highs[i-4:i+1]) - np.min(lows[i-4:i+1])) / price * 100
            else:
                f["hl_ratio_5"] = 0.0

            # Time
            f["hour_sin"] = float(np.sin(2 * np.pi * hour / 24))
            f["hour_cos"] = float(np.cos(2 * np.pi * hour / 24))
            f["day_of_week"] = float(dt.weekday())
            if hour < 8:
                f["session"] = 0.0
            elif hour < 14:
                f["session"] = 1.0
            elif hour < 22:
                f["session"] = 2.0
            else:
                f["session"] = 3.0

            # Multi-timeframe
            f["rsi_14_5m"] = safe(rsi_5m, idx_5m)
            f["macd_hist_5m"] = safe(mh5, idx_5m)
            f["sma_cross_5m"] = (safe(sma5_5m, idx_5m) - safe(sma10_5m, idx_5m)) / price * 100 if idx_5m >= 0 else 0.0
            f["roc_5_5m"] = (safe(c5, idx_5m) - safe(c5, idx_5m - 5)) / safe(c5, idx_5m - 5) * 100 if idx_5m >= 5 and safe(c5, idx_5m - 5) > 0 else 0.0

            f["rsi_14_15m"] = safe(rsi_15m, idx_15m)
            f["macd_hist_15m"] = safe(mh15, idx_15m)
            f["sma_cross_15m"] = (safe(sma5_15m, idx_15m) - safe(sma10_15m, idx_15m)) / price * 100 if idx_15m >= 0 else 0.0
            f["roc_5_15m"] = (safe(c15, idx_15m) - safe(c15, idx_15m - 5)) / safe(c15, idx_15m - 5) * 100 if idx_15m >= 5 and safe(c15, idx_15m - 5) > 0 else 0.0

            # Market-window contract features. These make the label match the
            # up/down contract: final price above/below the window target.
            f["distance_to_target_pct"] = (price - target_price) / target_price * 100
            f["seconds_left"] = seconds_left

            # Momentum acceleration
            if i >= 10:
                roc_now = (closes[i] - closes[i - 5]) / closes[i - 5]
                roc_prev = (closes[i - 5] - closes[i - 10]) / closes[i - 10]
                f["momentum_accel"] = (roc_now - roc_prev) * 100
            else:
                f["momentum_accel"] = 0.0

            # VWAP
            if i >= 20:
                typical = (highs[i-19:i+1] + lows[i-19:i+1] + closes[i-19:i+1]) / 3
                vol_sum = np.sum(volumes[i-19:i+1])
                vwap = np.sum(typical * volumes[i-19:i+1]) / vol_sum if vol_sum > 0 else price
                f["price_vs_vwap"] = (price - vwap) / price * 100
            else:
                f["price_vs_vwap"] = 0.0

            row = [f.get(name, 0.0) for name in FEATURE_NAMES]
            X_list.append(row)
            y_list.append(label)

        print(f"  Noise filter: skipped {skipped_noise} tiny-move samples ({MIN_MOVE_PCT}% threshold)")
        return np.array(X_list), np.array(y_list)

    def train(self, feed: BinancePriceFeed, retrain: bool = False) -> bool:
        """Train models on historical Binance data."""
        if not HAS_XGB:
            print("ERROR: xgboost not installed. Run: pip install xgboost")
            return False

        model_5m_path = self.MODEL_DIR / "model_5m_v3.pkl"
        model_15m_path = self.MODEL_DIR / "model_15m_v3.pkl"

        if not retrain and model_5m_path.exists() and model_15m_path.exists():
            try:
                with open(model_5m_path, "rb") as f:
                    self.model_5m = pickle.load(f)
                with open(model_15m_path, "rb") as f:
                    self.model_15m = pickle.load(f)
                self.is_trained = True
                print("Loaded pre-trained v3 models from disk.")
                return True
            except Exception as e:
                print(f"Failed to load models: {e}. Retraining...")

        print("Fetching historical data for training (20,000 klines)...")
        klines = feed.fetch_bulk_klines(interval="1m", total=20000)
        if len(klines) < 500:
            print(f"Not enough data: {len(klines)} klines (need 500+)")
            return False

        print(f"Got {len(klines)} klines. Building features with noise filter...")

        # 5-minute model
        X_5m, y_5m = self._prepare_training_data(klines, horizon_bars=5)
        X_15m, y_15m = self._prepare_training_data(klines, horizon_bars=15)

        if len(X_5m) < 200 or len(X_15m) < 200:
            print(f"Not enough samples after noise filter: 5m={len(X_5m)}, 15m={len(X_15m)}")
            return False

        split_5m = int(len(X_5m) * 0.8)
        split_15m = int(len(X_15m) * 0.8)

        # Hyperparameter candidates
        param_grid = [
            {"n_estimators": 300, "max_depth": 5, "learning_rate": 0.03, "subsample": 0.8, "colsample_bytree": 0.7, "min_child_weight": 5},
            {"n_estimators": 200, "max_depth": 6, "learning_rate": 0.05, "subsample": 0.85, "colsample_bytree": 0.8, "min_child_weight": 3},
            {"n_estimators": 400, "max_depth": 4, "learning_rate": 0.02, "subsample": 0.75, "colsample_bytree": 0.6, "min_child_weight": 7},
        ]

        # Train 5m model with best params
        print(f"Training 5m model on {split_5m} samples ({len(FEATURE_NAMES)} features)...")
        best_acc_5m = 0
        for params in param_grid:
            model = xgb.XGBClassifier(
                **params,
                reg_alpha=0.1,
                reg_lambda=1.0,
                use_label_encoder=False,
                eval_metric="logloss",
                verbosity=0,
            )
            model.fit(X_5m[:split_5m], y_5m[:split_5m])
            preds = model.predict(X_5m[split_5m:])
            acc = float(np.mean(preds == y_5m[split_5m:]))
            print(f"  5m params depth={params['max_depth']} lr={params['learning_rate']} → {acc:.1%}")
            if acc > best_acc_5m:
                best_acc_5m = acc
                self.model_5m = model

        self.training_accuracy_5m = best_acc_5m

        # Train 15m model
        print(f"Training 15m model on {split_15m} samples...")
        best_acc_15m = 0
        for params in param_grid:
            model = xgb.XGBClassifier(
                **params,
                reg_alpha=0.1,
                reg_lambda=1.0,
                use_label_encoder=False,
                eval_metric="logloss",
                verbosity=0,
            )
            model.fit(X_15m[:split_15m], y_15m[:split_15m])
            preds = model.predict(X_15m[split_15m:])
            acc = float(np.mean(preds == y_15m[split_15m:]))
            print(f"  15m params depth={params['max_depth']} lr={params['learning_rate']} → {acc:.1%}")
            if acc > best_acc_15m:
                best_acc_15m = acc
                self.model_15m = model

        self.training_accuracy_15m = best_acc_15m
        self.training_samples = len(X_5m)
        self.is_trained = True

        # Save
        with open(model_5m_path, "wb") as f:
            pickle.dump(self.model_5m, f)
        with open(model_15m_path, "wb") as f:
            pickle.dump(self.model_15m, f)

        print(f"\nTraining complete!")
        print(f"  5m accuracy: {self.training_accuracy_5m:.1%} (best of {len(param_grid)} configs)")
        print(f"  15m accuracy: {self.training_accuracy_15m:.1%}")
        print(f"  Features: {len(FEATURE_NAMES)}")
        print(f"  Training samples: {self.training_samples} (after noise filter)")

        return True

    def predict(
        self,
        klines: List[Kline],
        current_price: float,
        target_price: Optional[float] = None,
        seconds_left: Optional[float] = None,
    ) -> Optional[Prediction]:
        """Make a prediction from recent klines."""
        if not self.is_trained or self.model_5m is None or self.model_15m is None:
            return None

        features = FeatureEngine.compute_features(
            klines,
            target_price=target_price,
            seconds_left=seconds_left,
        )
        if features is None:
            return None

        row = np.array([[features.get(name, 0.0) for name in FEATURE_NAMES]])

        with self._lock:
            prob_5m = self.model_5m.predict_proba(row)[0]
            prob_15m = self.model_15m.predict_proba(row)[0]

        # Direction + confidence
        conf_5m = float(max(prob_5m[0], prob_5m[1]))
        conf_15m = float(max(prob_15m[0], prob_15m[1]))

        # NO_TRADE if confidence is too low (model is uncertain)
        min_conf = 0.55
        if conf_5m < min_conf:
            dir_5m = "NO_TRADE"
        else:
            dir_5m = "UP" if prob_5m[1] > 0.5 else "DOWN"

        if conf_15m < min_conf:
            dir_15m = "NO_TRADE"
        else:
            dir_15m = "UP" if prob_15m[1] > 0.5 else "DOWN"

        pred = Prediction(
            timestamp=time.time(),
            direction_5m=dir_5m,
            confidence_5m=round(conf_5m, 4),
            direction_15m=dir_15m,
            confidence_15m=round(conf_15m, 4),
            current_price=float(current_price),
            target_price=float(target_price) if target_price else None,
            seconds_left=float(seconds_left) if seconds_left is not None else None,
            features={k: float(v) for k, v in features.items()},
        )

        self.prediction_history.append(pred)
        return pred

    def get_feature_importance(self) -> Dict[str, float]:
        if not self.model_5m:
            return {}
        importances = self.model_5m.feature_importances_
        return {
            name: round(float(imp), 4)
            for name, imp in sorted(
                zip(FEATURE_NAMES, importances),
                key=lambda x: x[1],
                reverse=True,
            )
        }

    def get_recent_accuracy(self, klines: List[Kline]) -> Dict[str, float]:
        if not self.prediction_history or not klines:
            return {"5m": 0.0, "15m": 0.0, "total_checked": 0}

        now = time.time()
        correct_5m = total_5m = correct_15m = total_15m = 0

        for pred in self.prediction_history:
            if pred.direction_5m == "NO_TRADE":
                continue
            age = now - pred.timestamp
            if age >= 300:
                target_ts = pred.timestamp + 300
                closest = min(klines, key=lambda k: abs(k.close_time - target_ts))
                if abs(closest.close_time - target_ts) < 120:
                    actual = "UP" if closest.close > pred.current_price else "DOWN"
                    if actual == pred.direction_5m:
                        correct_5m += 1
                    total_5m += 1

            if pred.direction_15m == "NO_TRADE":
                continue
            if age >= 900:
                target_ts = pred.timestamp + 900
                closest = min(klines, key=lambda k: abs(k.close_time - target_ts))
                if abs(closest.close_time - target_ts) < 120:
                    actual = "UP" if closest.close > pred.current_price else "DOWN"
                    if actual == pred.direction_15m:
                        correct_15m += 1
                    total_15m += 1

        return {
            "5m": round(correct_5m / total_5m, 4) if total_5m > 0 else 0.0,
            "15m": round(correct_15m / total_15m, 4) if total_15m > 0 else 0.0,
            "total_5m_checked": total_5m,
            "total_15m_checked": total_15m,
        }
