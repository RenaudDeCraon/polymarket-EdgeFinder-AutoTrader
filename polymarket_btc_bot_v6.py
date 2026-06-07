"""
Polymarket BTC 5-Min Bot v6 — Momentum + Regime + Live Trading
===============================================================

Combines:
  - v5 MOMENTUM logic (buy WITH the trend, not against it)
  - v4 REGIME detection (STRONG_UP/STRONG_DN/RANGE filters)
  - v5 LIVE trading via py_clob_client

Strategy:
  1. OBSERVE (0-45s) — collect price data, detect regime
  2. TRADE (45s-270s):
     RANGE regime:
       → Side drops below 38¢ → BUY the OTHER (trending) side
       → If reversal → dutch the now-cheap side
     STRONG trend:
       → Tighter threshold (22¢) to avoid catching falling knives
       → Skip dead sides below 12¢
       → Tighter dutch limit (75¢ combined)
  3. FREEZE (last 30s) — no new trades

Setup:
  pip install py-clob-client python-dotenv

  # .env file:
  POLY_PRIVATE_KEY=0x...
  POLY_FUNDER=0x...
  POLY_SIGNATURE_TYPE=1

Usage:
  python polymarket_btc_bot_v6.py --paper --windows 6
  python polymarket_btc_bot_v6.py --live --windows 6 --bet 2
  python polymarket_btc_bot_v6.py --test --live
"""

import json, time, logging, argparse, os, requests, signal
from datetime import datetime, timezone
from typing import Optional, Tuple
from pathlib import Path
from collections import deque
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── Config ──────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
CHAIN_ID  = 137

WINDOW_SECONDS = 300
POLL_INTERVAL  = 2.0

# Strategy
ENTRY_THRESHOLD    = 0.38
OBSERVE_PERIOD     = 45
FREEZE_PERIOD      = 30
BET_SIZE           = 2.0
START_CASH         = 10.0
DUTCH_MAX_COST     = 0.85
MIN_DUTCH_PROFIT   = 0.05

# Regime detection (from v4)
TREND_LOOKBACK     = 10
STRONG_TREND_MOVES = 7
STRONG_TREND_DELTA = 0.18
EXTREME_ENTRY      = 0.22
NO_TRADE_ENTRY     = 0.12

# --- Safety / quality filters for momentum entries ---
MAX_MOMENTUM_PRICE = 0.70          # Never chase a side above this price
MIN_ENTRY_SECONDS_LEFT = 120       # Do not open new momentum positions too late
NO_ENTRY_FIRST_SECONDS = 35        # Warmup period: observe only, no fresh entry
MIN_ENTRY_EDGE = 0.04              # Chosen side must lead the other side by at least this much
MIN_CASH_BUFFER = 0.01             # Avoid spending the account to exactly zero
SKIP_EXTREME_LOW_PRICE = 0.12      # Avoid noisy 1-10c fake reversals as fresh entries

# ─── Logging ─────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("polybot")

ROOT = Path(__file__).resolve().parent
AGGREGATE_FILE = ROOT / "aggregated_bot_results.json"
SCRIPT_NAME = "polymarket_btc_bot_v6"
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


# ─── Regime Detection (from v4) ─────────────────────────────────
def detect_regime(history: deque) -> Tuple[str, str]:
    if len(history) < TREND_LOOKBACK:
        return "WARMUP", "none"
    recent = list(history)[-TREND_LOOKBACK:]
    delta = recent[-1][0] - recent[0][0]
    up_moves = sum(1 for i in range(1, len(recent)) if recent[i][0] > recent[i-1][0])
    dn_moves = sum(1 for i in range(1, len(recent)) if recent[i][0] < recent[i-1][0])
    if delta >= STRONG_TREND_DELTA and up_moves >= STRONG_TREND_MOVES:
        return "STRONG_UP", "up"
    if delta <= -STRONG_TREND_DELTA and dn_moves >= STRONG_TREND_MOVES:
        return "STRONG_DN", "down"
    return "RANGE", "none"

