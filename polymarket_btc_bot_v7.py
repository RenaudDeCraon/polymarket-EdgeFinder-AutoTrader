

"""
Polymarket BTC 5-Min Bot v7 — Mock-Testable Momentum + True Dutch Engine
========================================================================

Purpose
-------
v7 is designed to be tested safely in mock/paper mode before any live usage.
It fixes the biggest weaknesses observed in v5/v6 paper sessions:

1. Avoids warmup entries.
2. Avoids chasing already-expensive momentum prices.
3. Avoids opening fresh trades too late in the 5-minute window.
4. Uses true dutch only when both outcomes are guaranteed above a minimum profit.
5. Separates strategy decisions from market I/O so the logic can be mock tested.
6. Adds stronger risk controls: daily/session stop loss, max windows, cash buffer.
7. Handles Ctrl+C cleanly and writes a structured JSON result.

Important
---------
This is not financial advice. This bot can lose money. Use --mock or --paper first.
Live trading should only be enabled after long testing, logs review, and manual risk checks.

Usage
-----
Mock deterministic test:
  python polymarket_btc_bot_v7.py --mock --mock-scenario reversal_win
  python polymarket_btc_bot_v7.py --mock --mock-scenario fake_momentum_loss
  python polymarket_btc_bot_v7.py --mock --mock-scenario true_dutch

Paper mode against Polymarket public APIs:
  python polymarket_btc_bot_v7.py --paper --windows 6

Connectivity test:
  python polymarket_btc_bot_v7.py --test

Live mode, only after testing:
  python polymarket_btc_bot_v7.py --live --windows 3 --bet 1

.env for live mode:
  POLY_PRIVATE_KEY=0x...
  POLY_FUNDER=0x...
  POLY_SIGNATURE_TYPE=1 or 3
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import signal
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Protocol, Tuple

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
CHAIN_ID = 137

WINDOW_SECONDS = 300
SCRIPT_NAME = "polymarket_btc_bot_v7"
ROOT = Path(__file__).resolve().parent
AGGREGATE_FILE = ROOT / "aggregated_bot_results.json"

INTERRUPTED = False


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("polybot-v7")


# ─────────────────────────────────────────────────────────────────────────────
# Config / Data Models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StrategyConfig:
    poll_interval: float = 2.0
    observe_period: int = 55
    freeze_period: int = 35

    start_cash: float = 10.0
    bet_size: float = 2.0
    min_cash_buffer: float = 0.05
    session_stop_loss: float = 5.00
    session_take_profit: float = 3.00

    base_entry_threshold: float = 0.38
    strong_entry_threshold: float = 0.22
    no_trade_entry: float = 0.12

    max_momentum_price: float = 0.68
    min_momentum_price: float = 0.12
    min_entry_edge: float = 0.06
    min_entry_seconds_left: int = 135
    no_entry_first_seconds: int = 55
    momentum_confirm_ticks: int = 4
    min_momentum_delta: float = 0.02

    trend_lookback: int = 10
    strong_trend_moves: int = 7
    strong_trend_delta: float = 0.18

    min_true_dutch_profit: float = 0.05
    max_hedge_fraction_of_cash: float = 0.85
    max_live_order_budget_multiplier: float = 1.25
    live_no_paper_fallback: bool = True
    simulate_live_min_order_in_paper: bool = True
    min_exchange_order_shares: float = 5.0

    max_spread_gap: float = 0.08
    max_price_age_seconds: float = 8.0
    min_ticks_before_trade: int = 8

    allow_warmup_trade: bool = False


@dataclass
class Market:
    slug: str
    question: str
    up_token: str
    down_token: str


@dataclass
class PriceTick:
    ts: float
    up: float
    down: float

    @property
    def leader(self) -> str:
        if self.up > self.down:
            return "UP"
        if self.down > self.up:
            return "DN"
        return "TIE"


@dataclass
class Trade:
    time_s: int
    side: str
    price: float
    shares: float
    cost: float
    reason: str
    real: bool = False


@dataclass
class WindowResult:
    window_ts: int
    question: str
    winner: str
    payout: float
    spent: float
    pnl: float
    ticks: int
    swings: int
    skips: int
    dutch: bool
    real_trades: bool
    up_min: float
    down_min: float
    up_max: float
    down_max: float
    trades: List[Trade]
    notes: List[str] = field(default_factory=list)


@dataclass
class Portfolio:
    cash: float
    is_live: bool = False
    trader: Optional["LiveTrader"] = None
    up_shares: float = 0.0
    up_avg: float = 0.0
    dn_shares: float = 0.0
    dn_avg: float = 0.0
    trades: List[Trade] = field(default_factory=list)
    windows_played: int = 0
    windows_won: int = 0
    total_pnl: float = 0.0

    @property
    def has_up(self) -> bool:
        return self.up_shares > 0

    @property
    def has_dn(self) -> bool:
        return self.dn_shares > 0

    @property
    def is_dutch(self) -> bool:
        return self.has_up and self.has_dn

    def side_shares(self, side: str) -> float:
        return self.up_shares if side == "UP" else self.dn_shares

    def side_cost(self, side: str) -> float:
        return sum(t.cost for t in self.trades if t.side == side)

    def reset_window(self) -> None:
        self.up_shares = 0.0
        self.up_avg = 0.0
        self.dn_shares = 0.0
        self.dn_avg = 0.0
        self.trades = []

    def buy(self, side: str, price: float, amount: float, time_s: int, token_id: str, reason: str) -> Optional[Trade]:
        if price <= 0 or price >= 1:
            return None

        spend = min(amount, self.cash)
        if spend <= 0:
            return None

        real = False
        executed_price = price
        executed_cost = spend
        executed_shares: Optional[float] = None

        if self.is_live and self.trader and self.trader.initialized and token_id:
            log.info(f"   💸 LIVE BUY {side}: budget=${spend:.2f} signal_price=~{price:.3f}")
            try:
                resp = self.trader.market_buy(token_id, spend)
                if not resp:
                    log.warning("   ⚠️ Live order skipped/failed. No paper fallback in live mode.")
                    return None

                real = True
                executed_price = float(resp.get("executed_price", price))
                executed_cost = float(resp.get("executed_cost", spend))
                executed_shares = float(resp.get("executed_size", executed_cost / executed_price))
                log.info(
                    f"   ✅ FILLED actual={executed_shares:.2f}sh @ {executed_price:.3f} cost=${executed_cost:.2f}"
                )
            except Exception as exc:
                log.warning(f"   ⚠️ Live error: {exc}. No paper fallback in live mode.")
                return None

        if executed_shares is None:
            shares = math.floor((executed_cost / executed_price) * 100) / 100
        else:
            shares = math.floor(executed_shares * 100) / 100

        cost = round(shares * executed_price, 4)
        executed_price = round(executed_price, 4)

        if shares <= 0 or cost <= 0:
            return None

        if cost > self.cash + 1e-9:
            log.warning(f"   ⛔ Cost ${cost:.2f} exceeds tracked cash ${self.cash:.2f}; skipping")
            return None

        self.cash = round(self.cash - cost, 4)

        if side == "UP":
            total_cost = self.up_avg * self.up_shares + cost
            self.up_shares = round(self.up_shares + shares, 2)
            self.up_avg = round(total_cost / self.up_shares, 4) if self.up_shares else 0.0
        elif side == "DN":
            total_cost = self.dn_avg * self.dn_shares + cost
            self.dn_shares = round(self.dn_shares + shares, 2)
            self.dn_avg = round(total_cost / self.dn_shares, 4) if self.dn_shares else 0.0
        else:
            raise ValueError(f"Invalid side: {side}")

        trade = Trade(
            time_s=time_s,
            side=side,
            price=executed_price,
            shares=shares,
            cost=cost,
            reason=reason,
            real=real,
        )
        self.trades.append(trade)
        return trade

    def resolve(self, winner: str) -> Dict[str, float]:
        payout = self.up_shares if winner == "UP" else self.dn_shares
        spent = sum(t.cost for t in self.trades)
        pnl = round(payout - spent, 4)
        self.cash = round(self.cash + payout, 4)
        self.total_pnl = round(self.total_pnl + pnl, 4)
        self.windows_played += 1
        if pnl > 0:
            self.windows_won += 1
        return {"payout": round(payout, 4), "spent": round(spent, 4), "pnl": pnl}


# ─────────────────────────────────────────────────────────────────────────────
# Signal Handling
# ─────────────────────────────────────────────────────────────────────────────

def signal_handler(signum, frame) -> None:
    global INTERRUPTED
    INTERRUPTED = True
    log.info("  ⚠️ Ctrl+C detected. Finishing current save path...")


def setup_signal_handlers() -> None:
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy Engine
# ─────────────────────────────────────────────────────────────────────────────

class StrategyEngine:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def detect_regime(self, history: Deque[PriceTick]) -> Tuple[str, str]:
        if len(history) < self.cfg.trend_lookback:
            return "WARMUP", "none"

        recent = list(history)[-self.cfg.trend_lookback:]
        delta = recent[-1].up - recent[0].up
        up_moves = sum(1 for i in range(1, len(recent)) if recent[i].up > recent[i - 1].up)
        dn_moves = sum(1 for i in range(1, len(recent)) if recent[i].up < recent[i - 1].up)

        if delta >= self.cfg.strong_trend_delta and up_moves >= self.cfg.strong_trend_moves:
            return "STRONG_UP", "up"
        if delta <= -self.cfg.strong_trend_delta and dn_moves >= self.cfg.strong_trend_moves:
            return "STRONG_DN", "down"
        return "RANGE", "none"

    def entry_threshold(self, regime: str) -> float:
        if regime.startswith("STRONG"):
            return min(self.cfg.base_entry_threshold, self.cfg.strong_entry_threshold)
        return self.cfg.base_entry_threshold

    def validate_price_quality(self, tick: PriceTick, now: float) -> Tuple[bool, str]:
        age = now - tick.ts
        if age > self.cfg.max_price_age_seconds:
            return False, "stale_price"
        total = tick.up + tick.down
        if abs(total - 1.0) > self.cfg.max_spread_gap:
            return False, f"bad_sum_{total:.3f}"
        if tick.up <= 0 or tick.down <= 0 or tick.up >= 1 or tick.down >= 1:
            return False, "invalid_price"
        return True, "ok"

    def choose_momentum_entry(
        self,
        tick: PriceTick,
        regime: str,
        elapsed: float,
        seconds_left: float,
        ticks_seen: int,
        history: Deque[PriceTick],
    ) -> Tuple[Optional[str], Optional[float], str]:
        """Return side, entry price, reason.

        Momentum rule:
        - If UP becomes cheap, market is implying DN trend, so buy DN.
        - If DN becomes cheap, market is implying UP trend, so buy UP.
        """
        if ticks_seen < self.cfg.min_ticks_before_trade:
            return None, None, "not_enough_ticks"

        if regime == "WARMUP" and not self.cfg.allow_warmup_trade:
            return None, None, "warmup_regime"

        if elapsed < self.cfg.no_entry_first_seconds:
            return None, None, "warmup_time"

        if seconds_left < self.cfg.min_entry_seconds_left:
            return None, None, "too_late"

        threshold = self.entry_threshold(regime)
        side = None
        price = None

        if tick.up <= threshold:
            side = "DN"
            price = tick.down
        elif tick.down <= threshold:
            side = "UP"
            price = tick.up
        else:
            return None, None, "no_threshold_hit"

        assert side is not None and price is not None
        other_price = tick.down if side == "UP" else tick.up

        if price > self.cfg.max_momentum_price:
            return None, None, f"momentum_price_too_high_{price:.3f}"

        if price < self.cfg.min_momentum_price:
            return None, None, f"momentum_price_too_low_{price:.3f}"

        if price - other_price < self.cfg.min_entry_edge:
            return None, None, f"edge_too_small_{price - other_price:.3f}"

        confirm_ticks = max(2, self.cfg.momentum_confirm_ticks)
        if len(history) >= confirm_ticks:
            recent = list(history)[-confirm_ticks:]
            chosen_start = recent[0].up if side == "UP" else recent[0].down
            chosen_end = recent[-1].up if side == "UP" else recent[-1].down
            chosen_delta = chosen_end - chosen_start
            if chosen_delta < self.cfg.min_momentum_delta:
                return None, None, f"momentum_not_confirmed_{chosen_delta:+.3f}"

        if regime == "STRONG_UP" and side == "DN" and tick.down <= self.cfg.no_trade_entry:
            return None, None, "dn_dead_in_strong_up"

        if regime == "STRONG_DN" and side == "UP" and tick.up <= self.cfg.no_trade_entry:
            return None, None, "up_dead_in_strong_dn"

        return side, price, f"momentum_{side.lower()}_{regime.lower()}"

    def calculate_true_dutch(
        self,
        position_side: str,
        position_shares: float,
        position_cost: float,
        hedge_side: str,
        hedge_price: float,
        available_cash: float,
    ) -> Optional[Dict[str, float]]:
        """Calculate a hedge that makes both outcomes profitable.

        If original side wins:
            profit = position_shares - position_cost - hedge_cost
        If hedge side wins:
            profit = hedge_shares - position_cost - hedge_cost

        Required:
            min(original_win_profit, hedge_win_profit) >= min_true_dutch_profit
        """
        if hedge_price <= 0 or hedge_price >= 1:
            return None
        if position_shares <= 0 or position_cost <= 0:
            return None

        spendable_cash = max(0.0, available_cash - self.cfg.min_cash_buffer)
        spendable_cash = min(spendable_cash, available_cash * self.cfg.max_hedge_fraction_of_cash)
        if spendable_cash <= 0:
            return None

        min_hedge_shares = position_cost + self.cfg.min_true_dutch_profit
        hedge_cost = round(min_hedge_shares * hedge_price, 4)

        if hedge_cost <= 0 or hedge_cost > spendable_cash:
            return None

        original_win_profit = round(position_shares - position_cost - hedge_cost, 4)
        hedge_win_profit = round(min_hedge_shares - position_cost - hedge_cost, 4)
        worst_profit = min(original_win_profit, hedge_win_profit)

        if worst_profit < self.cfg.min_true_dutch_profit:
            return None

        return {
            "side": hedge_side,
            "shares": round(min_hedge_shares, 2),
            "price": hedge_price,
            "cost": hedge_cost,
            "worst_profit": worst_profit,
            "original_win_profit": original_win_profit,
            "hedge_win_profit": hedge_win_profit,
        }

    def maybe_hedge(self, tick: PriceTick, pf: Portfolio) -> Tuple[Optional[str], Optional[Dict[str, float]], str]:
        threshold = self.cfg.base_entry_threshold

        if pf.has_dn and not pf.has_up and tick.up <= threshold:
            hedge = self.calculate_true_dutch(
                position_side="DN",
                position_shares=pf.dn_shares,
                position_cost=pf.side_cost("DN"),
                hedge_side="UP",
                hedge_price=tick.up,
                available_cash=pf.cash,
            )
            if hedge:
                return "UP", hedge, "true_dutch_up"
            return None, None, "dutch_not_profitable"

        if pf.has_up and not pf.has_dn and tick.down <= threshold:
            hedge = self.calculate_true_dutch(
                position_side="UP",
                position_shares=pf.up_shares,
                position_cost=pf.side_cost("UP"),
                hedge_side="DN",
                hedge_price=tick.down,
                available_cash=pf.cash,
            )
            if hedge:
                return "DN", hedge, "true_dutch_dn"
            return None, None, "dutch_not_profitable"

        return None, None, "no_hedge_signal"


# ─────────────────────────────────────────────────────────────────────────────
# Market Data Providers
# ─────────────────────────────────────────────────────────────────────────────

class MarketDataProvider(Protocol):
    def find_market(self, window_ts: int) -> Optional[Market]:
        ...

    def fetch_prices(self, market: Market) -> Optional[PriceTick]:
        ...


class PolymarketProvider:
    def find_market(self, window_ts: int) -> Optional[Market]:
        slug = f"btc-updown-5m-{window_ts}"
        for endpoint in ("events", "markets"):
            try:
                response = requests.get(
                    f"{GAMMA_API}/{endpoint}",
                    params={"slug": slug, "limit": 1},
                    timeout=10,
                )
                if response.status_code != 200:
                    continue
                data = response.json()
                if not data:
                    continue

                market = data[0].get("markets", [{}])[0] if endpoint == "events" else data[0]
                outcomes = market.get("outcomes", "[]")
                token_ids = market.get("clobTokenIds", "[]")
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if isinstance(token_ids, str):
                    token_ids = json.loads(token_ids)

                up_idx = None
                dn_idx = None
                for i, outcome in enumerate(outcomes):
                    label = str(outcome).strip().lower()
                    if label == "up":
                        up_idx = i
                    elif label == "down":
                        dn_idx = i

                if up_idx is None or dn_idx is None:
                    continue
                if len(token_ids) <= max(up_idx, dn_idx):
                    continue

                return Market(
                    slug=slug,
                    question=market.get("question", slug),
                    up_token=str(token_ids[up_idx]).strip(),
                    down_token=str(token_ids[dn_idx]).strip(),
                )
            except Exception:
                continue
        return None

    def fetch_single_price(self, token_id: str) -> Optional[float]:
        methods = ("midpoint", "price", "last-trade-price")
        for method in methods:
            try:
                params = {"token_id": token_id}
                if method == "price":
                    params["side"] = "buy"
                response = requests.get(f"{CLOB_API}/{method}", params=params, timeout=5)
                if response.status_code != 200:
                    continue
                payload = response.json()
                value = payload.get("mid") or payload.get("price")
                if value is None:
                    continue
                price = float(value)
                if 0.005 < price < 0.995:
                    return price
            except Exception:
                continue
        return None

    def fetch_prices(self, market: Market) -> Optional[PriceTick]:
        up = self.fetch_single_price(market.up_token)
        down = self.fetch_single_price(market.down_token)

        if up is not None and down is None:
            down = round(1 - up, 4)
        elif down is not None and up is None:
            up = round(1 - down, 4)

        if up is not None and down is not None:
            return PriceTick(ts=time.time(), up=float(up), down=float(down))

        try:
            response = requests.get(
                f"{GAMMA_API}/events",
                params={"slug": market.slug, "limit": 1},
                timeout=8,
            )
            if response.status_code != 200:
                return None
            events = response.json()
            if not events:
                return None
            m2 = events[0].get("markets", [{}])[0]
            prices = m2.get("outcomePrices", "[]")
            outcomes = m2.get("outcomes", "[]")
            if isinstance(prices, str):
                prices = json.loads(prices)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)

            fallback_up = None
            fallback_down = None
            for i, outcome in enumerate(outcomes):
                label = str(outcome).strip().lower()
                if i >= len(prices):
                    continue
                if label == "up":
                    fallback_up = float(prices[i])
                elif label == "down":
                    fallback_down = float(prices[i])

            if fallback_up is not None and fallback_down is not None:
                return PriceTick(ts=time.time(), up=fallback_up, down=fallback_down)
        except Exception:
            return None

        return None


class MockProvider:
    def __init__(self, scenario: str, cfg: StrategyConfig):
        self.scenario = scenario
        self.cfg = cfg
        self.index = 0
        self.series = self._build_series(scenario)

    def find_market(self, window_ts: int) -> Optional[Market]:
        return Market(
            slug=f"mock-btc-updown-5m-{window_ts}",
            question=f"MOCK Bitcoin Up or Down - {self.scenario}",
            up_token="mock_up",
            down_token="mock_down",
        )

    def fetch_prices(self, market: Market) -> Optional[PriceTick]:
        if self.index >= len(self.series):
            up = self.series[-1]
        else:
            up = self.series[self.index]
        self.index += 1
        up = min(0.99, max(0.01, up))
        down = round(1 - up, 4)
        return PriceTick(ts=time.time(), up=round(up, 4), down=down)

    def _build_series(self, scenario: str) -> List[float]:
        n = int(WINDOW_SECONDS / max(self.cfg.poll_interval, 1))

        if scenario == "reversal_win":
            return self._piecewise(n, [(0, 0.50), (20, 0.35), (45, 0.20), (80, 0.05), (n, 0.01)])

        if scenario == "fake_momentum_loss":
            return self._piecewise(n, [(0, 0.50), (25, 0.30), (50, 0.12), (90, 0.55), (n, 0.99)])

        if scenario == "true_dutch":
            return self._piecewise(n, [(0, 0.50), (25, 0.35), (45, 0.20), (75, 0.18), (110, 0.08), (n, 0.01)])

        if scenario == "range_no_trade":
            return [0.50 + random.uniform(-0.04, 0.04) for _ in range(n)]

        if scenario == "late_signal_skip":
            return self._piecewise(n, [(0, 0.50), (90, 0.50), (125, 0.25), (n, 0.01)])

        raise ValueError(
            "Unknown mock scenario. Use: reversal_win, fake_momentum_loss, true_dutch, range_no_trade, late_signal_skip"
        )

    @staticmethod
    def _piecewise(n: int, anchors: List[Tuple[int, float]]) -> List[float]:
        values: List[float] = []
        anchors = sorted(anchors, key=lambda x: x[0])
        for i in range(len(anchors) - 1):
            x0, y0 = anchors[i]
            x1, y1 = anchors[i + 1]
            span = max(1, x1 - x0)
            for x in range(x0, min(x1, n)):
                t = (x - x0) / span
                noise = random.uniform(-0.01, 0.01)
                values.append(y0 + (y1 - y0) * t + noise)
        while len(values) < n:
            values.append(anchors[-1][1] + random.uniform(-0.005, 0.005))
        return values[:n]


# ─────────────────────────────────────────────────────────────────────────────
# Live Trading Adapter
# ─────────────────────────────────────────────────────────────────────────────

class LiveTrader:
    def __init__(self):
        self.client = None
        self.initialized = False
        self._imports: Dict[str, object] = {}

    def connect(self) -> bool:
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams, ClobClient

            self._imports = {
                "AssetType": AssetType,
                "BalanceAllowanceParams": BalanceAllowanceParams,
            }
        except ImportError:
            log.error("❌ Missing dependency: pip install py-clob-client-v2")
            return False

        private_key = os.getenv("POLY_PRIVATE_KEY")
        if not private_key:
            log.error("❌ POLY_PRIVATE_KEY is not set")
            return False

        try:
            ClobClient = self._imports.get("ClobClient")
            from py_clob_client_v2 import ClobClient as ImportedClobClient

            ClobClient = ImportedClobClient
            self.client = ClobClient(
                host=CLOB_API,
                key=private_key,
                chain_id=CHAIN_ID,
                signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "3")),
                funder=os.getenv("POLY_FUNDER"),
            )
            creds = self.client.derive_api_key()
            self.client.set_api_creds(creds)
            self.initialized = True
            log.info(f"   ✅ Connected. Collateral balance: ${self.get_balance():.2f}")
            return True
        except Exception as exc:
            log.error(f"   ❌ Live connect failed: {exc}")
            return False

    def get_balance(self) -> float:
        if not self.client:
            return 0.0
        try:
            params_cls = self._imports["BalanceAllowanceParams"]
            asset_type = self._imports["AssetType"].COLLATERAL
            response = self.client.get_balance_allowance(params_cls(asset_type=asset_type))
            return int(response.get("balance", 0)) / 1e6
        except Exception:
            return 0.0

    def market_buy(self, token_id: str, amount: float) -> Optional[dict]:
        if not self.initialized or not self.client:
            return None
        try:
            from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions, Side

            book = self.client.get_order_book(token_id)

            # py_clob_client_v2 usually returns a dict, but keep object support for compatibility.
            asks = book.get("asks", []) if isinstance(book, dict) else getattr(book, "asks", [])
            if not asks:
                log.error("   ❌ No asks available")
                return None

            ask0 = asks[0]
            ask = float(ask0["price"] if isinstance(ask0, dict) else ask0.price)

            min_size = float(book.get("min_order_size", 5) if isinstance(book, dict) else getattr(book, "min_order_size", 5))
            tick_size = str(book.get("tick_size", "0.01") if isinstance(book, dict) else getattr(book, "tick_size", "0.01"))
            neg_risk = bool(book.get("neg_risk", False) if isinstance(book, dict) else getattr(book, "neg_risk", False))

            requested_size = math.floor((amount / ask) * 100) / 100
            size = max(min_size, requested_size)
            actual_cost = round(size * ask, 4)

            # Critical live safety: Polymarket has minimum order sizes. Do not let a
            # configured $1 bet silently become a ~$5 live order.
            if actual_cost > amount * 1.25:
                log.warning(
                    f"   ⛔ Min order cost ${actual_cost:.2f} exceeds budget ${amount:.2f}; skipping live order"
                )
                return None

            options = PartialCreateOrderOptions(
                tick_size=tick_size,
                neg_risk=neg_risk,
            )

            response = self.client.create_and_post_order(
                OrderArgs(token_id=token_id, price=ask, size=size, side=Side.BUY),
                options=options,
            )

            status = response.get("status", "unknown") if isinstance(response, dict) else "unknown"
            success = bool(response.get("success")) if isinstance(response, dict) else False
            log.info(f"   📤 LIVE {size:.2f}sh @ {ask:.3f} = ${actual_cost:.2f} → {status}")

            if success:
                response["executed_price"] = ask
                response["executed_size"] = size
                response["executed_cost"] = actual_cost
                return response

            error_msg = response.get("errorMsg", "unknown error") if isinstance(response, dict) else "unknown error"
            log.error(f"   ❌ Live order rejected: {error_msg}")
            return None
        except Exception as exc:
            log.error(f"   ❌ Live order failed: {exc}")
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Bot Runner
# ─────────────────────────────────────────────────────────────────────────────

def get_window_ts() -> int:
    now = int(time.time())
    return now - (now % WINDOW_SECONDS)


def format_pos(pf: Portfolio) -> str:
    if pf.is_dutch:
        return "🎯DUTCH"
    if pf.has_up:
        return f"↑{pf.up_shares:.0f}@{pf.up_avg:.2f}"
    if pf.has_dn:
        return f"↓{pf.dn_shares:.0f}@{pf.dn_avg:.2f}"
    return "—"


def token_for_side(market: Market, side: str) -> str:
    return market.up_token if side == "UP" else market.down_token


def trade_window(
    window_ts: int,
    market: Market,
    pf: Portfolio,
    provider: MarketDataProvider,
    engine: StrategyEngine,
    cfg: StrategyConfig,
    mock: bool = False,
) -> Optional[WindowResult]:
    global INTERRUPTED

    window_end = window_ts + WINDOW_SECONDS
    if not mock and time.time() >= window_end - 5:
        return None

    pf.reset_window()
    history: Deque[PriceTick] = deque(maxlen=max(cfg.trend_lookback, 3))
    up_min = 1.0
    down_min = 1.0
    up_max = 0.0
    down_max = 0.0
    ticks = 0
    swings = 0
    skips = 0
    notes: List[str] = []
    last_leader: Optional[str] = None
    last_tick = PriceTick(ts=time.time(), up=0.5, down=0.5)

    mode = "MOCK" if mock else ("LIVE" if pf.is_live else "PAPER")
    log.info("\n" + "━" * 88)
    log.info(f"  🎯 {market.question}  [{mode}]")
    log.info(
        f"  ⏰ {datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime('%H:%M:%S')} → "
        f"{datetime.fromtimestamp(window_end, tz=timezone.utc).strftime('%H:%M:%S')} UTC │ "
        f"Cash=${pf.cash:.2f} │ Session=${pf.total_pnl:+.2f}"
    )
    log.info("  🧠 v7: guarded momentum + true dutch only")
    log.info("━" * 88)
    log.info(f"  {'Time':>8} {'Left':>4} {'Phase':>7} {'Up':>6} {'Dn':>6} {'Regime':>10} {'Pos':>12} Action")
    log.info(f"  {'─'*8} {'─'*4} {'─'*7} {'─'*6} {'─'*6} {'─'*10} {'─'*12} {'─'*34}")

    mock_elapsed = 0.0
    while True:
        now = time.time()
        if mock:
            elapsed = mock_elapsed
            seconds_left = max(0.0, WINDOW_SECONDS - elapsed)
            if seconds_left <= 3:
                break
        else:
            elapsed = now - window_ts
            seconds_left = window_end - now
            if seconds_left <= 3:
                break

        tick = provider.fetch_prices(market)
        if tick is None:
            skips += 1
            notes.append("missing_price")
            if not mock:
                time.sleep(cfg.poll_interval)
            else:
                mock_elapsed += cfg.poll_interval
            continue

        ok_quality, quality_reason = engine.validate_price_quality(tick, time.time())
        if not ok_quality:
            skips += 1
            action = f"⛔ {quality_reason}"
        else:
            action = ""
            last_tick = tick
            history.append(tick)
            up_min = min(up_min, tick.up)
            down_min = min(down_min, tick.down)
            up_max = max(up_max, tick.up)
            down_max = max(down_max, tick.down)

            leader = tick.leader
            if last_leader and leader != last_leader and leader != "TIE":
                swings += 1
            if leader != "TIE":
                last_leader = leader

            regime, _trend = engine.detect_regime(history)
            if elapsed < cfg.observe_period:
                phase = "👁 WATCH"
            elif seconds_left < cfg.freeze_period:
                phase = "🧊 FREEZE"
            else:
                phase = "⚡ TRADE"

            if phase == "⚡ TRADE":
                if not pf.has_up and not pf.has_dn:
                    side, entry_price, reason = engine.choose_momentum_entry(
                        tick=tick,
                        regime=regime,
                        elapsed=elapsed,
                        seconds_left=seconds_left,
                        ticks_seen=ticks,
                        history=history,
                    )
                    if side and entry_price:
                        spend = min(cfg.bet_size, max(0.0, pf.cash - cfg.min_cash_buffer))
                        if spend <= 0:
                            skips += 1
                            action = "⛔ no_cash"
                        else:
                            min_exchange_cost = cfg.min_exchange_order_shares * entry_price
                            if (not pf.is_live and cfg.simulate_live_min_order_in_paper and
                                    min_exchange_cost > spend * cfg.max_live_order_budget_multiplier):
                                skips += 1
                                action = f"⛔ paper_min_order_cost_${min_exchange_cost:.2f}_gt_budget_${spend:.2f}"
                            else:
                                trade = pf.buy(
                                    side=side,
                                    price=entry_price,
                                    amount=max(spend, min_exchange_cost) if (not pf.is_live and cfg.simulate_live_min_order_in_paper) else spend,
                                    time_s=int(elapsed),
                                    token_id=token_for_side(market, side),
                                    reason=reason,
                                )
                                if trade:
                                    action = f"📈 MOM {side} {trade.shares:.0f}sh @{trade.price:.3f} ${trade.cost:.2f} [{regime}]"
                                else:
                                    skips += 1
                                    action = "⛔ buy_failed"
                    elif reason not in {"no_threshold_hit", "not_enough_ticks", "warmup_time", "warmup_regime"}:
                        skips += 1
                        action = f"⛔ {reason}"

                elif not pf.is_dutch:
                    hedge_side, hedge, reason = engine.maybe_hedge(tick, pf)
                    if hedge_side and hedge:
                        trade = pf.buy(
                            side=hedge_side,
                            price=hedge["price"],
                            amount=hedge["cost"],
                            time_s=int(elapsed),
                            token_id=token_for_side(market, hedge_side),
                            reason=reason,
                        )
                        if trade:
                            action = (
                                f"🎯 TRUE DUTCH {hedge_side} {trade.shares:.0f}sh "
                                f"@{trade.price:.3f} worst=${hedge['worst_profit']:+.2f}"
                            )
                        else:
                            skips += 1
                            action = "⛔ hedge_buy_failed"
                    elif reason == "dutch_not_profitable":
                        skips += 1
                        action = "⛔ dutch_not_profitable"
        
            pos = format_pos(pf)
            log.info(
                f"  {datetime.now(tz=timezone.utc).strftime('%H:%M:%S')} {seconds_left:3.0f}s {phase:>7} "
                f"{tick.up:5.3f}  {tick.down:5.3f} {regime:>10} {pos:>12} {action}"
            )

        ticks += 1
        if INTERRUPTED:
            notes.append("interrupted")
            break

        if mock:
            mock_elapsed += cfg.poll_interval
        else:
            time.sleep(cfg.poll_interval)

    final_tick = provider.fetch_prices(market) or last_tick
    winner = "UP" if final_tick.up > final_tick.down else "DN"

    if pf.trades:
        resolved = pf.resolve(winner)
    else:
        resolved = {"payout": 0.0, "spent": 0.0, "pnl": 0.0}

    result = WindowResult(
        window_ts=window_ts,
        question=market.question,
        winner=winner,
        payout=resolved["payout"],
        spent=resolved["spent"],
        pnl=resolved["pnl"],
        ticks=ticks,
        swings=swings,
        skips=skips,
        dutch=any(t.side == "UP" for t in pf.trades) and any(t.side == "DN" for t in pf.trades),
        real_trades=any(t.real for t in pf.trades),
        up_min=round(up_min if up_min < 1.0 else final_tick.up, 4),
        down_min=round(down_min if down_min < 1.0 else final_tick.down, 4),
        up_max=round(up_max, 4),
        down_max=round(down_max, 4),
        trades=list(pf.trades),
        notes=notes,
    )

    log.info("\n  ┌──────────────────────────────────────────────────────┐")
    log.info(f"  │  ⏱  ENDED — Up={final_tick.up:.3f} Dn={final_tick.down:.3f} 🏆 {winner}")
    if pf.trades:
        log.info(f"  │  💰 Pay=${result.payout:.2f} Spent=${result.spent:.2f} PnL=${result.pnl:+.2f}")
        log.info(f"  │  📊 Cash=${pf.cash:.2f} Session=${pf.total_pnl:+.2f}")
        log.info(f"  │  {'🎯 True Dutch active' if result.dutch else '📈 Momentum only'}")
        for trade in pf.trades:
            rtag = " 🔴" if trade.real else ""
            log.info(
                f"  │    {trade.time_s:>3}s {trade.side:>2} {trade.shares:>5.0f}sh "
                f"@{trade.price:.3f} ${trade.cost:.2f} {trade.reason}{rtag}"
            )
    else:
        log.info("  │  📭 No trades")
    log.info(f"  │  Swings:{result.swings} Skips:{result.skips} Up☟={result.up_min:.3f} Dn☟={result.down_min:.3f}")
    log.info("  └──────────────────────────────────────────────────────┘")

    return result


def serializable_result(obj):
    if isinstance(obj, WindowResult):
        data = asdict(obj)
        return data
    if isinstance(obj, Trade):
        return asdict(obj)
    if isinstance(obj, StrategyConfig):
        return asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def append_aggregate(result: dict, status: str, output_file: str) -> None:
    entry = {
        "script_name": SCRIPT_NAME,
        "output_file": output_file,
        "status": status,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }

    existing = []
    try:
        if AGGREGATE_FILE.exists():
            existing = json.loads(AGGREGATE_FILE.read_text(encoding="utf-8")) or []
    except Exception:
        existing = []

    if not isinstance(existing, list):
        existing = []

    existing.append(entry)
    AGGREGATE_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"  💾 Appended aggregate result to {AGGREGATE_FILE.name}")


def run_session(num_windows: int, mode: str, cfg: StrategyConfig, mock_scenario: str) -> dict:
    global INTERRUPTED
    INTERRUPTED = False

    live = mode == "live"
    mock = mode == "mock"

    trader = None
    if live:
        trader = LiveTrader()
        log.info("\n  🔌 Connecting live trader...")
        if not trader.connect():
            return {"status": "live_connect_failed", "results": []}
        pf = Portfolio(cash=trader.get_balance(), is_live=True, trader=trader)
        provider: MarketDataProvider = PolymarketProvider()
    elif mock:
        pf = Portfolio(cash=cfg.start_cash)
        provider = MockProvider(mock_scenario, cfg)
    else:
        pf = Portfolio(cash=cfg.start_cash)
        provider = PolymarketProvider()

    engine = StrategyEngine(cfg)
    results: List[WindowResult] = []
    status = "completed"

    log.info("\n  ╔════════════════════════════════════════════════════════════════════╗")
    log.info(f"  ║  🤖 BTC 5-Min Bot v7 [{mode.upper()}]                              ║")
    log.info(f"  ║  Cash=${pf.cash:.2f} │ Bet=${cfg.bet_size:.2f} │ Entry≤{cfg.base_entry_threshold:.0%} │ MaxMom≤{cfg.max_momentum_price:.2f}       ║")
    log.info(f"  ║  Observe={cfg.observe_period}s │ Freeze={cfg.freeze_period}s │ MinLeft={cfg.min_entry_seconds_left}s │ TrueDutch≥${cfg.min_true_dutch_profit:.2f} ║")
    log.info(f"  ║  StopLoss=${cfg.session_stop_loss:.2f} │ TakeProfit=${cfg.session_take_profit:.2f} │ Windows={num_windows:<3}              ║")
    log.info("  ╚════════════════════════════════════════════════════════════════════╝")

    try:
        for i in range(num_windows):
            if INTERRUPTED:
                status = "interrupted"
                break

            if pf.total_pnl <= -abs(cfg.session_stop_loss):
                status = "stopped_loss"
                log.warning(f"  🛑 Session stop loss reached: ${pf.total_pnl:+.2f}")
                break

            if pf.total_pnl >= abs(cfg.session_take_profit):
                status = "take_profit"
                log.warning(f"  ✅ Session take profit reached: ${pf.total_pnl:+.2f}")
                break

            if pf.cash <= cfg.min_cash_buffer:
                status = "cash_depleted"
                log.warning("  🛑 Cash depleted / buffer reached")
                break

            window_ts = get_window_ts() if not mock else get_window_ts() + i * WINDOW_SECONDS
            window_end = window_ts + WINDOW_SECONDS

            if not mock:
                now = time.time()
                if window_end - now < 60:
                    wait = max(0, window_end - now + 3)
                    log.info(f"\n  ⏳ Waiting {wait:.0f}s for next window...")
                    time.sleep(wait)
                    window_ts = get_window_ts()

            log.info(f"\n  🔍 [{i + 1}/{num_windows}] Finding market...")
            market = provider.find_market(window_ts)
            retries = 0
            while not market and retries < 5 and not mock:
                retries += 1
                time.sleep(3)
                market = provider.find_market(window_ts)

            if not market:
                log.warning("  ❌ Market not found")
                if not mock:
                    time.sleep(30)
                continue

            if live and trader:
                pf.cash = trader.get_balance()

            result = trade_window(
                window_ts=window_ts,
                market=market,
                pf=pf,
                provider=provider,
                engine=engine,
                cfg=cfg,
                mock=mock,
            )
            if result:
                results.append(result)

            if not mock and i < num_windows - 1:
                time.sleep(2)

    except KeyboardInterrupt:
        status = "interrupted"
        log.info("\n  ⚠️ Interrupted by user. Saving partial session...")

    wins = sum(1 for r in results if r.pnl > 0)
    losses = sum(1 for r in results if r.pnl < 0)
    flats = sum(1 for r in results if r.pnl == 0)
    dutches = sum(1 for r in results if r.dutch)
    real_trades = sum(1 for r in results if r.real_trades)

    output = {
        "session": datetime.now(timezone.utc).isoformat(),
        "script": SCRIPT_NAME,
        "mode": mode,
        "mock_scenario": mock_scenario if mock else None,
        "status": status,
        "config": serializable_result(cfg),
        "live_execution_note": "In live mode, failed/skipped live orders do not create paper fallback positions. Filled live orders are tracked using actual executed price, size, and cost returned by Polymarket.",
        "summary": {
            "windows": len(results),
            "wins": wins,
            "losses": losses,
            "flats": flats,
            "dutches": dutches,
            "real_trade_windows": real_trades,
            "final_cash": round(pf.cash, 4),
            "total_pnl": round(pf.total_pnl, 4),
            "roi_pct": round((pf.total_pnl / cfg.start_cash) * 100, 2) if cfg.start_cash else 0.0,
        },
        "results": [serializable_result(r) for r in results],
    }

    log.info("\n  ╔═══════════════════════════════════════════════════════╗")
    log.info(f"  ║  📊 SESSION DONE [{mode.upper()}] status={status:<14} ║")
    log.info("  ╠═══════════════════════════════════════════════════════╣")
    log.info(
        f"  ║  {len(results)} windows │ {wins}W/{losses}L/{flats}F │ Dutch:{dutches} │ Real:{real_trades}       ║"
    )
    log.info(
        f"  ║  PnL=${pf.total_pnl:+.2f} │ Cash=${pf.cash:.2f} │ ROI={output['summary']['roi_pct']:+.1f}%             ║"
    )
    if results:
        log.info("  ╠═══════════════════════════════════════════════════════╣")
        log.info(f"  ║  {'W':>2} {'D':>2} {'PnL':>7} {'Sw':>2} {'Sk':>2} {'Up☟':>5} {'Dn☟':>5} {'Time':>5} ║")
        for r in results:
            tm = datetime.fromtimestamp(r.window_ts, tz=timezone.utc).strftime("%H:%M")
            ws = "✅" if r.pnl > 0 else ("❌" if r.pnl < 0 else "➖")
            ds = "🎯" if r.dutch else "  "
            log.info(
                f"  ║  {ws} {ds} ${r.pnl:+5.2f} {r.swings:>2} {r.skips:>2} "
                f"{r.up_min:5.2f} {r.down_min:5.2f} {tm:>5} ║"
            )
    log.info("  ╚═══════════════════════════════════════════════════════╝")

    filename = f"bot_v7_{mode}.json"
    with open(ROOT / filename, "w", encoding="utf-8") as file:
        json.dump(output, file, indent=2, ensure_ascii=False)
    log.info(f"  💾 {filename}")
    append_aggregate(output, status, filename)
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Test / CLI
# ─────────────────────────────────────────────────────────────────────────────

def connectivity_test() -> None:
    log.info("🔌 Connectivity test...")
    for name, url in (("Gamma", f"{GAMMA_API}/events?limit=1"), ("CLOB", f"{CLOB_API}/time")):
        try:
            response = requests.get(url, timeout=10)
            log.info(f"   {name}: {'✅' if response.status_code == 200 else '❌'} status={response.status_code}")
        except Exception as exc:
            log.error(f"   {name}: ❌ {exc}")

    provider = PolymarketProvider()
    market = provider.find_market(get_window_ts()) or provider.find_market(get_window_ts() - WINDOW_SECONDS)
    if market:
        log.info(f"   Market: ✅ {market.question}")
        tick = provider.fetch_prices(market)
        if tick:
            log.info(f"   Prices: Up={tick.up:.3f} Dn={tick.down:.3f}")
        else:
            log.warning("   Prices: ❌")
    else:
        log.warning("   Market: ❌")


def build_config(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
        poll_interval=args.interval,
        observe_period=args.observe,
        freeze_period=args.freeze,
        start_cash=args.cash,
        bet_size=args.bet,
        min_cash_buffer=args.cash_buffer,
        session_stop_loss=args.stop_loss,
        session_take_profit=args.take_profit,
        base_entry_threshold=args.entry,
        strong_entry_threshold=args.strong_entry,
        no_trade_entry=args.no_trade_entry,
        max_momentum_price=args.max_momentum_price,
        min_momentum_price=args.min_momentum_price,
        min_entry_edge=args.min_entry_edge,
        min_entry_seconds_left=args.min_entry_seconds_left,
        no_entry_first_seconds=args.no_entry_first_seconds,
        momentum_confirm_ticks=args.momentum_confirm_ticks,
        min_momentum_delta=args.min_momentum_delta,
        trend_lookback=args.trend_lookback,
        strong_trend_moves=args.strong_trend_moves,
        strong_trend_delta=args.strong_trend_delta,
        min_true_dutch_profit=args.min_dutch_profit,
        max_hedge_fraction_of_cash=args.max_hedge_fraction,
        max_live_order_budget_multiplier=args.max_live_order_budget_multiplier,
        live_no_paper_fallback=True,
        simulate_live_min_order_in_paper=not args.disable_live_min_order_sim,
        min_exchange_order_shares=args.min_exchange_order_shares,
        max_spread_gap=args.max_spread_gap,
        max_price_age_seconds=args.max_price_age,
        min_ticks_before_trade=args.min_ticks_before_trade,
        allow_warmup_trade=args.allow_warmup_trade,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5-Min Bot v7")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="Run deterministic mock scenario")
    mode.add_argument("--paper", action="store_true", help="Run paper mode against public APIs")
    mode.add_argument("--live", action="store_true", help="Run live trading mode")

    parser.add_argument("--test", action="store_true", help="Test public API connectivity")
    parser.add_argument("--mock-scenario", default="true_dutch", help="Mock scenario name")
    parser.add_argument("--windows", type=int, default=6)

    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--observe", type=int, default=55)
    parser.add_argument("--freeze", type=int, default=35)

    parser.add_argument("--cash", type=float, default=10.0)
    parser.add_argument("--bet", type=float, default=2.0)
    parser.add_argument("--cash-buffer", type=float, default=0.05)
    parser.add_argument("--stop-loss", type=float, default=5.0)
    parser.add_argument("--take-profit", type=float, default=3.0)

    parser.add_argument("--entry", type=float, default=0.38)
    parser.add_argument("--strong-entry", type=float, default=0.22)
    parser.add_argument("--no-trade-entry", type=float, default=0.12)
    parser.add_argument("--max-momentum-price", type=float, default=0.68)
    parser.add_argument("--min-momentum-price", type=float, default=0.12)
    parser.add_argument("--min-entry-edge", type=float, default=0.06)
    parser.add_argument("--min-entry-seconds-left", type=int, default=135)
    parser.add_argument("--no-entry-first-seconds", type=int, default=55)
    parser.add_argument("--momentum-confirm-ticks", type=int, default=4)
    parser.add_argument("--min-momentum-delta", type=float, default=0.02)

    parser.add_argument("--trend-lookback", type=int, default=10)
    parser.add_argument("--strong-trend-moves", type=int, default=7)
    parser.add_argument("--strong-trend-delta", type=float, default=0.18)

    parser.add_argument("--min-dutch-profit", type=float, default=0.05)
    parser.add_argument("--max-hedge-fraction", type=float, default=0.85)
    parser.add_argument("--max-live-order-budget-multiplier", type=float, default=1.25)
    parser.add_argument("--disable-live-min-order-sim", action="store_true")
    parser.add_argument("--min-exchange-order-shares", type=float, default=5.0)

    parser.add_argument("--max-spread-gap", type=float, default=0.08)
    parser.add_argument("--max-price-age", type=float, default=8.0)
    parser.add_argument("--min-ticks-before-trade", type=int, default=8)
    parser.add_argument("--allow-warmup-trade", action="store_true")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_signal_handlers()

    if args.test:
        connectivity_test()
        return

    cfg = build_config(args)
    if args.live:
        mode = "live"
    elif args.paper:
        mode = "paper"
    else:
        mode = "mock"

    run_session(
        num_windows=args.windows,
        mode=mode,
        cfg=cfg,
        mock_scenario=args.mock_scenario,
    )


if __name__ == "__main__":
    main()