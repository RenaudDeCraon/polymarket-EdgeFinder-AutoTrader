"""
Polymarket BTC 5-Min Hybrid Strategy Bot v4
============================================
Live paper trading with hybrid dutch book + momentum strategy.

Strategy:
  Phase 1 (0-60s):  OBSERVE — watch price direction, do nothing
  Phase 2 (60s+):   If a side drops below entry threshold → BUY cheap side
  Phase 3 (after buy): If price reverses and OTHER side drops → BUY it too → DUTCH BOOK
  Phase 4 (last 30s):  FREEZE — no new trades, too close to resolution

If dutch completes → guaranteed profit regardless of outcome.
If only one side bought → it's a momentum bet (you bought the cheap/trending side).

Usage:
  python polymarket_btc_bot_v4.py --test
  python polymarket_btc_bot_v4.py --windows 6
  python polymarket_btc_bot_v4.py --windows 12 --cash 10 --bet 2 --entry 0.38
"""

import json
import time
import logging
import argparse
import requests
import signal
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List
from dataclasses import dataclass, field

# ─── Config ──────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

WINDOW_SECONDS = 300
POLL_INTERVAL  = 2.0

# Strategy defaults (overridable via CLI)
ENTRY_THRESHOLD = 0.38   # Buy when a side drops to this
OBSERVE_PERIOD  = 45     # Seconds to watch before acting
FREEZE_PERIOD   = 30     # Stop trading this many seconds before end
BET_SIZE        = 2.0    # USD per side
START_CASH      = 10.0   # Starting paper balance

# ─── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("polybot")

ROOT = Path(__file__).resolve().parent
AGGREGATE_FILE = ROOT / "aggregated_bot_results.json"
SCRIPT_NAME = "polymarket_btc_bot_v4"
OUTPUT_FILENAME = "bot_session_v4.json"
INTERRUPTED = False


def setup_signal_handlers():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def signal_handler(signum, frame):
    global INTERRUPTED
    INTERRUPTED = True
    log.info("  ⚠️ Ctrl+C detected: will save partial results...")