# --- Helper: momentum entry guard ---
def can_enter_momentum(up_price, down_price, side, seconds_left, regime, elapsed_seconds=None):
    """Return (allowed, reason) for opening a fresh momentum position.

    This blocks the two biggest failure modes seen in paper trading:
    1) entering during warmup before the market has stabilized,
    2) chasing an already-expensive side with poor risk/reward.
    """
    if regime == "WARMUP":
        return False, "warmup"

    if elapsed_seconds is not None and elapsed_seconds < NO_ENTRY_FIRST_SECONDS:
        return False, "warmup"

    if seconds_left < MIN_ENTRY_SECONDS_LEFT:
        return False, "too_late"

    chosen_price = up_price if side == "UP" else down_price
    other_price = down_price if side == "UP" else up_price

    if chosen_price is None or other_price is None:
        return False, "missing_price"

    if chosen_price > MAX_MOMENTUM_PRICE:
        return False, "price_too_high"

    if chosen_price < SKIP_EXTREME_LOW_PRICE:
        return False, "price_too_low"

    if chosen_price - other_price < MIN_ENTRY_EDGE:
        return False, "edge_too_small"

    return True, "ok"

# --- Helper: True Dutch calculation ---
def calculate_true_dutch(position_side, position_shares, position_cost, hedge_side, hedge_price, available_cash):
    """Calculate a hedge that guarantees profit on both outcomes.

    Returns a dict when a true dutch is possible, otherwise None.
    The hedge spend is capped by available cash and never uses the last cash buffer.
    """
    if hedge_price is None or hedge_price <= 0 or hedge_price >= 1:
        return None

    if position_shares <= 0 or position_cost <= 0:
        return None

    spendable_cash = max(0.0, available_cash - MIN_CASH_BUFFER)
    if spendable_cash <= 0:
        return None

    # If the original side wins: payout is position_shares.
    # If the hedge side wins: payout is hedge_shares.
    # Need both payouts to exceed total cost by MIN_DUTCH_PROFIT.
    min_hedge_shares = position_cost + MIN_DUTCH_PROFIT
    hedge_cost = min_hedge_shares * hedge_price

    if hedge_cost > spendable_cash:
        return None

    original_win_profit = position_shares - position_cost - hedge_cost
    hedge_win_profit = min_hedge_shares - position_cost - hedge_cost
    worst_profit = min(original_win_profit, hedge_win_profit)

    if worst_profit < MIN_DUTCH_PROFIT:
        return None

    return {
        "side": hedge_side,
        "shares": int(min_hedge_shares) if abs(min_hedge_shares - int(min_hedge_shares)) < 1e-9 else min_hedge_shares,
        "price": hedge_price,
        "cost": hedge_cost,
        "worst_profit": worst_profit,
        "original_win_profit": original_win_profit,
        "hedge_win_profit": hedge_win_profit,
    }

def get_entry_threshold(regime: str) -> float:
    return min(ENTRY_THRESHOLD, EXTREME_ENTRY) if regime.startswith("STRONG") else ENTRY_THRESHOLD

def get_dutch_limit(regime: str) -> float:
    return min(DUTCH_MAX_COST, 0.75) if regime.startswith("STRONG") else DUTCH_MAX_COST


def should_skip_side(side: str, price: float, regime: str) -> Optional[str]:
    """Returns skip reason or None if OK to trade."""
    if regime == "STRONG_UP" and side == "UP":
        # In STRONG_UP, UP is expensive — we'd be buying momentum, that's fine
        return None
    if regime == "STRONG_DN" and side == "DN":
        return None
    # Check if contrarian side is too dead
    if regime == "STRONG_UP" and side == "DN" and price <= NO_TRADE_ENTRY:
        return "DN too dead in STRONG_UP"
    if regime == "STRONG_DN" and side == "UP" and price <= NO_TRADE_ENTRY:
        return "UP too dead in STRONG_DN"
    return None


