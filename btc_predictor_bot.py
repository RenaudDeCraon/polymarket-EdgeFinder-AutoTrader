"""
BTC Predictor Bot — Main Orchestrator
======================================
Ties together: Binance price feed → XGBoost predictor → Polymarket trader → Web dashboard.

Usage:
  Paper mode (default):
    python btc_predictor_bot.py

  Live mode:
    python btc_predictor_bot.py --live

  Retrain model:
    python btc_predictor_bot.py --retrain

  Custom settings:
    python btc_predictor_bot.py --bet 3.0 --min-confidence 0.65 --port 5050

  Dashboard only (no trading):
    python btc_predictor_bot.py --no-trade
"""

import argparse
import json
import logging
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional

from price_feed import BinancePriceFeed, Kline, PricePoint
from predictor import BTCPredictor, Prediction
from trader import PolymarketTrader, TraderConfig, TradeRecord
from dashboard import create_dashboard_app, run_dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("btc-bot")

ROOT = Path(__file__).resolve().parent
INTERRUPTED = False
SETTLEMENT_FALLBACK_DELAY = 120
PAPER_SETTLEMENT_FALLBACK_DELAY = 30
MODEL_CONFIDENCE_CAP = 0.85
INFERRED_TARGET_CONFIDENCE_CAP = 0.74


def signal_handler(signum, frame):
    global INTERRUPTED
    INTERRUPTED = True
    log.info("Shutting down...")


