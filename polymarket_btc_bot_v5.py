"""
Polymarket BTC 5-Min Hybrid Bot v5 — LIVE TRADING
===================================================
Momentum-first strategy + dutch book hedge.

KEY INSIGHT from backtesting:
  - Buying the CHEAP side = betting AGAINST the trend → loses often
  - Buying the EXPENSIVE side = betting WITH the trend → wins more
  - Solution: First leg = MOMENTUM (buy trending side when other is cheap)
             Second leg = DUTCH (if reversal, buy the now-cheap side)

Strategy:
  Phase 1 (0-45s):    OBSERVE — watch direction
  Phase 2 (45s-270s): TRADE
    → If a side drops below entry_threshold:
      - Buy the OTHER (expensive/trending) side ← MOMENTUM
    → If we have one side AND the other drops below entry_threshold:
      - Buy it too → DUTCH BOOK complete
  Phase 3 (last 30s):  FREEZE — no new trades

Modes:
  --paper    Paper trading (default, no real money)
  --live     Real trading with USDC on Polygon

Setup for live trading:
  1. pip install py-clob-client
  2. Create .env file with:
     POLY_PRIVATE_KEY=0x...
     POLY_FUNDER=0x...           (your Polymarket deposit address)
     POLY_SIGNATURE_TYPE=1       (1 for email/Magic, 0 for MetaMask/EOA)
  3. Ensure you have USDC on Polygon deposited in Polymarket
  4. python polymarket_btc_bot_v5.py --live --windows 6

Usage:
  python polymarket_btc_bot_v5.py --paper --windows 6
  python polymarket_btc_bot_v5.py --live --windows 3 --bet 2 --entry 0.38
"""

import json
import time
import logging
import argparse
import os
import requests
import signal
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field
from pathlib import Path

# ─── Try loading dotenv ──────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── Config ──────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
CHAIN_ID  = 137  # Polygon

WINDOW_SECONDS = 300
POLL_INTERVAL  = 2.0

# Strategy defaults
ENTRY_THRESHOLD = 0.38
OBSERVE_PERIOD  = 45
FREEZE_PERIOD   = 30
BET_SIZE        = 2.0
START_CASH      = 10.0
DUTCH_MAX_COST  = 0.85  # Max combined cost for dutch book
MIN_MOMENTUM_PRICE = 0.55  # Do not buy the expensive side unless it is clearly leading
MAX_MOMENTUM_PRICE = 0.72  # Avoid chasing very expensive late moves
MIN_DUTCH_PROFIT   = 0.05  # Minimum guaranteed profit after hedge, in USD
REQUIRE_CONFIRM_TICKS = 2  # Momentum signal must persist for this many ticks

# ─── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("polybot")

ROOT = Path(__file__).resolve().parent
AGGREGATE_FILE = ROOT / "aggregated_bot_results.json"
SCRIPT_NAME = "polymarket_btc_bot_v5"
OUTPUT_FILENAME = "bot_session_v5_paper.json"
INTERRUPTED = False


def setup_signal_handlers():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def signal_handler(signum, frame):
    global INTERRUPTED
    INTERRUPTED = True
    log.info("  ⚠️ Ctrl+C detected: will save partial results...")


def append_aggregate(result: dict, status: str, output_file: str):
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