# ─── Live Trader (from v5) ───────────────────────────────────────
class LiveTrader:
    def __init__(self):
        self.client = None
        self.initialized = False
        self._imports = {}

    def connect(self) -> bool:
        try:
            from py_clob_client_v2 import ClobClient, BalanceAllowanceParams, AssetType
            self._imports = {"BalanceAllowanceParams": BalanceAllowanceParams, "AssetType": AssetType}
        except ImportError:
            log.error("❌ pip install py-clob-client-v2")
            return False
        pk = os.getenv("POLY_PRIVATE_KEY")
        if not pk:
            log.error("❌ POLY_PRIVATE_KEY not set")
            return False
        try:
            self.client = ClobClient(host=CLOB_API, key=pk, chain_id=CHAIN_ID,
                signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "3")),
                funder=os.getenv("POLY_FUNDER"))
            creds = self.client.derive_api_key()
            self.client.set_api_creds(creds)
            bal = self.get_balance()
            log.info(f"   ✅ Connected! Balance: ${bal:.2f}")
            self.initialized = True
            return True
        except Exception as e:
            log.error(f"   ❌ {e}")
            return False

    def get_balance(self) -> float:
        if not self.client: return 0.0
        try:
            b = self.client.get_balance_allowance(
                self._imports["BalanceAllowanceParams"](
                    asset_type=self._imports["AssetType"].COLLATERAL))
            return int(b.get("balance", 0)) / 1e6
        except: return 0.0

    def market_buy(self, token_id: str, amount: float, price: float = None) -> Optional[dict]:
        if not self.initialized: return None
        try:
            from py_clob_client_v2 import OrderArgs, Side, PartialCreateOrderOptions
            book = self.client.get_order_book(token_id)
            tick = str(book.get('tick_size', '0.01'))
            neg = bool(book.get('neg_risk', False))
            min_size = float(book.get('min_order_size', 5))

            if price is None:
                asks = book.get('asks', [])
                if not asks:
                    log.error("   ❌ No asks")
                    return None
                price = float(asks[0]['price'])

            # Round price to tick size
            price = round(price, 2)
            size = max(min_size, round(amount / price))

            opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg)
            resp = self.client.create_and_post_order(
                OrderArgs(token_id=token_id, price=price, size=size, side=Side.BUY),
                options=opts,
            )
            status = resp.get('status', 'unknown')
            log.info(f"   📤 {size:.0f}sh @ {price} = ${size*price:.2f} → {status}")
            if resp.get('success'):
                return resp
            else:
                log.error(f"   ❌ {resp.get('errorMsg', 'unknown error')}")
                return None
        except Exception as e:
            log.error(f"   ❌ Order failed: {e}")
            return None

# ─── Portfolio ───────────────────────────────────────────────────
@dataclass
class Trade:
    time_s: int; side: str; price: float; shares: float; cost: float; real: bool = False

@dataclass
class Portfolio:
    cash: float
    is_live: bool = False
    trader: Optional[LiveTrader] = None
    up_shares: float = 0.0; up_avg: float = 0.0
    dn_shares: float = 0.0; dn_avg: float = 0.0
    trades: list = field(default_factory=list)
    windows_played: int = 0; windows_won: int = 0; total_pnl: float = 0.0

    def buy(self, side: str, price: float, amount: float, time_s: int, token_id: str = "") -> Optional[Trade]:
        if amount > self.cash: amount = self.cash
        if amount < 0.10: return None

        real = False
        if self.is_live and self.trader and self.trader.initialized and token_id:
            log.info(f"   💸 LIVE: {side} ${amount:.2f} @ ~{price:.3f}")
            try:
                resp = self.trader.market_buy(token_id, amount, price=price)
                if resp:
                    real = True
                    log.info(f"   ✅ FILLED")
                else:
                    log.warning(f"   ⚠️ Failed, paper fallback")
            except Exception as e:
                log.warning(f"   ⚠️ Live error: {e}, paper fallback")
        shares = round(amount / price, 2)
        cost = round(shares * price, 4)
        self.cash = round(self.cash - cost, 4)

        if side == "UP":
            tc = self.up_avg * self.up_shares + cost
            self.up_shares = round(self.up_shares + shares, 2)
            self.up_avg = round(tc / self.up_shares, 4) if self.up_shares > 0 else 0
        else:
            tc = self.dn_avg * self.dn_shares + cost
            self.dn_shares = round(self.dn_shares + shares, 2)
            self.dn_avg = round(tc / self.dn_shares, 4) if self.dn_shares > 0 else 0

        t = Trade(time_s=time_s, side=side, price=price, shares=shares, cost=cost, real=real)
        self.trades.append(t)
        return t

    def resolve(self, winner: str):
        payout = (self.up_shares if winner == "UP" else self.dn_shares) * 1.0
        spent = sum(t.cost for t in self.trades)
        self.cash = round(self.cash + payout, 4)
        pnl = round(payout - spent, 4)
        self.total_pnl = round(self.total_pnl + pnl, 4)
        self.windows_played += 1
        if pnl > 0: self.windows_won += 1
        return {"winner": winner, "payout": payout, "spent": spent, "pnl": pnl}

    def reset_window(self):
        self.up_shares = self.up_avg = self.dn_shares = self.dn_avg = 0.0
        self.trades = []

    @property
    def has_up(self): return self.up_shares > 0
    @property
    def has_dn(self): return self.dn_shares > 0
    @property
    def is_dutch(self): return self.has_up and self.has_dn


