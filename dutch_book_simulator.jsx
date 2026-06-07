import { useState, useEffect, useCallback, useRef } from "react";

// ─── Simulated price data from real observations ───
const REAL_WINDOWS = [
  {
    label: "Window A — One-way (Down wins)",
    description: "BTC drops steadily, no reversal",
    ticks: (() => {
      const t = [];
      // Starts 50/50, Up drops to 0.01
      for (let i = 0; i < 30; i++) t.push({ s: i * 2, up: Math.max(0.505 - i * 0.016, 0.01), dn: null });
      t.forEach(x => { x.dn = +(1 - x.up).toFixed(3); });
      return t;
    })()
  },
  {
    label: "Window B — Swing market (7 swings)",
    description: "BTC drops then reverses — dutch book opportunity",
    ticks: (() => {
      const raw = [
        [0,0.495],[5,0.405],[7,0.325],[10,0.315],[13,0.365],[16,0.335],[20,0.405],[24,0.445],
        [28,0.435],[32,0.395],[36,0.475],[40,0.545],[42,0.625],[44,0.615],[46,0.695],[48,0.735],
        [50,0.680],[52,0.665],[54,0.545],[56,0.515],[58,0.430],[60,0.475],[62,0.525],[64,0.575],
        [66,0.545],[70,0.495],[74,0.525],[78,0.515],[82,0.545],[86,0.565],[90,0.625],[94,0.615],
        [98,0.565],[102,0.555],[106,0.625],[110,0.615],[114,0.575],[118,0.565],[122,0.515],[126,0.445],
        [130,0.375],[134,0.345],[138,0.355],[142,0.485],[146,0.525],[150,0.605],[154,0.615],[158,0.515],
        [162,0.505],[166,0.525],[170,0.585],[174,0.620],[178,0.635],[182,0.660],[186,0.695],[190,0.735],
        [194,0.775],[198,0.845],[202,0.935],[206,0.950],[210,0.965],[214,0.975],[218,0.985],[222,0.965],
        [226,0.975],[230,0.965],[234,0.985],[238,0.975],[242,0.985],[246,0.985],[250,0.990],[254,0.955],
        [258,0.915],[262,0.530],[266,0.635],[270,0.765],[274,0.865],[278,0.855],[282,0.685],[286,0.715],
        [290,0.755],[294,0.715],[298,0.925]
      ];
      return raw.map(([s, up]) => ({ s, up, dn: +(1 - up).toFixed(3) }));
    })()
  },
  {
    label: "Window C — High vol swing (10 swings)",
    description: "Wild swings, dutch = 0.27 possible",
    ticks: (() => {
      const raw = [
        [0,0.505],[4,0.515],[8,0.505],[12,0.465],[16,0.465],[20,0.455],[24,0.445],[28,0.395],
        [32,0.395],[36,0.385],[40,0.385],[44,0.275],[48,0.255],[52,0.295],[56,0.285],[60,0.285],
        [64,0.285],[68,0.295],[72,0.275],[76,0.275],[80,0.275],[84,0.285],[88,0.285],[92,0.335],
        [96,0.345],[100,0.355],[104,0.375],[108,0.395],[112,0.475],[116,0.525],[120,0.495],[124,0.495],
        [128,0.505],[132,0.515],[136,0.515],[140,0.525],[144,0.525],[148,0.525],[152,0.505],[156,0.545],
        [160,0.535],[164,0.535],[168,0.575],[172,0.525],[176,0.525],[180,0.525],[184,0.465],[188,0.465],
        [192,0.475],[196,0.475],[200,0.425],[204,0.435],[208,0.455],[212,0.475],[216,0.485],[220,0.545],
        [224,0.545],[228,0.615],[232,0.655],[236,0.655],[240,0.645],[244,0.675],[248,0.685],[252,0.685],
        [256,0.705],[260,0.705],[264,0.705],[268,0.675],[272,0.665],[276,0.665],[280,0.675],[284,0.635],
        [288,0.645],[292,0.595],[296,0.595],[300,0.595]
      ];
      return raw.map(([s, up]) => ({ s, up, dn: +(1 - up).toFixed(3) }));
    })()
  },
  {
    label: "Window D — Choppy then trend",
    description: "Starts flat, then trends up sharply",
    ticks: (() => {
      const raw = [
        [0,0.495],[4,0.495],[8,0.465],[12,0.405],[16,0.445],[20,0.430],[24,0.335],[28,0.365],
        [32,0.365],[36,0.385],[40,0.385],[44,0.445],[48,0.375],[52,0.375],[56,0.375],[60,0.355],
        [64,0.365],[68,0.375],[72,0.395],[76,0.395],[80,0.375],[84,0.435],[88,0.475],[92,0.485],
        [96,0.545],[100,0.545],[104,0.630],[108,0.645],[112,0.675],[116,0.645],[120,0.675],[124,0.685],
        [128,0.685],[132,0.705],[136,0.705],[140,0.705],[144,0.675],[148,0.665],[152,0.665],[156,0.675],
        [160,0.635],[164,0.645],[168,0.595],[172,0.595],[176,0.595],[180,0.380],[184,0.345],[188,0.460],
        [192,0.475],[196,0.475],[200,0.435],[204,0.435],[208,0.435],[212,0.395],[216,0.385],[220,0.395],
        [224,0.455],[228,0.415],[232,0.395],[236,0.385],[240,0.445],[244,0.455],[248,0.505],[252,0.515],
        [256,0.505],[260,0.505],[264,0.515],[268,0.525],[272,0.525],[276,0.505],[280,0.545],[284,0.535],
        [288,0.535],[292,0.575],[296,0.525]
      ];
      return raw.map(([s, up]) => ({ s, up, dn: +(1 - up).toFixed(3) }));
    })()
  }
];