# ─── Live Trading Client ─────────────────────────────────────────
class LiveTrader:
    """Wrapper around py_clob_client for real order execution."""
    
    def __init__(self):
        self.client = None
        self.initialized = False
    
    def connect(self):
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import (
                MarketOrderArgs, OrderArgs, OrderType,
                BalanceAllowanceParams, AssetType
            )
            self.MarketOrderArgs = MarketOrderArgs
            self.OrderArgs = OrderArgs
            self.OrderType = OrderType
            self.BalanceAllowanceParams = BalanceAllowanceParams
            self.AssetType = AssetType
            
            from py_clob_client.order_builder.constants import BUY, SELL
            self.BUY = BUY
            self.SELL = SELL
        except ImportError:
            log.error("❌ py-clob-client not installed!")
            log.error("   Run: pip install py-clob-client")
            return False
        
        private_key = os.getenv("POLY_PRIVATE_KEY")
        funder = os.getenv("POLY_FUNDER")
        sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))
        
        if not private_key:
            log.error("❌ POLY_PRIVATE_KEY not set in .env")
            return False
        
        try:
            self.client = ClobClient(
                CLOB_API,
                key=private_key,
                chain_id=CHAIN_ID,
                signature_type=sig_type,
                funder=funder,
            )
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            
            # Check balance
            bal = self.client.get_balance_allowance(
                self.BalanceAllowanceParams(asset_type=self.AssetType.COLLATERAL)
            )
            balance_usd = int(bal.get("balance", 0)) / 1e6
            log.info(f"   ✅ Connected! Balance: ${balance_usd:.2f} USDC")
            self.initialized = True
            return True
            
        except Exception as e:
            log.error(f"   ❌ Connection failed: {e}")
            return False
    
    def get_balance(self) -> float:
        if not self.initialized:
            return 0.0
        try:
            bal = self.client.get_balance_allowance(
                self.BalanceAllowanceParams(asset_type=self.AssetType.COLLATERAL)
            )
            return int(bal.get("balance", 0)) / 1e6
        except:
            return 0.0
    
    def market_buy(self, token_id: str, amount_usd: float) -> Optional[dict]:
        """Place a market buy order (FOK) for amount_usd worth of tokens."""
        if not self.initialized:
            return None
        
        try:
            order_args = self.MarketOrderArgs(
                token_id=token_id,
                amount=amount_usd,
                side=self.BUY,
                order_type=self.OrderType.FOK,
            )
            signed = self.client.create_market_order(order_args)
            resp = self.client.post_order(signed, self.OrderType.FOK)
            log.info(f"   📤 Order response: {resp}")
            return resp
        except Exception as e:
            log.error(f"   ❌ Order failed: {e}")
            return None


# ─── Portfolio (Paper + Live) ────────────────────────────────────
@dataclass
class Trade:
    time_s: int
    side: str
    price: float
    shares: float
    cost: float
    real: bool = False  # True if executed on-chain

