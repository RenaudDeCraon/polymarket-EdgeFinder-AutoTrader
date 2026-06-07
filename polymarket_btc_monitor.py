"""
Polymarket BTC 5-Min Dutch Booking Monitor v2
==============================================
Uses Gamma API real-time prices instead of CLOB order book.
Monitors price swings within 5-min windows and alerts when
dutch booking opportunities arise.

Usage:
  python polymarket_btc_monitor_v2.py              # Monitor 6 windows (30 min)
  python polymarket_btc_monitor_v2.py --windows 12 # Monitor 12 windows (1 hour)
  python polymarket_btc_monitor_v2.py --test        # API test only
"""

import os
import sys
import json
import time
import math
import logging
import argparse
import requests
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional, List

# ─── Config ──────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

WINDOW_SECONDS   = 300    # 5 minutes
POLL_INTERVAL    = 1.5    # Check every 1.5 seconds (stay under rate limits)

# Strategy thresholds
CHEAP_THRESHOLD  = 0.35   # A side is "cheap" if ≤ 35¢
DUTCH_THRESHOLD  = 0.90   # Dutch book if combined ≤ 90¢ (10¢+ guaranteed profit)

# ─── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("polybot")

# ─── Data Classes ────────────────────────────────────────────────
@dataclass
class Tick:
    time: str
    remaining: float
    up_price: Optional[float]
    down_price: Optional[float]
    price_sum: Optional[float]
    up_low: float       # Lowest Up price seen this window
    down_low: float     # Lowest Down price seen this window
    dutch_cost: float   # up_low + down_low (best possible dutch book)
    signal: str

@dataclass
class WindowResult:
    window_ts: int
    question: str
    start_time: str
    ticks: int
    up_min: float
    up_max: float
    down_min: float
    down_max: float
    best_dutch: float      # Lowest up_low + down_low seen
    had_opportunity: bool
    swings: int            # Number of times lead side changed
    signals: List[str]


# ─── Market Discovery ────────────────────────────────────────────
def get_current_window_ts() -> int:
    now = int(time.time())
    return now - (now % WINDOW_SECONDS)

def find_market(window_ts: int) -> Optional[dict]:
    """Find BTC 5-min market via Gamma API. Returns market dict or None."""
    slug = f"btc-updown-5m-{window_ts}"
    
    for endpoint in ["events", "markets"]:
        try:
            params = {"slug": slug, "limit": 1}
            resp = requests.get(f"{GAMMA_API}/{endpoint}", params=params, timeout=10)
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
            
            # Parse outcomes and prices
            outcomes = market.get("outcomes", "[]")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            prices = market.get("outcomePrices", "[]")
            if isinstance(prices, str):
                prices = json.loads(prices)
            
            tokens = market.get("clobTokenIds", "[]")
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            
            # Map Up/Down
            up_idx = down_idx = None
            for i, o in enumerate(outcomes):
                if str(o).strip().lower() == "up":
                    up_idx = i
                elif str(o).strip().lower() == "down":
                    down_idx = i
            
            if up_idx is None or down_idx is None:
                continue
            
            return {
                "market_id": str(market.get("id", "")),
                "condition_id": market.get("conditionId", ""),
                "slug": slug,
                "question": market.get("question", slug),
                "up_idx": up_idx,
                "down_idx": down_idx,
                "up_token": tokens[up_idx] if len(tokens) > up_idx else None,
                "down_token": tokens[down_idx] if len(tokens) > down_idx else None,
            }
        except Exception as e:
            log.debug(f"  {endpoint} lookup failed: {e}")
    
    return None