# ─── Market + Price ──────────────────────────────────────────────
def get_window_ts() -> int:
    now = int(time.time()); return now - (now % WINDOW_SECONDS)

def find_market(wts: int) -> Optional[dict]:
    slug = f"btc-updown-5m-{wts}"
    for ep in ["events", "markets"]:
        try:
            r = requests.get(f"{GAMMA_API}/{ep}", params={"slug": slug, "limit": 1}, timeout=10)
            if r.status_code != 200: continue
            d = r.json()
            if not d: continue
            m = d[0].get("markets", [{}])[0] if ep == "events" else d[0]
            oc = m.get("outcomes", "[]")
            if isinstance(oc, str): oc = json.loads(oc)
            tk = m.get("clobTokenIds", "[]")
            if isinstance(tk, str): tk = json.loads(tk)
            ui = di = None
            for i, o in enumerate(oc):
                l = str(o).strip().lower()
                if l == "up": ui = i
                elif l == "down": di = i
            if ui is not None and di is not None and len(tk) > max(ui, di):
                return {"slug": slug, "question": m.get("question", slug),
                        "up_token": tk[ui].strip(), "down_token": tk[di].strip()}
        except: pass
    return None

def fetch_price(tid: str) -> Optional[float]:
    for method in ["midpoint", "price", "last-trade-price"]:
        try:
            p = {"token_id": tid}
            if method == "price": p["side"] = "buy"
            r = requests.get(f"{CLOB_API}/{method}", params=p, timeout=5)
            if r.status_code == 200:
                v = r.json().get("mid") or r.json().get("price")
                if v and 0.005 < float(v) < 0.995: return float(v)
        except: pass
    return None

def fetch_prices(mkt: dict) -> Optional[dict]:
    up = fetch_price(mkt["up_token"])
    dn = fetch_price(mkt["down_token"])
    if up and not dn: dn = round(1 - up, 4)
    elif dn and not up: up = round(1 - dn, 4)
    if up and dn: return {"up": up, "down": dn}
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": mkt["slug"], "limit": 1}, timeout=8)
        if r.status_code == 200:
            ev = r.json()
            if ev:
                m2 = ev[0].get("markets", [{}])[0]
                pr = m2.get("outcomePrices", "[]")
                if isinstance(pr, str): pr = json.loads(pr)
                oc = m2.get("outcomes", "[]")
                if isinstance(oc, str): oc = json.loads(oc)
                u = d = None
                for i, o in enumerate(oc):
                    if str(o).strip().lower() == "up": u = float(pr[i])
                    elif str(o).strip().lower() == "down": d = float(pr[i])
                if u and d: return {"up": u, "down": d}
    except: pass
    return None