@dataclass
class Portfolio:
    cash: float
    is_live: bool = False
    trader: Optional[LiveTrader] = None
    up_shares: float = 0.0
    up_avg: float = 0.0
    dn_shares: float = 0.0
    dn_avg: float = 0.0
    trades: list = field(default_factory=list)
    windows_played: int = 0
    windows_won: int = 0
    total_pnl: float = 0.0

    def buy(self, side: str, price: float, amount_usd: float, time_s: int,
            token_id: str = "") -> Optional[Trade]:
        if price is None or price <= 0:
            return None
        if amount_usd > self.cash:
            amount_usd = self.cash
        if amount_usd < 0.10:
            return None
    def preview_buy(self, side: str, price: float, amount_usd: float) -> Optional[dict]:
        """Preview position state after a buy without mutating portfolio."""
        if price is None or price <= 0:
            return None
        amount = min(amount_usd, self.cash)
        if amount < 0.10:
            return None

        shares = round(amount / price, 2)
        cost = round(shares * price, 4)

        up_shares = self.up_shares
        up_avg = self.up_avg
        dn_shares = self.dn_shares
        dn_avg = self.dn_avg

        if side == "UP":
            total_cost = up_avg * up_shares + cost
            up_shares = round(up_shares + shares, 2)
            up_avg = round(total_cost / up_shares, 4) if up_shares > 0 else 0
        else:
            total_cost = dn_avg * dn_shares + cost
            dn_shares = round(dn_shares + shares, 2)
            dn_avg = round(total_cost / dn_shares, 4) if dn_shares > 0 else 0

        spent = sum(t.cost for t in self.trades) + cost
        worst_payout = min(up_shares, dn_shares) if up_shares > 0 and dn_shares > 0 else 0.0
        guaranteed_pnl = round(worst_payout - spent, 4)

        return {
            "shares": shares,
            "cost": cost,
            "up_shares": up_shares,
            "up_avg": up_avg,
            "dn_shares": dn_shares,
            "dn_avg": dn_avg,
            "spent": spent,
            "worst_payout": worst_payout,
            "guaranteed_pnl": guaranteed_pnl,
        }

        # ── Live execution ──
        real_executed = False
        if self.is_live and self.trader and self.trader.initialized and token_id:
            log.info(f"   💸 LIVE ORDER: {side} ${amount_usd:.2f} @ ~{price:.3f}")
            resp = self.trader.market_buy(token_id, amount_usd)
            if resp:
                real_executed = True
                log.info(f"   ✅ FILLED")
            else:
                log.warning(f"   ⚠️ Order failed, recording as paper trade")

        shares = round(amount_usd / price, 2)
        cost = round(shares * price, 4)
        self.cash = round(self.cash - cost, 4)

        if side == "UP":
            total_cost = self.up_avg * self.up_shares + cost
            self.up_shares = round(self.up_shares + shares, 2)
            self.up_avg = round(total_cost / self.up_shares, 4) if self.up_shares > 0 else 0
        else:
            total_cost = self.dn_avg * self.dn_shares + cost
            self.dn_shares = round(self.dn_shares + shares, 2)
            self.dn_avg = round(total_cost / self.dn_shares, 4) if self.dn_shares > 0 else 0

        trade = Trade(time_s=time_s, side=side, price=price, shares=shares,
                      cost=cost, real=real_executed)
        self.trades.append(trade)
        return trade

    def resolve(self, winner: str):
        payout = self.up_shares * 1.0 if winner == "UP" else self.dn_shares * 1.0
        spent = sum(t.cost for t in self.trades)
        self.cash = round(self.cash + payout, 4)
        pnl = round(payout - spent, 4)
        self.total_pnl = round(self.total_pnl + pnl, 4)
        self.windows_played += 1
        if pnl > 0:
            self.windows_won += 1
        return {"winner": winner, "payout": payout, "spent": spent, "pnl": pnl}

    def reset_window(self):
        self.up_shares = 0.0
        self.up_avg = 0.0
        self.dn_shares = 0.0
        self.dn_avg = 0.0
        self.trades = []

    @property
    def has_up(self): return self.up_shares > 0
    @property
    def has_dn(self): return self.dn_shares > 0
    @property
    def is_dutch(self): return self.has_up and self.has_dn


# ─── Market Discovery ────────────────────────────────────────────
def get_current_window_ts() -> int:
    now = int(time.time())
    return now - (now % WINDOW_SECONDS)