// ─── Strategy engine ───
function runStrategy(ticks, params) {
  const { entryThreshold, betSize, enableDutch } = params;
  let cash = params.startCash;
  const startCash = cash;
  let upShares = 0, downShares = 0;
  let upAvgPrice = 0, downAvgPrice = 0;
  let upBought = false, downBought = false;
  const trades = [];
  const equityCurve = [];
  let dutchComplete = false;

  for (let i = 0; i < ticks.length; i++) {
    const { s, up, dn } = ticks[i];
    const timeLeft = 300 - s;

    // Strategy: buy cheap side
    if (!upBought && up <= entryThreshold && timeLeft > 30) {
      const shares = Math.floor((betSize / up) * 100) / 100;
      const cost = +(shares * up).toFixed(4);
      if (cost <= cash) {
        cash = +(cash - cost).toFixed(4);
        upShares = shares;
        upAvgPrice = up;
        upBought = true;
        trades.push({ time: s, side: "UP", price: up, shares, cost, action: "BUY" });
      }
    }

    if (!downBought && dn <= entryThreshold && timeLeft > 30) {
      const shares = Math.floor((betSize / dn) * 100) / 100;
      const cost = +(shares * dn).toFixed(4);
      if (cost <= cash) {
        cash = +(cash - cost).toFixed(4);
        downShares = shares;
        downAvgPrice = dn;
        downBought = true;
        trades.push({ time: s, side: "DN", price: dn, shares, cost, action: "BUY" });
      }
    }

    if (upBought && downBought && !dutchComplete) {
      dutchComplete = true;
      trades.push({ time: s, side: "--", price: 0, shares: 0, cost: 0, action: "🎯 DUTCH COMPLETE" });
    }

    // Equity = cash + position value at current prices
    const posValue = upShares * up + downShares * dn;
    equityCurve.push({ s, equity: +(cash + posValue).toFixed(4), cash, posValue: +posValue.toFixed(4) });
  }

  // Resolution: simulate both outcomes
  const upWinPayout = upShares * 1.0;
  const downWinPayout = downShares * 1.0;

  const upWinTotal = +(cash + upWinPayout).toFixed(4);
  const downWinTotal = +(cash + downWinPayout).toFixed(4);

  const upWinPnL = +(upWinTotal - startCash).toFixed(4);
  const downWinPnL = +(downWinTotal - startCash).toFixed(4);

  const guaranteed = upBought && downBought && upWinPnL > 0 && downWinPnL > 0;

  return {
    trades,
    equityCurve,
    upShares, downShares,
    upAvgPrice, downAvgPrice,
    cashLeft: cash,
    upWinTotal, downWinTotal,
    upWinPnL, downWinPnL,
    guaranteed,
    dutchComplete,
    totalSpent: +(startCash - cash).toFixed(4)
  };
}