def append_aggregate(result: dict, status: str):
    entry = {
        "script_name": SCRIPT_NAME,
        "output_file": OUTPUT_FILENAME,
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


# ─── Portfolio ───────────────────────────────────────────────────
@dataclass
class Trade:
    time_s: int          # seconds into window
    side: str            # "UP" or "DN"
    price: float
    shares: float
    cost: float

@dataclass
class Portfolio:
    cash: float
    up_shares: float = 0.0
    up_avg: float = 0.0
    dn_shares: float = 0.0
    dn_avg: float = 0.0
    trades: list = field(default_factory=list)
    windows_played: int = 0
    windows_won: int = 0
    total_pnl: float = 0.0

    def buy(self, side: str, price: float, amount_usd: float, time_s: int) -> Optional[Trade]:
        """Execute a mock buy. Returns Trade or None if insufficient cash."""
        if amount_usd > self.cash:
            amount_usd = self.cash  # Use remaining cash
        if amount_usd < 0.01:
            return None

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

        trade = Trade(time_s=time_s, side=side, price=price, shares=shares, cost=cost)
        self.trades.append(trade)
        return trade

    def resolve(self, winner: str):
        """Resolve window: winner gets $1/share, loser gets $0."""
        payout = 0.0
        if winner == "UP":
            payout = self.up_shares * 1.0
        elif winner == "DN":
            payout = self.dn_shares * 1.0

        spent = sum(t.cost for t in self.trades)
        self.cash = round(self.cash + payout, 4)
        pnl = round(payout - spent, 4)
        self.total_pnl = round(self.total_pnl + pnl, 4)
        self.windows_played += 1
        if pnl > 0:
            self.windows_won += 1

        return {"winner": winner, "payout": payout, "spent": spent, "pnl": pnl}

    def reset_window(self):
        """Reset per-window state, keep cash and stats."""
        self.up_shares = 0.0
        self.up_avg = 0.0
        self.dn_shares = 0.0
        self.dn_avg = 0.0
        self.trades = []

    @property
    def has_up(self):
        return self.up_shares > 0

    @property
    def has_dn(self):
        return self.dn_shares > 0

    @property
    def is_dutch(self):
        return self.has_up and self.has_dn


# ─── Market Discovery ────────────────────────────────────────────
def get_current_window_ts() -> int:
    now = int(time.time())
    return now - (now % WINDOW_SECONDS)


def find_market(window_ts: int) -> Optional[dict]:
    slug = f"btc-updown-5m-{window_ts}"
    for endpoint in ["events", "markets"]:
        try:
            resp = requests.get(f"{GAMMA_API}/{endpoint}", params={"slug": slug, "limit": 1}, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if not data:
                continue
            if endpoint == "events":
                markets = data[0].get("markets", [])
                if not markets:
                    continue
                market = markets[0]
            else:
                market = data[0]

            outcomes = market.get("outcomes", "[]")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            tokens = market.get("clobTokenIds", "[]")
            if isinstance(tokens, str):
                tokens = json.loads(tokens)

            up_idx = down_idx = None
            for i, o in enumerate(outcomes):
                low = str(o).strip().lower()
                if low == "up":
                    up_idx = i
                elif low == "down":
                    down_idx = i

            if up_idx is not None and down_idx is not None and len(tokens) > max(up_idx, down_idx):
                return {
                    "slug": slug,
                    "question": market.get("question", slug),
                    "condition_id": market.get("conditionId", ""),
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
            if method == "price":
                params["side"] = "buy"
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
    up_price = fetch_clob_price(market["up_token"])
    down_price = fetch_clob_price(market["down_token"])

    if up_price and not down_price:
        down_price = round(1.0 - up_price, 4)
    elif down_price and not up_price:
        up_price = round(1.0 - down_price, 4)

    if up_price and down_price:
        return {"up": up_price, "down": down_price, "source": "clob"}

    # Gamma fallback
    try:
        resp = requests.get(f"{GAMMA_API}/events", params={"slug": market["slug"], "limit": 1}, timeout=8)
        if resp.status_code == 200:
            events = resp.json()
            if events:
                m = events[0].get("markets", [{}])[0]
                prices = m.get("outcomePrices", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                outcomes = m.get("outcomes", "[]")
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                up_p = down_p = None
                for i, o in enumerate(outcomes):
                    if str(o).strip().lower() == "up":
                        up_p = float(prices[i])
                    elif str(o).strip().lower() == "down":
                        down_p = float(prices[i])
                if up_p and down_p:
                    return {"up": up_p, "down": down_p, "source": "gamma"}
    except:
        pass
    return None


# ─── Hybrid Strategy ─────────────────────────────────────────────
def trade_window(window_ts: int, market: dict, portfolio: Portfolio) -> Optional[dict]:
    """Run hybrid strategy on a single window with paper trading."""
    window_end = window_ts + WINDOW_SECONDS
    window_start = time.time()
    elapsed_at_start = window_start - window_ts

    if window_start >= window_end - 5:
        return None

    portfolio.reset_window()

    log.info(f"")
    log.info(f"{'━'*78}")
    log.info(f"  🎯 {market['question']}")
    utc_s = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime('%H:%M:%S')
    utc_e = datetime.fromtimestamp(window_end, tz=timezone.utc).strftime('%H:%M:%S')
    log.info(f"  ⏰ {utc_s} → {utc_e} UTC  │  💰 Cash: ${portfolio.cash:.2f}  │  PnL: ${portfolio.total_pnl:+.2f}")
    log.info(f"  📋 Strategy: observe {OBSERVE_PERIOD}s → entry ≤{ENTRY_THRESHOLD:.0%} → dutch if reversal → freeze last {FREEZE_PERIOD}s")
    log.info(f"{'━'*78}")
    log.info(f"  {'Time':>8} {'Left':>4} {'Phase':>7} {'Up':>6} {'Down':>6} {'Pos':>12} {'Action'}")
    log.info(f"  {'─'*8} {'─'*4} {'─'*7} {'─'*6} {'─'*6} {'─'*12} {'─'*30}")

    # Tracking
    up_min = 1.0
    down_min = 1.0
    last_up = 0.5
    last_dn = 0.5
    tick_count = 0
    swings = 0
    last_leader = None

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

        # Track swings
        leader = "up" if up > dn else ("down" if dn > up else "tie")
        if last_leader and leader != last_leader and leader != "tie":
            swings += 1
        last_leader = leader

        # ── Determine phase ──
        if elapsed < OBSERVE_PERIOD:
            phase = "👁 WATCH"
        elif remaining < FREEZE_PERIOD:
            phase = "🧊 FREEZE"
        else:
            phase = "⚡ TRADE"

        # ── Position display ──
        pos_str = ""
        if portfolio.has_up and portfolio.has_dn:
            pos_str = f"🎯 DUTCH"
        elif portfolio.has_up:
            pos_str = f"↑{portfolio.up_shares:.0f}@{portfolio.up_avg:.2f}"
        elif portfolio.has_dn:
            pos_str = f"↓{portfolio.dn_shares:.0f}@{portfolio.dn_avg:.2f}"
        else:
            pos_str = "—"

        # ── Strategy logic ──
        action = ""

        if phase == "⚡ TRADE":
            # Rule 1: If no position yet, buy the cheap side
            if not portfolio.has_up and not portfolio.has_dn:
                if up <= ENTRY_THRESHOLD:
                    trade = portfolio.buy("UP", up, BET_SIZE, int(elapsed))
                    if trade:
                        action = f"🟢 BUY UP {trade.shares:.0f}sh @{up:.3f} (${trade.cost:.2f})"
                        pos_str = f"↑{portfolio.up_shares:.0f}@{portfolio.up_avg:.2f}"
                elif dn <= ENTRY_THRESHOLD:
                    trade = portfolio.buy("DN", dn, BET_SIZE, int(elapsed))
                    if trade:
                        action = f"🟢 BUY DN {trade.shares:.0f}sh @{dn:.3f} (${trade.cost:.2f})"
                        pos_str = f"↓{portfolio.dn_shares:.0f}@{portfolio.dn_avg:.2f}"

            # Rule 2: If we have one side, look for dutch opportunity
            elif portfolio.has_up and not portfolio.has_dn:
                if dn <= ENTRY_THRESHOLD:
                    # Check if dutch would be profitable
                    combined = portfolio.up_avg + dn
                    if combined < 0.95:  # Leave some margin
                        trade = portfolio.buy("DN", dn, BET_SIZE, int(elapsed))
                        if trade:
                            action = f"🎯 DUTCH! BUY DN {trade.shares:.0f}sh @{dn:.3f} (combined={combined:.2f})"
                            pos_str = f"🎯 DUTCH"

            elif portfolio.has_dn and not portfolio.has_up:
                if up <= ENTRY_THRESHOLD:
                    combined = up + portfolio.dn_avg
                    if combined < 0.95:
                        trade = portfolio.buy("UP", up, BET_SIZE, int(elapsed))
                        if trade:
                            action = f"🎯 DUTCH! BUY UP {trade.shares:.0f}sh @{up:.3f} (combined={combined:.2f})"
                            pos_str = f"🎯 DUTCH"

        now_str = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        log.info(
            f"  {now_str} {remaining:3.0f}s {phase:>7} {up:5.3f}  {dn:5.3f}  {pos_str:>12} {action}"
        )

        tick_count += 1
        time.sleep(POLL_INTERVAL)

    # ── Resolution ──
    # Determine winner from final price
    final_prices = fetch_prices(market)
    if final_prices:
        last_up = final_prices["up"]
        last_dn = final_prices["down"]

    winner = "UP" if last_up > 0.5 else "DN"

    log.info(f"")
    log.info(f"  ┌─────────────────────────────────────────────────┐")
    log.info(f"  │  ⏱  WINDOW ENDED — Final: Up={last_up:.3f} Dn={last_dn:.3f}")
    log.info(f"  │  🏆 Winner: {winner}")

    if portfolio.trades:
        res = portfolio.resolve(winner)
        pnl_color = "+" if res["pnl"] >= 0 else ""
        log.info(f"  │  💰 Payout: ${res['payout']:.2f}  Spent: ${res['spent']:.2f}  PnL: {pnl_color}${res['pnl']:.2f}")
        log.info(f"  │  📊 Portfolio: ${portfolio.cash:.2f} cash  │  Session PnL: ${portfolio.total_pnl:+.2f}")

        if portfolio.is_dutch:
            log.info(f"  │  🎯 Dutch book was active!")
        else:
            log.info(f"  │  📌 Single-side bet: {portfolio.trades[0].side}")

        # Trade recap
        log.info(f"  │  ── Trades ──")
        for t in portfolio.trades:
            log.info(f"  │    {t.time_s:>3}s  {t.side:>2}  {t.shares:>5.0f}sh  @{t.price:.3f}  ${t.cost:.2f}")
    else:
        log.info(f"  │  📭 No trades this window")

    log.info(f"  │  📈 Swings: {swings}  Up☟={up_min:.3f}  Dn☟={down_min:.3f}")
    log.info(f"  └─────────────────────────────────────────────────┘")

    return {
        "window_ts": window_ts,
        "question": market["question"],
        "ticks": tick_count,
        "swings": swings,
        "winner": winner,
        "trades": len(portfolio.trades),
        "dutch": portfolio.is_dutch if portfolio.trades else False,
        "pnl": res["pnl"] if portfolio.trades else 0,
        "up_min": up_min,
        "down_min": down_min,
    }


# ─── Main Loop ───────────────────────────────────────────────────
def run_bot(num_windows: int = 6):
    portfolio = Portfolio(cash=START_CASH)
    results = []
    status = "completed"

    log.info(f"")
    log.info(f"  ╔══════════════════════════════════════════════════════╗")
    log.info(f"  ║  🤖 Polymarket BTC 5-Min Hybrid Bot v4              ║")
    log.info(f"  ║  💰 Paper Balance: ${START_CASH:.2f}                        ║")
    log.info(f"  ║  📋 Entry: ≤{ENTRY_THRESHOLD:.0%} │ Bet: ${BET_SIZE:.2f}/side │ Obs: {OBSERVE_PERIOD}s     ║")
    log.info(f"  ║  🔄 Windows: {num_windows} (~{num_windows * 5} min)                        ║")
    log.info(f"  ╚══════════════════════════════════════════════════════╝")

    try:
        for i in range(num_windows):
            window_ts = get_current_window_ts()
            window_end = window_ts + WINDOW_SECONDS
            now = time.time()

            # Wait for a fresh window if too late
            if window_end - now < 60:
                next_ts = window_ts + WINDOW_SECONDS
                wait = next_ts - now + 3
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

            result = trade_window(window_ts, market, portfolio)
            if result:
                results.append(result)

            if i < num_windows - 1:
                time.sleep(2)
    except KeyboardInterrupt:
        status = "interrupted"
        log.info("\n  ⚠️ Interrupted by user. Saving partial session...")
    finally:
        # ── Session Summary ──
        log.info(f"")
        log.info(f"  ╔══════════════════════════════════════════════════════╗")
        log.info(f"  ║  📊 SESSION COMPLETE                                 ║")
        log.info(f"  ╠══════════════════════════════════════════════════════╣")

        if results:
            wins = sum(1 for r in results if r["pnl"] > 0)
            losses = sum(1 for r in results if r["pnl"] < 0)
            flat = sum(1 for r in results if r["pnl"] == 0)
            dutch_count = sum(1 for r in results if r["dutch"])
            total_pnl = sum(r["pnl"] for r in results)

            log.info(f"  ║  Windows:  {len(results)} played                            ║")
            log.info(f"  ║  W/L/F:    {wins}W / {losses}L / {flat}F                            ║")
            log.info(f"  ║  Dutch:    {dutch_count}/{len(results)} completed                         ║")
            log.info(f"  ║  Total PnL: ${total_pnl:+.2f}                                ║")
            log.info(f"  ║  Final Cash: ${portfolio.cash:.2f}                            ║")
            log.info(f"  ║  ROI:      {(total_pnl / START_CASH) * 100:+.1f}%                                ║")
            log.info(f"  ╠══════════════════════════════════════════════════════╣")
            log.info(f"  ║  {'Win':>5} {'Dutch':>5} {'PnL':>7} {'Swings':>6} {'Up☟':>5} {'Dn☟':>5}  Window    ║")
            log.info(f"  ║  {'─'*5} {'─'*5} {'─'*7} {'─'*6} {'─'*5} {'─'*5}  {'─'*10}║")
            for r in results:
                w = datetime.fromtimestamp(r["window_ts"], tz=timezone.utc).strftime("%H:%M")
                pnl_s = f"${r['pnl']:+.2f}"
                win_s = "✅" if r["pnl"] > 0 else ("❌" if r["pnl"] < 0 else "➖")
                dutch_s = "🎯" if r["dutch"] else "  "
                log.info(
                    f"  ║  {win_s:>5} {dutch_s:>5} {pnl_s:>7} {r['swings']:>6} "
                    f"{r['up_min']:5.2f} {r['down_min']:5.2f}  {w:>10}║"
                )

        log.info(f"  ╚══════════════════════════════════════════════════════╝")

        # Save
        output = {
            "session": datetime.now(tz=timezone.utc).isoformat(),
            "config": {
                "entry_threshold": ENTRY_THRESHOLD,
                "bet_size": BET_SIZE,
                "start_cash": START_CASH,
                "observe_period": OBSERVE_PERIOD,
                "freeze_period": FREEZE_PERIOD,
            },
            "final_cash": portfolio.cash,
            "total_pnl": portfolio.total_pnl,
            "results": results,
        }
        with open("bot_session_v4.json", "w") as f:
            json.dump(output, f, indent=2)
        log.info(f"  💾 Saved to bot_session_v4.json")
        append_aggregate(output, status)


# ─── API Test ────────────────────────────────────────────────────
def test_api():
    log.info("🔌 Testing APIs...")
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"limit": 1}, timeout=10)
        log.info(f"   Gamma: {'✅' if r.status_code == 200 else '❌'} ({r.status_code})")
    except Exception as e:
        log.error(f"   Gamma: ❌ ({e})")
    try:
        r = requests.get(f"{CLOB_API}/time", timeout=10)
        log.info(f"   CLOB:  {'✅' if r.status_code == 200 else '❌'} ({r.status_code})")
    except Exception as e:
        log.error(f"   CLOB:  ❌ ({e})")

    window_ts = get_current_window_ts()
    market = find_market(window_ts)
    if not market:
        market = find_market(window_ts - WINDOW_SECONDS)
    if market:
        log.info(f"   Market: ✅ {market['question']}")
        prices = fetch_prices(market)
        if prices:
            log.info(f"   Prices: Up={prices['up']:.3f} Dn={prices['down']:.3f} ({prices['source']})")
    else:
        log.warning(f"   Market: ❌")


# ─── Entry Point ─────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket BTC Hybrid Bot v4")
    parser.add_argument("--test", action="store_true", help="Test API only")
    parser.add_argument("--windows", type=int, default=6, help="Windows to trade (default: 6)")
    parser.add_argument("--interval", type=float, default=2.0, help="Poll interval (default: 2.0)")
    parser.add_argument("--entry", type=float, default=0.38, help="Entry threshold (default: 0.38)")
    parser.add_argument("--observe", type=int, default=45, help="Observe period seconds (default: 45)")
    parser.add_argument("--freeze", type=int, default=30, help="Freeze period seconds (default: 30)")
    parser.add_argument("--bet", type=float, default=2.0, help="Bet size per side (default: 2.0)")
    parser.add_argument("--cash", type=float, default=10.0, help="Starting cash (default: 10.0)")
    args = parser.parse_args()

    POLL_INTERVAL = args.interval
    ENTRY_THRESHOLD = args.entry
    OBSERVE_PERIOD = args.observe
    FREEZE_PERIOD = args.freeze
    BET_SIZE = args.bet
    START_CASH = args.cash

    if args.test:
        test_api()
    else:
        setup_signal_handlers()
        run_bot(args.windows)