def find_market(window_ts: int) -> Optional[dict]:
    slug = f"btc-updown-5m-{window_ts}"
    for endpoint in ["events", "markets"]:
        try:
            resp = requests.get(f"{GAMMA_API}/{endpoint}",
                                params={"slug": slug, "limit": 1}, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if not data:
                continue
            market = data[0].get("markets", [{}])[0] if endpoint == "events" else data[0]
            
            outcomes = market.get("outcomes", "[]")
            if isinstance(outcomes, str): outcomes = json.loads(outcomes)
            tokens = market.get("clobTokenIds", "[]")
            if isinstance(tokens, str): tokens = json.loads(tokens)
            
            up_idx = down_idx = None
            for i, o in enumerate(outcomes):
                low = str(o).strip().lower()
                if low == "up": up_idx = i
                elif low == "down": down_idx = i
            
            if up_idx is not None and down_idx is not None and len(tokens) > max(up_idx, down_idx):
                return {
                    "slug": slug,
                    "question": market.get("question", slug),
                    "up_token": tokens[up_idx].strip(),
                    "down_token": tokens[down_idx].strip(),
                }
        except Exception as e:
            log.debug(f"  {endpoint} error: {e}")
    return None


# ─── Price Fetching ──────────────────────────────────────────────
def fetch_clob_price(token_id: str) -> Optional[float]:
    for method in ["midpoint", "price", "last-trade-price"]:
        try:
            params = {"token_id": token_id}
            if method == "price": params["side"] = "buy"
            resp = requests.get(f"{CLOB_API}/{method}", params=params, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                val = data.get("mid") or data.get("price")
                if val and 0.005 < float(val) < 0.995:
                    return float(val)
        except:
            pass
    return None

def fetch_prices(market: dict) -> Optional[dict]:
    up = fetch_clob_price(market["up_token"])
    dn = fetch_clob_price(market["down_token"])
    if up and not dn: dn = round(1.0 - up, 4)
    elif dn and not up: up = round(1.0 - dn, 4)
    if up and dn:
        return {"up": up, "down": dn}
    # Gamma fallback
    try:
        resp = requests.get(f"{GAMMA_API}/events",
                            params={"slug": market["slug"], "limit": 1}, timeout=8)
        if resp.status_code == 200:
            events = resp.json()
            if events:
                m = events[0].get("markets", [{}])[0]
                prices = m.get("outcomePrices", "[]")
                if isinstance(prices, str): prices = json.loads(prices)
                outcomes = m.get("outcomes", "[]")
                if isinstance(outcomes, str): outcomes = json.loads(outcomes)
                up_p = dn_p = None
                for i, o in enumerate(outcomes):
                    if str(o).strip().lower() == "up": up_p = float(prices[i])
                    elif str(o).strip().lower() == "down": dn_p = float(prices[i])
                if up_p and dn_p:
                    return {"up": up_p, "down": dn_p}
    except: pass
    return None


# ─── MOMENTUM-FIRST Hybrid Strategy ─────────────────────────────
def trade_window(window_ts: int, market: dict, portfolio: Portfolio) -> Optional[dict]:
    window_end = window_ts + WINDOW_SECONDS
    if time.time() >= window_end - 5:
        return None

    portfolio.reset_window()
    mode_label = "🔴 LIVE" if portfolio.is_live else "📝 PAPER"

    log.info(f"")
    log.info(f"{'━'*78}")
    log.info(f"  🎯 {market['question']}  [{mode_label}]")
    utc_s = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime('%H:%M:%S')
    utc_e = datetime.fromtimestamp(window_end, tz=timezone.utc).strftime('%H:%M:%S')
    log.info(f"  ⏰ {utc_s} → {utc_e} UTC  │  💰 ${portfolio.cash:.2f}  │  PnL: ${portfolio.total_pnl:+.2f}")
    log.info(f"  📋 MOMENTUM strategy: trend with → dutch on reversal")
    log.info(f"{'━'*78}")
    log.info(f"  {'Time':>8} {'Left':>4} {'Phase':>7} {'Up':>6} {'Down':>6} {'Pos':>12} {'Action'}")
    log.info(f"  {'─'*8} {'─'*4} {'─'*7} {'─'*6} {'─'*6} {'─'*12} {'─'*35}")

    up_min = 1.0
    down_min = 1.0
    last_up = 0.5
    last_dn = 0.5
    tick_count = 0
    swings = 0
    last_leader = None
    cheap_up_confirm = 0
    cheap_dn_confirm = 0

    while time.time() < window_end - 3:
        remaining = window_end - time.time()
        elapsed = time.time() - window_ts

        prices = fetch_prices(market)
        if not prices:
            time.sleep(POLL_INTERVAL)
            continue

        up = prices["up"]
        dn = prices["down"]
        last_up = up
        last_dn = dn
        up_min = min(up_min, up)
        down_min = min(down_min, dn)
        cheap_up_confirm = cheap_up_confirm + 1 if up <= ENTRY_THRESHOLD else 0
        cheap_dn_confirm = cheap_dn_confirm + 1 if dn <= ENTRY_THRESHOLD else 0

        leader = "up" if up > dn else ("down" if dn > up else "tie")
        if last_leader and leader != last_leader and leader != "tie":
            swings += 1
        last_leader = leader

        # Phase
        if elapsed < OBSERVE_PERIOD:
            phase = "👁 WATCH"
        elif remaining < FREEZE_PERIOD:
            phase = "🧊 FREEZE"
        else:
            phase = "⚡ TRADE"

        # Position display
        if portfolio.is_dutch:
            pos_str = "🎯 DUTCH"
        elif portfolio.has_up:
            pos_str = f"↑{portfolio.up_shares:.0f}@{portfolio.up_avg:.2f}"
        elif portfolio.has_dn:
            pos_str = f"↓{portfolio.dn_shares:.0f}@{portfolio.dn_avg:.2f}"
        else:
            pos_str = "—"

        action = ""

        if phase == "⚡ TRADE":
            # ═══ FILTERED MOMENTUM + TRUE DUTCH CHECK ═══
            # First leg:
            #   Buy the leading side only if the opposite side is cheap for multiple ticks.
            #   Also avoid chasing when the leading side is already too expensive.
            # Second leg:
            #   Only hedge if the post-hedge worst-case payout beats total spent.

            if not portfolio.has_up and not portfolio.has_dn:
                # Up is cheap = BTC is trending DOWN → buy DOWN, but only if not too expensive.
                if cheap_up_confirm >= REQUIRE_CONFIRM_TICKS and MIN_MOMENTUM_PRICE <= dn <= MAX_MOMENTUM_PRICE:
                    trade = portfolio.buy("DN", dn, BET_SIZE, int(elapsed),
                                         token_id=market["down_token"])
                    if trade:
                        real_tag = " 🔴LIVE" if trade.real else ""
                        action = f"📈 MOMENTUM DN {trade.shares:.0f}sh @{dn:.3f} (${trade.cost:.2f}){real_tag}"

                # Down is cheap = BTC is trending UP → buy UP, but only if not too expensive.
                elif cheap_dn_confirm >= REQUIRE_CONFIRM_TICKS and MIN_MOMENTUM_PRICE <= up <= MAX_MOMENTUM_PRICE:
                    trade = portfolio.buy("UP", up, BET_SIZE, int(elapsed),
                                         token_id=market["up_token"])
                    if trade:
                        real_tag = " 🔴LIVE" if trade.real else ""
                        action = f"📈 MOMENTUM UP {trade.shares:.0f}sh @{up:.3f} (${trade.cost:.2f}){real_tag}"

            elif portfolio.has_dn and not portfolio.has_up:
                # We have DOWN. Buy UP only if it creates a true positive worst-case dutch.
                if cheap_up_confirm >= REQUIRE_CONFIRM_TICKS:
                    preview = portfolio.preview_buy("UP", up, BET_SIZE)
                    if preview and preview["guaranteed_pnl"] >= MIN_DUTCH_PROFIT:
                        trade = portfolio.buy("UP", up, BET_SIZE, int(elapsed),
                                             token_id=market["up_token"])
                        if trade:
                            real_tag = " 🔴LIVE" if trade.real else ""
                            action = (
                                f"🎯 TRUE DUTCH UP {trade.shares:.0f}sh @{up:.3f} "
                                f"guaranteed=${preview['guaranteed_pnl']:+.2f}{real_tag}"
                            )

            elif portfolio.has_up and not portfolio.has_dn:
                # We have UP. Buy DN only if it creates a true positive worst-case dutch.
                if cheap_dn_confirm >= REQUIRE_CONFIRM_TICKS:
                    preview = portfolio.preview_buy("DN", dn, BET_SIZE)
                    if preview and preview["guaranteed_pnl"] >= MIN_DUTCH_PROFIT:
                        trade = portfolio.buy("DN", dn, BET_SIZE, int(elapsed),
                                             token_id=market["down_token"])
                        if trade:
                            real_tag = " 🔴LIVE" if trade.real else ""
                            action = (
                                f"🎯 TRUE DUTCH DN {trade.shares:.0f}sh @{dn:.3f} "
                                f"guaranteed=${preview['guaranteed_pnl']:+.2f}{real_tag}"
                            )

        now_str = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        log.info(
            f"  {now_str} {remaining:3.0f}s {phase:>7} {up:5.3f}  {dn:5.3f}  {pos_str:>12} {action}"
        )
        tick_count += 1
        time.sleep(POLL_INTERVAL)

    # ── Resolution ──
    final = fetch_prices(market)
    if final:
        last_up = final["up"]
        last_dn = final["down"]
    winner = "UP" if last_up > 0.5 else "DN"

    log.info(f"")
    log.info(f"  ┌─────────────────────────────────────────────────────┐")
    log.info(f"  │  ⏱  WINDOW ENDED — Final: Up={last_up:.3f} Dn={last_dn:.3f}")
    log.info(f"  │  🏆 Winner: {winner}")

    if portfolio.trades:
        res = portfolio.resolve(winner)
        log.info(f"  │  💰 Payout: ${res['payout']:.2f}  Spent: ${res['spent']:.2f}  PnL: ${res['pnl']:+.2f}")
        log.info(f"  │  📊 Cash: ${portfolio.cash:.2f}  │  Session: ${portfolio.total_pnl:+.2f}")
        if portfolio.is_dutch:
            log.info(f"  │  🎯 Dutch book was active!")
        else:
            log.info(f"  │  📈 Momentum bet: {portfolio.trades[0].side}")
        log.info(f"  │  ── Trades ──")
        for t in portfolio.trades:
            real_tag = " 🔴" if t.real else ""
            log.info(f"  │    {t.time_s:>3}s  {t.side:>2}  {t.shares:>5.0f}sh  @{t.price:.3f}  ${t.cost:.2f}{real_tag}")
    else:
        log.info(f"  │  📭 No trades — no entry signal")
        res = {"pnl": 0}

    log.info(f"  │  📈 Swings: {swings}  Up☟={up_min:.3f}  Dn☟={down_min:.3f}")
    log.info(f"  └─────────────────────────────────────────────────────┘")

    return {
        "window_ts": window_ts, "question": market["question"],
        "ticks": tick_count, "swings": swings, "winner": winner,
        "trades": len(portfolio.trades),
        "dutch": portfolio.is_dutch if portfolio.trades else False,
        "pnl": res["pnl"], "up_min": up_min, "down_min": down_min,
        "had_real_trades": any(t.real for t in portfolio.trades),
    }


# ─── Main Loop ───────────────────────────────────────────────────
def run_bot(num_windows: int, live: bool):
    trader = None
    if live:
        trader = LiveTrader()
        log.info(f"\n  🔌 Connecting to Polymarket CLOB...")
        if not trader.connect():
            log.error("  ❌ Failed to connect. Check .env file.")
            return
        balance = trader.get_balance()
        portfolio = Portfolio(cash=balance, is_live=True, trader=trader)
    else:
        portfolio = Portfolio(cash=START_CASH, is_live=False)

    results = []
    status = "completed"
    mode = "🔴 LIVE TRADING" if live else "📝 PAPER TRADING"

    log.info(f"")
    log.info(f"  ╔═══════════════════════════════════════════════════════╗")
    log.info(f"  ║  🤖 Polymarket BTC 5-Min MOMENTUM Bot v5             ║")
    log.info(f"  ║  {mode:>20}                                ║")
    log.info(f"  ║  💰 Balance: ${portfolio.cash:.2f}                           ║")
    log.info(f"  ║  📋 Entry ≤{ENTRY_THRESHOLD:.0%} │ Bet ${BET_SIZE:.2f} │ Dutch ≤{DUTCH_MAX_COST:.0%}        ║")
    log.info(f"  ║  📈 MOMENTUM: buy trending side, dutch on reversal   ║")
    log.info(f"  ║  🔄 Windows: {num_windows} (~{num_windows * 5} min)                         ║")
    log.info(f"  ╚═══════════════════════════════════════════════════════╝")

    try:
        for i in range(num_windows):
            window_ts = get_current_window_ts()
            window_end = window_ts + WINDOW_SECONDS
            now = time.time()

            if window_end - now < 60:
                wait = (window_ts + WINDOW_SECONDS) - now + 3
                log.info(f"\n  ⏳ Waiting {wait:.0f}s for fresh window...")
                time.sleep(wait)
                window_ts = get_current_window_ts()

            log.info(f"\n  🔍 [{i+1}/{num_windows}] Finding market...")
            market = find_market(window_ts)
            retries = 0
            while not market and retries < 5:
                retries += 1
                time.sleep(3)
                market = find_market(window_ts)

            if not market:
                log.warning(f"  ❌ Market not found, skipping.")
                time.sleep(30)
                continue

            # Refresh balance for live
            if live and trader:
                portfolio.cash = trader.get_balance()

            result = trade_window(window_ts, market, portfolio)
            if result:
                results.append(result)

            if i < num_windows - 1:
                time.sleep(2)
    except KeyboardInterrupt:
        status = "interrupted"
        log.info("\n  ⚠️ Interrupted by user. Saving partial session...")
    finally:
        # ── Summary ──
        log.info(f"\n  ╔═══════════════════════════════════════════════════════╗")
        log.info(f"  ║  📊 SESSION COMPLETE  [{mode}]           ║")
        log.info(f"  ╠═══════════════════════════════════════════════════════╣")

        if results:
            wins = sum(1 for r in results if r["pnl"] > 0)
            losses = sum(1 for r in results if r["pnl"] < 0)
            flat = sum(1 for r in results if r["pnl"] == 0)
            dutch_count = sum(1 for r in results if r["dutch"])
            real_count = sum(1 for r in results if r.get("had_real_trades"))

            log.info(f"  ║  Windows:  {len(results)}  │  W/L/F: {wins}/{losses}/{flat}          ║")
            log.info(f"  ║  Dutch:    {dutch_count}/{len(results)}  │  Real trades: {real_count}         ║")
            log.info(f"  ║  Total PnL: ${portfolio.total_pnl:+.2f}                          ║")
            log.info(f"  ║  Final:     ${portfolio.cash:.2f}  │  ROI: {(portfolio.total_pnl/START_CASH)*100:+.1f}%       ║")
            log.info(f"  ╠═══════════════════════════════════════════════════════╣")
            log.info(f"  ║  {'Win':>3} {'D':>2} {'PnL':>7} {'Sw':>2} {'Up☟':>5} {'Dn☟':>5}  {'Window':>8} {'Real':>4} ║")
            log.info(f"  ║  {'─'*3} {'─'*2} {'─'*7} {'─'*2} {'─'*5} {'─'*5}  {'─'*8} {'─'*4} ║")
            for r in results:
                w = datetime.fromtimestamp(r["window_ts"], tz=timezone.utc).strftime("%H:%M")
                win_s = "✅" if r["pnl"] > 0 else ("❌" if r["pnl"] < 0 else "➖")
                d_s = "🎯" if r["dutch"] else "  "
                real_s = "🔴" if r.get("had_real_trades") else "  "
                log.info(
                    f"  ║  {win_s:>3} {d_s:>2} ${r['pnl']:+5.2f} {r['swings']:>2} "
                    f"{r['up_min']:5.2f} {r['down_min']:5.2f}  {w:>8} {real_s:>4} ║"
                )

        log.info(f"  ╚═══════════════════════════════════════════════════════╝")

        output = {
            "session": datetime.now(tz=timezone.utc).isoformat(),
            "mode": "live" if live else "paper",
            "config": {
                "entry_threshold": ENTRY_THRESHOLD, "bet_size": BET_SIZE,
                "start_cash": START_CASH, "dutch_max_cost": DUTCH_MAX_COST,
                "min_momentum_price": MIN_MOMENTUM_PRICE,
                "max_momentum_price": MAX_MOMENTUM_PRICE,
                "min_dutch_profit": MIN_DUTCH_PROFIT,
                "require_confirm_ticks": REQUIRE_CONFIRM_TICKS,
                "observe_period": OBSERVE_PERIOD, "freeze_period": FREEZE_PERIOD,
            },
            "final_cash": portfolio.cash, "total_pnl": portfolio.total_pnl,
            "results": results,
        }
        fname = f"bot_session_v5_{'live' if live else 'paper'}.json"
        with open(fname, "w") as f:
            json.dump(output, f, indent=2)
        log.info(f"  💾 Saved to {fname}")
        append_aggregate(output, status, fname)
        return output


# ─── API Test ────────────────────────────────────────────────────
def test_api(live: bool):
    log.info("🔌 Testing APIs...")
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"limit": 1}, timeout=10)
        log.info(f"   Gamma: {'✅' if r.status_code == 200 else '❌'}")
    except Exception as e:
        log.error(f"   Gamma: ❌ ({e})")
    try:
        r = requests.get(f"{CLOB_API}/time", timeout=10)
        log.info(f"   CLOB:  {'✅' if r.status_code == 200 else '❌'}")
    except Exception as e:
        log.error(f"   CLOB:  ❌ ({e})")

    if live:
        log.info("   Testing live connection...")
        trader = LiveTrader()
        trader.connect()

    window_ts = get_current_window_ts()
    market = find_market(window_ts)
    if market:
        log.info(f"   Market: ✅ {market['question']}")
        prices = fetch_prices(market)
        if prices:
            log.info(f"   Prices: Up={prices['up']:.3f} Dn={prices['down']:.3f}")
    else:
        log.warning(f"   Market: ❌")