# ─── MOMENTUM + REGIME Strategy ─────────────────────────────────
def trade_window(wts: int, mkt: dict, pf: Portfolio) -> Optional[dict]:
    global INTERRUPTED
    wend = wts + WINDOW_SECONDS
    if time.time() >= wend - 5: return None
    pf.reset_window()
    mode = "🔴LIVE" if pf.is_live else "📝PAPER"

    log.info(f"\n{'━'*80}")
    log.info(f"  🎯 {mkt['question']}  [{mode}]")
    log.info(f"  ⏰ {datetime.fromtimestamp(wts, tz=timezone.utc).strftime('%H:%M:%S')} → "
             f"{datetime.fromtimestamp(wend, tz=timezone.utc).strftime('%H:%M:%S')} UTC  │  "
             f"💰 ${pf.cash:.2f}  │  PnL: ${pf.total_pnl:+.2f}")
    log.info(f"  📈 MOMENTUM + REGIME: trend-follow first, dutch on reversal")
    log.info(f"{'━'*80}")
    log.info(f"  {'Time':>8} {'Left':>4} {'Phase':>7} {'Up':>6} {'Dn':>6} {'Regime':>9} {'Pos':>12} Action")
    log.info(f"  {'─'*8} {'─'*4} {'─'*7} {'─'*6} {'─'*6} {'─'*9} {'─'*12} {'─'*30}")

    history = deque(maxlen=max(TREND_LOOKBACK, 3))
    up_min = dn_min = 1.0
    last_up = last_dn = 0.5
    ticks = swings = skips = 0
    last_leader = None

    while time.time() < wend - 3:
        rem = wend - time.time()
        elapsed = time.time() - wts
        pr = fetch_prices(mkt)
        if not pr:
            time.sleep(POLL_INTERVAL); continue

        up, dn = pr["up"], pr["down"]
        last_up, last_dn = up, dn
        up_min, dn_min = min(up_min, up), min(dn_min, dn)
        history.append((up, dn))

        regime, trend = detect_regime(history)
        thresh = get_entry_threshold(regime)
        dlimit = get_dutch_limit(regime)

        leader = "up" if up > dn else ("down" if dn > up else "tie")
        if last_leader and leader != last_leader and leader != "tie": swings += 1
        last_leader = leader

        phase = "👁 WATCH" if elapsed < OBSERVE_PERIOD else ("🧊 FREEZE" if rem < FREEZE_PERIOD else "⚡ TRADE")

        pos = "🎯DUTCH" if pf.is_dutch else (f"↑{pf.up_shares:.0f}@{pf.up_avg:.2f}" if pf.has_up else
              (f"↓{pf.dn_shares:.0f}@{pf.dn_avg:.2f}" if pf.has_dn else "—"))
        action = ""

        if phase == "⚡ TRADE":
            if not pf.has_up and not pf.has_dn:
                # ═══ MOMENTUM: cheap side signals trend → buy the OTHER side ═══
                momentum_side = None
                entry_price = None
                if up <= thresh:
                    momentum_side = "DN"
                    entry_price = dn
                elif dn <= thresh:
                    momentum_side = "UP"
                    entry_price = up
                if momentum_side is not None:
                    skip = should_skip_side(momentum_side, entry_price, regime)
                    if skip:
                        skips += 1; action = f"⛔ {skip}"
                    else:
                        elapsed_seconds = max(0, WINDOW_SECONDS - rem) if 'WINDOW_SECONDS' in globals() else None
                        allowed, block_reason = can_enter_momentum(
                            up_price=up,
                            down_price=dn,
                            side=momentum_side,
                            seconds_left=int(rem),
                            regime=regime,
                            elapsed_seconds=elapsed_seconds,
                        )
                        if not allowed:
                            action = f"⛔ SKIP {momentum_side} ({block_reason})"
                        else:
                            spend = min(BET_SIZE, max(0.0, pf.cash - MIN_CASH_BUFFER))
                            if spend <= 0:
                                action = "⛔ SKIP no_cash"
                            else:
                                t = pf.buy(momentum_side, entry_price, spend, int(elapsed), mkt["down_token"] if momentum_side == "DN" else mkt["up_token"])
                                if t:
                                    rtag = " 🔴" if t.real else ""
                                    action = f"📈 MOM {momentum_side} {t.shares:.0f}sh @{entry_price:.3f} ${t.cost:.2f} [{regime}]{rtag}"
                                    pos = (f"↓{pf.dn_shares:.0f}@{pf.dn_avg:.2f}" if momentum_side == "DN"
                                           else f"↑{pf.up_shares:.0f}@{pf.up_avg:.2f}")

            # ═══ DUTCH: if we have one side and other becomes cheap ═══
            elif pf.has_dn and not pf.has_up:
                if up <= thresh:
                    existing_spent = sum(t.cost for t in pf.trades if t.side == "DN")
                    # Use the new true dutch calculation
                    hedge = calculate_true_dutch(
                        position_side="DN",
                        position_shares=pf.dn_shares,
                        position_cost=existing_spent,
                        hedge_side="UP",
                        hedge_price=up,
                        available_cash=pf.cash,
                    )
                    if hedge is not None:
                        t = pf.buy("UP", up, hedge["cost"], int(elapsed), mkt["up_token"])
                        if t:
                            rtag = " 🔴" if t.real else ""
                            action = f"🎯 TRUE DUTCH {hedge['side']} {hedge['shares']}sh @{hedge['price']:.3f} worst=${hedge['worst_profit']:+.2f}{rtag}"
                            pos = "🎯DUTCH"
                    else:
                        skips += 1
                        action = f"⛔ Dutch not profitable"

            elif pf.has_up and not pf.has_dn:
                if dn <= thresh:
                    existing_spent = sum(t.cost for t in pf.trades if t.side == "UP")
                    hedge = calculate_true_dutch(
                        position_side="UP",
                        position_shares=pf.up_shares,
                        position_cost=existing_spent,
                        hedge_side="DN",
                        hedge_price=dn,
                        available_cash=pf.cash,
                    )
                    if hedge is not None:
                        t = pf.buy("DN", dn, hedge["cost"], int(elapsed), mkt["down_token"])
                        if t:
                            rtag = " 🔴" if t.real else ""
                            action = f"🎯 TRUE DUTCH {hedge['side']} {hedge['shares']}sh @{hedge['price']:.3f} worst=${hedge['worst_profit']:+.2f}{rtag}"
                            pos = "🎯DUTCH"
                    else:
                        skips += 1
                        action = f"⛔ Dutch not profitable"

        log.info(f"  {datetime.now(tz=timezone.utc).strftime('%H:%M:%S')} {rem:3.0f}s {phase:>7} "
                 f"{up:5.3f}  {dn:5.3f} {regime:>9} {pos:>12} {action}")
        ticks += 1
        if INTERRUPTED:
            break
        time.sleep(POLL_INTERVAL)

    # ── Resolve ──
    fp = fetch_prices(mkt)
    if fp: last_up, last_dn = fp["up"], fp["down"]
    winner = "UP" if last_up > 0.5 else "DN"

    log.info(f"\n  ┌──────────────────────────────────────────────────────┐")
    log.info(f"  │  ⏱  ENDED — Up={last_up:.3f} Dn={last_dn:.3f}  🏆 {winner}")

    res = {"pnl": 0}
    if pf.trades:
        res = pf.resolve(winner)
        log.info(f"  │  💰 Pay=${res['payout']:.2f} Spent=${res['spent']:.2f} PnL=${res['pnl']:+.2f}")
        log.info(f"  │  📊 Cash=${pf.cash:.2f}  Session=${pf.total_pnl:+.2f}")
        log.info(f"  │  {'🎯 Dutch active' if pf.is_dutch else '📈 Momentum: ' + pf.trades[0].side}")
        for t in pf.trades:
            rtag = " 🔴" if t.real else ""
            log.info(f"  │    {t.time_s:>3}s {t.side:>2} {t.shares:>4.0f}sh @{t.price:.3f} ${t.cost:.2f}{rtag}")
    else:
        log.info(f"  │  📭 No trades")

    log.info(f"  │  Swings:{swings} Skips:{skips} Up☟={up_min:.3f} Dn☟={dn_min:.3f}")
    log.info(f"  └──────────────────────────────────────────────────────┘")

    return {"window_ts": wts, "question": mkt["question"], "ticks": ticks,
            "swings": swings, "winner": winner, "trades": len(pf.trades),
            "dutch": pf.is_dutch if pf.trades else False, "pnl": res["pnl"],
            "up_min": up_min, "down_min": dn_min, "skips": skips,
            "real_trades": any(t.real for t in pf.trades)}