class BTCPredictorBot:
    """Main bot that orchestrates everything."""

    def __init__(self, args):
        self.args = args

        # Components
        self.feed = BinancePriceFeed(max_history=20000)
        self.predictor = BTCPredictor()
        self.trader = PolymarketTrader(TraderConfig(
            bet_size=args.bet,
            min_confidence=args.min_confidence,
            high_confidence=args.high_confidence,
            paper_mode=not args.live,
            session_stop_loss=args.stop_loss,
            session_take_profit=args.take_profit,
        ))

        # State
        self.kline_buffer: List[Kline] = []
        self.latest_prediction: Optional[Prediction] = None
        self.prediction_interval = args.predict_interval  # seconds between predictions
        self.last_prediction_time = 0.0
        self.last_trade_window: Optional[int] = None
        self.last_target_warning_window: Optional[int] = None
        self.price_for_chart: Deque = deque(maxlen=2000)
        self.current_market: Optional[Dict] = None
        self.nearby_markets: List[Dict] = []
        self._market_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def get_dashboard_state(self) -> Dict:
        """Returns full state for the dashboard API."""
        with self._lock:
            pred_dict = None
            if self.latest_prediction:
                p = self.latest_prediction
                pred_dict = {
                    "direction_5m": p.direction_5m,
                    "confidence_5m": float(p.confidence_5m),
                    "direction_15m": p.direction_15m,
                    "confidence_15m": float(p.confidence_15m),
                    "current_price": float(p.current_price),
                    "target_price": float(p.target_price) if p.target_price else None,
                    "seconds_left": float(p.seconds_left) if p.seconds_left is not None else None,
                }

            pred_history = [
                {
                    "timestamp": float(p.timestamp),
                    "current_price": float(p.current_price),
                    "direction_5m": p.direction_5m,
                    "confidence_5m": float(p.confidence_5m),
                    "direction_15m": p.direction_15m,
                    "confidence_15m": float(p.confidence_15m),
                    "target_price": float(p.target_price) if p.target_price else None,
                    "seconds_left": float(p.seconds_left) if p.seconds_left is not None else None,
                }
                for p in list(self.predictor.prediction_history)[-50:]
            ]

            trades = [
                {
                    "timestamp": float(t.timestamp),
                    "window_ts": int(t.window_ts),
                    "side": t.side,
                    "price": float(t.price),
                    "shares": float(t.shares),
                    "cost": float(t.cost),
                    "prediction_confidence": float(t.prediction_confidence),
                    "is_live": t.is_live,
                    "outcome": t.outcome,
                    "pnl": float(t.pnl) if t.pnl is not None else None,
                    "market_slug": t.market_slug,
                    "market_question": t.market_question,
                    "target_price": float(t.target_price) if t.target_price else None,
                    "target_source": t.target_source,
                    "polymarket_current_price": (
                        float(t.polymarket_current_price)
                        if t.polymarket_current_price is not None else None
                    ),
                    "entry_btc_price": float(t.entry_btc_price) if t.entry_btc_price else None,
                    "settlement_source": t.settlement_source,
                }
                for t in self.trader.trades[-50:]
            ]

            features = {}
            if self.latest_prediction and self.latest_prediction.features:
                features = {k: round(float(v), 4) for k, v in self.latest_prediction.features.items()}

            # Accuracy tracking
            accuracy = {"5m": 0.0, "15m": 0.0}
            if self.kline_buffer:
                accuracy = self.predictor.get_recent_accuracy(self.kline_buffer)

            # Price history for chart (sampled)
            price_hist = list(self.price_for_chart)
            markets = [
                {
                    "slug": m.get("slug"),
                    "window_ts": int(m.get("window_ts", 0)),
                    "window_end": int(m.get("window_end", 0)),
                    "question": m.get("question", ""),
                    "target_price": float(m["target_price"]) if m.get("target_price") else None,
                    "target_source": m.get("target_source", "unknown"),
                    "polymarket_current_price": (
                        float(m["polymarket_current_price"])
                        if m.get("polymarket_current_price") is not None else None
                    ),
                    "binance_price_gap": (
                        float(self.feed.current_price - m["polymarket_current_price"])
                        if self.feed.current_price is not None and m.get("polymarket_current_price") is not None
                        else None
                    ),
                    "up_price": float(m["prices"]["UP"]) if m.get("prices") and "UP" in m["prices"] else None,
                    "down_price": float(m["prices"]["DOWN"]) if m.get("prices") and "DOWN" in m["prices"] else None,
                }
                for m in self.nearby_markets
            ]

            return {
                "connected": self.feed.connected,
                "current_price": self.feed.current_price,
                "prediction": pred_dict,
                "prediction_history": pred_history,
                "stats": self.trader.get_stats(),
                "trades": trades,
                "features": features,
                "accuracy": accuracy,
                "price_history": price_hist,
                "paper_mode": self.trader.config.paper_mode,
                "model_trained": self.predictor.is_trained,
                "training_accuracy_5m": float(self.predictor.training_accuracy_5m),
                "training_accuracy_15m": float(self.predictor.training_accuracy_15m),
                "markets": markets,
            }

    def _on_price(self, point: PricePoint):
        """Called for each new price tick from Binance."""
        with self._lock:
            self.price_for_chart.append((point.timestamp, point.price))

    def _refresh_klines(self):
        """Fetch recent 1-min klines for prediction."""
        try:
            klines = self.feed.fetch_historical_klines(interval="1m", limit=100)
            if klines:
                self.kline_buffer = klines
        except Exception as e:
            log.error(f"Kline refresh error: {e}")

    def _make_prediction(self):
        """Run the predictor on current data."""
        if not self.predictor.is_trained:
            return

        if not self.kline_buffer or not self.feed.current_price:
            return

        now = time.time()
        if now - self.last_prediction_time < self.prediction_interval:
            return

        market = self.current_market
        target_price = market.get("target_price") if market else None
        seconds_left = max(0.0, market["window_end"] - now) if market else None

        pred = self.predictor.predict(
            self.kline_buffer, self.feed.current_price,
            target_price=target_price,
            seconds_left=seconds_left,
        )
        if pred:
            cap = MODEL_CONFIDENCE_CAP
            if not market or market.get("target_source") != "polymarket":
                cap = INFERRED_TARGET_CONFIDENCE_CAP
            pred.confidence_5m = min(pred.confidence_5m, cap)
            pred.confidence_15m = min(pred.confidence_15m, cap)

            with self._lock:
                self.latest_prediction = pred
            self.last_prediction_time = now

            arrow_5 = "▲" if pred.direction_5m == "UP" else ("▼" if pred.direction_5m == "DOWN" else "—")
            arrow_15 = "▲" if pred.direction_15m == "UP" else ("▼" if pred.direction_15m == "DOWN" else "—")
            target_msg = f"target=${target_price:,.2f}" if target_price else "target=unknown"
            log.info(
                f"PREDICTION │ ${pred.current_price:,.2f} │ "
                f"5m: {arrow_5} {pred.direction_5m} ({pred.confidence_5m:.1%}) │ "
                f"15m: {arrow_15} {pred.direction_15m} ({pred.confidence_15m:.1%}) │ "
                f"{target_msg}"
            )

    def _refresh_markets(self) -> Optional[Dict]:
        """Fetch current/nearby market windows and attach target/price info."""
        try:
            markets = self.trader.find_nearby_markets(count=3)
            for market in markets:
                if not market.get("target_price"):
                    market["target_price"] = self._infer_market_target(market["window_ts"])
                    market["target_source"] = "binance_inferred" if market.get("target_price") else "unknown"
                prices = self.trader.get_market_prices(market)
                if prices:
                    market["prices"] = prices
            with self._lock:
                self.nearby_markets = markets
                self.current_market = markets[0] if markets else None
            return markets[0] if markets else None
        except Exception as e:
            log.debug(f"Market refresh failed: {e}")
            return None

    def _start_market_refresh(self):
        """Refresh Polymarket metadata in the background so Binance ticks stay fast."""
        if self._market_thread and self._market_thread.is_alive():
            return

        def loop():
            while not INTERRUPTED:
                self._refresh_markets()
                time.sleep(self.args.market_refresh_interval)

        self._market_thread = threading.Thread(target=loop, daemon=True)
        self._market_thread.start()

    def _infer_market_target(self, window_ts: int) -> Optional[float]:
        """Infer the price-to-beat from Binance's 1m candle at market open."""
        for k in self.kline_buffer:
            if abs(k.open_time - window_ts) < 1:
                return k.open
        try:
            klines = self.feed.fetch_historical_klines(
                interval="1m",
                limit=1,
                end_time=int((window_ts + 60) * 1000),
            )
            if klines:
                return klines[-1].open
        except Exception:
            pass
        if self.feed.current_price and abs(time.time() - window_ts) <= 15:
            return self.feed.current_price
        return None

    def _maybe_trade(self):
        """Check if we should place a trade based on latest prediction."""
        if self.args.no_trade:
            return

        if not self.latest_prediction:
            return

        now = time.time()
        window_ts = int(now) - (int(now) % 300)

        # Resolve pending trades from previous windows. Prefer Polymarket's
        # actual resolved winner; only fall back to Binance-vs-target if the
        # market has not exposed a resolved outcome yet.
        for trade in self.trader.get_pending_trades():
            if trade.window_ts < window_ts:
                trade_end = trade.window_ts + 300
                if now >= trade_end:
                    winner = self._determine_window_winner(trade)
                    if winner:
                        self.trader.resolve_trade(trade, winner)
                        result = "WIN" if trade.outcome == "WIN" else "LOSS"
                        log.info(
                            f"RESOLVED │ {trade.side} @ ${trade.cost:.2f} → {result} "
                            f"PnL=${trade.pnl:+.2f} │ Total=${self.trader.total_pnl:+.2f} "
                            f"│ Source={trade.settlement_source}"
                        )

        # Only trade once per window. Keep this after settlement so older paper
        # trades do not get stuck while the bot waits for the next window.
        if window_ts == self.last_trade_window:
            return

        pred = self.latest_prediction
        market = self.current_market or self._refresh_markets()
        if not market:
            return
        if not market.get("target_price"):
            return
        inferred_blocked = (
            market.get("target_source") != "polymarket"
            and self.args.live
            and not self.args.allow_inferred_target
        )
        if inferred_blocked:
            if self.last_target_warning_window != window_ts:
                log.warning(
                    "Skipping LIVE trade: Polymarket target unavailable; refusing Binance-inferred target. "
                    "Use --allow-inferred-target only if you accept this settlement risk."
                )
                self.last_target_warning_window = window_ts
            return

        # Use 5m prediction for Polymarket 5-min markets
        side = pred.direction_5m
        confidence = pred.confidence_5m

        # Skip if model says NO_TRADE (too uncertain)
        if side == "NO_TRADE":
            return

        trade = self.trader.execute_trade(
            market,
            side,
            confidence,
            entry_btc_price=self.feed.current_price,
        )
        if trade:
            self.last_trade_window = window_ts
            mode = "LIVE" if trade.is_live else "PAPER"
            log.info(
                f"TRADE [{mode}] │ {trade.side} {trade.shares:.0f}sh @ ${trade.price:.3f} "
                f"= ${trade.cost:.2f} │ Confidence: {trade.prediction_confidence:.1%}"
            )

    def _determine_window_winner(self, trade: TradeRecord) -> Optional[str]:
        """Determine if BTC went UP or DOWN during a 5-min window."""
        if trade.market_slug:
            winner = self.trader.get_resolved_winner(trade.market_slug)
            if winner:
                trade.settlement_source = "polymarket"
                return winner

        window_end = trade.window_ts + 300
        fallback_delay = (
            PAPER_SETTLEMENT_FALLBACK_DELAY
            if self.trader.config.paper_mode
            else SETTLEMENT_FALLBACK_DELAY
        )
        if time.time() < window_end + fallback_delay:
            return None

        target_price = trade.target_price
        end_price = None

        for k in self.kline_buffer:
            if abs(k.close_time - window_end) < 60:
                end_price = k.close

        if end_price is None:
            try:
                klines = self.feed.fetch_historical_klines(
                    interval="1m",
                    limit=1,
                    end_time=int(window_end * 1000),
                )
                if klines:
                    end_price = klines[-1].close
            except Exception:
                pass

        if target_price and end_price:
            trade.settlement_source = "binance_target_fallback"
            return "UP" if end_price >= target_price else "DOWN"

        return None

    def run(self):
        """Main loop."""
        global INTERRUPTED

        log.info("=" * 70)
        log.info("  BTC PREDICTOR BOT")
        log.info(f"  Mode: {'LIVE' if self.args.live else 'PAPER'}")
        log.info(f"  Bet: ${self.args.bet:.2f} │ Min confidence: {self.args.min_confidence:.0%}")
        log.info(f"  Dashboard: http://localhost:{self.args.port}")
        log.info("=" * 70)

        if self.args.live and not self.args.confirm_live:
            log.error("Live mode requires --confirm-live. Refusing to place real orders.")
            return

        # 1. Start price feed
        log.info("Starting Binance WebSocket price feed...")
        self.feed.on_price(self._on_price)
        self.feed.start()

        # Wait for connection + first price
        for _ in range(60):
            if self.feed.connected and self.feed.current_price is not None:
                break
            time.sleep(0.5)

        if not self.feed.connected or self.feed.current_price is None:
            log.error("Failed to connect to Binance WebSocket. Check your internet connection.")
            return

        log.info(f"Connected! Current BTC price: ${self.feed.current_price:,.2f}")

        # 2. Start dashboard IMMEDIATELY so user can see progress
        log.info(f"Starting dashboard on port {self.args.port}...")
        app = create_dashboard_app(self.get_dashboard_state)
        run_dashboard(app, host='127.0.0.1', port=self.args.port)
        log.info(f"Dashboard running at http://localhost:{self.args.port}")

        # Background Polymarket metadata refresh. This keeps Binance price/chart
        # updates responsive while still collecting target/odds/order data.
        self._refresh_markets()
        self._start_market_refresh()

        # 3. Train predictor (dashboard is already visible)
        log.info("Training prediction model...")
        if not self.predictor.train(self.feed, retrain=self.args.retrain):
            log.error("Model training failed.")
            return

        # 4. Connect live trader if requested
        if self.args.live:
            log.info("Connecting to Polymarket for live trading...")
            if not self.trader.connect_live():
                log.error("Live connection failed. Falling back to paper mode.")
                self.trader.config.paper_mode = True

        # 5. Main loop
        log.info("Bot running. Press Ctrl+C to stop.\n")
        kline_refresh_interval = 30  # refresh klines every 30s
        last_kline_refresh = 0

        while not INTERRUPTED:
            try:
                now = time.time()

                # Refresh klines periodically
                if now - last_kline_refresh >= kline_refresh_interval:
                    self._refresh_klines()
                    last_kline_refresh = now

                # Make prediction
                self._make_prediction()

                # Maybe trade
                self._maybe_trade()

                time.sleep(1)

            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"Loop error: {e}")
                time.sleep(5)

        # Shutdown
        log.info("Stopping...")
        self.feed.stop()

        # Save session results
        self._save_session()
        log.info("Done.")

    def _save_session(self):
        """Save session results to file."""
        results = {
            "session_end": datetime.now(timezone.utc).isoformat(),
            "mode": "live" if self.args.live else "paper",
            "stats": self.trader.get_stats(),
            "training_accuracy_5m": self.predictor.training_accuracy_5m,
            "training_accuracy_15m": self.predictor.training_accuracy_15m,
            "trades": [
                {
                    "timestamp": t.timestamp,
                    "side": t.side,
                    "price": t.price,
                    "cost": t.cost,
                    "confidence": t.prediction_confidence,
                    "outcome": t.outcome,
                    "pnl": t.pnl,
                    "is_live": t.is_live,
                    "market_slug": t.market_slug,
                    "window_ts": t.window_ts,
                    "target_price": t.target_price,
                    "target_source": t.target_source,
                    "polymarket_current_price": t.polymarket_current_price,
                    "entry_btc_price": t.entry_btc_price,
                    "settlement_source": t.settlement_source,
                }
                for t in self.trader.trades
            ],
            "predictions_made": len(self.predictor.prediction_history),
            "feature_importance": self.predictor.get_feature_importance(),
        }

        mode = "live" if self.args.live else "paper"
        filename = ROOT / f"predictor_session_{mode}.json"
        with open(filename, "w") as f:
            json.dump(results, f, indent=2)
        log.info(f"Session saved to {filename.name}")