def fetch_gamma_prices(market_info: dict) -> Optional[dict]:
    """Fetch current Up/Down prices from Gamma API."""
    slug = market_info["slug"]
    
    # Try events endpoint first (more reliable for price updates)
    for endpoint in ["events", "markets"]:
        try:
            resp = requests.get(
                f"{GAMMA_API}/{endpoint}",
                params={"slug": slug, "limit": 1},
                timeout=8
            )
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
            
            prices = market.get("outcomePrices", "[]")
            if isinstance(prices, str):
                prices = json.loads(prices)
            
            if len(prices) > max(market_info["up_idx"], market_info["down_idx"]):
                up_price = float(prices[market_info["up_idx"]])
                down_price = float(prices[market_info["down_idx"]])
                return {"up": up_price, "down": down_price}
        
        except Exception as e:
            log.debug(f"  Price fetch ({endpoint}) failed: {e}")
    
    return None


# ─── Monitor Loop ────────────────────────────────────────────────
def monitor_window(window_ts: int, market_info: dict) -> Optional[WindowResult]:
    """Monitor a single 5-min window."""
    window_end = window_ts + WINDOW_SECONDS
    now = time.time()
    
    if now >= window_end - 3:
        log.warning("  Window almost over, skipping.")
        return None
    
    log.info(f"")
    log.info(f"{'━'*70}")
    log.info(f"  🎯 {market_info['question']}")
    log.info(f"  ⏰ {datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime('%H:%M')} → "
             f"{datetime.fromtimestamp(window_end, tz=timezone.utc).strftime('%H:%M')} UTC")
    log.info(f"{'━'*70}")
    log.info(f"  {'Time':>8}  {'Left':>5}  {'Up':>6}  {'Down':>6}  {'Sum':>6}  "
             f"{'Up☟':>5}  {'Dn☟':>5}  {'Dutch':>6}  Signal")
    log.info(f"  {'─'*8}  {'─'*5}  {'─'*6}  {'─'*6}  {'─'*6}  "
             f"{'─'*5}  {'─'*5}  {'─'*6}  {'─'*20}")
    
    # Tracking
    up_min = 1.0
    up_max = 0.0
    down_min = 1.0
    down_max = 0.0
    best_dutch = 2.0
    last_leader = None
    swings = 0
    signals = []
    tick_count = 0
    had_opportunity = False
    
    while time.time() < window_end - 3:
        remaining = window_end - time.time()
        
        prices = fetch_gamma_prices(market_info)
        if not prices:
            time.sleep(POLL_INTERVAL)
            continue
        
        up_p = prices["up"]
        down_p = prices["down"]
        price_sum = up_p + down_p
        
        # Update extremes
        up_min = min(up_min, up_p)
        up_max = max(up_max, up_p)
        down_min = min(down_min, down_p)
        down_max = max(down_max, down_p)
        
        dutch_cost = up_min + down_min
        best_dutch = min(best_dutch, dutch_cost)
        
        # Detect leader changes (swings)
        current_leader = "up" if up_p > down_p else "down" if down_p > up_p else "tie"
        if last_leader and current_leader != last_leader and current_leader != "tie":
            swings += 1
        last_leader = current_leader
        
        # ── Signals ──
        signal_parts = []
        
        # Cheap side detection
        if up_p <= CHEAP_THRESHOLD:
            signal_parts.append(f"🟢Up={up_p:.2f}")
        if down_p <= CHEAP_THRESHOLD:
            signal_parts.append(f"🟢Dn={down_p:.2f}")
        
        # Dutch book check (using historical mins)
        if dutch_cost <= DUTCH_THRESHOLD:
            profit_pct = (1.0 - dutch_cost) * 100
            signal_parts.append(f"🎯DUTCH {dutch_cost:.2f} (+{profit_pct:.0f}%)")
            had_opportunity = True
        
        # Swing alert
        if swings > 0 and current_leader != last_leader:
            signal_parts.append(f"🔄Swing#{swings}")
        
        # High volatility (big range developing)
        up_range = up_max - up_min
        down_range = down_max - down_min
        if up_range > 0.15 or down_range > 0.15:
            signal_parts.append(f"📊Vol!")
        
        signal = " ".join(signal_parts)
        if signal and signal not in signals:
            signals.append(signal)
        
        # Print tick
        now_str = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        log.info(
            f"  {now_str}  {remaining:4.0f}s  "
            f"{up_p:5.3f}   {down_p:5.3f}   {price_sum:5.3f}   "
            f"{up_min:4.2f}   {down_min:4.2f}   {dutch_cost:5.3f}   "
            f"{signal}"
        )
        
        tick_count += 1
        time.sleep(POLL_INTERVAL)
    
    # Window complete
    result = WindowResult(
        window_ts=window_ts,
        question=market_info["question"],
        start_time=datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime("%H:%M:%S"),
        ticks=tick_count,
        up_min=up_min,
        up_max=up_max,
        down_min=down_min,
        down_max=down_max,
        best_dutch=best_dutch,
        had_opportunity=had_opportunity,
        swings=swings,
        signals=signals,
    )
    
    log.info(f"")
    log.info(f"  📋 Window Summary:")
    log.info(f"     Up  range: {up_min:.3f} → {up_max:.3f} (Δ {up_max-up_min:.3f})")
    log.info(f"     Dn  range: {down_min:.3f} → {down_max:.3f} (Δ {down_max-down_min:.3f})")
    log.info(f"     Best dutch: {best_dutch:.3f} {'✅ PROFITABLE' if best_dutch < 1.0 else '❌ No arb'}")
    log.info(f"     Swings: {swings}")
    
    return result