# ─── Main ────────────────────────────────────────────────────────
def run(num_windows: int, live: bool):
    trader = None
    if live:
        trader = LiveTrader()
        log.info(f"\n  🔌 Connecting...")
        if not trader.connect():
            return
        pf = Portfolio(cash=trader.get_balance(), is_live=True, trader=trader)
    else:
        pf = Portfolio(cash=START_CASH)

    results = []
    status = "completed"
    mode = "🔴 LIVE" if live else "📝 PAPER"

    log.info(f"\n  ╔═══════════════════════════════════════════════════════╗")
    log.info(f"  ║  🤖 BTC 5-Min Momentum+Regime Bot v6  [{mode}]    ║")
    log.info(f"  ║  💰 ${pf.cash:.2f}  │  Bet ${BET_SIZE:.2f}  │  Entry ≤{ENTRY_THRESHOLD:.0%}       ║")
    log.info(f"  ║  📈 Momentum first → dutch on reversal              ║")
    log.info(
        f"  ║  🔍 Filters: maxEntry≤{MAX_MOMENTUM_PRICE:.2f}, minLeft≥{MIN_ENTRY_SECONDS_LEFT}s, trueDutch≥${MIN_DUTCH_PROFIT:.2f} ║"
    )
    log.info(f"  ║  🔄 {num_windows} windows (~{num_windows*5} min)                          ║")
    log.info(f"  ╚═══════════════════════════════════════════════════════╝")

    try:
        for i in range(num_windows):
            wts = get_window_ts()
            wend = wts + WINDOW_SECONDS
            now = time.time()
            if wend - now < 60:
                wait = (wts + WINDOW_SECONDS) - now + 3
                log.info(f"\n  ⏳ Waiting {wait:.0f}s...")
                time.sleep(wait)
                wts = get_window_ts()

            log.info(f"\n  🔍 [{i+1}/{num_windows}] Finding market...")
            mkt = find_market(wts)
            rt = 0
            while not mkt and rt < 5:
                rt += 1; time.sleep(3); mkt = find_market(wts)
            if not mkt:
                log.warning(f"  ❌ Not found"); time.sleep(30); continue

            if live and trader: pf.cash = trader.get_balance()

            r = trade_window(wts, mkt, pf)
            if r: results.append(r)
            if i < num_windows - 1: time.sleep(2)
    except KeyboardInterrupt:
        status = "interrupted"
        log.info("\n  ⚠️ Interrupted by user. Saving partial session...")
    finally:
        # ── Summary ──
        log.info(f"\n  ╔═══════════════════════════════════════════════════════╗")
        log.info(f"  ║  📊 SESSION DONE  [{mode}]                          ║")
        log.info(f"  ╠═══════════════════════════════════════════════════════╣")
        if results:
            w = sum(1 for r in results if r["pnl"] > 0)
            l = sum(1 for r in results if r["pnl"] < 0)
            f = sum(1 for r in results if r["pnl"] == 0)
            dc = sum(1 for r in results if r["dutch"])
            rc = sum(1 for r in results if r.get("real_trades"))
            log.info(f"  ║  {len(results)} windows │ {w}W/{l}L/{f}F │ Dutch: {dc} │ Real: {rc}  ║")
            log.info(f"  ║  PnL: ${pf.total_pnl:+.2f}  │  Cash: ${pf.cash:.2f}  │  "
                     f"ROI: {(pf.total_pnl/START_CASH)*100:+.1f}%    ║")
            log.info(f"  ╠═══════════════════════════════════════════════════════╣")
            log.info(f"  ║  {'W':>2} {'D':>2} {'PnL':>7} {'Sw':>2} {'Sk':>2} {'Up☟':>5} {'Dn☟':>5} {'Time':>5} {'R':>2} ║")
            for r in results:
                tm = datetime.fromtimestamp(r["window_ts"], tz=timezone.utc).strftime("%H:%M")
                ws = "✅" if r["pnl"] > 0 else ("❌" if r["pnl"] < 0 else "➖")
                ds = "🎯" if r["dutch"] else "  "
                rs = "🔴" if r.get("real_trades") else "  "
                log.info(f"  ║  {ws} {ds} ${r['pnl']:+5.2f} {r['swings']:>2} {r['skips']:>2} "
                         f"{r['up_min']:5.2f} {r['down_min']:5.2f} {tm:>5} {rs} ║")
        log.info(f"  ╚═══════════════════════════════════════════════════════╝")

        out = {
            "session": datetime.now(tz=timezone.utc).isoformat(),
            "mode": "live" if live else "paper",
            "strategy_settings": {
                "max_momentum_price": MAX_MOMENTUM_PRICE,
                "min_entry_seconds_left": MIN_ENTRY_SECONDS_LEFT,
                "min_entry_edge": MIN_ENTRY_EDGE,
                "min_dutch_profit": MIN_DUTCH_PROFIT,
                "no_entry_first_seconds": NO_ENTRY_FIRST_SECONDS,
            },
            "config": {
                "entry": ENTRY_THRESHOLD,
                "bet": BET_SIZE,
                "cash": START_CASH,
                "dutch_max": DUTCH_MAX_COST,
                "extreme_entry": EXTREME_ENTRY,
                "no_trade": NO_TRADE_ENTRY,
                "min_dutch_profit": MIN_DUTCH_PROFIT,
                "observe": OBSERVE_PERIOD,
                "freeze": FREEZE_PERIOD,
            },
            "final_cash": pf.cash,
            "total_pnl": pf.total_pnl,
            "results": results,
        }
        fn = f"bot_v6_{'live' if live else 'paper'}.json"
        with open(fn, "w") as fh: json.dump(out, fh, indent=2)
        log.info(f"  💾 {fn}")
        append_aggregate(out, status, fn)
        return out


