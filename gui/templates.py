CHART_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
<style>
html, body { margin:0; padding:0; background:#0b1220; color:#e5e7eb; overflow:hidden; font-family:Segoe UI, Arial, sans-serif; }
#chart { width:100vw; height:100vh; }
</style>
</head>
<body>
<div id="chart"></div>
<script>
let chart = null;
let candleSeries = null;
let ema20 = null;
let ema50 = null;
let ema200 = null;
let autoFollow = true;

function initChart() {
    chart = LightweightCharts.createChart(document.getElementById('chart'), {
        layout: { background: { color: '#0b1220' }, textColor: '#e5e7eb' },
        grid: {
            vertLines: { color: 'rgba(255,255,255,0.05)' },
            horzLines: { color: 'rgba(255,255,255,0.05)' }
        },
        rightPriceScale: { borderColor: 'rgba(255,255,255,0.15)' },
        timeScale: { borderColor: 'rgba(255,255,255,0.15)', timeVisible: true, secondsVisible: false },
        crosshair: { mode: 0 },
        handleScroll: true,
        handleScale: true
    });

    candleSeries = chart.addCandlestickSeries({
        upColor: '#16a34a',
        downColor: '#dc2626',
        borderVisible: false,
        wickUpColor: '#16a34a',
        wickDownColor: '#dc2626'
    });

    ema20 = chart.addLineSeries({ color: '#38bdf8', lineWidth: 2, priceLineVisible: false });
    ema50 = chart.addLineSeries({ color: '#f59e0b', lineWidth: 2, priceLineVisible: false });
    ema200 = chart.addLineSeries({ color: '#a78bfa', lineWidth: 2, priceLineVisible: false });
}

initChart();

window.chartApi = {
    setAutoFollow(v) { autoFollow = !!v; },
    setAll(candles, e20, e50, e200, markers) {
        if (!candleSeries) return;
        candleSeries.setData(candles || []);
        ema20.setData(e20 || []);
        ema50.setData(e50 || []);
        ema200.setData(e200 || []);
        candleSeries.setMarkers(markers || []);
        if (autoFollow && chart) chart.timeScale().scrollToRealTime();
    },
    updateOne(bar, e20v, e50v, e200v, markers) {
        if (!candleSeries) return;
        candleSeries.update(bar);
        if (e20v !== null) ema20.update({ time: bar.time, value: e20v });
        if (e50v !== null) ema50.update({ time: bar.time, value: e50v });
        if (e200v !== null) ema200.update({ time: bar.time, value: e200v });
        candleSeries.setMarkers(markers || []);
        if (autoFollow && chart) chart.timeScale().scrollToRealTime();
    }
};

window.addEventListener('resize', () => {
    if (!chart) return;
    chart.applyOptions({ width: window.innerWidth, height: window.innerHeight });
});
</script>
</body>
</html>
"""

HEATMAP_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<style>
html, body { margin:0; padding:0; background:#0b1220; color:#e5e7eb; font-family:Segoe UI, Arial, sans-serif; overflow:hidden; }
#wrap { width:100vw; height:100vh; display:flex; flex-direction:column; }
#head { padding:8px 12px; border-bottom:1px solid rgba(255,255,255,0.08); display:flex; gap:18px; font-size:13px; }
#heat { flex:1; width:100%; height:100%; display:block; }
.small { color:#9ca3af; }
</style>
</head>
<body>
<div id="wrap">
  <div id="head">
    <div>Order Book Heatmap</div>
    <div id="mid" class="small">Mid: --</div>
    <div id="imb" class="small">Imbalance: --</div>
    <div id="spr" class="small">Spread bps: --</div>
  </div>
  <canvas id="heat"></canvas>
</div>
<script>
const canvas = document.getElementById('heat');
const ctx = canvas.getContext('2d');
let state = { history: [], meta: {} };

function resize() {
  canvas.width = canvas.clientWidth * devicePixelRatio;
  canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  draw();
}
window.addEventListener('resize', resize);

function colorFor(v, maxv) {
  const x = maxv > 0 ? v / maxv : 0;
  const r = Math.min(255, Math.floor(30 + x * 220));
  const g = Math.min(255, Math.floor(15 + x * 100));
  const b = Math.min(255, Math.floor(35 + x * 35));
  return `rgb(${r},${g},${b})`;
}

function draw() {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#0b1220';
  ctx.fillRect(0, 0, w, h);

  const history = state.history || [];
  if (!history.length) {
    ctx.fillStyle = '#9ca3af';
    ctx.font = '16px Segoe UI';
    ctx.fillText('Waiting for depth data...', 20, 40);
    return;
  }

  const pricesSet = new Set();
  history.forEach(snap => {
    (snap.levels || []).forEach(x => pricesSet.add(Number(x.price)));
  });
  const prices = Array.from(pricesSet).sort((a, b) => b - a);
  if (!prices.length) return;

  const rows = prices.length;
  const cols = history.length;
  const cellW = Math.max(2, w / Math.max(1, cols));
  const cellH = Math.max(2, h / Math.max(1, rows));
  const priceToIndex = {};
  prices.forEach((p, i) => { priceToIndex[p] = i; });

  let maxHeat = 1;
  history.forEach(snap => {
    (snap.levels || []).forEach(lvl => {
      if (lvl.heat > maxHeat) maxHeat = lvl.heat;
    });
  });

  history.forEach((snap, c) => {
    (snap.levels || []).forEach(lvl => {
      const idx = priceToIndex[Number(lvl.price)];
      if (idx === undefined) return;
      ctx.fillStyle = colorFor(lvl.heat, maxHeat);
      ctx.fillRect(c * cellW, idx * cellH, cellW + 0.5, cellH + 0.5);
    });
  });

  const last = history[history.length - 1];
  if (last && last.mid_price != null) {
    const nearest = prices.reduce((a, b) =>
      Math.abs(b - last.mid_price) < Math.abs(a - last.mid_price) ? b : a
    );
    const idx = priceToIndex[nearest];
    const y = idx * cellH + cellH / 2;
    ctx.strokeStyle = '#22c55e';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }

  ctx.fillStyle = '#e5e7eb';
  ctx.font = '11px Segoe UI';
  const step = Math.max(1, Math.floor(rows / 12));
  for (let i = 0; i < rows; i += step) {
    ctx.fillText(prices[i].toFixed(2), 6, i * cellH + 10);
  }
}

window.heatmapApi = {
  update(payload) {
    state = payload || { history: [], meta: {} };
    document.getElementById('mid').textContent = 'Mid: ' + (state.meta.mid_price ?? '--');
    document.getElementById('imb').textContent = 'Imbalance: ' + (state.meta.book_imbalance ?? '--');
    document.getElementById('spr').textContent = 'Spread bps: ' + (state.meta.spread_bps ?? '--');
    draw();
  }
};

resize();
</script>
</body>
</html>
"""

PROFILE_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<style>
html, body { margin:0; padding:0; background:#0b1220; color:#e5e7eb; font-family:Segoe UI, Arial, sans-serif; overflow:hidden; }
#wrap { width:100vw; height:100vh; display:flex; flex-direction:column; }
#head { padding:8px 12px; border-bottom:1px solid rgba(255,255,255,0.08); font-size:13px; }
#c { flex:1; width:100%; height:100%; display:block; }
</style>
</head>
<body>
<div id="wrap">
  <div id="head">VRVP / Session Volume Profile</div>
  <canvas id="c"></canvas>
</div>
<script>
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
let state = { vrvp: null, session: null, last_price: null };

function resize() {
  canvas.width = canvas.clientWidth * devicePixelRatio;
  canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  draw();
}
window.addEventListener('resize', resize);

function drawProfile(profile, x0, width, lineColor, barColor, title) {
  if (!profile || !profile.prices || !profile.prices.length) return;
  const h = canvas.clientHeight;
  const topPad = 30;
  const bottomPad = 20;
  const workH = h - topPad - bottomPad;
  const prices = profile.prices;
  const vols = profile.volumes;
  const minP = Math.min(...prices);
  const maxP = Math.max(...prices);
  const maxV = Math.max(...vols, 1);

  function py(p) {
    return topPad + (maxP - p) / Math.max(1e-9, maxP - minP) * workH;
  }

  ctx.fillStyle = '#e5e7eb';
  ctx.font = '12px Segoe UI';
  ctx.fillText(title, x0 + 10, 18);

  for (let i = 0; i < prices.length; i++) {
    const y = py(prices[i]);
    const bw = (vols[i] / maxV) * (width - 50);
    ctx.fillStyle = barColor;
    ctx.fillRect(x0 + 40, y - 2, bw, 4);
  }

  [['POC', profile.poc], ['VAH', profile.vah], ['VAL', profile.val]].forEach(([name, val]) => {
    if (val == null) return;
    const y = py(val);
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x0 + 35, y);
    ctx.lineTo(x0 + width, y);
    ctx.stroke();
    ctx.fillStyle = lineColor;
    ctx.fillText(name + ' ' + Number(val).toFixed(2), x0 + 4, y - 4);
  });
}

function drawLastPrice(p, minP, maxP, fullW) {
  if (p == null) return;
  const h = canvas.clientHeight;
  const topPad = 30;
  const bottomPad = 20;
  const workH = h - topPad - bottomPad;
  const y = topPad + (maxP - p) / Math.max(1e-9, maxP - minP) * workH;
  ctx.strokeStyle = '#22c55e';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, y);
  ctx.lineTo(fullW, y);
  ctx.stroke();
}