// ─── Components ───
function PriceChart({ ticks, trades, height = 180 }) {
  const w = 680, h = height, pad = { t: 20, r: 40, b: 30, l: 50 };
  const cw = w - pad.l - pad.r, ch = h - pad.t - pad.b;

  const maxS = ticks[ticks.length - 1].s;
  const x = (s) => pad.l + (s / maxS) * cw;
  const y = (p) => pad.t + (1 - p) * ch;

  const upPath = ticks.map((t, i) => `${i === 0 ? 'M' : 'L'}${x(t.s).toFixed(1)},${y(t.up).toFixed(1)}`).join(' ');
  const dnPath = ticks.map((t, i) => `${i === 0 ? 'M' : 'L'}${x(t.s).toFixed(1)},${y(t.dn).toFixed(1)}`).join(' ');

  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: '100%', maxWidth: w, display: 'block' }}>
      <rect x={pad.l} y={pad.t} width={cw} height={ch} fill="var(--chart-bg)" rx="4" />
      {/* Grid */}
      {[0, 0.25, 0.5, 0.75, 1].map(v => (
        <g key={v}>
          <line x1={pad.l} x2={w - pad.r} y1={y(v)} y2={y(v)} stroke="var(--grid)" strokeWidth="0.5" />
          <text x={pad.l - 6} y={y(v) + 4} textAnchor="end" fill="var(--text-dim)" fontSize="10" fontFamily="'JetBrains Mono',monospace">{(v * 100).toFixed(0)}¢</text>
        </g>
      ))}
      {/* 50¢ line */}
      <line x1={pad.l} x2={w - pad.r} y1={y(0.5)} y2={y(0.5)} stroke="var(--text-dim)" strokeWidth="1" strokeDasharray="4 3" opacity="0.5" />
      {/* Paths */}
      <path d={upPath} fill="none" stroke="var(--up-color)" strokeWidth="2" />
      <path d={dnPath} fill="none" stroke="var(--dn-color)" strokeWidth="2" />
      {/* Trade markers */}
      {trades.filter(t => t.action === "BUY").map((t, i) => (
        <g key={i}>
          <circle cx={x(t.time)} cy={y(t.price)} r="6" fill={t.side === "UP" ? "var(--up-color)" : "var(--dn-color)"} stroke="var(--bg)" strokeWidth="2" />
          <text x={x(t.time)} y={y(t.price) - 10} textAnchor="middle" fill={t.side === "UP" ? "var(--up-color)" : "var(--dn-color)"} fontSize="9" fontWeight="700" fontFamily="'JetBrains Mono',monospace">
            {t.side} @{(t.price * 100).toFixed(0)}¢
          </text>
        </g>
      ))}
      {/* Labels */}
      <text x={w - pad.r + 4} y={y(ticks[ticks.length - 1].up) + 4} fill="var(--up-color)" fontSize="10" fontWeight="700" fontFamily="'JetBrains Mono',monospace">Up</text>
      <text x={w - pad.r + 4} y={y(ticks[ticks.length - 1].dn) + 4} fill="var(--dn-color)" fontSize="10" fontWeight="700" fontFamily="'JetBrains Mono',monospace">Dn</text>
      {/* X axis */}
      {[0, 60, 120, 180, 240, 300].map(s => (
        <text key={s} x={x(Math.min(s, maxS))} y={h - 6} textAnchor="middle" fill="var(--text-dim)" fontSize="9" fontFamily="'JetBrains Mono',monospace">{s}s</text>
      ))}
    </svg>
  );
}

