"""
Market-window backtest for the BTC predictor.

This backtest uses Binance 1-minute candles to simulate Polymarket-style
5-minute windows:

- target_price = BTC open price at the 5-minute window start
- winner = UP if window close >= target_price else DOWN
- a trade is counted only when model confidence >= --min-confidence

It is not a substitute for live paper mode because it does not include
Polymarket order-book slippage or settlement-source differences.
"""

import argparse
from dataclasses import dataclass

import xgboost as xgb

from predictor import BTCPredictor
from price_feed import BinancePriceFeed


@dataclass
class BacktestResult:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    skipped: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


def run_backtest(total_klines: int, min_confidence: float, train_ratio: float) -> BacktestResult:
    feed = BinancePriceFeed()
    klines = feed.fetch_bulk_klines(interval="1m", total=total_klines)
    if len(klines) < 1000:
        raise RuntimeError(f"Not enough klines fetched: {len(klines)}")

    predictor = BTCPredictor()
    X, y = predictor._prepare_training_data(klines, horizon_bars=5)
    if len(X) < 500:
        raise RuntimeError(f"Not enough samples after filtering: {len(X)}")

    split = int(len(X) * train_ratio)
    model = xgb.XGBClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.7,
        min_child_weight=5,
        reg_alpha=0.1,
        reg_lambda=1.0,
        eval_metric="logloss",
        verbosity=0,
    )
    model.fit(X[:split], y[:split])

    result = BacktestResult()
    probs = model.predict_proba(X[split:])
    actual = y[split:]

    for prob, label in zip(probs, actual):
        confidence = float(max(prob[0], prob[1]))
        if confidence < min_confidence:
            result.skipped += 1
            continue
        pred = 1 if prob[1] > prob[0] else 0
        result.trades += 1
        if pred == label:
            result.wins += 1
            # Approximate binary-share P&L for a $2 buy at 65c average.
            result.pnl += 2.0 * ((1.0 / 0.65) - 1.0)
        else:
            result.losses += 1
            result.pnl -= 2.0

    return result


def main():
    parser = argparse.ArgumentParser(description="Backtest BTC market-window predictor")
    parser.add_argument("--klines", type=int, default=20000)
    parser.add_argument("--min-confidence", type=float, default=0.60)
    parser.add_argument("--train-ratio", type=float, default=0.70)
    args = parser.parse_args()

    result = run_backtest(args.klines, args.min_confidence, args.train_ratio)
    print("Market-window backtest")
    print(f"  trades:     {result.trades}")
    print(f"  wins:       {result.wins}")
    print(f"  losses:     {result.losses}")
    print(f"  skipped:    {result.skipped}")
    print(f"  win rate:   {result.win_rate:.1%}")
    print(f"  approx pnl: ${result.pnl:+.2f}")


if __name__ == "__main__":
    main()
