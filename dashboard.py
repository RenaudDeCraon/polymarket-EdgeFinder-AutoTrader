"""
BTC Predictor Dashboard v2
==========================
Minimalistic dark dashboard with scrolling chart, market info on trades.
"""

import json
import time
import threading
from datetime import datetime, timezone
from flask import Flask, render_template_string, jsonify


DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BTC Predictor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
:root {
    --bg: #09090b;
    --card: #111113;
    --border: #1e1e22;
    --text: #d4d4d8;
    --text2: #71717a;
    --green: #22c55e;
    --red: #ef4444;
    --orange: #f7931a;
    --blue: #3b82f6;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace; background:var(--bg); color:var(--text); font-size:13px; }

/* Header */
.hdr { padding:12px 20px; display:flex; align-items:center; justify-content:space-between; border-bottom:1px solid var(--border); }
.hdr h1 { font-size:15px; font-weight:600; letter-spacing:0.5px; }
.hdr h1 span { color:var(--orange); }
.hdr-right { display:flex; gap:14px; align-items:center; color:var(--text2); font-size:11px; }
.dot { width:6px; height:6px; border-radius:50%; display:inline-block; margin-right:3px; }
.dot.on { background:var(--green); }
.dot.off { background:var(--red); }
.badge { padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; letter-spacing:0.5px; }
.badge-paper { background:#27272a; color:var(--text2); }
.badge-live { background:rgba(249,115,22,0.15); color:var(--orange); }

/* Layout */
.wrap { max-width:1200px; margin:0 auto; padding:16px; }
.row { display:grid; gap:12px; margin-bottom:12px; }
.r2 { grid-template-columns:1fr 1fr; }
.r3 { grid-template-columns:1fr 1fr 1fr; }
.full { grid-template-columns:1fr; }

/* Cards */
.c { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:16px; }
.c h2 { font-size:10px; text-transform:uppercase; letter-spacing:1.5px; color:var(--text2); margin-bottom:10px; font-weight:500; }

/* Price */
.price-big { font-size:32px; font-weight:700; color:#fff; letter-spacing:-1px; }
.price-change { font-size:12px; margin-top:2px; }
.price-change.up { color:var(--green); }
.price-change.down { color:var(--red); }

/* Predictions */
.pred-row { display:flex; gap:10px; margin-top:10px; }
.pred-item { flex:1; padding:12px; border-radius:6px; text-align:center; }
.pred-item.up { background:rgba(34,197,94,0.06); border:1px solid rgba(34,197,94,0.15); }
.pred-item.down { background:rgba(239,68,68,0.06); border:1px solid rgba(239,68,68,0.15); }
.pred-item.none { background:rgba(113,113,122,0.06); border:1px solid rgba(113,113,122,0.15); }
.pred-label { font-size:10px; color:var(--text2); letter-spacing:1px; }
.pred-dir { font-size:20px; font-weight:700; margin:4px 0 2px; }
.pred-item.up .pred-dir { color:var(--green); }
.pred-item.down .pred-dir { color:var(--red); }
.pred-item.none .pred-dir { color:var(--text2); }
.pred-conf { font-size:11px; color:var(--text2); }

/* Metrics */
.metrics { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }
.m { text-align:center; padding:10px 6px; background:var(--bg); border-radius:6px; }
.m-val { font-size:18px; font-weight:700; }
.m-val.pos { color:var(--green); }
.m-val.neg { color:var(--red); }
.m-lbl { font-size:9px; color:var(--text2); text-transform:uppercase; letter-spacing:1px; margin-top:2px; }

/* Chart */
.chart-wrap { position:relative; height:280px; }

/* Tables */
table { width:100%; border-collapse:collapse; font-size:12px; }
th { text-align:left; padding:6px 8px; color:var(--text2); font-size:10px; text-transform:uppercase; letter-spacing:0.5px; border-bottom:1px solid var(--border); font-weight:500; }
td { padding:6px 8px; border-bottom:1px solid #18181b; }
tr:hover { background:rgba(255,255,255,0.02); }
.t-up { color:var(--green); }
.t-down { color:var(--red); }
.t-notrade { color:var(--text2); }
.pill { display:inline-block; padding:1px 6px; border-radius:3px; font-size:10px; font-weight:600; }
.pill-win { background:rgba(34,197,94,0.1); color:var(--green); }
.pill-loss { background:rgba(239,68,68,0.1); color:var(--red); }
.pill-pending { background:rgba(234,179,8,0.1); color:#eab308; }
.pill-up { background:rgba(34,197,94,0.08); color:var(--green); }
.pill-down { background:rgba(239,68,68,0.08); color:var(--red); }
.scroll-y { max-height:280px; overflow-y:auto; }
.scroll-y::-webkit-scrollbar { width:4px; }
.scroll-y::-webkit-scrollbar-track { background:transparent; }
.scroll-y::-webkit-scrollbar-thumb { background:var(--border); border-radius:2px; }

/* Poly prices */
.poly-row { display:flex; gap:10px; margin-top:8px; }
.poly-item { flex:1; padding:8px; background:var(--bg); border-radius:6px; text-align:center; }
.poly-item .lbl { font-size:9px; color:var(--text2); text-transform:uppercase; letter-spacing:1px; }
.poly-item .val { font-size:16px; font-weight:700; margin-top:2px; }

/* Features grid */
.feat-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:4px; font-size:11px; }
.feat { display:flex; justify-content:space-between; padding:3px 6px; background:var(--bg); border-radius:3px; }
.feat-n { color:var(--text2); }
.feat-v { font-weight:600; color:var(--text); }

/* Timeframe buttons */
.tf-btns { display:flex; gap:2px; }
.tf { background:var(--bg); border:1px solid var(--border); color:var(--text2); padding:3px 10px; border-radius:4px; font-size:10px; font-family:inherit; cursor:pointer; letter-spacing:0.5px; }
.tf:hover { border-color:#3f3f46; color:var(--text); }
.tf.active { background:#f7931a; border-color:#f7931a; color:#000; font-weight:600; }
</style>
</head>
<body>

<div class="hdr">
    <h1><span>₿</span> BTC Predictor</h1>
    <div class="hdr-right">
        <span><span class="dot" id="wsDot"></span><span id="wsStatus">...</span></span>
        <span id="clock"></span>
        <span id="modeTag"></span>
    </div>
</div>

<div class="wrap">
    <!-- Row 1: Price + Predictions | Metrics -->
    <div class="row r2">
        <div class="c">
            <h2>BTC / USDT</h2>
            <div class="price-big" id="price">—</div>
            <div class="price-change" id="priceChange"></div>
            <div class="pred-row">
                <div class="pred-item none" id="p5">
                    <div class="pred-label">5 MIN → <span id="p5t" style="color:var(--text)"></span></div>
                    <div class="pred-dir" id="p5d">—</div>
                    <div class="pred-conf" id="p5c"></div>
                </div>
                <div class="pred-item none" id="p15">
                    <div class="pred-label">15 MIN → <span id="p15t" style="color:var(--text)"></span></div>
                    <div class="pred-dir" id="p15d">—</div>
                    <div class="pred-conf" id="p15c"></div>
                </div>
            </div>
            <div class="poly-row">
                <div class="poly-item"><div class="lbl">Target</div><div class="val" id="mTarget">—</div></div>
                <div class="poly-item"><div class="lbl">Poly BTC</div><div class="val" id="polyBtc">—</div></div>
                <div class="poly-item"><div class="lbl">Gap</div><div class="val" id="priceGap">—</div></div>
                <div class="poly-item"><div class="lbl">Ends</div><div class="val" id="mEnds">—</div></div>
                <div class="poly-item"><div class="lbl">Poly UP</div><div class="val t-up" id="polyUp">—</div></div>
                <div class="poly-item"><div class="lbl">Poly DOWN</div><div class="val t-down" id="polyDn">—</div></div>
            </div>
        </div>
        <div class="c">
            <h2>Performance</h2>
            <div class="metrics">
                <div class="m"><div class="m-val" id="pnl">$0</div><div class="m-lbl">P&L</div></div>
                <div class="m"><div class="m-val" id="wr">0%</div><div class="m-lbl">Win Rate</div></div>
                <div class="m"><div class="m-val" id="tc">0</div><div class="m-lbl">Trades</div></div>
                <div class="m"><div class="m-val" id="cash">$0</div><div class="m-lbl">Cash</div></div>
            </div>
            <div class="metrics" style="margin-top:8px;">
                <div class="m"><div class="m-val" id="wins">0</div><div class="m-lbl">Wins</div></div>
                <div class="m"><div class="m-val" id="losses">0</div><div class="m-lbl">Losses</div></div>
                <div class="m"><div class="m-val" id="a5">—</div><div class="m-lbl">5m Acc</div></div>
                <div class="m"><div class="m-val" id="a15">—</div><div class="m-lbl">15m Acc</div></div>
            </div>
            <div class="metrics" style="margin-top:8px;">
                <div class="m"><div class="m-val" id="trainAcc5">—</div><div class="m-lbl">Train 5m</div></div>
                <div class="m"><div class="m-val" id="trainAcc15">—</div><div class="m-lbl">Train 15m</div></div>
                <div class="m"><div class="m-val" id="nFeatures">—</div><div class="m-lbl">Features</div></div>
                <div class="m"><div class="m-val" id="nPreds">0</div><div class="m-lbl">Preds</div></div>
            </div>
        </div>
    </div>

    <!-- Row 2: Chart -->
    <div class="row full">
        <div class="c">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
                <h2 style="margin:0">Price</h2>
                <div class="tf-btns">
                    <button class="tf active" data-tf="120" onclick="setTF(120)">2m</button>
                    <button class="tf" data-tf="300" onclick="setTF(300)">5m</button>
                    <button class="tf" data-tf="600" onclick="setTF(600)">10m</button>
                    <button class="tf" data-tf="1800" onclick="setTF(1800)">30m</button>
                    <button class="tf" data-tf="3600" onclick="setTF(3600)">1h</button>
                </div>
                <div style="display:flex;gap:6px;align-items:center;">
                    <label style="font-size:10px;color:var(--text2);cursor:pointer;">
                        <input type="checkbox" id="padToggle" checked onchange="togglePad()" style="margin-right:3px;accent-color:#f7931a;">Smooth
                    </label>
                    <span id="chartRange" style="font-size:10px;color:var(--text2);"></span>
                </div>
            </div>
            <div class="chart-wrap"><canvas id="chart"></canvas></div>
        </div>
    </div>

    <!-- Row 3: Trades | Predictions -->
    <div class="row r2">
        <div class="c">
            <h2>Trades</h2>
            <div class="scroll-y">
                <table><thead><tr>
                    <th>Time</th><th>Market</th><th>Side</th><th>Paid</th><th>Cost</th><th>BTC</th><th>Target</th><th>Conf</th><th>Result</th><th>P&L</th>
                </tr></thead><tbody id="tBody"></tbody></table>
            </div>
        </div>
        <div class="c">
            <h2>Prediction Log</h2>
            <div class="scroll-y">
                <table><thead><tr>
                    <th>Time</th><th>Price</th><th>5m</th><th>Conf</th><th>Target</th><th>15m</th><th>Conf</th><th>Target</th>
                </tr></thead><tbody id="pBody"></tbody></table>
            </div>
        </div>
    </div>

    <!-- Row 4: Features -->
    <div class="row full">
        <div class="c">
            <h2>Live Features</h2>
            <div class="feat-grid" id="fGrid"></div>
        </div>
    </div>
</div>

<script>
let chart;
let chartData = [];
let predUpData = [];
let predDnData = [];
let currentTF = 600; // default 10 min
let usePadding = true;
let allPriceHistory = [];
let allPredHistory = [];
let allMarkets = [];

// Vertical line plugin for market windows
const windowLinePlugin = {
    id: 'windowLines',
    afterDraw(chart) {
        const xScale = chart.scales.x;
        const ctx = chart.ctx;
        const now = Date.now();
        // Draw 5-min window boundaries
        const windowMs = 5 * 60 * 1000;
        const startWindow = Math.floor(now / windowMs) * windowMs;
        // Draw a few past and the next boundary
        for (let i = -6; i <= 2; i++) {
            const t = startWindow + i * windowMs;
            const x = xScale.getPixelForValue(t);
            if (x < xScale.left || x > xScale.right) continue;
            ctx.save();
            ctx.strokeStyle = i === 0 ? '#3b82f6' : '#27272a';
            ctx.lineWidth = i === 0 ? 1.5 : 0.5;
            ctx.setLineDash(i === 0 ? [] : [3,3]);
            ctx.beginPath();
            ctx.moveTo(x, chart.chartArea.top);
            ctx.lineTo(x, chart.chartArea.bottom);
            ctx.stroke();
            // Label at top
            if (i >= -2) {
                const d = new Date(t);
                const lbl = d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
                ctx.fillStyle = i === 0 ? '#3b82f6' : '#52525b';
                ctx.font = '9px monospace';
                ctx.textAlign = 'center';
                ctx.fillText(lbl, x, chart.chartArea.top - 3);
            }
            ctx.restore();
        }
    }
};

const targetLinePlugin = {
    id: 'targetLine',
    afterDraw(chart) {
        if (!allMarkets.length) return;
        const market = allMarkets[0];
        if (!market || !market.target_price) return;
        const y = chart.scales.y.getPixelForValue(market.target_price);
        if (y < chart.chartArea.top || y > chart.chartArea.bottom) return;
        const inferred = market.target_source !== 'polymarket';
        const color = inferred ? '#eab308' : '#3b82f6';
        const label = (inferred ? 'est target $' : 'poly target $')
            + market.target_price.toLocaleString(undefined,{maximumFractionDigits:2});
        const ctx = chart.ctx;
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.setLineDash(inferred ? [8, 5] : [5, 4]);
        ctx.beginPath();
        ctx.moveTo(chart.chartArea.left, y);
        ctx.lineTo(chart.chartArea.right, y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = color;
        ctx.font = '10px monospace';
        ctx.textAlign = 'right';
        ctx.fillText(label, chart.chartArea.right - 4, y - 5);
        ctx.restore();
    }
};

function initChart() {
    const ctx = document.getElementById('chart').getContext('2d');
    chart = new Chart(ctx, {
        type: 'line',
        data: { datasets: [
            { label:'Price', data:chartData, borderColor:'#f7931a', borderWidth:1.5, pointRadius:0, fill:false, tension:0.1, order:2 },
            { label:'▲ UP', data:predUpData, borderColor:'#22c55e', backgroundColor:'#22c55e', pointRadius:7, pointStyle:'triangle', showLine:false, order:1 },
            { label:'▼ DN', data:predDnData, borderColor:'#ef4444', backgroundColor:'#ef4444', pointRadius:7, pointStyle:'triangle', pointRotation:180, showLine:false, order:1 },
        ]},
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            layout: { padding: { top: 14 } },
            scales: {
                x: {
                    type: 'time',
                    time: { unit:'minute', displayFormats:{minute:'HH:mm',second:'HH:mm:ss'}},
                    grid: { color:'#1a1a1e' },
                    ticks: { color:'#52525b', font:{size:10,family:'monospace'}, maxTicksLimit:10 },
                    border: { color:'#1e1e22' },
                },
                y: {
                    grid: { color:'#1a1a1e' },
                    ticks: { color:'#52525b', font:{size:10,family:'monospace'}, maxTicksLimit:8,
                        callback: v => '$'+v.toLocaleString(undefined,{minimumFractionDigits:0}) },
                    border: { color:'#1e1e22' },
                }
            },
            plugins: {
                legend: { display:false },
                tooltip: {
                    backgroundColor:'#18181b', titleColor:'#d4d4d8', bodyColor:'#d4d4d8',
                    borderColor:'#27272a', borderWidth:1, titleFont:{family:'monospace',size:11},
                    bodyFont:{family:'monospace',size:11},
                    callbacks: { label: ctx => {
                        if (ctx.datasetIndex === 0) return '$'+ctx.parsed.y.toLocaleString(undefined,{minimumFractionDigits:2});
                        return ctx.dataset.label + ' prediction';
                    }}
                }
            }
        },
        plugins: [windowLinePlugin, targetLinePlugin]
    });
}

function setTF(seconds) {
    currentTF = seconds;
    document.querySelectorAll('.tf').forEach(b => b.classList.toggle('active', parseInt(b.dataset.tf) === seconds));
    refreshChart();
}

function togglePad() {
    usePadding = document.getElementById('padToggle').checked;
    refreshChart();
}

function refreshChart() {
    if (!allPriceHistory.length) return;
    const now = Date.now() / 1000;
    const cutoff = now - currentTF;

    // Filter price data
    chartData.length = 0;
    const filtered = allPriceHistory.filter(p => p[0] >= cutoff);
    // Sample to avoid too many points: keep ~500 max
    const step = Math.max(1, Math.floor(filtered.length / 500));
    filtered.forEach((p,i) => { if (i % step === 0 || i === filtered.length-1) chartData.push({x:new Date(p[0]*1000), y:p[1]}); });

    // Filter predictions
    predUpData.length = 0;
    predDnData.length = 0;
    allPredHistory.filter(p => p.timestamp >= cutoff).forEach(p => {
        const point = {x:new Date(p.timestamp*1000), y:p.current_price};
        if (p.direction_5m === 'UP') predUpData.push(point);
        else if (p.direction_5m === 'DOWN') predDnData.push(point);
    });

    // Y-axis padding
    if (usePadding && chartData.length > 1) {
        const prices = chartData.map(d => d.y);
        const min = Math.min(...prices);
        const max = Math.max(...prices);
        const range = max - min;
        const pad = Math.max(range * 1.5, 15); // at least $15 range
        const mid = (min + max) / 2;
        chart.options.scales.y.min = Math.floor(mid - pad / 2);
        chart.options.scales.y.max = Math.ceil(mid + pad / 2);
    } else {
        delete chart.options.scales.y.min;
        delete chart.options.scales.y.max;
    }

    // Range label
    if (chartData.length > 1) {
        const first = chartData[0].y, last = chartData[chartData.length-1].y;
        const diff = last - first;
        const pct = (diff/first*100).toFixed(3);
        const el = document.getElementById('chartRange');
        el.textContent = (diff>=0?'+':'')+'$'+diff.toFixed(2)+' ('+pct+'%)';
        el.style.color = diff >= 0 ? '#22c55e' : '#ef4444';

        const pcEl = document.getElementById('priceChange');
        pcEl.textContent = (diff>=0?'+':'')+'$'+diff.toFixed(2)+' ('+pct+'%)';
        pcEl.className = 'price-change '+(diff>=0?'up':'down');
    }

    chart.update('none');
}

function windowLabel(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const h = d.getUTCHours().toString().padStart(2,'0');
    const m = d.getUTCMinutes().toString().padStart(2,'0');
    const end = new Date((ts + 300) * 1000);
    const eh = end.getUTCHours().toString().padStart(2,'0');
    const em = end.getUTCMinutes().toString().padStart(2,'0');
    return h+':'+m+'–'+eh+':'+em;
}

function update() {
    fetch('/api/state').then(r=>r.json()).then(d => {
        // Connection
        document.getElementById('wsDot').className = 'dot '+(d.connected?'on':'off');
        document.getElementById('wsStatus').textContent = d.connected?'Live':'Off';
        document.getElementById('modeTag').innerHTML = d.paper_mode
            ? '<span class="badge badge-paper">PAPER</span>'
            : '<span class="badge badge-live">LIVE</span>';
        document.getElementById('clock').textContent = new Date().toLocaleTimeString();

        // Price
        if (d.current_price) {
            document.getElementById('price').textContent = '$'+d.current_price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
        }

        // Predictions
        if (d.prediction) {
            const p = d.prediction;
            const currentMarket = (d.markets || [])[0];
            const nextMarket = (d.markets || [])[1];
            setPred('p5','p5d','p5c', p.direction_5m, p.confidence_5m);
            setPred('p15','p15d','p15c', p.direction_15m, p.confidence_15m);
            document.getElementById('p5t').textContent = currentMarket
                ? new Date(currentMarket.window_end*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})
                : '—';
            document.getElementById('p15t').textContent = nextMarket
                ? new Date(nextMarket.window_end*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})
                : '—';
            document.getElementById('polyUp').textContent = currentMarket && currentMarket.up_price ? (currentMarket.up_price*100).toFixed(0)+'¢' : '—';
            document.getElementById('polyDn').textContent = currentMarket && currentMarket.down_price ? (currentMarket.down_price*100).toFixed(0)+'¢' : '—';
            document.getElementById('mTarget').innerHTML = currentMarket && currentMarket.target_price
                ? '$'+currentMarket.target_price.toLocaleString(undefined,{maximumFractionDigits:2})+' '+sourceTag(currentMarket.target_source)
                : '—';
            document.getElementById('polyBtc').textContent = currentMarket && currentMarket.polymarket_current_price
                ? '$'+currentMarket.polymarket_current_price.toLocaleString(undefined,{maximumFractionDigits:2})
                : '—';
            const gapEl = document.getElementById('priceGap');
            if (currentMarket && currentMarket.binance_price_gap !== null && currentMarket.binance_price_gap !== undefined) {
                gapEl.textContent = (currentMarket.binance_price_gap >= 0 ? '+' : '') + '$' + currentMarket.binance_price_gap.toFixed(2);
                gapEl.className = 'val ' + (currentMarket.binance_price_gap >= 0 ? 't-up' : 't-down');
            } else {
                gapEl.textContent = '—';
                gapEl.className = 'val';
            }
            document.getElementById('mEnds').textContent = currentMarket
                ? new Date(currentMarket.window_end*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})
                : '—';
        }

        // Stats
        const s = d.stats||{};
        const pnlEl = document.getElementById('pnl');
        pnlEl.textContent = '$'+(s.total_pnl||0).toFixed(2);
        pnlEl.className = 'm-val '+((s.total_pnl||0)>=0?'pos':'neg');
        document.getElementById('wr').textContent = ((s.win_rate||0)*100).toFixed(0)+'%';
        document.getElementById('tc').textContent = s.total_trades||0;
        document.getElementById('cash').textContent = '$'+(s.cash||0).toFixed(2);
        document.getElementById('wins').textContent = s.wins||0;
        document.getElementById('losses').textContent = s.losses||0;
        const acc = d.accuracy||{};
        document.getElementById('a5').textContent = acc['5m']?(acc['5m']*100).toFixed(0)+'%':'—';
        document.getElementById('a15').textContent = acc['15m']?(acc['15m']*100).toFixed(0)+'%':'—';
        document.getElementById('trainAcc5').textContent = d.training_accuracy_5m?(d.training_accuracy_5m*100).toFixed(1)+'%':'—';
        document.getElementById('trainAcc15').textContent = d.training_accuracy_15m?(d.training_accuracy_15m*100).toFixed(1)+'%':'—';
        document.getElementById('nFeatures').textContent = d.features?Object.keys(d.features).length:'—';
        document.getElementById('nPreds').textContent = (d.prediction_history||[]).length;

        // Store data and refresh chart
        if (d.price_history && d.price_history.length) allPriceHistory = d.price_history;
        if (d.prediction_history) allPredHistory = d.prediction_history;
        if (d.markets) allMarkets = d.markets;
        refreshChart();

        // Trades
        const tb = document.getElementById('tBody');
        tb.innerHTML = '';
        (d.trades||[]).slice(-15).reverse().forEach(t => {
            const r = document.createElement('tr');
            const side = '<span class="pill pill-'+t.side.toLowerCase()+'">'+t.side+'</span>';
            let res = '<span class="pill pill-pending">OPEN</span>';
            if (t.outcome==='WIN') res='<span class="pill pill-win">WIN</span>';
            if (t.outcome==='LOSS') res='<span class="pill pill-loss">LOSS</span>';
            if (!t.outcome && Date.now()/1000 > t.window_ts + 300) {
                res = '<span class="pill pill-pending">SETTLING</span>';
            }
            const pnl = t.pnl!==null?(t.pnl>=0?'+':'')+'$'+t.pnl.toFixed(2):'—';
            const pc = t.pnl!==null?(t.pnl>=0?'t-up':'t-down'):'';
            const paid = (t.price*100).toFixed(1)+'¢';
            const btc = t.entry_btc_price ? '$'+t.entry_btc_price.toLocaleString(undefined,{maximumFractionDigits:2}) : '—';
            const target = t.target_price ? '$'+t.target_price.toLocaleString(undefined,{maximumFractionDigits:2}) : '—';
            r.innerHTML = '<td>'+new Date(t.timestamp*1000).toLocaleTimeString()+'</td>'
                +'<td style="font-size:10px;color:var(--text2)">'+windowLabel(t.window_ts)+'</td>'
                +'<td>'+side+'</td><td>'+paid+'</td><td>$'+t.cost.toFixed(2)+'</td>'
                +'<td style="font-size:10px;color:var(--text2)">'+btc+'</td>'
                +'<td style="font-size:10px;color:var(--text2)">'+target+' '+sourceTag(t.target_source)+'</td>'
                +'<td>'+(t.prediction_confidence*100).toFixed(0)+'%</td>'
                +'<td>'+res+'</td><td class="'+pc+'">'+pnl+'</td>';
            tb.appendChild(r);
        });

        // Prediction log
        const pb = document.getElementById('pBody');
        pb.innerHTML = '';
        (d.prediction_history||[]).slice(-15).reverse().forEach(p => {
            const r = document.createElement('tr');
            const tgt5 = p.seconds_left !== null && p.seconds_left !== undefined
                ? new Date((p.timestamp + p.seconds_left)*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})
                : '—';
            const tgt15 = new Date(p.timestamp*1000+15*60000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
            r.innerHTML = '<td>'+new Date(p.timestamp*1000).toLocaleTimeString()+'</td>'
                +'<td>$'+p.current_price.toLocaleString()+'</td>'
                +'<td>'+dirCell(p.direction_5m)+'</td>'
                +'<td>'+(p.confidence_5m*100).toFixed(0)+'%</td>'
                +'<td style="color:var(--text2)">'+tgt5+'</td>'
                +'<td>'+dirCell(p.direction_15m)+'</td>'
                +'<td>'+(p.confidence_15m*100).toFixed(0)+'%</td>'
                +'<td style="color:var(--text2)">'+tgt15+'</td>';
            pb.appendChild(r);
        });

        // Features
        if (d.features) {
            const fg = document.getElementById('fGrid');
            fg.innerHTML = '';
            Object.entries(d.features).sort((a,b)=>Math.abs(b[1])-Math.abs(a[1])).forEach(([k,v]) => {
                const div = document.createElement('div');
                div.className = 'feat';
                div.innerHTML = '<span class="feat-n">'+k+'</span><span class="feat-v">'+(typeof v==='number'?v.toFixed(3):v)+'</span>';
                fg.appendChild(div);
            });
        }
    }).catch(()=>{
        document.getElementById('wsDot').className='dot off';
        document.getElementById('wsStatus').textContent='Err';
    });
}

function setPred(boxId,dirId,confId,dir,conf,note) {
    const box=document.getElementById(boxId), de=document.getElementById(dirId), ce=document.getElementById(confId);
    if(dir==='UP'){box.className='pred-item up';de.textContent='▲ UP';}
    else if(dir==='DOWN'){box.className='pred-item down';de.textContent='▼ DOWN';}
    else if(dir==='WAIT'){box.className='pred-item none';de.textContent='— WAIT';}
    else{box.className='pred-item none';de.textContent='— SKIP';}
    ce.textContent=note || ((conf*100).toFixed(1)+'%');
}

function dirCell(dir) {
    if(dir==='UP') return '<span class="t-up">▲ UP</span>';
    if(dir==='DOWN') return '<span class="t-down">▼ DOWN</span>';
    return '<span class="t-notrade">— SKIP</span>';
}

function sourceTag(source) {
    if (source === 'polymarket') return '<span style="color:#22c55e;font-size:9px;">POLY</span>';
    if (source === 'binance_inferred') return '<span style="color:#eab308;font-size:9px;">EST</span>';
    return '<span style="color:#71717a;font-size:9px;">UNK</span>';
}

initChart();
update();
setInterval(update, 2000);
</script>
</body>
</html>
"""


class NumpyEncoder(json.JSONEncoder):
    """JSON encoder that handles numpy types."""
    def default(self, obj):
        try:
            import numpy as np
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
        except ImportError:
            pass
        return super().default(obj)


def create_dashboard_app(get_state_fn):
    """Create Flask app."""
    app = Flask(__name__)
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

    @app.route('/')
    def index():
        return render_template_string(DASHBOARD_HTML)

    @app.route('/api/state')
    def api_state():
        state = get_state_fn()
        return app.response_class(
            json.dumps(state, cls=NumpyEncoder),
            mimetype='application/json',
        )

    return app


def run_dashboard(app, host='127.0.0.1', port=5050):
    """Run dashboard in a background thread."""
    thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    return thread