function TradeLog({ result }) {
  if (!result.trades.length) return <p style={{ color: 'var(--text-dim)', fontStyle: 'italic', margin: '8px 0' }}>No trades triggered</p>;
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13, fontFamily: "'JetBrains Mono', monospace" }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--border)' }}>
            {['Time', 'Action', 'Side', 'Price', 'Shares', 'Cost'].map(h => (
              <th key={h} style={{ padding: '6px 8px', textAlign: 'left', color: 'var(--text-dim)', fontWeight: 500, fontSize: 11 }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {result.trades.map((t, i) => (
            <tr key={i} style={{ borderBottom: '1px solid var(--border-light)' }}>
              <td style={{ padding: '5px 8px' }}>{t.time}s</td>
              <td style={{ padding: '5px 8px', color: t.action.includes('DUTCH') ? 'var(--accent)' : 'var(--text)' }}>{t.action}</td>
              <td style={{ padding: '5px 8px', color: t.side === 'UP' ? 'var(--up-color)' : t.side === 'DN' ? 'var(--dn-color)' : 'var(--text-dim)' }}>{t.side}</td>
              <td style={{ padding: '5px 8px' }}>{t.price ? `${(t.price * 100).toFixed(1)}¢` : '—'}</td>
              <td style={{ padding: '5px 8px' }}>{t.shares || '—'}</td>
              <td style={{ padding: '5px 8px' }}>{t.cost ? `$${t.cost.toFixed(2)}` : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OutcomeCard({ label, total, pnl, color }) {
  return (
    <div style={{
      padding: '12px 16px',
      background: pnl > 0 ? 'rgba(34,197,94,0.08)' : pnl < 0 ? 'rgba(239,68,68,0.08)' : 'var(--card-bg)',
      borderRadius: 8,
      border: `1px solid ${pnl > 0 ? 'rgba(34,197,94,0.3)' : pnl < 0 ? 'rgba(239,68,68,0.3)' : 'var(--border)'}`,
      flex: 1,
      minWidth: 140
    }}>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 4, fontWeight: 600, textTransform: 'uppercase', letterSpacing: 1 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color }}>${total.toFixed(2)}</div>
      <div style={{ fontSize: 13, fontWeight: 600, color: pnl > 0 ? '#22c55e' : pnl < 0 ? '#ef4444' : 'var(--text-dim)', fontFamily: "'JetBrains Mono', monospace" }}>
        {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)}
      </div>
    </div>
  );
}

// ─── Main App ───
export default function App() {
  const [selectedWindow, setSelectedWindow] = useState(1);
  const [entryThreshold, setEntryThreshold] = useState(0.35);
  const [betSize, setBetSize] = useState(2.0);
  const [startCash, setStartCash] = useState(10.0);
  const [playback, setPlayback] = useState(false);
  const [playIdx, setPlayIdx] = useState(0);
  const timerRef = useRef(null);

  const windowData = REAL_WINDOWS[selectedWindow];
  const allTicks = windowData.ticks;
  const visibleTicks = playback ? allTicks.slice(0, playIdx + 1) : allTicks;

  const result = runStrategy(visibleTicks, { entryThreshold, betSize, startCash, enableDutch: true });

  // Batch sim across all windows
  const batchResults = REAL_WINDOWS.map((w, i) =>
    runStrategy(w.ticks, { entryThreshold, betSize, startCash, enableDutch: true })
  );

  useEffect(() => {
    if (playback && playIdx < allTicks.length - 1) {
      timerRef.current = setTimeout(() => setPlayIdx(p => p + 1), 80);
    } else if (playIdx >= allTicks.length - 1) {
      setPlayback(false);
    }
    return () => clearTimeout(timerRef.current);
  }, [playback, playIdx, allTicks.length]);

  const startPlayback = () => {
    setPlayIdx(0);
    setPlayback(true);
  };

  return (
    <div style={{
      '--bg': '#0c0e14',
      '--card-bg': '#12151e',
      '--border': '#1e2232',
      '--border-light': '#181c28',
      '--text': '#e2e4ea',
      '--text-dim': '#6b7280',
      '--up-color': '#22c55e',
      '--dn-color': '#ef4444',
      '--accent': '#f59e0b',
      '--chart-bg': '#0a0c12',
      '--grid': '#1a1e2a',
      '--input-bg': '#181c28',
      fontFamily: "'Inter', -apple-system, sans-serif",
      color: 'var(--text)',
      padding: '20px 24px',
      minHeight: '100vh',
      background: 'var(--bg)',
    }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500;600;700&display=swap');
        input[type=range] { accent-color: var(--accent); }
      `}</style>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 24 }}>
        <div style={{ width: 36, height: 36, borderRadius: 8, background: 'linear-gradient(135deg, #f59e0b, #d97706)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 18, fontWeight: 700 }}>₿</div>
        <div>
          <h1 style={{ margin: 0, fontSize: 20, fontWeight: 700, letterSpacing: -0.5 }}>Dutch Book Simulator</h1>
          <p style={{ margin: 0, fontSize: 12, color: 'var(--text-dim)' }}>BTC 5-Min Up/Down — Strategy Backtester</p>
        </div>
      </div>

      {/* Controls */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 12, marginBottom: 20 }}>
        <div style={{ background: 'var(--card-bg)', padding: '12px 16px', borderRadius: 10, border: '1px solid var(--border)' }}>
          <label style={{ fontSize: 11, color: 'var(--text-dim)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.8 }}>Entry Threshold</label>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
            <input type="range" min="0.10" max="0.50" step="0.01" value={entryThreshold}
              onChange={e => setEntryThreshold(+e.target.value)} style={{ flex: 1 }} />
            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 16, minWidth: 50, textAlign: 'right' }}>{(entryThreshold * 100).toFixed(0)}¢</span>
          </div>
        </div>
        <div style={{ background: 'var(--card-bg)', padding: '12px 16px', borderRadius: 10, border: '1px solid var(--border)' }}>
          <label style={{ fontSize: 11, color: 'var(--text-dim)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.8 }}>Bet Size / Side</label>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
            <input type="range" min="0.5" max="5.0" step="0.25" value={betSize}
              onChange={e => setBetSize(+e.target.value)} style={{ flex: 1 }} />
            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 16, minWidth: 50, textAlign: 'right' }}>${betSize.toFixed(2)}</span>
          </div>
        </div>
        <div style={{ background: 'var(--card-bg)', padding: '12px 16px', borderRadius: 10, border: '1px solid var(--border)' }}>
          <label style={{ fontSize: 11, color: 'var(--text-dim)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.8 }}>Starting Cash</label>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
            <input type="range" min="2" max="20" step="0.5" value={startCash}
              onChange={e => setStartCash(+e.target.value)} style={{ flex: 1 }} />
            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 16, minWidth: 50, textAlign: 'right' }}>${startCash.toFixed(1)}</span>
          </div>
        </div>
      </div>

      {/* Window selector */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
        {REAL_WINDOWS.map((w, i) => (
          <button key={i} onClick={() => { setSelectedWindow(i); setPlayback(false); setPlayIdx(0); }}
            style={{
              padding: '8px 14px', borderRadius: 8, border: `1px solid ${i === selectedWindow ? 'var(--accent)' : 'var(--border)'}`,
              background: i === selectedWindow ? 'rgba(245,158,11,0.12)' : 'var(--card-bg)',
              color: i === selectedWindow ? 'var(--accent)' : 'var(--text-dim)',
              cursor: 'pointer', fontSize: 12, fontWeight: 600, transition: 'all 0.15s'
            }}>
            {w.label.split('—')[0].trim()}
          </button>
        ))}
        <button onClick={startPlayback}
          style={{
            padding: '8px 16px', borderRadius: 8, border: '1px solid var(--accent)',
            background: 'var(--accent)', color: '#000', cursor: 'pointer',
            fontSize: 12, fontWeight: 700, marginLeft: 'auto'
          }}>
          ▶ Replay
        </button>
      </div>

      {/* Window info */}
      <div style={{ marginBottom: 12 }}>
        <h2 style={{ margin: '0 0 2px', fontSize: 15, fontWeight: 700 }}>{windowData.label}</h2>
        <p style={{ margin: 0, fontSize: 12, color: 'var(--text-dim)' }}>{windowData.description}</p>
      </div>

      {/* Chart */}
      <div style={{ background: 'var(--card-bg)', padding: 16, borderRadius: 12, border: '1px solid var(--border)', marginBottom: 16 }}>
        <PriceChart ticks={visibleTicks} trades={result.trades} />
      </div>

      {/* Results */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 16, marginBottom: 20 }}>
        {/* Outcomes */}
        <div style={{ background: 'var(--card-bg)', padding: 16, borderRadius: 12, border: '1px solid var(--border)' }}>
          <h3 style={{ margin: '0 0 12px', fontSize: 13, fontWeight: 700, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: 1 }}>Outcomes</h3>
          <div style={{ display: 'flex', gap: 10, marginBottom: 12 }}>
            <OutcomeCard label="If Up wins" total={result.upWinTotal} pnl={result.upWinPnL} color="var(--up-color)" />
            <OutcomeCard label="If Down wins" total={result.downWinTotal} pnl={result.downWinPnL} color="var(--dn-color)" />
          </div>
          {result.guaranteed && (
            <div style={{
              padding: '10px 14px', background: 'rgba(245,158,11,0.1)', borderRadius: 8,
              border: '1px solid rgba(245,158,11,0.3)', display: 'flex', alignItems: 'center', gap: 8
            }}>
              <span style={{ fontSize: 18 }}>🎯</span>
              <div>
                <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--accent)' }}>Dutch Book Complete!</div>
                <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                  Guaranteed profit: min ${Math.min(result.upWinPnL, result.downWinPnL).toFixed(2)} — max ${Math.max(result.upWinPnL, result.downWinPnL).toFixed(2)}
                </div>
              </div>
            </div>
          )}
          {!result.guaranteed && result.trades.length > 0 && (
            <div style={{ padding: '10px 14px', background: 'rgba(239,68,68,0.08)', borderRadius: 8, border: '1px solid rgba(239,68,68,0.2)' }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: '#ef4444' }}>Single-side bet only</div>
              <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>No reversal — profit depends on outcome</div>
            </div>
          )}
          <div style={{ marginTop: 12, fontSize: 12, fontFamily: "'JetBrains Mono', monospace", color: 'var(--text-dim)' }}>
            Spent: ${result.totalSpent.toFixed(2)} · Cash left: ${result.cashLeft.toFixed(2)} · Up shares: {result.upShares.toFixed(1)} · Down shares: {result.downShares.toFixed(1)}
          </div>
        </div>

        {/* Trade log */}
        <div style={{ background: 'var(--card-bg)', padding: 16, borderRadius: 12, border: '1px solid var(--border)' }}>
          <h3 style={{ margin: '0 0 12px', fontSize: 13, fontWeight: 700, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: 1 }}>Trade Log</h3>
          <TradeLog result={result} />
        </div>
      </div>

      {/* Batch results */}
      <div style={{ background: 'var(--card-bg)', padding: 16, borderRadius: 12, border: '1px solid var(--border)' }}>
        <h3 style={{ margin: '0 0 12px', fontSize: 13, fontWeight: 700, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: 1 }}>
          All Windows — Current Settings ({(entryThreshold * 100).toFixed(0)}¢ / ${betSize.toFixed(2)})
        </h3>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12, fontFamily: "'JetBrains Mono', monospace" }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)' }}>
                {['Window', 'Trades', 'Dutch?', 'Spent', 'If Up', 'If Down', 'Min PnL', 'Guaranteed'].map(h => (
                  <th key={h} style={{ padding: '6px 8px', textAlign: 'left', color: 'var(--text-dim)', fontWeight: 500, fontSize: 10, textTransform: 'uppercase' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {batchResults.map((r, i) => {
                const minPnL = Math.min(r.upWinPnL, r.downWinPnL);
                return (
                  <tr key={i} style={{ borderBottom: '1px solid var(--border-light)', cursor: 'pointer', background: i === selectedWindow ? 'rgba(245,158,11,0.06)' : 'transparent' }}
                    onClick={() => { setSelectedWindow(i); setPlayback(false); setPlayIdx(0); }}>
                    <td style={{ padding: '6px 8px' }}>{REAL_WINDOWS[i].label.split('—')[0].trim()}</td>
                    <td style={{ padding: '6px 8px' }}>{r.trades.filter(t => t.action === 'BUY').length}</td>
                    <td style={{ padding: '6px 8px' }}>{r.dutchComplete ? '✅' : '❌'}</td>
                    <td style={{ padding: '6px 8px' }}>${r.totalSpent.toFixed(2)}</td>
                    <td style={{ padding: '6px 8px', color: r.upWinPnL > 0 ? '#22c55e' : '#ef4444' }}>{r.upWinPnL >= 0 ? '+' : ''}{r.upWinPnL.toFixed(2)}</td>
                    <td style={{ padding: '6px 8px', color: r.downWinPnL > 0 ? '#22c55e' : '#ef4444' }}>{r.downWinPnL >= 0 ? '+' : ''}{r.downWinPnL.toFixed(2)}</td>
                    <td style={{ padding: '6px 8px', color: minPnL > 0 ? '#22c55e' : '#ef4444', fontWeight: 700 }}>{minPnL >= 0 ? '+' : ''}{minPnL.toFixed(2)}</td>
                    <td style={{ padding: '6px 8px' }}>{r.guaranteed ? '🎯' : '—'}</td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr style={{ borderTop: '2px solid var(--border)' }}>
                <td colSpan={6} style={{ padding: '8px', fontWeight: 700, fontSize: 12 }}>Expected PnL (avg)</td>
                <td colSpan={2} style={{
                  padding: '8px', fontWeight: 700, fontSize: 14,
                  color: batchResults.reduce((s, r) => s + Math.min(r.upWinPnL, r.downWinPnL), 0) / batchResults.length > 0 ? '#22c55e' : '#ef4444'
                }}>
                  {(batchResults.reduce((s, r) => s + Math.min(r.upWinPnL, r.downWinPnL), 0) / batchResults.length).toFixed(2)}/window
                </td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>
    </div>
  );
}