# ─── Main Monitor ────────────────────────────────────────────────
def run_monitor(num_windows: int = 6):
    """Monitor multiple 5-min windows."""
    results: List[WindowResult] = []
    
    log.info(f"🚀 Polymarket BTC 5-Min Monitor v2 (Gamma Prices)")
    log.info(f"   Cheap threshold: ≤ {CHEAP_THRESHOLD:.0%}")
    log.info(f"   Dutch threshold: ≤ {DUTCH_THRESHOLD:.0%}")
    log.info(f"   Monitoring {num_windows} windows (~{num_windows*5} min)")
    log.info(f"   Poll interval: {POLL_INTERVAL}s")
    
    for i in range(num_windows):
        window_ts = get_current_window_ts()
        window_end = window_ts + WINDOW_SECONDS
        now = time.time()
        
        # If window is almost over, wait for next
        if window_end - now < 30:
            next_ts = window_ts + WINDOW_SECONDS
            wait = next_ts - now + 3  # 3s buffer
            log.info(f"")
            log.info(f"  ⏳ Window almost over, waiting {wait:.0f}s for next...")
            time.sleep(wait)
            window_ts = get_current_window_ts()
        
        # Find market
        log.info(f"")
        log.info(f"  🔍 Finding market for window {window_ts}...")
        
        market = find_market(window_ts)
        attempts = 0
        while not market and attempts < 5:
            attempts += 1
            log.info(f"     Retry {attempts}/5...")
            time.sleep(3)
            market = find_market(window_ts)
        
        if not market:
            log.warning(f"  ❌ Market not found, skipping window.")
            time.sleep(30)
            continue
        
        # Monitor
        result = monitor_window(window_ts, market)
        if result:
            results.append(result)
        
        # Brief pause
        if i < num_windows - 1:
            time.sleep(2)
    
    # ── Session Summary ──
    log.info(f"")
    log.info(f"{'━'*70}")
    log.info(f"  📊 SESSION SUMMARY ({len(results)} windows)")
    log.info(f"{'━'*70}")
    
    if results:
        total_swings = sum(r.swings for r in results)
        dutch_opps = sum(1 for r in results if r.had_opportunity)
        avg_dutch = sum(r.best_dutch for r in results) / len(results)
        min_dutch = min(r.best_dutch for r in results)
        
        up_ranges = [r.up_max - r.up_min for r in results]
        down_ranges = [r.down_max - r.down_min for r in results]
        avg_up_range = sum(up_ranges) / len(up_ranges)
        avg_down_range = sum(down_ranges) / len(down_ranges)
        
        log.info(f"  Windows monitored:  {len(results)}")
        log.info(f"  Total swings:       {total_swings}")
        log.info(f"  Dutch opportunities: {dutch_opps}")
        log.info(f"  Best dutch cost:    {min_dutch:.3f} "
                 f"({'✅ ' + str(round((1-min_dutch)*100)) + '¢ profit/share' if min_dutch < 1.0 else '❌'})")
        log.info(f"  Avg dutch cost:     {avg_dutch:.3f}")
        log.info(f"  Avg Up swing:       {avg_up_range:.3f} ({avg_up_range*100:.1f}¢)")
        log.info(f"  Avg Down swing:     {avg_down_range:.3f} ({avg_down_range*100:.1f}¢)")
        log.info(f"")
        
        # Per-window breakdown
        log.info(f"  {'Window':>8}  {'Up Range':>10}  {'Dn Range':>10}  {'Dutch':>7}  {'Swings':>6}  {'Opp':>3}")
        log.info(f"  {'─'*8}  {'─'*10}  {'─'*10}  {'─'*7}  {'─'*6}  {'─'*3}")
        for r in results:
            opp_str = "✅" if r.had_opportunity else "❌"
            log.info(
                f"  {r.start_time:>8}  "
                f"{r.up_min:.2f}→{r.up_max:.2f}  "
                f"{r.down_min:.2f}→{r.down_max:.2f}  "
                f"{r.best_dutch:6.3f}  "
                f"{r.swings:>6}  "
                f"{opp_str:>3}"
            )
    
    log.info(f"{'━'*70}")
    
    # Save results
    output = {
        "session_time": datetime.now(tz=timezone.utc).isoformat(),
        "windows": [asdict(r) for r in results],
    }
    with open("monitor_results.json", "w") as f:
        json.dump(output, f, indent=2)
    log.info(f"  💾 Results saved to monitor_results.json")
    
    return results


