"""
Polymarket Trader
=================
Places orders on Polymarket BTC 5-min up/down markets based on predictions.
Reuses the proven order placement logic from v7 with prediction-driven entries.
"""

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("trader")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
CHAIN_ID = 137


@dataclass
class TradeRecord:
    timestamp: float
    window_ts: int
    side: str            # "UP" or "DOWN"
    price: float
    shares: float
    cost: float
    prediction_confidence: float
    is_live: bool
    market_slug: str = ""
    market_question: str = ""
    target_price: Optional[float] = None
    target_source: str = "unknown"
    polymarket_current_price: Optional[float] = None
    entry_btc_price: Optional[float] = None
    settlement_source: str = "pending"
    outcome: Optional[str] = None   # "WIN", "LOSS", or None if pending
    pnl: Optional[float] = None

    @property
    def dt(self) -> str:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).strftime("%H:%M:%S")


@dataclass
class TraderConfig:
    bet_size: float = 2.0
    min_confidence: float = 0.60       # minimum prediction confidence to trade
    high_confidence: float = 0.75      # threshold for larger bet
    high_confidence_multiplier: float = 1.5
    max_open_positions: int = 2
    session_stop_loss: float = 10.0
    session_take_profit: float = 15.0
    min_entry_seconds: int = 30        # min seconds into window before entry
    max_entry_seconds: int = 200       # max seconds into window for entry
    paper_mode: bool = True            # default to paper