# ─── Entry ───────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket BTC Momentum Bot v5")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--live", action="store_true", help="Enable real trading")
    parser.add_argument("--paper", action="store_true", help="Paper trading (default)")
    parser.add_argument("--windows", type=int, default=6)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--entry", type=float, default=0.38)
    parser.add_argument("--observe", type=int, default=45)
    parser.add_argument("--freeze", type=int, default=30)
    parser.add_argument("--bet", type=float, default=2.0)
    parser.add_argument("--cash", type=float, default=10.0)
    parser.add_argument("--dutch-max", type=float, default=0.85)
    parser.add_argument("--min-momentum", type=float, default=0.55)
    parser.add_argument("--max-momentum", type=float, default=0.72)
    parser.add_argument("--min-dutch-profit", type=float, default=0.05)
    parser.add_argument("--confirm", type=int, default=2)
    args = parser.parse_args()

    POLL_INTERVAL = args.interval
    ENTRY_THRESHOLD = args.entry
    OBSERVE_PERIOD = args.observe
    FREEZE_PERIOD = args.freeze
    BET_SIZE = args.bet
    START_CASH = args.cash
    DUTCH_MAX_COST = args.dutch_max
    MIN_MOMENTUM_PRICE = args.min_momentum
    MAX_MOMENTUM_PRICE = args.max_momentum
    MIN_DUTCH_PROFIT = args.min_dutch_profit
    REQUIRE_CONFIRM_TICKS = args.confirm

    if args.test:
        test_api(live=args.live)
    else:
        setup_signal_handlers()
        run_bot(args.windows, live=args.live)