function draw() {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#0b1220';
  ctx.fillRect(0, 0, w, h);

  if (!state.vrvp && !state.session) {
    ctx.fillStyle = '#9ca3af';
    ctx.font = '16px Segoe UI';
    ctx.fillText('Waiting for profile data...', 20, 40);
    return;
  }

  const allPrices = [];
  if (state.vrvp?.prices) allPrices.push(...state.vrvp.prices);
  if (state.session?.prices) allPrices.push(...state.session.prices);
  const minP = Math.min(...allPrices);
  const maxP = Math.max(...allPrices);

  drawProfile(state.vrvp, 0, w / 2, '#f59e0b', 'rgba(245,158,11,0.65)', 'VRVP');
  drawProfile(state.session, w / 2, w / 2, '#38bdf8', 'rgba(56,189,248,0.65)', 'Session');
  drawLastPrice(state.last_price, minP, maxP, w);
}

window.profileApi = {
  update(payload) {
    state = payload || { vrvp: null, session: null, last_price: null };
    draw();
  }
};

resize();
</script>
</body>
</html>
"""

FOOTPRINT_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<style>
html, body { margin:0; padding:0; background:#0b1220; color:#e5e7eb; font-family:Segoe UI, Arial, sans-serif; overflow:hidden; }
#wrap { width:100vw; height:100vh; display:flex; flex-direction:column; }
#head { padding:8px 12px; border-bottom:1px solid rgba(255,255,255,0.08); font-size:13px; }
#c { flex:1; width:100%; height:100%; display:block; }
</style>
</head>
<body>
<div id="wrap">
  <div id="head">Agg Trade Footprint</div>
  <canvas id="c"></canvas>
</div>
<script>
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
let state = { levels: [], buy_vol: [], sell_vol: [], delta: [], total_buy: 0, total_sell: 0, delta_total: 0 };

function resize() {
  canvas.width = canvas.clientWidth * devicePixelRatio;
  canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  draw();
}
window.addEventListener('resize', resize);

function draw() {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#0b1220';
  ctx.fillRect(0, 0, w, h);

  if (!state.levels || !state.levels.length) {
    ctx.fillStyle = '#9ca3af';
    ctx.font = '16px Segoe UI';
    ctx.fillText('Waiting for agg-trade footprint...', 20, 40);
    return;
  }

  const levels = state.levels;
  const buys = state.buy_vol;
  const sells = state.sell_vol;
  const maxV = Math.max(...buys, ...sells, 1);
  const rowH = Math.max(14, h / Math.min(levels.length, 28));
  const visible = Math.min(levels.length, Math.floor((h - 24) / rowH));
  const start = Math.max(0, levels.length - visible);

  ctx.fillStyle = '#e5e7eb';
  ctx.font = '12px Segoe UI';
  ctx.fillText(`Buy ${Number(state.total_buy).toFixed(2)} | Sell ${Number(state.total_sell).toFixed(2)} | Delta ${Number(state.delta_total).toFixed(2)}`, 10, 16);

  for (let i = start; i < levels.length; i++) {
    const y = 26 + (i - start) * rowH;
    const bw = (buys[i] / maxV) * (w * 0.35);
    const sw = (sells[i] / maxV) * (w * 0.35);

    ctx.fillStyle = 'rgba(34,197,94,0.75)';
    ctx.fillRect(w * 0.5, y, bw, rowH - 2);

    ctx.fillStyle = 'rgba(239,68,68,0.75)';
    ctx.fillRect(w * 0.5 - sw, y, sw, rowH - 2);

    ctx.fillStyle = '#cbd5e1';
    ctx.font = '11px Consolas';
    ctx.fillText(Number(levels[i]).toFixed(2), w * 0.5 - 35, y + rowH - 4);
    ctx.fillText(Number(buys[i]).toFixed(2), w * 0.5 + bw + 4, y + rowH - 4);
    ctx.fillText(Number(sells[i]).toFixed(2), Math.max(2, w * 0.5 - sw - 60), y + rowH - 4);
  }

  ctx.strokeStyle = 'rgba(255,255,255,0.15)';
  ctx.beginPath();
  ctx.moveTo(w * 0.5, 24);
  ctx.lineTo(w * 0.5, h);
  ctx.stroke();
}

window.footprintApi = {
  update(payload) {
    state = payload || { levels: [], buy_vol: [], sell_vol: [], delta: [] };
    draw();
  }
};

resize();
</script>
</body>
</html>
"""