class PolymarketTrader:
    """Manages Polymarket BTC 5-min trading based on predictions."""

    def __init__(self, config: Optional[TraderConfig] = None):
        self.config = config or TraderConfig()
        self.trades: List[TradeRecord] = []
        self.total_pnl: float = 0.0
        self.total_trades: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.cash: float = 10.0
        self._client = None
        self._initialized = False
        self._imports: Dict = {}

    def connect_live(self) -> bool:
        """Connect to Polymarket for live trading."""
        try:
            from py_clob_client_v2 import ClobClient, AssetType, BalanceAllowanceParams
            self._imports = {
                "AssetType": AssetType,
                "BalanceAllowanceParams": BalanceAllowanceParams,
            }
        except ImportError:
            log.error("Missing py-clob-client-v2")
            return False

        private_key = os.getenv("POLY_PRIVATE_KEY")
        if not private_key:
            log.error("POLY_PRIVATE_KEY not set")
            return False

        try:
            from py_clob_client_v2 import ClobClient as CC
            self._client = CC(
                host=CLOB_API,
                key=private_key,
                chain_id=CHAIN_ID,
                signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "3")),
                funder=os.getenv("POLY_FUNDER"),
            )
            creds = self._client.derive_api_key()
            self._client.set_api_creds(creds)
            self._initialized = True
            self.cash = self._get_balance()
            self.config.paper_mode = False
            log.info(f"Live connected. Balance: ${self.cash:.2f}")
            return True
        except Exception as e:
            log.error(f"Live connect failed: {e}")
            return False

    def _get_balance(self) -> float:
        if not self._client:
            return 0.0
        try:
            params_cls = self._imports["BalanceAllowanceParams"]
            asset_type = self._imports["AssetType"].COLLATERAL
            resp = self._client.get_balance_allowance(params_cls(asset_type=asset_type))
            return int(resp.get("balance", 0)) / 1e6
        except Exception:
            return 0.0

    def find_current_market(self) -> Optional[Dict]:
        """Find the current BTC 5-min up/down market on Polymarket."""
        now = int(time.time())
        window_ts = now - (now % 300)
        return self.find_market_by_window(window_ts)

    def find_market_by_window(self, window_ts: int) -> Optional[Dict]:
        """Find a BTC 5-minute market by its Unix window timestamp."""

        slug = f"btc-updown-5m-{window_ts}"
        for endpoint in ("events", "markets"):
            try:
                resp = requests.get(
                    f"{GAMMA_API}/{endpoint}",
                    params={"slug": slug, "limit": 1},
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                if not data:
                    continue

                raw = data[0]
                market = raw.get("markets", [{}])[0] if endpoint == "events" else raw
                outcomes = market.get("outcomes", "[]")
                token_ids = market.get("clobTokenIds", "[]")
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if isinstance(token_ids, str):
                    token_ids = json.loads(token_ids)

                up_idx = dn_idx = None
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

                return {
                    "slug": slug,
                    "window_ts": window_ts,
                    "question": market.get("question", slug),
                    "up_token": str(token_ids[up_idx]).strip(),
                    "down_token": str(token_ids[dn_idx]).strip(),
                    "window_end": window_ts + 300,
                    "target_price": self._extract_target_price(market, raw),
                    "target_source": "polymarket" if self._extract_target_price(market, raw) else "unknown",
                    "polymarket_current_price": self._extract_current_price(market, raw),
                    "raw_market": market,
                }
            except Exception:
                continue
        return None

    def find_nearby_markets(self, count: int = 3) -> List[Dict]:
        """Return current and next BTC 5-minute markets when available."""
        now = int(time.time())
        window_ts = now - (now % 300)
        markets = []
        for i in range(count):
            market = self.find_market_by_window(window_ts + i * 300)
            if market:
                markets.append(market)
        return markets

    def _extract_target_price(self, market: Dict, event: Optional[Dict] = None) -> Optional[float]:
        """Best-effort extraction of the Polymarket price-to-beat from API payloads."""
        candidates = []
        for obj in (market, event or {}):
            for key in (
                "targetPrice", "target_price", "strike", "strikePrice", "strike_price",
                "priceToBeat", "price_to_beat", "startPrice", "start_price",
            ):
                value = obj.get(key) if isinstance(obj, dict) else None
                if value not in (None, ""):
                    candidates.append(str(value))
            for key in ("question", "description", "title", "subtitle", "rules"):
                value = obj.get(key) if isinstance(obj, dict) else None
                if value:
                    candidates.append(str(value))

        for text in candidates:
            match = re.search(r"\$?\s*([0-9]{2,3}(?:,[0-9]{3})+(?:\.\d+)?)", text)
            if match:
                return float(match.group(1).replace(",", ""))
        return None

    def _extract_current_price(self, market: Dict, event: Optional[Dict] = None) -> Optional[float]:
        """Best-effort extraction of Polymarket's displayed BTC current price."""
        candidates = []
        for obj in (market, event or {}):
            for key in (
                "currentPrice", "current_price", "currentValue", "current_value",
                "oraclePrice", "oracle_price", "lastPrice", "last_price",
            ):
                value = obj.get(key) if isinstance(obj, dict) else None
                if value not in (None, ""):
                    candidates.append(str(value))
            for key in ("question", "description", "title", "subtitle", "rules"):
                value = obj.get(key) if isinstance(obj, dict) else None
                if value:
                    candidates.append(str(value))

        patterns = [
            r"current\s+price[^0-9$]*\$?\s*([0-9]{2,3}(?:,[0-9]{3})+(?:\.\d+)?)",
            r"currently[^0-9$]*\$?\s*([0-9]{2,3}(?:,[0-9]{3})+(?:\.\d+)?)",
            r"oracle[^0-9$]*\$?\s*([0-9]{2,3}(?:,[0-9]{3})+(?:\.\d+)?)",
        ]
        for text in candidates:
            lowered = text.lower()
            for pattern in patterns:
                match = re.search(pattern, lowered)
                if match:
                    return float(match.group(1).replace(",", ""))
        return None

    def get_resolved_winner(self, market_or_slug) -> Optional[str]:
        """Ask Gamma for the actual resolved winning outcome, if available."""
        slug = market_or_slug if isinstance(market_or_slug, str) else market_or_slug.get("slug")
        if not slug:
            return None

        for endpoint in ("events", "markets"):
            try:
                resp = requests.get(
                    f"{GAMMA_API}/{endpoint}",
                    params={"slug": slug, "limit": 1},
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                if not data:
                    continue
                raw = data[0]
                market = raw.get("markets", [{}])[0] if endpoint == "events" else raw
                winner = self._extract_winner(market) or self._extract_winner(raw)
                if winner:
                    return winner
            except Exception:
                continue
        return None

    def _extract_winner(self, payload: Dict) -> Optional[str]:
        """Handle several possible Gamma resolved-market shapes."""
        if not isinstance(payload, dict):
            return None
        for key in ("winner", "winningOutcome", "winning_outcome", "resolvedOutcome", "resolved_outcome"):
            value = payload.get(key)
            if value is not None:
                label = str(value).strip().lower()
                if label in ("up", "yes"):
                    return "UP"
                if label in ("down", "no"):
                    return "DOWN"

        outcomes = payload.get("outcomes", "[]")
        prices = (
            payload.get("outcomePrices")
            or payload.get("outcome_prices")
            or payload.get("prices")
            or "[]"
        )
        try:
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                prices = json.loads(prices)
            if len(outcomes) == len(prices) and prices:
                best_i = max(range(len(prices)), key=lambda i: float(prices[i]))
                if float(prices[best_i]) >= 0.99:
                    label = str(outcomes[best_i]).strip().lower()
                    if label == "up":
                        return "UP"
                    if label == "down":
                        return "DOWN"
        except Exception:
            pass
        return None

    def get_market_prices(self, market: Dict) -> Optional[Dict[str, float]]:
        """Get current UP/DOWN prices from Polymarket."""
        prices = {}
        for side, token_key in [("UP", "up_token"), ("DOWN", "down_token")]:
            token_id = market[token_key]
            for method in ("midpoint", "price"):
                try:
                    params = {"token_id": token_id}
                    if method == "price":
                        params["side"] = "buy"
                    resp = requests.get(f"{CLOB_API}/{method}", params=params, timeout=5)
                    if resp.status_code != 200:
                        continue
                    payload = resp.json()
                    value = payload.get("mid") or payload.get("price")
                    if value:
                        p = float(value)
                        if 0.005 < p < 0.995:
                            prices[side] = p
                            break
                except Exception:
                    continue

        if "UP" in prices and "DOWN" not in prices:
            prices["DOWN"] = round(1 - prices["UP"], 4)
        elif "DOWN" in prices and "UP" not in prices:
            prices["UP"] = round(1 - prices["DOWN"], 4)

        return prices if "UP" in prices and "DOWN" in prices else None

    def execute_trade(
        self, market: Dict, side: str, confidence: float, entry_btc_price: Optional[float] = None
    ) -> Optional[TradeRecord]:
        """Execute a trade based on prediction."""
        now = time.time()
        window_ts = market["window_ts"]
        elapsed = now - window_ts
        seconds_left = market["window_end"] - now

        # Timing checks
        if elapsed < self.config.min_entry_seconds:
            return None
        if elapsed > self.config.max_entry_seconds:
            return None

        # Confidence check
        if confidence < self.config.min_confidence:
            return None

        # Stop loss / take profit
        if self.total_pnl <= -self.config.session_stop_loss:
            log.warning(f"Session stop loss reached: ${self.total_pnl:+.2f}")
            return None
        if self.total_pnl >= self.config.session_take_profit:
            log.info(f"Session take profit reached: ${self.total_pnl:+.2f}")
            return None

        # Get market prices
        prices = self.get_market_prices(market)
        if not prices:
            return None

        price = prices[side]
        token_id = market["up_token"] if side == "UP" else market["down_token"]

        # Bet sizing
        bet = self.config.bet_size
        if confidence >= self.config.high_confidence:
            bet *= self.config.high_confidence_multiplier

        bet = min(bet, self.cash - 0.05)
        if bet <= 0:
            return None

        shares = math.floor((bet / price) * 100) / 100
        cost = round(shares * price, 4)

        is_live = False

        if not self.config.paper_mode and self._initialized and self._client:
            # Live order
            try:
                from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions, Side

                book = self._client.get_order_book(token_id)
                asks = book.get("asks", []) if isinstance(book, dict) else getattr(book, "asks", [])
                if not asks:
                    log.error("No asks available")
                    return None

                ask0 = asks[0]
                ask = float(ask0["price"] if isinstance(ask0, dict) else ask0.price)
                min_size = float(book.get("min_order_size", 5) if isinstance(book, dict) else getattr(book, "min_order_size", 5))
                tick_size = str(book.get("tick_size", "0.01") if isinstance(book, dict) else getattr(book, "tick_size", "0.01"))
                neg_risk = bool(book.get("neg_risk", False) if isinstance(book, dict) else getattr(book, "neg_risk", False))

                size = max(min_size, math.floor((bet / ask) * 100) / 100)
                actual_cost = round(size * ask, 4)

                if actual_cost > bet * 1.5:
                    log.warning(f"Min order ${actual_cost:.2f} > budget ${bet:.2f}")
                    return None

                options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
                response = self._client.create_and_post_order(
                    OrderArgs(token_id=token_id, price=ask, size=size, side=Side.BUY),
                    options=options,
                )

                if isinstance(response, dict) and response.get("success"):
                    price = ask
                    shares = size
                    cost = actual_cost
                    is_live = True
                    log.info(f"LIVE ORDER: {side} {shares:.2f}sh @ {price:.3f} = ${cost:.2f}")
                else:
                    error = response.get("errorMsg", "unknown") if isinstance(response, dict) else "unknown"
                    log.error(f"Live order failed: {error}")
                    return None
            except Exception as e:
                log.error(f"Live order error: {e}")
                return None
        else:
            log.info(f"PAPER: {side} {shares:.0f}sh @ {price:.3f} = ${cost:.2f} (conf={confidence:.1%})")

        self.cash -= cost
        self.total_trades += 1

        record = TradeRecord(
            timestamp=now,
            window_ts=window_ts,
            side=side,
            price=price,
            shares=shares,
            cost=cost,
            prediction_confidence=confidence,
            is_live=is_live,
            market_slug=market.get("slug", ""),
            market_question=market.get("question", ""),
            target_price=market.get("target_price"),
            target_source=market.get("target_source", "unknown"),
            polymarket_current_price=market.get("polymarket_current_price"),
            entry_btc_price=entry_btc_price,
        )
        self.trades.append(record)
        return record

    def resolve_trade(self, trade: TradeRecord, winner: str):
        """Resolve a trade when the window ends."""
        if trade.outcome is not None:
            return  # already resolved

        if trade.side == winner:
            trade.outcome = "WIN"
            trade.pnl = round(trade.shares - trade.cost, 4)
            self.wins += 1
        else:
            trade.outcome = "LOSS"
            trade.pnl = round(-trade.cost, 4)
            self.losses += 1

        self.total_pnl += trade.pnl
        self.cash += trade.shares if trade.side == winner else 0

        if self._initialized and not self.config.paper_mode:
            self.cash = self._get_balance()

    def get_pending_trades(self) -> List[TradeRecord]:
        return [t for t in self.trades if t.outcome is None]

    def get_stats(self) -> Dict:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.wins / max(1, self.wins + self.losses), 4),
            "total_pnl": round(self.total_pnl, 4),
            "cash": round(self.cash, 4),
            "paper_mode": self.config.paper_mode,
        }