def parse_args():
    parser = argparse.ArgumentParser(description="BTC Predictor Bot")
    parser.add_argument("--live", action="store_true", help="Enable live trading on Polymarket")
    parser.add_argument("--confirm-live", action="store_true", help="Required with --live to allow real orders")
    parser.add_argument(
        "--allow-inferred-target",
        action="store_true",
        help="Allow trading when Polymarket target is unavailable and Binance fallback is used",
    )
    parser.add_argument("--retrain", action="store_true", help="Force retrain the model")
    parser.add_argument("--no-trade", action="store_true", help="Predictions only, no trading")
    parser.add_argument("--bet", type=float, default=2.0, help="Bet size in USD")
    parser.add_argument("--min-confidence", type=float, default=0.60, help="Minimum prediction confidence to trade")
    parser.add_argument("--high-confidence", type=float, default=0.75, help="High confidence threshold for larger bet")
    parser.add_argument("--stop-loss", type=float, default=10.0, help="Session stop loss")
    parser.add_argument("--take-profit", type=float, default=15.0, help="Session take profit")
    parser.add_argument("--predict-interval", type=int, default=30, help="Seconds between predictions")
    parser.add_argument("--market-refresh-interval", type=float, default=1.5, help="Seconds between Polymarket metadata refreshes")
    parser.add_argument("--port", type=int, default=5050, help="Dashboard port")
    return parser.parse_args()


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    args = parse_args()
    bot = BTCPredictorBot(args)
    bot.run()


if __name__ == "__main__":
    main()