def test(live: bool):
    log.info("🔌 Testing...")
    for name, url in [("Gamma", f"{GAMMA_API}/events?limit=1"), ("CLOB", f"{CLOB_API}/time")]:
        try:
            r = requests.get(url, timeout=10)
            log.info(f"   {name}: {'✅' if r.status_code == 200 else '❌'}")
        except Exception as e: log.error(f"   {name}: ❌ {e}")
    if live:
        t = LiveTrader(); t.connect()
    wts = get_window_ts()
    m = find_market(wts) or find_market(wts - WINDOW_SECONDS)
    if m:
        log.info(f"   Market: ✅ {m['question']}")
        p = fetch_prices(m)
        if p: log.info(f"   Up={p['up']:.3f} Dn={p['down']:.3f}")
    else: log.warning("   Market: ❌")


if __name__ == "__main__":
    pa = argparse.ArgumentParser()
    pa.add_argument("--test", action="store_true")
    pa.add_argument("--live", action="store_true")
    pa.add_argument("--paper", action="store_true")
    pa.add_argument("--windows", type=int, default=6)
    pa.add_argument("--interval", type=float, default=2.0)
    pa.add_argument("--entry", type=float, default=0.38)
    pa.add_argument("--observe", type=int, default=45)
    pa.add_argument("--freeze", type=int, default=30)
    pa.add_argument("--bet", type=float, default=2.0)
    pa.add_argument("--cash", type=float, default=10.0)
    pa.add_argument("--dutch-max", type=float, default=0.85)
    pa.add_argument("--min-dutch-profit", type=float, default=0.05)
    pa.add_argument("--extreme-entry", type=float, default=0.22)
    pa.add_argument("--no-trade-entry", type=float, default=0.12)
    pa.add_argument("--max-momentum-price", type=float, default=MAX_MOMENTUM_PRICE)
    pa.add_argument("--min-entry-seconds-left", type=int, default=MIN_ENTRY_SECONDS_LEFT)
    pa.add_argument("--min-entry-edge", type=float, default=MIN_ENTRY_EDGE)

    a = pa.parse_args()

    MAX_MOMENTUM_PRICE = a.max_momentum_price
    MIN_ENTRY_SECONDS_LEFT = a.min_entry_seconds_left
    MIN_ENTRY_EDGE = a.min_entry_edge
    MIN_DUTCH_PROFIT = a.min_dutch_profit

    POLL_INTERVAL = a.interval; ENTRY_THRESHOLD = a.entry
    OBSERVE_PERIOD = a.observe; FREEZE_PERIOD = a.freeze
    BET_SIZE = a.bet; START_CASH = a.cash
    DUTCH_MAX_COST = a.dutch_max
    EXTREME_ENTRY = a.extreme_entry; NO_TRADE_ENTRY = a.no_trade_entry

    if a.test:
        test(a.live)
    else:
        setup_signal_handlers()
        run(a.windows, a.live)