# ─── API Test ────────────────────────────────────────────────────
def test_api():
    log.info("🔌 Testing APIs...")
    
    # Gamma
    try:
        resp = requests.get(f"{GAMMA_API}/events", params={"limit": 1}, timeout=10)
        log.info(f"   Gamma API: {'✅' if resp.status_code == 200 else '❌'} ({resp.status_code})")
    except Exception as e:
        log.error(f"   Gamma API: ❌ ({e})")
    
    # CLOB
    try:
        resp = requests.get(f"{CLOB_API}/time", timeout=10)
        log.info(f"   CLOB API:  {'✅' if resp.status_code == 200 else '❌'} ({resp.status_code})")
    except Exception as e:
        log.error(f"   CLOB API:  ❌ ({e})")
    
    # Find current market
    window_ts = get_current_window_ts()
    market = find_market(window_ts)
    if market:
        log.info(f"   Market: ✅ {market['question']}")
        prices = fetch_gamma_prices(market)
        if prices:
            log.info(f"   Up: {prices['up']:.3f}  Down: {prices['down']:.3f}  Sum: {prices['up']+prices['down']:.3f}")
        else:
            log.warning(f"   Prices: ❌ Could not fetch")
    else:
        log.warning(f"   Market: ❌ Not found for {window_ts}")


# ─── Entry Point ─────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket BTC 5-Min Monitor v2")
    parser.add_argument("--test", action="store_true", help="Test API only")
    parser.add_argument("--windows", type=int, default=6, help="Windows to monitor (default: 6 = 30 min)")
    parser.add_argument("--interval", type=float, default=1.5, help="Poll interval seconds (default: 1.5)")
    parser.add_argument("--cheap", type=float, default=0.35, help="Cheap threshold (default: 0.35)")
    parser.add_argument("--dutch", type=float, default=0.90, help="Dutch threshold (default: 0.90)")
    args = parser.parse_args()
    
    POLL_INTERVAL = args.interval
    CHEAP_THRESHOLD = args.cheap
    DUTCH_THRESHOLD = args.dutch
    
    if args.test:
        test_api()
    else:
        run_monitor(num_windows=args.windows)