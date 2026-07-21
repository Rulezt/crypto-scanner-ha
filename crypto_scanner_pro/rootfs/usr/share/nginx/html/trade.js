// Order Book Standalone Vue App
const { createApp, ref, computed, onMounted, onUnmounted, watch, nextTick } = Vue;

// ── Lightweight Charts ────────────────────────────────────────────────────────
const LC = window.LightweightCharts;

const DEFAULT_EMA_CFG = [
    { p: 5,   color: '#ef4444', style: 0, width: 2   },
    { p: 10,  color: '#fbbf24', style: 0, width: 2   },
    { p: 60,  color: '#3b82f6', style: 0, width: 3   },
    { p: 223, color: '#a855f7', style: 0, width: 2.5 },
];
const DEFAULT_LEVELS_CFG = {
    dayHigh:  { color: '#22c55e', style: 0, width: 2, vis:{chart:true, mtf:true, ob:true} },
    dayLow:   { color: '#ef4444', style: 0, width: 2, vis:{chart:true, mtf:true, ob:true} },
    prevHigh: { color: '#22c55e', style: 2, width: 2, vis:{chart:true, mtf:true, ob:true} },
    prevLow:  { color: '#ef4444', style: 2, width: 2, vis:{chart:true, mtf:true, ob:true} },
    obBid:    { color: '#3b82f6', style: 0, width: 1, vis:{chart:true, mtf:true, ob:true} },
    obAsk:    { color: '#3b82f6', style: 0, width: 1, vis:{chart:true, mtf:true, ob:true} },
    ath:      { color: '#f59e0b', style: 2, width: 1, vis:{chart:true, mtf:true, ob:false} },
    atl:      { color: '#a855f7', style: 2, width: 1, vis:{chart:true, mtf:true, ob:false} },
};
function getEmaCfg() { try{const s=JSON.parse(localStorage.getItem('chart_ema_cfg'));if(s&&s.length===4)return s;}catch(e){}return DEFAULT_EMA_CFG.map(x=>({...x})); }
function getLvCfg() {
    try {
        const s = JSON.parse(localStorage.getItem('chart_levels_cfg'));
        if (s) {
            if (!s._obBlueMigrated) {
                if (s.obBid) s.obBid.color = DEFAULT_LEVELS_CFG.obBid.color;
                if (s.obAsk) s.obAsk.color = DEFAULT_LEVELS_CFG.obAsk.color;
                s._obBlueMigrated = true;
                try { localStorage.setItem('chart_levels_cfg', JSON.stringify(s)); } catch(e) {}
            }
            const out = {};
            for (const k of Object.keys(DEFAULT_LEVELS_CFG)) {
                out[k] = s[k]
                    ? {...DEFAULT_LEVELS_CFG[k], ...s[k], vis:{...DEFAULT_LEVELS_CFG[k].vis, ...(s[k].vis||{})}}
                    : {...DEFAULT_LEVELS_CFG[k]};
            }
            return out;
        }
    } catch(e) {}
    return Object.fromEntries(Object.entries(DEFAULT_LEVELS_CFG).map(([k,v])=>[k,{...v,vis:{...v.vis}}]));
}
const EMA_CFG = getEmaCfg();

const DEFAULT_BB_CFG = { enabled: false, period: 20, mult: 2, color: '#60a5fa', width: 1, style: 0 };
function getBbCfg() {
    try { const s = JSON.parse(localStorage.getItem('chart_bb_cfg')); if (s) return { ...DEFAULT_BB_CFG, ...s }; } catch(e) {}
    return { ...DEFAULT_BB_CFG };
}
function calcBB(klines, period, mult) {
    const upper = [], mid = [], lower = [];
    for (let i = period - 1; i < klines.length; i++) {
        const sl = klines.slice(i - period + 1, i + 1);
        const mean = sl.reduce((s, c) => s + c.close, 0) / period;
        const std  = Math.sqrt(sl.reduce((s, c) => s + (c.close - mean) ** 2, 0) / period);
        upper.push({ time: klines[i].time, value: mean + mult * std });
        mid.push(  { time: klines[i].time, value: mean });
        lower.push({ time: klines[i].time, value: mean - mult * std });
    }
    return { upper, mid, lower };
}

const TF_OPTIONS = [
    { v: '1',   l: '1m'  },
    { v: '5',   l: '5m'  },
    { v: '30',  l: '30m' },
    { v: '60',  l: '1h'  },
    { v: '240', l: '4h'  },
    { v: 'D',   l: 'D'   },
];

const DEFAULT_CANDLES = { '1': 120, '5': 100, '30': 80, '60': 80, '240': 60, 'D': 50 };

// ── helpers ───────────────────────────────────────────────────────────────────
function calcEMA(bars, period) {
    const k = 2 / (period + 1);
    let v = bars[0].close;
    return bars.map((b, i) => {
        if (i > 0) v = b.close * k + v * (1 - k);
        return { time: b.time, value: v };
    });
}

function getPriceFormat(price) {
    const p = Math.abs(price || 0);
    if (p >= 100)   return { type: 'price', precision: 2, minMove: 0.01 };
    if (p >= 10)    return { type: 'price', precision: 3, minMove: 0.001 };
    if (p >= 1)     return { type: 'price', precision: 4, minMove: 0.0001 };
    if (p >= 0.1)   return { type: 'price', precision: 5, minMove: 0.00001 };
    if (p >= 0.01)  return { type: 'price', precision: 6, minMove: 0.000001 };
    if (p >= 0.001) return { type: 'price', precision: 7, minMove: 0.0000001 };
    return             { type: 'price', precision: 8, minMove: 0.00000001 };
}

function makeOBChart(el) {
    const chart = LC.createChart(el, {
        autoSize: true,
        layout: { background: { color: '#101014' }, textColor: '#B2B5BE', fontSize: 13 },
        grid:    { vertLines: { color: '#FFFFFF0F' }, horzLines: { color: '#FFFFFF0F' } },
        crosshair: { mode: LC.CrosshairMode ? LC.CrosshairMode.Normal : 1 },
        rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.05, bottom: 0.05 }, autoScale: true },
        timeScale: { borderVisible: false, visible: true, timeVisible: true, secondsVisible: false,
                     barSpacing: 6, rightOffset: 30 },
    });
    return chart;
}

function addSeries(chart, type, opts) {
    if (typeof chart.addSeries === 'function' && LC[type]) return chart.addSeries(LC[type], opts);
    const legacy = { CandlestickSeries: 'addCandlestickSeries', LineSeries: 'addLineSeries' };
    return chart[legacy[type]](opts);
}

// ── Candle Countdown Primitive ────────────────────────────────────────────────
const _OB_TF_SECS = { '1':60, '5':300, '30':1800, '60':3600, '240':14400 };
function _obCdRemain(tf) {
    const s = Math.floor(Date.now() / 1000);
    if (_OB_TF_SECS[tf]) return _OB_TF_SECS[tf] - (s % _OB_TF_SECS[tf]);
    if (tf === 'D') return 86400 - (s % 86400);
    return 0;
}
function _obCdFmt(s) {
    if (s <= 0) return '0:00';
    if (s >= 3600) { const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=s%60; return `${h}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`; }
    return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;
}
class _OBCountdownPrimitive {
    constructor(slot) { this._slot = slot; this._series = null; }
    attached({ series }) { this._series = series; }
    detached() { this._series = null; }
    updateAllViews() {}
    paneViews() { return []; }
    priceAxisViews() {
        const s = this._slot;
        if (!this._series || !s.curTF || !s.lastPrice) return [];
        const y = this._series.priceToCoordinate(s.lastPrice);
        if (y == null) return [];
        const bull = s.lastOpen == null || s.lastPrice >= s.lastOpen;
        const rem = _obCdRemain(s.curTF);
        return [{ coordinate: () => y + 17, text: () => _obCdFmt(rem), textColor: () => '#FFFFFF', backColor: () => bull ? '#20B26C' : '#EF454A' }];
    }
}

// ── OB Levels band fill (riempimento tra le linee bid/ask) ────────────────────
function _hexToRgba(hex, alpha) {
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '');
    if (!m) return `rgba(59,130,246,${alpha})`;
    return `rgba(${parseInt(m[1],16)},${parseInt(m[2],16)},${parseInt(m[3],16)},${alpha})`;
}
class _ObBandFillPrimitive {
    constructor(slot) {
        this._slot = slot;
        this._series = null;
        const renderer = {
            draw: target => target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, verticalPixelRatio }) => {
                const s = this._slot, series = this._series;
                if (!series || !s.obActive || s.obBidVal == null || s.obAskVal == null) return;
                const y1 = series.priceToCoordinate(s.obBidVal), y2 = series.priceToCoordinate(s.obAskVal);
                if (y1 == null || y2 == null) return;
                const top = Math.min(y1, y2) * verticalPixelRatio, bot = Math.max(y1, y2) * verticalPixelRatio;
                ctx.save();
                ctx.fillStyle = _hexToRgba(getLvCfg().obBid.color, 0.12);
                ctx.fillRect(0, top, bitmapSize.width, Math.max(1, bot - top));
                ctx.restore();
            })
        };
        this._view = { renderer: () => renderer, zOrder: () => 'bottom' };
    }
    attached({ series }) { this._series = series; }
    detached() { this._series = null; }
    updateAllViews() {}
    paneViews() { return [this._view]; }
}

function fmtVol(v) {
    if (!v && v !== 0) return '';
    if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K';
    return v.toFixed(0);
}

// ── TRADE MODULE (module-level, accessible from HTML onclick handlers) ────────
// Variables and functions copied verbatim from chart.html with 4 substitutions:
// fsCandleS→_obCandleS, fsChart→_obChart, fsCoin.symbol→_obSymbol, _livePrice[...]→_obLivePrice
let _obCandleS = null, _obChart = null, _obSymbol = '', _obLivePrice = 0;
let _isLoggedIn = false;

// ── Bollinger Bands (BB) ───────────────────────────────────────────────────────
let _obBbActive = false, _obBbSeries = { upper: null, mid: null, lower: null }, _obKlines = [];

function _obApplyBB() {
    for (const k of ['upper', 'mid', 'lower']) {
        if (_obBbSeries[k]) { try { _obChart.removeSeries(_obBbSeries[k]); } catch(e) {} _obBbSeries[k] = null; }
    }
    if (!_obBbActive || !_obChart) return;
    const cfg = getBbCfg();
    const lb = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    _obBbSeries.upper = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.color, lineWidth: cfg.width, lineStyle: cfg.style });
    _obBbSeries.mid   = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.color, lineWidth: cfg.width, lineStyle: 2 });
    _obBbSeries.lower = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.color, lineWidth: cfg.width, lineStyle: cfg.style });
    if (_obKlines.length >= cfg.period) {
        const bb = calcBB(_obKlines, cfg.period, cfg.mult);
        _obBbSeries.upper.setData(bb.upper);
        _obBbSeries.mid.setData(bb.mid);
        _obBbSeries.lower.setData(bb.lower);
    }
}

function toggleObBB() {
    _obBbActive = !_obBbActive;
    const btn = document.getElementById('ob-bb-btn');
    if (btn) btn.style.color = _obBbActive ? '#60a5fa' : '#B2B5BE';
    _obApplyBB();
}

// ── Volume Profile (visible range, POC) ─────────────────────────────────────────
// Stima standard "OHLC volume profile": ogni candela distribuisce il suo volume
// sui bin di prezzo proporzionalmente alla sovrapposizione col range [low, high].
// Non serve storico tick (che Bybit non offre): funziona su qualunque candela storica.
let _obVpActive = false;
const _OB_VP_BINS = 24;

function _obVpCompute(klines, fromIdx, toIdx) {
    const lo = Math.max(0, fromIdx), hi = Math.min(klines.length - 1, toIdx);
    if (hi < lo) return null;
    let minLow = Infinity, maxHigh = -Infinity;
    for (let i = lo; i <= hi; i++) {
        const k = klines[i];
        if (k.low  < minLow)  minLow  = k.low;
        if (k.high > maxHigh) maxHigh = k.high;
    }
    if (!isFinite(minLow) || !isFinite(maxHigh) || maxHigh <= minLow) return null;
    const binSize = (maxHigh - minLow) / _OB_VP_BINS;
    const bins = new Array(_OB_VP_BINS).fill(0);
    for (let i = lo; i <= hi; i++) {
        const k = klines[i];
        const vol = k.volume || 0;
        if (!vol) continue;
        const span = k.high - k.low;
        if (span <= 0) {
            const idx = Math.min(_OB_VP_BINS - 1, Math.max(0, Math.floor((k.close - minLow) / binSize)));
            bins[idx] += vol;
            continue;
        }
        const b0 = Math.max(0, Math.floor((k.low  - minLow) / binSize));
        const b1 = Math.min(_OB_VP_BINS - 1, Math.floor((k.high - minLow) / binSize));
        for (let b = b0; b <= b1; b++) {
            const binLo = minLow + b * binSize, binHi = binLo + binSize;
            const overlap = Math.min(k.high, binHi) - Math.max(k.low, binLo);
            if (overlap > 0) bins[b] += vol * (overlap / span);
        }
    }
    let pocIdx = 0, maxVol = -1;
    for (let b = 0; b < _OB_VP_BINS; b++) if (bins[b] > maxVol) { maxVol = bins[b]; pocIdx = b; }
    if (maxVol <= 0) return null;
    return { minLow, binSize, bins, maxVol, pocIdx };
}

class _VolProfilePrimitive {
    constructor(getKlines) {
        this._getKlines = getKlines;
        this._series = null;
        const renderer = {
            draw: target => target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio, verticalPixelRatio }) => {
                if (!_obVpActive || !this._series || !_obChart) return;
                const klines = this._getKlines();
                if (!klines || klines.length < 2) return;
                const range = _obChart.timeScale().getVisibleLogicalRange();
                if (!range) return;
                const profile = _obVpCompute(klines, Math.floor(range.from), Math.ceil(range.to));
                if (!profile) return;
                const { minLow, binSize, bins, maxVol, pocIdx } = profile;
                const maxBarW = bitmapSize.width * 0.16;
                ctx.save();
                for (let b = 0; b < bins.length; b++) {
                    if (bins[b] <= 0) continue;
                    const priceLo = minLow + b * binSize, priceHi = priceLo + binSize;
                    const yLo = this._series.priceToCoordinate(priceLo), yHi = this._series.priceToCoordinate(priceHi);
                    if (yLo == null || yHi == null) continue;
                    const top = Math.min(yLo, yHi) * verticalPixelRatio, bot = Math.max(yLo, yHi) * verticalPixelRatio;
                    const w = Math.max(1 * horizontalPixelRatio, (bins[b] / maxVol) * maxBarW * horizontalPixelRatio);
                    ctx.fillStyle = b === pocIdx ? 'rgba(251,191,36,0.55)' : 'rgba(96,165,250,0.26)';
                    ctx.fillRect(bitmapSize.width - w, top, w, Math.max(1, bot - top));
                }
                const pocPrice = minLow + (pocIdx + 0.5) * binSize;
                const yPoc = this._series.priceToCoordinate(pocPrice);
                if (yPoc != null) {
                    ctx.strokeStyle = 'rgba(251,191,36,0.85)';
                    ctx.lineWidth = Math.max(1, horizontalPixelRatio);
                    ctx.setLineDash([4 * horizontalPixelRatio, 3 * horizontalPixelRatio]);
                    ctx.beginPath();
                    ctx.moveTo(0, yPoc * verticalPixelRatio);
                    ctx.lineTo(bitmapSize.width, yPoc * verticalPixelRatio);
                    ctx.stroke();
                    ctx.setLineDash([]);
                }
                ctx.restore();
            })
        };
        this._view = { renderer: () => renderer, zOrder: () => 'bottom' };
    }
    attached({ series }) { this._series = series; }
    detached() { this._series = null; }
    updateAllViews() {}
    paneViews() { return [this._view]; }
}

function toggleObVolProfile() {
    _obVpActive = !_obVpActive;
    const btn = document.getElementById('ob-vp-btn');
    if (btn) btn.style.color = _obVpActive ? '#60a5fa' : '#B2B5BE';
    try { _obChart.applyOptions({}); } catch(e) {}
}

let _tradeEnabled = false, _tradePos = null, _tradeSide = null, _hadPosition = false;
let _tradeBalance = null, _instInfo = null, _tradePollT = null;
let _fsOrderType = 'Market', _fsCondExec = 'Market';
let _fsPendingOrderId = null, _fsPendingOrderFilter = null, _orderSeenInList = false, _pendingOrderSetAt = 0, _lastAmendAt = 0, _orderMissingCount = 0, _posMissingCount = 0;
let _pricePickTarget = null;
let _fsSlLine = null, _fsTpLine = null, _fsSlPrice = null, _fsTpPrice = null;
let _fsSlLabel = null, _fsTpLabel = null, _fsExecLabel = null;
let _fsEntryLine = null, _fsEntryPrice = null, _fsEntryLabel = null;
let _fsExecLine = null, _fsExecPrice = null;
let _dragMode = null, _slTpTimer = null, _entryDragMM = null, _dragOverlay = null, _labelDragMM = null;
function _evY(ev) { return (ev.touches && ev.touches.length) ? ev.touches[0].clientY : ev.clientY; }
let _labelDisplayMode = 'both';

(async function initTrading() {
    try {
        const d = await fetch('api/trade/config').then(r => r.json());
        _tradeEnabled = d.enabled;
        if (_tradeEnabled) { const _tb = document.getElementById('fs-trade-btn'); if (_tb) _tb.style.display = 'inline-flex'; }
    } catch(e) {}
    initSlTpDrag();
})();

function toggleTradePanel() {
    const p = document.getElementById('ob-trade-panel') || document.getElementById('fs-trade-panel');
    if (!p) return;
    const open = p.style.display !== 'none' && p.style.display !== '';
    p.style.display = open ? 'none' : 'flex';
    p.style.flexDirection = 'column';
    const btn = document.getElementById('fs-trade-btn');
    if (btn) { btn.style.background = open ? '#2A2E39' : '#1e3a5f'; btn.style.color = open ? '#B2B5BE' : '#60a5fa'; }
    if (!open) { clearInterval(_tradePollT); _tradePollT = null; loadTradeData(); _tradePollT = setInterval(loadTradeData, 3000); }
    else { clearInterval(_tradePollT); _tradePollT = null; resetTradeSide('panelClose'); }
}

async function loadTradeData() {
    if (!_obSymbol) return;
    if (!_isLoggedIn) {
        if (!_tradeBalance) { _tradeBalance = { available: 1000 }; }
        if (!_instInfo) { _instInfo = { maxLeverage: 100 }; _buildLevOptions(100); updateQtyPreview(); }
        return;
    }
    const [balR, posR, instR, ordR] = await Promise.all([
        fetch('api/trade/balance').then(r => r.json()).catch(() => null),
        fetch(`api/trade/position?symbol=${_obSymbol}`).then(r => r.json()).catch(() => null),
        _instInfo ? Promise.resolve(null) : fetch(`api/trade/instrument?symbol=${_obSymbol}`).then(r => r.json()).catch(() => null),
        fetch(`api/trade/orders?symbol=${_obSymbol}&_t=${Date.now()}`).then(r => r.json()).catch(() => null),
    ]);
    if (balR && !balR.error) { _tradeBalance = balR; _renderBalance(); }
    if (posR && !posR.error) { _tradePos = posR.position; if (_tradePos) _hadPosition = true; _renderPosition(); }
    if (instR && !instR.error) { _instInfo = instR; _buildLevOptions(_instInfo.maxLeverage); updateQtyPreview(); }
    const _ordOk = ordR && !ordR.error;
    _renderOrders(_ordOk ? (ordR.orders || []) : [], _ordOk);
}

function _renderOrders(orders, ordersLoaded = true) {
    const row = document.getElementById('fs-orders-row');
    const list = document.getElementById('fs-orders-list');
    if (!row || !list) return;
    // Track and detect when our pending order disappears from Bybit's list
    if (ordersLoaded && _fsPendingOrderId) {
        const amendGrace = Date.now() - _lastAmendAt < 8000;
        if (orders.find(o => o.orderId === _fsPendingOrderId)) {
            _orderSeenInList = true;
            _orderMissingCount = 0;
        } else if ((_orderSeenInList || (_pendingOrderSetAt > 0 && Date.now() - _pendingOrderSetAt > 20000)) && !amendGrace) {
            _orderMissingCount++;
            if (_orderMissingCount >= 5) {
                _fsPendingOrderId = null;
                _fsPendingOrderFilter = null;
                _orderSeenInList = false;
                _orderMissingCount = 0;
                if (_tradePos || _hadPosition) {
                    _showTradeMsg(window.t('order_triggered'), true);
                } else {
                    _showTradeMsg(window.t('order_cancelled_ext'), false);
                    resetTradeSide('orderMissing_noPos');
                }
            }
        }
    }
    if (!orders || !orders.length) { list.innerHTML = ''; row.style.display = 'none'; return; }
    // Reattach pending conditional order after page reload
    if (!_fsEntryLine) {
        const cond = orders.find(o => o.orderFilter === 'StopOrder') || orders.find(o => o.orderFilter !== 'StopOrder' && o.status === 'New');
        if (cond) {
            _fsPendingOrderId = cond.orderId;
            _fsPendingOrderFilter = cond.orderFilter === 'StopOrder' ? 'StopOrder' : 'Order';
            _orderSeenInList = true;
            // Draw line after delay to ensure _obCandleS is ready
            setTimeout(() => {
                if (_fsEntryLine || !_obCandleS) return;
                const trigP = parseFloat(cond.triggerPrice) || parseFloat(cond.price) || 0;
                if (trigP <= 0) return;
                const long = cond.side === 'Buy';
                _tradeSide = cond.side;
                _fsOrderType = 'Conditional';
                const execP = parseFloat(cond.price) || 0;
                const trigInp = document.getElementById('fs-order-trigger');
                if (trigInp) trigInp.value = trigP;
                _fsExecPrice = trigP;
                _fsExecLine = _obCandleS.createPriceLine({ price: trigP, color: long ? 'rgba(16,185,129,0.6)' : 'rgba(239,68,68,0.6)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
                _fsExecLabel = _buildExecLabel(); _updateExecLabelPos();
                if (execP > 0) {
                    const execInp = document.getElementById('fs-cond-price');
                    if (execInp) execInp.value = execP;
                    _fsEntryPrice = execP;
                    _fsEntryLine = _obCandleS.createPriceLine({ price: execP, color: long ? '#10b981' : '#ef4444', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
                } else {
                    _fsEntryPrice = trigP;
                    _fsEntryLine = _obCandleS.createPriceLine({ price: trigP, color: long ? '#10b981' : '#ef4444', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
                }
                // Set state before creating label so it shows correct values
                _fsCondExec = execP > 0 ? 'Limit' : 'Market';
                _fsOrderType = 'Conditional';
                // Populate size from order qty
                const sizeInp = document.getElementById('fs-order-size');
                if (sizeInp && cond.qty) {
                    const lev = parseFloat(document.getElementById('fs-order-lev')?.value) || 1;
                    const notional = parseFloat(cond.qty) * trigP;
                    sizeInp.value = parseFloat((notional / lev).toFixed(2));
                    updateQtyPreview();
                }
                // Show conditional type fields
                document.getElementById('fs-cond-fields').style.display = 'flex';
                document.getElementById('fs-cond-price-wrap').style.display = execP > 0 ? 'flex' : 'none';
                document.getElementById('fs-order-price-wrap').style.display = 'none';
                // Draw SL/TP lines if present in order
                const sl = parseFloat(cond.stopLoss); const tp = parseFloat(cond.takeProfit);
                if (sl > 0) { _fsSlPrice = sl; const si = document.getElementById('fs-sl-input'); if (si) si.value = sl; }
                if (tp > 0) { _fsTpPrice = tp; const ti = document.getElementById('fs-tp-input'); if (ti) ti.value = tp; }
                if (sl > 0 || tp > 0) _drawSlTpLines();
                _createEntryLabel(long);
                _updateEntryLabelPos();
            }, 800);
        }
    }
    row.style.display = 'flex';
    list.innerHTML = orders.map(o => {
        const long = o.side === 'Buy';
        const sideColor = long ? '#10b981' : '#ef4444';
        const sideLabel = long ? 'LONG' : 'SHORT';
        const isStop = o.orderFilter === 'StopOrder';
        const typeLabel = isStop ? `Cond.${o.orderType}` : o.orderType;
        const priceLabel = o.triggerPrice && o.triggerPrice !== '0' ? `trg ${o.triggerPrice}` : (o.price && o.price !== '0' ? `@ ${o.price}` : 'Market');
        const sl = parseFloat(o.stopLoss); const tp = parseFloat(o.takeProfit);
        const slTpRow = (sl > 0 || tp > 0)
            ? `<div style="display:flex;gap:10px;font-size:10px;padding-left:42px;">`
                + (sl > 0 ? `<span style="color:#ef4444;">SL ${fmtPrice(sl)}</span>` : '')
                + (tp > 0 ? `<span style="color:#10b981;">TP ${fmtPrice(tp)}</span>` : '')
              + `</div>`
            : '';
        return `<div style="display:flex;flex-direction:column;padding:3px 0;border-bottom:1px solid #1E222D;gap:2px;">`
            + `<div style="display:flex;align-items:center;gap:6px;font-size:11px;">`
            + `<span style="color:${sideColor};font-weight:700;min-width:36px;">${sideLabel}</span>`
            + `<span style="color:#9CA3AF;flex:1;">${typeLabel} ${priceLabel}</span>`
            + `<span style="color:#6B7280;min-width:30px;text-align:right;">${o.qty}</span>`
            + `<button onclick="cancelFsOrder('${o.orderId}','${o.orderFilter}')" `
            + `style="background:none;border:none;color:#6B7280;cursor:pointer;font-size:13px;padding:0 2px;line-height:1;" `
            + `title="${isStop ? window.t('cancel_cond_tip') : window.t('cancel_limit_tip')}" onmouseover="this.style.color='#ef4444'" onmouseout="this.style.color='#6B7280'">✕</button>`
            + `</div>${slTpRow}</div>`;
    }).join('');
}

async function cancelFsOrder(orderId, orderFilter) {
    if (!_obSymbol) return;
    if (!confirm(window.t('cancel_order_q'))) return;
    try {
        const d = await fetch('api/trade/cancel', { method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ symbol: _obSymbol, orderId, orderFilter }) }).then(r => r.json());
        if (d.success) { _showTradeMsg(window.t('order_cancelled'), true); resetTradeSide('cancelFsOrder_ok'); setTimeout(loadTradeData, 1000); }
        else if (d.error && d.error.toLowerCase().includes('not exists')) { _showTradeMsg(window.t('order_done'), true); resetTradeSide('cancelFsOrder_notExists'); setTimeout(loadTradeData, 500); }
        else _showTradeMsg(_bybMsg(d), false);
    } catch(e) { _showTradeMsg(window.t('err_net'), false); }
}

function _buildLevOptions(maxLev) {
    const inp = document.getElementById('fs-order-lev');
    const container = document.getElementById('fs-lev-options');
    if (!inp || !container) return;
    const STEPS = [1,2,3,5,7,10,15,20,25,50,75,100];
    const maxFloor = Math.floor(maxLev);
    const curVal = Math.min(parseInt(inp.value) || 10, maxFloor);
    const filtered = STEPS.filter(v => v <= maxFloor);
    if (!filtered.length || filtered[filtered.length-1] < maxFloor) filtered.push(maxFloor);
    container.innerHTML = '';
    filtered.forEach(v => {
        const el = document.createElement('div');
        el.className = 'lev-opt'; el.dataset.val = v;
        el.textContent = v + 'x';
        const sel = v === curVal;
        el.style.cssText = `padding:7px 12px;font-size:12px;cursor:pointer;user-select:none;color:${sel?'#FF9C2E':'#B2B5BE'};background:${sel?'#2A2E39':''};font-weight:${sel?'700':'400'};`;
        el.onmouseover = () => { if (v !== parseInt(document.getElementById('fs-order-lev').value)) el.style.background='#1E222D'; };
        el.onmouseout  = () => { if (v !== parseInt(document.getElementById('fs-order-lev').value)) el.style.background=''; };
        el.onclick = () => selectLev(v);
        container.appendChild(el);
    });
    const ci = document.getElementById('fs-lev-custom-val');
    if (ci) ci.max = maxFloor;
    inp.value = curVal;
    const lbl = document.getElementById('fs-lev-label');
    if (lbl) lbl.textContent = curVal + 'x';
}

function toggleLevDropdown(e) {
    if (e) e.stopPropagation();
    const panel = document.getElementById('fs-lev-panel');
    const arrow = document.getElementById('fs-lev-arrow');
    const backdrop = document.getElementById('fs-lev-backdrop');
    const isOpen = panel.style.display !== 'none';
    panel.style.display = isOpen ? 'none' : 'block';
    if (backdrop) backdrop.style.display = isOpen ? 'none' : 'block';
    if (arrow) arrow.style.transform = isOpen ? '' : 'rotate(180deg)';
    if (!isOpen) setTimeout(() => document.addEventListener('click', _closeLevOutside, {once:true}), 0);
}

function _closeLevOutside(e) {
    const wrap = document.getElementById('fs-lev-wrap');
    if (wrap && wrap.contains(e.target)) {
        setTimeout(() => document.addEventListener('click', _closeLevOutside, {once:true}), 0);
        return;
    }
    const panel = document.getElementById('fs-lev-panel');
    const arrow = document.getElementById('fs-lev-arrow');
    const backdrop = document.getElementById('fs-lev-backdrop');
    if (panel) panel.style.display = 'none';
    if (backdrop) backdrop.style.display = 'none';
    if (arrow) arrow.style.transform = '';
}

function selectLev(val) {
    const inp = document.getElementById('fs-order-lev');
    if (inp) inp.value = val;
    const lbl = document.getElementById('fs-lev-label');
    if (lbl) lbl.textContent = val + 'x';
    document.querySelectorAll('#fs-lev-options .lev-opt').forEach(el => {
        const v = parseInt(el.dataset.val), sel = v === val;
        el.style.background = sel ? '#2A2E39' : '';
        el.style.color = sel ? '#FF9C2E' : '#B2B5BE';
        el.style.fontWeight = sel ? '700' : '400';
    });
    const panel = document.getElementById('fs-lev-panel');
    const arrow = document.getElementById('fs-lev-arrow');
    const backdrop = document.getElementById('fs-lev-backdrop');
    if (panel) panel.style.display = 'none';
    if (backdrop) backdrop.style.display = 'none';
    if (arrow) arrow.style.transform = '';
    const slVal = parseFloat(document.getElementById('fs-size-slider')?.value || 0);
    if (slVal > 0) onSizeSliderInput(slVal);
    else updateQtyPreview();
}

function toggleLevCustomize() {
    const row = document.getElementById('fs-lev-custom-row');
    if (!row) return;
    const open = row.style.display !== 'none';
    row.style.display = open ? 'none' : 'block';
    if (!open) document.getElementById('fs-lev-custom-val')?.focus();
}

function confirmLevCustomize() {
    const inp = document.getElementById('fs-lev-custom-val');
    if (!inp) return;
    const maxL = _instInfo ? Math.floor(_instInfo.maxLeverage) : 100;
    const v = parseInt(inp.value);
    if (!v || v < 1 || v > maxL) { inp.style.borderColor = '#ef4444'; return; }
    inp.style.borderColor = '#374151'; inp.value = '';
    document.getElementById('fs-lev-custom-row').style.display = 'none';
    selectLev(v);
}

function _renderBalance() {
    if (!_isLoggedIn) return;
    const el = document.getElementById('fs-trade-balance');
    if (el && _tradeBalance) el.textContent = `$${_tradeBalance.available.toFixed(2)}`;
}

function _renderPosition() {
    const row = document.getElementById('fs-pos-row');
    if (!_tradePos) {
        if (row) row.style.display = 'none';
        // Require 3 consecutive missing polls before declaring position closed (avoids API glitch false positives)
        if (_hadPosition && !_fsPendingOrderId) {
            _posMissingCount++;
            if (_posMissingCount >= 5) {
                _posMissingCount = 0;
                _hadPosition = false;
                _showTradeMsg(window.t('position_closed_ext'), true);
                resetTradeSide('posMissing_5polls');
            }
        }
        return;
    }
    _posMissingCount = 0;
    if (row) row.style.display = 'flex';
    const p = _tradePos, long = p.side === 'Buy';
    const margin = p.entryPrice > 0 ? (p.size * p.entryPrice / p.leverage) : 0;
    const pnlPct = margin > 0 ? (p.unrealizedPnl / margin * 100) : 0;
    const badge = document.getElementById('fs-pos-badge');
    if (badge) { badge.textContent = `${long ? 'LONG' : 'SHORT'} ${p.size} @ ${fmtPrice(p.entryPrice)} · ${p.leverage}x`; badge.style.color = long ? '#10b981' : '#ef4444'; }
    const pnlEl = document.getElementById('fs-pos-pnl');
    if (pnlEl) { pnlEl.textContent = `P&L: ${p.unrealizedPnl >= 0 ? '+' : ''}${p.unrealizedPnl.toFixed(2)} (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)`; pnlEl.style.color = p.unrealizedPnl >= 0 ? '#10b981' : '#ef4444'; }
    const liqEl = document.getElementById('fs-pos-liq');
    if (liqEl) liqEl.textContent = p.liqPrice ? `Liq: ${fmtPrice(p.liqPrice)}` : '';
    if (!_tradeSide) { _fsSlPrice = p.stopLoss || null; _fsTpPrice = p.takeProfit || null; _drawSlTpLines(); }
}

function _calcLinePnl(price) {
    if (!_fsEntryPrice || !price || !_tradeSide) return { pct: null, usdt: null };
    const long = _tradeSide === 'Buy';
    const diff = long ? (price - _fsEntryPrice) : (_fsEntryPrice - price);
    const pct = diff / _fsEntryPrice * 100;
    const size = parseFloat(document.getElementById('fs-order-size')?.value) || 0;
    const usdt = size > 0 ? (size * Math.abs(diff) / _fsEntryPrice) * (diff >= 0 ? 1 : -1) : null;
    return { pct, usdt };
}

function _buildSlTpLabel(kind) {
    const chartEl = document.getElementById('ob-chart-container');
    if (!chartEl) return null;
    const price = kind === 'tp' ? _fsTpPrice : _fsSlPrice;
    if (price == null) return null;
    const long = _tradeSide === 'Buy';
    const isInvalid = _fsEntryPrice != null && (
        kind === 'tp'
            ? (long ? price <= _fsEntryPrice : price >= _fsEntryPrice)
            : (long ? price >= _fsEntryPrice : price <= _fsEntryPrice)
    );
    const color  = kind === 'tp' ? '#10b981' : '#ef4444';
    const border = isInvalid ? '#7f1d1d' : (kind === 'tp' ? '#1e3d2a' : '#3d1e1e');
    const { pct, usdt } = _calcLinePnl(price);
    const pctStr  = pct  != null ? (pct  >= 0 ? '+' : '') + pct.toFixed(2)  + '%'    : '';
    const usdtStr = usdt != null ? (usdt >= 0 ? '+' : '') + usdt.toFixed(2) + ' USDT' : '';
    let infoHtml = '';
    if (_labelDisplayMode === 'pct')  infoHtml = pctStr  ? `<span>${pctStr}</span>` : '';
    else if (_labelDisplayMode === 'usdt') infoHtml = usdtStr ? `<span>${usdtStr}</span>` : '';
    else if (_labelDisplayMode === 'both') {
        const combined = usdtStr ? usdtStr + (pctStr ? ` (${pctStr})` : '') : pctStr;
        infoHtml = combined ? `<span>${combined}</span>` : '';
    }
    const kindLabel = kind === 'tp' ? 'TP' : 'SL';
    const alertIcon = `<svg width="13" height="13" viewBox="0 0 24 24" style="display:block;flex-shrink:0"><circle cx="12" cy="12" r="11" fill="#ef4444"/><rect x="11" y="5" width="2" height="9" rx="1" fill="white"/><rect x="11" y="16" width="2" height="2.5" rx="1" fill="white"/></svg>`;
    const leftContent = isInvalid ? alertIcon : kindLabel;
    const leftBg      = isInvalid ? '#3d0a0a' : '#1E222D';
    const leftColor   = isInvalid ? '#ef4444' : color;
    const label = document.createElement('div');
    label.id = `fs-${kind}-label`;
    label.style.cssText = `position:absolute;right:180px;z-index:20;display:flex;align-items:stretch;border:1px solid ${border};border-radius:4px;overflow:hidden;font-size:11px;font-weight:600;pointer-events:all;white-space:nowrap;user-select:none;transform:translateY(-50%);box-shadow:0 2px 8px rgba(0,0,0,.5);cursor:ns-resize;`;
    label.innerHTML = `
        <div onmousedown="_startSlTpLabelDrag(event,'${kind}')" ontouchstart="_startSlTpLabelDrag(event,'${kind}')" style="padding:5px 8px;background:${leftBg};color:${leftColor};cursor:ns-resize;touch-action:none;display:flex;align-items:center;">${leftContent}</div>
        ${infoHtml ? `<div onmousedown="_startSlTpLabelDrag(event,'${kind}')" ontouchstart="_startSlTpLabelDrag(event,'${kind}')" style="padding:5px 8px;background:${isInvalid ? '#2d0a0a' : '#1E222D'};color:${isInvalid ? '#f87171' : color};display:flex;gap:6px;align-items:center;cursor:ns-resize;touch-action:none;border-left:1px solid #2A2E39;">${infoHtml}</div>` : ''}
        <div onclick="_removeSlTpLine('${kind}')" title="${window.t('remove_sltp')}" style="padding:5px 7px;color:#6B7280;cursor:pointer;border-left:1px solid #2A2E39;background:#1E222D;" onmouseenter="this.style.background='#2A2E39'" onmouseleave="this.style.background='#1E222D'">✕</div>
    `;
    label.onmouseenter = () => _showTradeZone(kind);
    label.onmouseleave = () => _hideTradeZone();
    chartEl.appendChild(label);
    return label;
}

function _updateSlTpLabelPos(kind) {
    const label = kind === 'tp' ? _fsTpLabel : _fsSlLabel;
    const price = kind === 'tp' ? _fsTpPrice   : _fsSlPrice;
    if (!label || !_obCandleS || price == null) return;
    const y = _obCandleS.priceToCoordinate(price);
    if (y != null) label.style.top = y + 'px';
}

function _updateAllLabels() {
    _updateEntryLabelPos();
    _updateSlTpLabelPos('sl');
    _updateSlTpLabelPos('tp');
    _updateExecLabelPos();
}

function _buildExecLabel() {
    const chartEl = document.getElementById('ob-chart-container');
    if (!chartEl || _fsExecPrice == null) return null;
    const label = document.createElement('div');
    label.id = 'fs-exec-label';
    label.style.cssText = `position:absolute;right:180px;z-index:20;display:flex;align-items:stretch;border:1px solid #7a4a00;border-radius:4px;overflow:hidden;font-size:11px;font-weight:600;pointer-events:all;white-space:nowrap;user-select:none;transform:translateY(-50%);box-shadow:0 2px 8px rgba(0,0,0,.5);cursor:ns-resize;`;
    label.innerHTML = `
        <div onmousedown="_startExecLabelDrag(event)" ontouchstart="_startExecLabelDrag(event)" title="${window.t('drag_trigger')}" style="padding:5px 8px;background:#1E222D;color:#FF9C2E;cursor:ns-resize;touch-action:none;" onmouseenter="this.style.background='#2A2E39'" onmouseleave="this.style.background='#1E222D'">${window.t('trigger_price_lbl')}</div>
        <div onclick="_removeExecLine()" title="${window.t('remove_trigger')}" style="padding:5px 7px;color:#6B7280;cursor:pointer;border-left:1px solid #2A2E39;background:#1E222D;" onmouseenter="this.style.background='#2A2E39'" onmouseleave="this.style.background='#1E222D'">✕</div>
    `;
    chartEl.appendChild(label);
    return label;
}

function _updateExecLabelPos() {
    if (!_fsExecLabel || !_obCandleS || _fsExecPrice == null) return;
    const y = _obCandleS.priceToCoordinate(_fsExecPrice);
    if (y != null) _fsExecLabel.style.top = y + 'px';
}

function _removeExecLine() {
    if (_fsExecLine && _obCandleS) { try { _obCandleS.removePriceLine(_fsExecLine); } catch(e){} _fsExecLine = null; }
    if (_fsExecLabel) { _fsExecLabel.remove(); _fsExecLabel = null; }
    _fsExecPrice = null;
    const ti = document.getElementById('fs-order-trigger'); if (ti) ti.value = '';
    const trEl = document.getElementById('fs-el-tr'); if (trEl) trEl.style.display = '';
    _hideTradeZone();
}

function _startExecLabelDrag(e) {
    e.preventDefault(); e.stopPropagation();
    _dragMode = 'exec';
    if (_dragOverlay) { _dragOverlay.style.pointerEvents = 'all'; _dragOverlay.style.cursor = 'ns-resize'; }
    _entryDragMM = function(ev) {
        if (_dragMode !== 'exec') return;
        ev.preventDefault?.();
        const el = document.getElementById('ob-chart-container');
        if (!el || !_obCandleS) return;
        const rect = el.getBoundingClientRect();
        const newP = _obCandleS.coordinateToPrice(_evY(ev) - rect.top);
        if (newP == null) return;
        _fsExecPrice = newP;
        if (_fsExecLine) { try { _obCandleS.removePriceLine(_fsExecLine); } catch(ex){} }
        const long = _tradeSide === 'Buy';
        _fsExecLine = _obCandleS.createPriceLine({ price: _fsExecPrice, color: long ? 'rgba(16,185,129,0.6)' : 'rgba(239,68,68,0.6)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
        const ti = document.getElementById('fs-order-trigger'); if (ti) ti.value = parseFloat(_fsExecPrice.toPrecision(8));
        _updateExecLabelPos();
    };
    document.addEventListener('mousemove', _entryDragMM);
    document.addEventListener('touchmove', _entryDragMM, {passive:false});
}

function _removeSlTpLine(kind) {
    if (kind === 'sl') {
        if (_fsSlLine && _obCandleS) { try { _obCandleS.removePriceLine(_fsSlLine); } catch(e){} _fsSlLine = null; }
        if (_fsSlLabel) { _fsSlLabel.remove(); _fsSlLabel = null; }
        _fsSlPrice = null;
        const el = document.getElementById('fs-el-sl');
        if (el) { el.style.display = ''; el.style.color = '#ef4444'; el.style.pointerEvents = 'auto'; el.textContent = 'SL'; }
        const si = document.getElementById('fs-sl-input'); if (si) si.value = '';
        if (_tradePos && _obSymbol) {
            fetch('api/trade/set-sltp', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ symbol: _obSymbol, positionIdx: _tradePos.positionIdx ?? 0, stopLoss: 0 }) })
                .then(r => r.json()).then(d => { if (!d.success) _showTradeMsg(_bybMsg(d), false); })
                .catch(() => _showTradeMsg(window.t('err_net'), false));
        } else if (_fsPendingOrderId && _obSymbol) {
            fetch('api/trade/amend', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'Order', stopLoss: 0 }) })
                .then(r => r.json()).then(d => { if (!d.success) _showTradeMsg(_bybMsg(d), false); })
                .catch(() => _showTradeMsg(window.t('err_net'), false));
        }
    } else {
        if (_fsTpLine && _obCandleS) { try { _obCandleS.removePriceLine(_fsTpLine); } catch(e){} _fsTpLine = null; }
        if (_fsTpLabel) { _fsTpLabel.remove(); _fsTpLabel = null; }
        _fsTpPrice = null;
        const el = document.getElementById('fs-el-tp');
        if (el) { el.style.display = ''; el.style.color = '#10b981'; el.style.pointerEvents = 'auto'; el.textContent = 'TP'; }
        const ti = document.getElementById('fs-tp-input'); if (ti) ti.value = '';
        if (_tradePos && _obSymbol) {
            fetch('api/trade/set-sltp', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ symbol: _obSymbol, positionIdx: _tradePos.positionIdx ?? 0, takeProfit: 0 }) })
                .then(r => r.json()).then(d => { if (!d.success) _showTradeMsg(_bybMsg(d), false); })
                .catch(() => _showTradeMsg(window.t('err_net'), false));
        } else if (_fsPendingOrderId && _obSymbol) {
            fetch('api/trade/amend', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'Order', takeProfit: 0 }) })
                .then(r => r.json()).then(d => { if (!d.success) _showTradeMsg(_bybMsg(d), false); })
                .catch(() => _showTradeMsg(window.t('err_net'), false));
        }
    }
    _syncPosBtns();
    _hideTradeZone();
}

function _startSlTpLabelDrag(e, kind) {
    e.preventDefault(); e.stopPropagation();
    _dragMode = kind;
    if (_dragOverlay) { _dragOverlay.style.pointerEvents = 'all'; _dragOverlay.style.cursor = 'ns-resize'; }
    _entryDragMM = function(ev) {
        if (_dragMode !== kind) return;
        ev.preventDefault?.();
        const el = document.getElementById('ob-chart-container');
        if (!el || !_obCandleS) return;
        const rect = el.getBoundingClientRect();
        const newP = _obCandleS.coordinateToPrice(_evY(ev) - rect.top);
        if (newP == null) return;
        if (kind === 'sl') { _fsSlPrice = newP; const si = document.getElementById('fs-sl-input'); if (si) si.value = parseFloat(newP.toPrecision(8)); }
        else               { _fsTpPrice = newP; const ti = document.getElementById('fs-tp-input'); if (ti) ti.value = parseFloat(newP.toPrecision(8)); }
        _drawSlTpLines();
        _showTradeZone(kind);
    };
    document.addEventListener('mousemove', _entryDragMM);
    document.addEventListener('touchmove', _entryDragMM, {passive:false});
}

function _syncPosBtns() {
    const slBtn = document.getElementById('fs-pos-sl-btn');
    const tpBtn = document.getElementById('fs-pos-tp-btn');
    if (slBtn) slBtn.style.display = _fsSlPrice != null ? 'none' : '';
    if (tpBtn) tpBtn.style.display = _fsTpPrice != null ? 'none' : '';
}

function _setLabelDisplayMode(mode) {
    _labelDisplayMode = mode;
    const btns = { pct: 'fs-lbl-pct', usdt: 'fs-lbl-usdt', both: 'fs-lbl-both' };
    Object.entries(btns).forEach(([m, id]) => {
        const b = document.getElementById(id);
        if (!b) return;
        const active = m === mode;
        b.style.background = active ? '#2A2E39' : '#1E222D';
        b.style.color       = active ? '#E5E7EB' : '#6B7280';
        b.style.borderColor = active ? '#10b981'  : '#374151';
    });
    _drawSlTpLines();
}

function _drawSlTpLines() {
    if (!_obCandleS) return;
    if (_fsSlLine) { try { _obCandleS.removePriceLine(_fsSlLine); } catch(e) {} _fsSlLine = null; }
    if (_fsTpLine) { try { _obCandleS.removePriceLine(_fsTpLine); } catch(e) {} _fsTpLine = null; }
    if (_fsSlLabel) { _fsSlLabel.remove(); _fsSlLabel = null; }
    if (_fsTpLabel) { _fsTpLabel.remove(); _fsTpLabel = null; }
    if (_fsSlPrice != null) {
        _fsSlLine  = _obCandleS.createPriceLine({ price: _fsSlPrice, color: '#ef4444', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' });
        _fsSlLabel = _buildSlTpLabel('sl');
        _updateSlTpLabelPos('sl');
    }
    if (_fsTpPrice != null) {
        _fsTpLine  = _obCandleS.createPriceLine({ price: _fsTpPrice, color: '#10b981', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' });
        _fsTpLabel = _buildSlTpLabel('tp');
        _updateSlTpLabelPos('tp');
    }
    _syncPosBtns();
}

function _removeSlTpLines() {
    if (_fsSlLine) { try { _obCandleS.removePriceLine(_fsSlLine); } catch(e) {} _fsSlLine = null; }
    if (_fsTpLine) { try { _obCandleS.removePriceLine(_fsTpLine); } catch(e) {} _fsTpLine = null; }
    if (_fsSlLabel) { _fsSlLabel.remove(); _fsSlLabel = null; }
    if (_fsTpLabel) { _fsTpLabel.remove(); _fsTpLabel = null; }
    _fsSlPrice = null; _fsTpPrice = null;
}

function addFsSlLine() {
    if (!_obCandleS || !_tradePos) return;
    const price = _obLivePrice || _tradePos.entryPrice;
    const long = _tradePos.side === 'Buy';
    _fsSlPrice = long ? price * 0.98 : price * 1.02;
    _drawSlTpLines();
    if (_obSymbol) {
        fetch('api/trade/set-sltp', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ symbol: _obSymbol, positionIdx: _tradePos.positionIdx ?? 0, stopLoss: parseFloat(_fsSlPrice.toFixed(8)) }) })
            .then(r => r.json()).then(d => { if (!d.success) _showTradeMsg(_bybMsg(d), false); })
            .catch(() => _showTradeMsg(window.t('err_net'), false));
    }
}

function addFsTpLine() {
    if (!_obCandleS || !_tradePos) return;
    const price = _obLivePrice || _tradePos.entryPrice;
    const long = _tradePos.side === 'Buy';
    _fsTpPrice = long ? price * 1.04 : price * 0.96;
    _drawSlTpLines();
    if (_obSymbol) {
        fetch('api/trade/set-sltp', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ symbol: _obSymbol, positionIdx: _tradePos.positionIdx ?? 0, takeProfit: parseFloat(_fsTpPrice.toFixed(8)) }) })
            .then(r => r.json()).then(d => { if (!d.success) _showTradeMsg(_bybMsg(d), false); })
            .catch(() => _showTradeMsg(window.t('err_net'), false));
    }
}

function initSlTpDrag() {
    const el = document.getElementById('ob-chart-container');
    if (!el) return;
    // Transparent overlay on top of chart — activated when mouse is near a price line.
    // This blocks LightweightCharts (pointerdown/pan) without needing capture tricks.
    _dragOverlay = document.createElement('div');
    _dragOverlay.id = 'fs-drag-overlay';
    _dragOverlay.style.cssText = 'position:absolute;inset:0;z-index:12;pointer-events:none;';
    el.appendChild(_dragOverlay);
    _dragOverlay.addEventListener('mousemove', _onFsMM);
    _dragOverlay.addEventListener('mousedown', _onFsMD);
    el.addEventListener('mousemove', _onFsMM); // for cursor when overlay is transparent
    document.addEventListener('mouseup', _onFsMU);
    document.addEventListener('touchend', _onFsMU);
    document.addEventListener('touchcancel', _onFsMU);
}

function _onFsMM(e) {
    const el = document.getElementById('ob-chart-container');
    if (!el || !_obCandleS) return;
    const rect = el.getBoundingClientRect();
    const y = e.clientY - rect.top;
    if (_dragMode) {
        const newP = _obCandleS.coordinateToPrice(y);
        if (newP == null) return;
        if (_dragMode === 'sl') { _fsSlPrice = newP; _drawSlTpLines(); const si = document.getElementById('fs-sl-input'); if (si) si.value = parseFloat(newP.toPrecision(8)); _showTradeZone('sl'); }
        else if (_dragMode === 'tp') { _fsTpPrice = newP; _drawSlTpLines(); const ti = document.getElementById('fs-tp-input'); if (ti) ti.value = parseFloat(newP.toPrecision(8)); _showTradeZone('tp'); }
        else if (_dragMode === 'exec') {
            _fsExecPrice = newP;
            if (_fsExecLine) { try { _obCandleS.removePriceLine(_fsExecLine); } catch(ex){} }
            const long = _tradeSide === 'Buy';
            _fsExecLine = _obCandleS.createPriceLine({ price: _fsExecPrice, color: long ? 'rgba(16,185,129,0.6)' : 'rgba(239,68,68,0.6)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
            const ti = document.getElementById('fs-order-trigger'); if (ti) ti.value = parseFloat(_fsExecPrice.toPrecision(8));
            if (!_fsExecLabel) { _fsExecLabel = _buildExecLabel(); }
            _updateExecLabelPos();
        }
        else if (_dragMode === 'entry') {
            _fsEntryPrice = newP;
            if (_fsEntryLine) { try { _obCandleS.removePriceLine(_fsEntryLine); } catch(ex){} }
            const long = _tradeSide === 'Buy';
            _fsEntryLine = _obCandleS.createPriceLine({ price: _fsEntryPrice, color: long ? '#10b981' : '#ef4444', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
            _updateEntryLabelPos();
            _syncEntryPrice(_fsEntryPrice);
            _showEntryZone();
        }
        return;
    }
    _updateAllLabels();
    const THR = 8;
    let near = null;
    if (_fsEntryLine && _fsEntryPrice != null) { const ey = _obCandleS.priceToCoordinate(_fsEntryPrice); if (ey != null && Math.abs(y - ey) < THR) near = 'entry'; }
    if (!near && _fsExecPrice != null) { const ep = _obCandleS.priceToCoordinate(_fsExecPrice); if (ep != null && Math.abs(y - ep) < THR) near = 'exec'; }
    if (!near && _fsSlPrice != null) { const sy = _obCandleS.priceToCoordinate(_fsSlPrice); if (sy != null && Math.abs(y - sy) < THR) near = 'sl'; }
    if (!near && _fsTpPrice != null) { const ty = _obCandleS.priceToCoordinate(_fsTpPrice); if (ty != null && Math.abs(y - ty) < THR) near = 'tp'; }
    // Activate overlay to block chart panning when near a draggable line
    if (_dragOverlay) { _dragOverlay.style.pointerEvents = near ? 'all' : 'none'; _dragOverlay.style.cursor = near ? 'ns-resize' : ''; }
    el.style.cursor = near ? 'ns-resize' : '';
}

function _onFsMD(e) {
    const el = document.getElementById('ob-chart-container');
    if (!el || !_obCandleS) return;
    e.preventDefault();
    const rect = el.getBoundingClientRect();
    const y = e.clientY - rect.top;
    const THR = 8;
    if (_fsEntryLine && _fsEntryPrice != null) { const ey = _obCandleS.priceToCoordinate(_fsEntryPrice); if (ey != null && Math.abs(y - ey) < THR) { _dragMode = 'entry'; return; } }
    if (_fsExecPrice != null) { const ep = _obCandleS.priceToCoordinate(_fsExecPrice); if (ep != null && Math.abs(y - ep) < THR) { _dragMode = 'exec'; return; } }
    if (_fsSlPrice != null) { const sy = _obCandleS.priceToCoordinate(_fsSlPrice); if (sy != null && Math.abs(y - sy) < THR) { _dragMode = 'sl'; return; } }
    if (_fsTpPrice != null) { const ty = _obCandleS.priceToCoordinate(_fsTpPrice); if (ty != null && Math.abs(y - ty) < THR) { _dragMode = 'tp'; } }
}

async function _onFsMU() {
    if (!_dragMode) return;
    const mode = _dragMode; _dragMode = null;
    if (_entryDragMM) { document.removeEventListener('mousemove', _entryDragMM); document.removeEventListener('touchmove', _entryDragMM); _entryDragMM = null; }
    if (_labelDragMM) { document.removeEventListener('mousemove', _labelDragMM); document.removeEventListener('touchmove', _labelDragMM); _labelDragMM = null; }
    if (_dragOverlay) { _dragOverlay.style.pointerEvents = 'none'; _dragOverlay.style.cursor = ''; }
    const el = document.getElementById('ob-chart-container'); if (el) el.style.cursor = '';
    _hideTradeZone();
    if (mode === 'exec') {
        if (_fsPendingOrderId && _obSymbol && _fsExecPrice != null) {
            clearTimeout(_slTpTimer);
            _slTpTimer = setTimeout(async () => {
                try {
                    _lastAmendAt = Date.now();
                    const body = { symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'Order', triggerPrice: parseFloat(_fsExecPrice.toFixed(8)) };
                    const d = await fetch('api/trade/amend', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
                    if (!d.success) _showTradeMsg(_bybMsg(d), false);
                } catch(e) { _showTradeMsg(window.t('err_net'), false); }
            }, 400);
        }
        return;
    }
    if (mode === 'entry' && _fsPendingOrderId && _obSymbol) {
        clearTimeout(_slTpTimer);
        _slTpTimer = setTimeout(async () => {
            try {
                _lastAmendAt = Date.now();
                const amendField = (_fsOrderType === 'Conditional' && _fsCondExec !== 'Limit') ? 'triggerPrice' : 'price';
                const body = { symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'Order', [amendField]: parseFloat(_fsEntryPrice.toFixed(8)) };
                const d = await fetch('api/trade/amend', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
                if (!d.success) _showTradeMsg(_bybMsg(d), false);
            } catch(e) { _showTradeMsg(window.t('err_net'), false); }
        }, 400);
        return;
    }
    if (mode === 'entry') return;
    if (!_obSymbol) return;
    clearTimeout(_slTpTimer);
    _slTpTimer = setTimeout(async () => {
        try {
            const slVal = (mode === 'sl') ? _fsSlPrice : null;
            const tpVal = (mode !== 'sl') ? _fsTpPrice : null;
            if (!slVal && !tpVal) return;
            if (_tradePos) {
                // Open position: use trading-stop
                const body = { symbol: _obSymbol, positionIdx: _tradePos.positionIdx ?? 0 };
                if (slVal) body.stopLoss = parseFloat(slVal.toFixed(8));
                if (tpVal) body.takeProfit = parseFloat(tpVal.toFixed(8));
                const d = await fetch('api/trade/set-sltp', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
                if (!d.success) _showTradeMsg(_bybMsg(d), false);
            } else if (_fsPendingOrderId) {
                // Pending order: amend
                _lastAmendAt = Date.now();
                const body = { symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'Order' };
                if (slVal) body.stopLoss = parseFloat(slVal.toFixed(8));
                if (tpVal) body.takeProfit = parseFloat(tpVal.toFixed(8));
                const d = await fetch('api/trade/amend', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
                if (!d.success) _showTradeMsg(_bybMsg(d), false);
            }
            // else: no position and no pending order — UI only
        } catch(e) { _showTradeMsg(window.t('err_net'), false); }
    }, 400);
}

function _getEntryPrice() {
    const liveP = _obLivePrice || 0;
    if (_fsOrderType === 'Limit') {
        const inp = document.getElementById('fs-order-price');
        const v = parseFloat(inp?.value);
        if (v > 0) return v;
        return 0;
    } else if (_fsOrderType === 'Conditional') {
        const inp = document.getElementById('fs-order-trigger');
        const v = parseFloat(inp?.value);
        if (v > 0) return v;
        if (inp && liveP) inp.value = parseFloat(liveP.toPrecision(8));
    }
    return liveP;
}

function _syncEntryPrice(price) {
    const s = parseFloat(price.toPrecision(8));
    if (_fsOrderType === 'Limit') {
        const el = document.getElementById('fs-order-price'); if (el) el.value = s;
    } else if (_fsOrderType === 'Conditional') {
        if (_fsCondExec === 'Limit') {
            const el = document.getElementById('fs-cond-price'); if (el) el.value = s;
            if (_fsPendingOrderId && _obSymbol) {
                clearTimeout(_slTpTimer);
                _slTpTimer = setTimeout(async () => {
                    try {
                        _lastAmendAt = Date.now();
                        const body = { symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'Order', price: s };
                        const d = await fetch('api/trade/amend', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
                        if (!d.success) _showTradeMsg(_bybMsg(d), false);
                    } catch(e) { _showTradeMsg(window.t('err_net'), false); }
                }, 600);
            }
        } else {
            const el = document.getElementById('fs-order-trigger'); if (el) el.value = s;
            if (_fsPendingOrderId && _obSymbol) {
                clearTimeout(_slTpTimer);
                _slTpTimer = setTimeout(async () => {
                    try {
                        _lastAmendAt = Date.now();
                        const body = { symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'Order', triggerPrice: s };
                        const d = await fetch('api/trade/amend', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
                        if (!d.success) _showTradeMsg(_bybMsg(d), false);
                    } catch(e) { _showTradeMsg(window.t('err_net'), false); }
                }, 600);
            }
        }
    }
    // Rebuild SL/TP labels to refresh % and USDT values
    if (_fsSlPrice != null || _fsTpPrice != null) _drawSlTpLines();
}

function _onSlTpInput(kind, val) {
    if (!_obCandleS) return;
    const p = parseFloat(val);
    if (kind === 'sl') {
        _fsSlPrice = (p > 0) ? p : null;
        if (_tradeSide) {
            const el = document.getElementById('fs-el-sl');
            if (el) el.style.display = _fsSlPrice ? 'none' : '';
        }
        _drawSlTpLines();
    } else {
        _fsTpPrice = (p > 0) ? p : null;
        if (_tradeSide) {
            const el = document.getElementById('fs-el-tp');
            if (el) el.style.display = _fsTpPrice ? 'none' : '';
        }
        _drawSlTpLines();
    }
    if (_tradePos && _obSymbol) {
        clearTimeout(_slTpTimer);
        _slTpTimer = setTimeout(async () => {
            try {
                const body = { symbol: _obSymbol, positionIdx: _tradePos.positionIdx ?? 0 };
                if (kind === 'sl' && _fsSlPrice) body.stopLoss = parseFloat(_fsSlPrice.toFixed(8));
                else if (kind !== 'sl' && _fsTpPrice) body.takeProfit = parseFloat(_fsTpPrice.toFixed(8));
                if (!body.stopLoss && !body.takeProfit) return;
                const d = await fetch('api/trade/set-sltp', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
                if (!d.success) _showTradeMsg(_bybMsg(d), false);
            } catch(e) { _showTradeMsg(window.t('err_net'), false); }
        }, 600);
    }
}

function _onPriceInput(kind, val) {
    if (!_tradeSide || !_obCandleS) return;
    const p = parseFloat(val);
    if (!p || p <= 0) return;
    const long = _tradeSide === 'Buy';
    if (kind === 'limit' && _fsOrderType === 'Limit') {
        _fsEntryPrice = p;
        if (_fsEntryLine) { try { _obCandleS.removePriceLine(_fsEntryLine); } catch(e){} }
        _fsEntryLine = _obCandleS.createPriceLine({ price: p, color: long ? '#10b981' : '#ef4444', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
        _updateEntryLabelPos();
        const trEl = document.getElementById('fs-el-tr');
        if (trEl) trEl.style.display = 'none';
    } else if (kind === 'exec' && _fsOrderType === 'Conditional' && _fsCondExec === 'Limit') {
        _fsEntryPrice = p;
        if (_fsEntryLine) { try { _obCandleS.removePriceLine(_fsEntryLine); } catch(e){} }
        _fsEntryLine = _obCandleS.createPriceLine({ price: p, color: long ? '#10b981' : '#ef4444', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
        _updateEntryLabelPos();
        if (_fsPendingOrderId && _obSymbol) {
            clearTimeout(_slTpTimer);
            _slTpTimer = setTimeout(async () => {
                try {
                    _lastAmendAt = Date.now();
                    const body = { symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'Order', price: parseFloat(_fsEntryPrice.toFixed(8)) };
                    const d = await fetch('api/trade/amend', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
                    if (!d.success) _showTradeMsg(_bybMsg(d), false);
                } catch(e) { _showTradeMsg(window.t('err_net'), false); }
            }, 600);
        }
    } else if (kind === 'trigger' && _fsOrderType === 'Conditional') {
        if (_fsCondExec === 'Limit') {
            _fsExecPrice = p;
            if (_fsExecLine) { try { _obCandleS.removePriceLine(_fsExecLine); } catch(e){} }
            _fsExecLine = _obCandleS.createPriceLine({ price: p, color: long ? 'rgba(16,185,129,0.6)' : 'rgba(239,68,68,0.6)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
            if (_fsPendingOrderId && _obSymbol) {
                clearTimeout(_slTpTimer);
                _slTpTimer = setTimeout(async () => {
                    try {
                        _lastAmendAt = Date.now();
                        const body = { symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'Order', triggerPrice: parseFloat(_fsExecPrice.toFixed(8)) };
                        const d = await fetch('api/trade/amend', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
                        if (!d.success) _showTradeMsg(_bybMsg(d), false);
                    } catch(e) { _showTradeMsg(window.t('err_net'), false); }
                }, 600);
            }
        } else {
            _fsEntryPrice = p;
            if (_fsEntryLine) { try { _obCandleS.removePriceLine(_fsEntryLine); } catch(e){} }
            _fsEntryLine = _obCandleS.createPriceLine({ price: p, color: long ? '#10b981' : '#ef4444', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
            _updateEntryLabelPos();
            const trEl = document.getElementById('fs-el-tr');
            if (trEl) trEl.style.display = 'none';
            if (_fsPendingOrderId && _obSymbol) {
                clearTimeout(_slTpTimer);
                _slTpTimer = setTimeout(async () => {
                    try {
                        _lastAmendAt = Date.now();
                        const body = { symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'Order', triggerPrice: parseFloat(p.toFixed(8)) };
                        const d = await fetch('api/trade/amend', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
                        if (!d.success) _showTradeMsg(_bybMsg(d), false);
                    } catch(e) { _showTradeMsg(window.t('err_net'), false); }
                }, 600);
            }
        }
    }
}

function _startLabelDrag(kind, e) {
    e.preventDefault(); e.stopPropagation();
    _dragMode = kind;
    if (_dragOverlay) { _dragOverlay.style.pointerEvents = 'all'; _dragOverlay.style.cursor = 'ns-resize'; }
    const chartEl = document.getElementById('ob-chart-container'); if (chartEl) chartEl.style.cursor = 'ns-resize';
    _labelDragMM = (ev) => {
        if (!_obCandleS) return;
        ev.preventDefault?.();
        const el = document.getElementById('ob-chart-container'); if (!el) return;
        const rect = el.getBoundingClientRect();
        const y = _evY(ev) - rect.top;
        if (y < 0 || y > rect.height) return;
        const newP = _obCandleS.coordinateToPrice(y);
        if (!newP || newP <= 0) return;
        if (kind === 'sl') { _fsSlPrice = newP; const si = document.getElementById('fs-sl-input'); if (si) si.value = parseFloat(newP.toPrecision(8)); _drawSlTpLines(); const slBtn = document.getElementById('fs-el-sl'); if (slBtn) slBtn.style.display = 'none'; }
        else if (kind === 'tp') { _fsTpPrice = newP; const ti = document.getElementById('fs-tp-input'); if (ti) ti.value = parseFloat(newP.toPrecision(8)); _drawSlTpLines(); const tpBtn = document.getElementById('fs-el-tp'); if (tpBtn) tpBtn.style.display = 'none'; }
        else if (kind === 'entry') {
            _fsEntryPrice = newP;
            if (_fsEntryLine) { try { _obCandleS.removePriceLine(_fsEntryLine); } catch(e){} }
            const long = _tradeSide === 'Buy';
            _fsEntryLine = _obCandleS.createPriceLine({ price: newP, color: long ? '#10b981' : '#ef4444', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
            _updateEntryLabelPos();
            const trEl = document.getElementById('fs-el-tr'); if (trEl) trEl.style.display = 'none';
            _syncEntryPrice(newP);
        }
    };
    document.addEventListener('mousemove', _labelDragMM);
    document.addEventListener('touchmove', _labelDragMM, {passive:false});
}

function _createExecLine(long) {
    if (_fsExecLine && _obCandleS) { try { _obCandleS.removePriceLine(_fsExecLine); } catch(e){} _fsExecLine = null; }
    if (_fsOrderType !== 'Conditional') return;
    const trigInp = document.getElementById('fs-order-trigger');
    const liveP = _obLivePrice || 0;
    const tv = parseFloat(trigInp?.value) || 0;
    const trigP = tv > 0 ? tv : (liveP > 0 ? parseFloat((long ? liveP * 1.01 : liveP * 0.99).toPrecision(6)) : 0);
    if (!trigP || trigP <= 0) return;
    _fsExecPrice = trigP;
    if (!tv && trigInp) trigInp.value = parseFloat(trigP.toPrecision(8));
    _fsExecLine = _obCandleS.createPriceLine({ price: trigP, color: long ? 'rgba(16,185,129,0.6)' : 'rgba(239,68,68,0.6)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
}

function setFsSide(side) {
    _tradeSide = side;
    const l = document.getElementById('fs-btn-long'), s = document.getElementById('fs-btn-short');
    if (l) { l.style.background = side === 'Buy' ? '#065f46' : '#0d1a14'; l.style.color = '#10b981'; l.style.borderColor = side === 'Buy' ? '#10b981' : '#1e3d2a'; }
    if (s) { s.style.background = side === 'Sell' ? '#7f1d1d' : '#1a0d0d'; s.style.color = '#ef4444'; s.style.borderColor = side === 'Sell' ? '#ef4444' : '#3d1e1e'; }
    if (!_obCandleS) return;
    const long = side === 'Buy';
    _removeEntryLine();
    if (_fsOrderType === 'Conditional') {
        const liveP = _obLivePrice || 0;
        if (_fsCondExec === 'Limit') {
            const execInp = document.getElementById('fs-cond-price');
            const execV = parseFloat(execInp?.value) || 0;
            const execP = execV > 0 ? execV : (liveP > 0 ? parseFloat((long ? liveP * 0.999 : liveP * 1.001).toPrecision(6)) : 0);
            if (execP > 0) {
                _fsEntryPrice = execP;
                _fsEntryLine = _obCandleS.createPriceLine({ price: execP, color: long ? '#10b981' : '#ef4444', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
                if (!execV && execInp) execInp.value = parseFloat(execP.toPrecision(8));
            } else {
                _fsEntryPrice = liveP;
            }
        } else {
            _fsEntryPrice = liveP;
        }
    } else {
        const price = _getEntryPrice();
        const liveP = _obLivePrice || 0;
        if (_fsOrderType === 'Limit') {
            const drawP = price > 0 ? price : (liveP > 0 ? parseFloat((long ? liveP * 0.995 : liveP * 1.005).toPrecision(6)) : 0);
            _fsEntryPrice = drawP;
            if (drawP > 0) {
                _fsEntryLine = _obCandleS.createPriceLine({ price: drawP, color: long ? '#10b981' : '#ef4444', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
            }
        } else {
            _fsEntryPrice = liveP;
        }
    }
    if (_fsOrderType !== 'Market') {
        _createEntryLabel(long);
        _updateEntryLabelPos();
    }
}

function _removeEntryLine() {
    if (_fsEntryLine && _obCandleS) { try { _obCandleS.removePriceLine(_fsEntryLine); } catch(e) {} }
    _fsEntryLine = null; _fsEntryPrice = null;
    if (_fsExecLine && _obCandleS) { try { _obCandleS.removePriceLine(_fsExecLine); } catch(e) {} }
    _fsExecLine = null; _fsExecPrice = null;
    if (_fsExecLabel) { _fsExecLabel.remove(); _fsExecLabel = null; }
    if (_fsEntryLabel) { _fsEntryLabel.remove(); _fsEntryLabel = null; }
    document.getElementById('fs-entry-label')?.remove();
}

function _entryLabelTypeText() {
    if (_fsOrderType === 'Limit') return 'Limit';
    if (_fsOrderType === 'Conditional') return _fsCondExec === 'Limit' ? 'Cond.Limit' : 'Cond.Market';
    return 'Market';
}

function _createEntryLabel(long) {
    if (_fsEntryLabel) { _fsEntryLabel.remove(); _fsEntryLabel = null; }
    const chartEl = document.getElementById('ob-chart-container');
    if (!chartEl) return;
    const parent = chartEl;
    const sideColor = long ? '#10b981' : '#ef4444';
    const sideBg    = long ? '#065f46' : '#7f1d1d';
    const sideText  = long ? 'Long' : 'Short';
    const sizeVal   = document.getElementById('fs-order-size')?.value || '0';
    const typeText  = _entryLabelTypeText();
    const sep = `border-left:1px solid #2A2E39;`;
    const cell = `padding:5px 8px;cursor:pointer;background:#1E222D;`;
    const label = document.createElement('div');
    label.id = 'fs-entry-label';
    label.style.cssText = `position:absolute;right:180px;z-index:20;display:flex;align-items:stretch;border:1px solid ${sideColor};border-radius:4px;overflow:hidden;font-size:11px;font-weight:600;pointer-events:all;white-space:nowrap;user-select:none;transform:translateY(-50%);box-shadow:0 2px 8px rgba(0,0,0,.5);`;
    const trBtn = (_fsOrderType === 'Conditional' && _fsCondExec === 'Limit')
        ? `<div id="fs-el-tr" onmousedown="_startTriggerDrag(event)" ontouchstart="_startTriggerDrag(event)" title="${window.t('drag_trigger')}" style="${cell}${sep}color:#FF9C2E;cursor:grab;touch-action:none;" onmouseenter="this.style.background='#2A2E39'" onmouseleave="this.style.background='#1E222D'">TR</div>`
        : '';
    const isMarket = _fsOrderType === 'Market';
    const slTpBtns = isMarket ? '' : `
        <div id="fs-el-tp" onmousedown="_startLabelDrag('tp',event)" ontouchstart="_startLabelDrag('tp',event)" title="${window.t('drag_tp')}" style="${cell}${sep}color:#10b981;cursor:grab;touch-action:none;${_fsTpPrice != null ? 'display:none;' : ''}" onmouseenter="this.style.background='#1a3028'" onmouseleave="this.style.background='#1E222D'">TP</div>
        <div id="fs-el-sl" onmousedown="_startLabelDrag('sl',event)" ontouchstart="_startLabelDrag('sl',event)" title="${window.t('drag_sl')}" style="${cell}${sep}color:#ef4444;cursor:grab;touch-action:none;${_fsSlPrice != null ? 'display:none;' : ''}" onmouseenter="this.style.background='#2d1717'" onmouseleave="this.style.background='#1E222D'">SL</div>`;
    label.innerHTML = `
        <div id="fs-el-side" onmousedown="_startEntryDrag(event)" ontouchstart="_startEntryDrag(event)" style="padding:5px 9px;background:${sideBg};color:${sideColor};cursor:${isMarket ? 'default' : 'ns-resize'};touch-action:none;">${sideText}</div>
        <div id="fs-el-type" onclick="_openEntryTypeMenu(event)" title="${window.t('change_order_type')}" style="${cell}${sep}color:#9CA3AF;" onmouseenter="this.style.background='#2A2E39'" onmouseleave="this.style.background='#1E222D'">${typeText}</div>
        ${trBtn}
        ${slTpBtns}
        <div onclick="_cancelOrReset()" title="${window.t('remove_cancel')}" style="${cell}${sep}color:#6B7280;" onmouseenter="this.style.background='#2A2E39'" onmouseleave="this.style.background='#1E222D'">✕</div>
    `;
    parent.appendChild(label);
    _fsEntryLabel = label;
    _fsEntryLabel.onmouseenter = () => _showEntryZone();
    _fsEntryLabel.onmouseleave = () => _hideTradeZone();
}

function _editEntrySize(e) {
    e.stopPropagation();
    const el = document.getElementById('fs-el-size');
    if (!el || el.querySelector('input')) return;
    const cur = document.getElementById('fs-order-size')?.value || '0';
    el.onmouseenter = el.onmouseleave = null;
    el.style.background = '#2A2E39';
    el.innerHTML = `<input id="fs-el-size-inp" type="number" value="${cur}" min="1" step="1"
        style="width:54px;background:transparent;border:none;outline:none;color:#E5E7EB;font-size:11px;font-weight:600;padding:0;"
        onclick="event.stopPropagation()"
        onkeydown="if(event.key==='Enter'||event.key==='Escape'){event.preventDefault();_confirmEntrySize();}">`;
    const inp = el.querySelector('input');
    if (inp) { inp.focus(); inp.select(); inp.addEventListener('blur', _confirmEntrySize); }
}

function _confirmEntrySize() {
    const inp = document.getElementById('fs-el-size-inp');
    const el  = document.getElementById('fs-el-size');
    if (!el) return;
    const val = parseFloat(inp?.value);
    const sizeInp = document.getElementById('fs-order-size');
    if (sizeInp && val > 0) { sizeInp.value = val; updateQtyPreview(); }
    const show = (val > 0 ? val : sizeInp?.value) || '0';
    el.innerHTML = show;
    el.onmouseenter = function(){ this.style.background='#2A2E39'; };
    el.onmouseleave = function(){ this.style.background='#1E222D'; };
    el.style.background = '#1E222D';
    _refreshAllSizeCells(show);
}

function _editSlTpSize(e, kind) {
    e.stopPropagation();
    const cellId = `fs-${kind}-size`;
    const el = document.getElementById(cellId);
    if (!el || el.querySelector('input')) return;
    const cur = document.getElementById('fs-order-size')?.value || '0';
    el.onmouseenter = el.onmouseleave = null;
    el.style.background = '#2A2E39';
    el.innerHTML = `<input id="${cellId}-inp" type="number" value="${cur}" min="1" step="1"
        style="width:54px;background:transparent;border:none;outline:none;color:#E5E7EB;font-size:11px;font-weight:600;padding:0;"
        onclick="event.stopPropagation()"
        onkeydown="if(event.key==='Enter'||event.key==='Escape'){event.preventDefault();_confirmSlTpSize('${kind}');}">`;
    const inp = el.querySelector('input');
    if (inp) { inp.focus(); inp.select(); inp.addEventListener('blur', () => _confirmSlTpSize(kind)); }
}

function _confirmSlTpSize(kind) {
    const cellId = `fs-${kind}-size`;
    const inp = document.getElementById(cellId + '-inp');
    const el  = document.getElementById(cellId);
    if (!el) return;
    const val = parseFloat(inp?.value);
    const sizeInp = document.getElementById('fs-order-size');
    if (sizeInp && val > 0) { sizeInp.value = val; updateQtyPreview(); }
    const show = (val > 0 ? val : sizeInp?.value) || '0';
    el.innerHTML = show;
    el.onmouseenter = function(){ this.style.background='#2A2E39'; };
    el.onmouseleave = function(){ this.style.background='#1E222D'; };
    el.style.background = '#1E222D';
    _refreshAllSizeCells(show);
}

function _refreshAllSizeCells(val) {
    const show = String(val || '0');
    ['fs-el-size', 'fs-tp-size', 'fs-sl-size'].forEach(id => {
        const el = document.getElementById(id);
        if (el && !el.querySelector('input')) el.innerHTML = show;
    });
}

function _openEntryTypeMenu(e) {
    e.stopPropagation();
    document.getElementById('fs-el-type-menu')?.remove();
    if (!_fsEntryLabel) return;
    const parent = document.getElementById('ob-chart-container');
    if (!parent) return;
    const labelRect  = _fsEntryLabel.getBoundingClientRect();
    const parentRect = parent.getBoundingClientRect();
    const types = [
        { otype: 'Limit',       cexec: null,      label: 'Limit'       },
        { otype: 'Conditional', cexec: 'Market',  label: 'Cond.Market' },
        { otype: 'Conditional', cexec: 'Limit',   label: 'Cond.Limit'  },
    ];
    const menu = document.createElement('div');
    menu.id = 'fs-el-type-menu';
    menu.style.cssText = `position:absolute;right:180px;top:${labelRect.bottom - parentRect.top + 2}px;background:#1E222D;border:1px solid #374151;border-radius:4px;overflow:hidden;z-index:30;box-shadow:0 4px 12px rgba(0,0,0,.6);pointer-events:all;`;
    types.forEach(t => {
        const item = document.createElement('div');
        const active = t.otype === _fsOrderType && (t.cexec === null || t.cexec === _fsCondExec);
        item.textContent = t.label;
        item.style.cssText = `padding:7px 16px;font-size:11px;cursor:pointer;color:${active?'#FF9C2E':'#B2B5BE'};font-weight:${active?'700':'400'};`;
        item.onmouseenter = () => item.style.background = '#2A2E39';
        item.onmouseleave = () => item.style.background = '';
        item.onclick = ev => {
            ev.stopPropagation();
            if (t.cexec !== null) {
                _fsCondExec = t.cexec;
                document.getElementById('fs-cond-price-wrap').style.display = t.cexec === 'Limit' ? 'flex' : 'none';
            }
            setOrderType(t.otype);
            const typeEl = document.getElementById('fs-el-type');
            if (typeEl) typeEl.textContent = _entryLabelTypeText();
            if (_tradeSide && _fsExecLine) _createExecLine(_tradeSide === 'Buy');
            menu.remove();
        };
        menu.appendChild(item);
    });
    parent.appendChild(menu);
    setTimeout(() => document.addEventListener('click', () => { menu.remove(); }, { once: true }), 0);
}

function _updateEntryLabelPos() {
    if (!_fsEntryLabel || !_obCandleS || _fsEntryPrice == null) return;
    const y = _obCandleS.priceToCoordinate(_fsEntryPrice);
    if (y == null) return;
    _fsEntryLabel.style.top = y + 'px';
}

function _addEntryTp() {
    if (!_fsEntryPrice || !_tradeSide) return;
    const long = _tradeSide === 'Buy';
    if (_fsTpPrice == null) _fsTpPrice = _fsEntryPrice * (long ? 1.02 : 0.98);
    _drawSlTpLines();
    const el = document.getElementById('fs-el-tp');
    if (el) el.style.display = 'none';
    const ti = document.getElementById('fs-tp-input'); if (ti) ti.value = parseFloat(_fsTpPrice.toPrecision(8));
}

function _addEntrySl() {
    if (!_fsEntryPrice || !_tradeSide) return;
    const long = _tradeSide === 'Buy';
    if (_fsSlPrice == null) _fsSlPrice = _fsEntryPrice * (long ? 0.99 : 1.01);
    _drawSlTpLines();
    const el = document.getElementById('fs-el-sl');
    if (el) el.style.display = 'none';
    const si = document.getElementById('fs-sl-input'); if (si) si.value = parseFloat(_fsSlPrice.toPrecision(8));
}

function _addEntryTr() {
    if (!_tradeSide || !_obCandleS) return;
    const long = _tradeSide === 'Buy';
    const trigInp = document.getElementById('fs-order-trigger');
    const liveP = _obLivePrice || 0;
    const v = parseFloat(trigInp?.value);
    _fsEntryPrice = v > 0 ? v : liveP;
    if (!v && trigInp && liveP) trigInp.value = parseFloat(_fsEntryPrice.toPrecision(8));
    if (_fsEntryLine) { try { _obCandleS.removePriceLine(_fsEntryLine); } catch(e){} }
    _fsEntryLine = _obCandleS.createPriceLine({ price: _fsEntryPrice, color: long ? '#10b981' : '#ef4444', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
    _updateEntryLabelPos();
    const el = document.getElementById('fs-el-tr');
    if (el) el.style.display = 'none';
}

function _startEntryDrag(e) {
    e.preventDefault(); e.stopPropagation();
    if (!_obCandleS || !_tradeSide || _fsOrderType === 'Market') return;
    _dragMode = 'entry';
    if (_dragOverlay) { _dragOverlay.style.pointerEvents = 'all'; _dragOverlay.style.cursor = 'ns-resize'; }
    _entryDragMM = function(ev) {
        if (_dragMode !== 'entry') return;
        ev.preventDefault?.();
        const el = document.getElementById('ob-chart-container');
        if (!el || !_obCandleS) return;
        const rect = el.getBoundingClientRect();
        const y = _evY(ev) - rect.top;
        const newP = _obCandleS.coordinateToPrice(y);
        if (newP == null) return;
        _fsEntryPrice = newP;
        if (_fsEntryLine) { try { _obCandleS.removePriceLine(_fsEntryLine); } catch(ex){} }
        const long = _tradeSide === 'Buy';
        _fsEntryLine = _obCandleS.createPriceLine({ price: _fsEntryPrice, color: long ? '#10b981' : '#ef4444', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
        _updateEntryLabelPos();
        _syncEntryPrice(_fsEntryPrice);
        _showEntryZone();
    };
    document.addEventListener('mousemove', _entryDragMM);
    document.addEventListener('touchmove', _entryDragMM, {passive:false});
}

function _startTriggerDrag(e) {
    e.preventDefault(); e.stopPropagation();
    if (!_obCandleS || !_tradeSide) return;
    _dragMode = 'exec';
    if (_dragOverlay) { _dragOverlay.style.pointerEvents = 'all'; _dragOverlay.style.cursor = 'ns-resize'; }
    _entryDragMM = function(ev) {
        if (_dragMode !== 'exec') return;
        ev.preventDefault?.();
        const el = document.getElementById('ob-chart-container');
        if (!el || !_obCandleS) return;
        const rect = el.getBoundingClientRect();
        const y = _evY(ev) - rect.top;
        const newP = _obCandleS.coordinateToPrice(y);
        if (newP == null) return;
        _fsExecPrice = newP;
        if (_fsExecLine) { try { _obCandleS.removePriceLine(_fsExecLine); } catch(ex){} }
        const long = _tradeSide === 'Buy';
        _fsExecLine = _obCandleS.createPriceLine({ price: _fsExecPrice, color: long ? 'rgba(16,185,129,0.6)' : 'rgba(239,68,68,0.6)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
        const ti = document.getElementById('fs-order-trigger'); if (ti) ti.value = parseFloat(_fsExecPrice.toPrecision(8));
        if (!_fsExecLabel) { _fsExecLabel = _buildExecLabel(); }
        _updateExecLabelPos();
        const trEl = document.getElementById('fs-el-tr'); if (trEl) trEl.style.display = 'none';
    };
    document.addEventListener('mousemove', _entryDragMM);
    document.addEventListener('touchmove', _entryDragMM, {passive:false});
}

async function _cancelOrReset() {
    if (_fsPendingOrderId && _obSymbol) {
        const cancelKey = _fsOrderType === 'Limit' ? 'cancel_limit_q' : 'cancel_cond_q';
        if (!confirm(window.t(cancelKey))) return;
        try {
            const d = await fetch('api/trade/cancel', { method: 'POST', headers: {'Content-Type':'application/json'},
                body: JSON.stringify({ symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'StopOrder' })
            }).then(r => r.json());
            if (d.success || (d.error && d.error.toLowerCase().includes('not exists'))) {
                _showTradeMsg(window.t('order_cancelled'), true);
            } else {
                _showTradeMsg(_bybMsg(d), false); return;
            }
        } catch(e) { _showTradeMsg(window.t('err_net'), false); return; }
    }
    _fsPendingOrderId = null; _fsPendingOrderFilter = null;
    resetTradeSide('cancelOrReset_btn');
    setTimeout(loadTradeData, 800);
}

function resetTradeSide(reason) {
    _tradeSide = null;
    _fsPendingOrderId = null; _fsPendingOrderFilter = null;
    _hadPosition = false; _orderSeenInList = false; _pendingOrderSetAt = 0; _lastAmendAt = 0; _orderMissingCount = 0; _posMissingCount = 0;
    const l = document.getElementById('fs-btn-long'), s = document.getElementById('fs-btn-short');
    if (l) { l.style.background = '#0d1a14'; l.style.color = '#10b981'; l.style.borderColor = '#1e3d2a'; }
    if (s) { s.style.background = '#1a0d0d'; s.style.color = '#ef4444'; s.style.borderColor = '#3d1e1e'; }
    _removeExecLine();
    _removeEntryLine();
    _removeSlTpLines();
    const si = document.getElementById('fs-sl-input'); if (si) si.value = '';
    const ti = document.getElementById('fs-tp-input'); if (ti) ti.value = '';
    _hideTradeZone();
}

function setOrderType(type) {
    _fsOrderType = type;
    if (type === 'Conditional' && !_fsCondExec) _fsCondExec = 'Limit';
    const ids = { Limit: 'fs-ot-limit', Market: 'fs-ot-market' };
    Object.entries(ids).forEach(([t, id]) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.style.color = t === type ? '#FF9C2E' : '#6B7280';
    });
    const condMkt = document.getElementById('fs-ot-cond-mkt');
    const condLim = document.getElementById('fs-ot-cond-lim');
    if (condMkt) condMkt.style.color = (type === 'Conditional' && _fsCondExec === 'Market') ? '#FF9C2E' : '#6B7280';
    if (condLim) condLim.style.color = (type === 'Conditional' && _fsCondExec === 'Limit')  ? '#FF9C2E' : '#6B7280';
    document.getElementById('fs-order-price-wrap').style.display = type === 'Limit' ? 'flex' : 'none';
    document.getElementById('fs-cond-fields').style.display      = type === 'Conditional' ? 'flex' : 'none';
    if (type === 'Conditional') document.getElementById('fs-cond-price-wrap').style.display = _fsCondExec === 'Limit' ? 'flex' : 'none';
    else document.getElementById('fs-cond-price-wrap').style.display = 'none';
    const typeEl = document.getElementById('fs-el-type');
    if (typeEl) typeEl.textContent = _entryLabelTypeText();
    _updateOtDropdownUI();
    if (_tradeSide) setFsSide(_tradeSide);
}

function _updateOtDropdownUI() {
    const label = document.getElementById('fs-ot-dd-label');
    if (label) label.textContent = _entryLabelTypeText();
    const current = _fsOrderType === 'Conditional' ? (_fsCondExec === 'Limit' ? 'CondLim' : 'CondMkt') : _fsOrderType;
    document.querySelectorAll('.fs-ot-opt').forEach(el => {
        const sel = el.dataset.ot === current;
        el.style.color = sel ? '#FF9C2E' : '#6B7280';
        el.style.fontWeight = sel ? '700' : '500';
    });
}

function toggleOtDropdown(e) {
    if (e) e.stopPropagation();
    const panel = document.getElementById('fs-ot-dd-panel');
    const arrow = document.getElementById('fs-ot-dd-arrow');
    const backdrop = document.getElementById('fs-ot-dd-backdrop');
    const isOpen = panel.style.display !== 'none';
    panel.style.display = isOpen ? 'none' : 'block';
    if (backdrop) backdrop.style.display = isOpen ? 'none' : 'block';
    if (arrow) arrow.style.transform = isOpen ? '' : 'rotate(180deg)';
    if (!isOpen) setTimeout(() => document.addEventListener('click', _closeOtOutside, {once:true}), 0);
}

function closeOtDropdown() {
    const panel = document.getElementById('fs-ot-dd-panel');
    const arrow = document.getElementById('fs-ot-dd-arrow');
    const backdrop = document.getElementById('fs-ot-dd-backdrop');
    if (panel) panel.style.display = 'none';
    if (backdrop) backdrop.style.display = 'none';
    if (arrow) arrow.style.transform = '';
}

function _closeOtOutside(e) {
    const wrap = document.getElementById('fs-ot-dd-wrap');
    if (wrap && wrap.contains(e.target)) {
        setTimeout(() => document.addEventListener('click', _closeOtOutside, {once:true}), 0);
        return;
    }
    closeOtDropdown();
}

function openCondTypeDD() {
    document.getElementById('fs-cond-type-dd').style.display = 'block';
    const svg = document.querySelector('#fs-cond-dd-arrow svg');
    if (svg) svg.style.transform = 'rotate(180deg)';
}

function closeCondTypeDD() {
    const dd = document.getElementById('fs-cond-type-dd');
    if (dd) dd.style.display = 'none';
    const svg = document.querySelector('#fs-cond-dd-arrow svg');
    if (svg) svg.style.transform = '';
}

function toggleCondTypeDD(e) {
    if (e) e.stopPropagation();
    const dd = document.getElementById('fs-cond-type-dd');
    dd.style.display !== 'none' ? closeCondTypeDD() : openCondTypeDD();
}

function setCondTypeFromDD(execType) {
    _fsCondExec = execType;
    setOrderType('Conditional');
    _fsCondExec = execType;
    document.getElementById('fs-cond-price-wrap').style.display = execType === 'Limit' ? 'flex' : 'none';
    const typeEl = document.getElementById('fs-el-type');
    if (typeEl) typeEl.textContent = _entryLabelTypeText();
    closeCondTypeDD();
    if (_tradeSide) setFsSide(_tradeSide);
}

function _updateSliderFill(pct) {
    const fill  = document.getElementById('fs-slider-fill');
    const thumb = document.getElementById('fs-slider-thumb');
    const tip   = document.getElementById('fs-slider-tip');
    const sl    = document.getElementById('fs-size-slider');
    if (fill)  fill.style.width = pct + '%';
    if (thumb) thumb.style.left = pct + '%';
    if (tip)   { tip.style.left = pct + '%'; tip.textContent = Math.round(pct) + '%'; }
    if (sl)    sl.value = pct;
    document.querySelectorAll('.fs-sdot').forEach(d => {
        parseFloat(d.dataset.pct) <= pct ? d.classList.add('on') : d.classList.remove('on');
    });
}

function onSizeSliderInput(pct) {
    pct = Math.max(0, Math.min(100, parseFloat(pct) || 0));
    _updateSliderFill(pct);
    const avail = _tradeBalance?.available || 0;
    const lev = parseFloat(document.getElementById('fs-order-lev')?.value) || 1;
    if (!avail) return;
    const size = pct === 0 ? 0 : Math.floor(avail * lev * pct / 100);
    const sEl = document.getElementById('fs-order-size');
    if (sEl) { sEl.value = size || ''; updateQtyPreview(); }
}

function clickSizeStop(pct) { onSizeSliderInput(pct); }

function startSliderDrag(e) {
    e.preventDefault();
    const tip = document.getElementById('fs-slider-tip');
    function getPct(ev) {
        const src = ev.touches ? ev.touches[0] : ev;
        const wrap = document.getElementById('fs-size-slider-wrap');
        if (!wrap) return 0;
        const rect = wrap.getBoundingClientRect();
        return Math.max(0, Math.min(100, (src.clientX - rect.left) / rect.width * 100));
    }
    if (tip) tip.style.display = 'block';
    window._sliderDragging = true;
    const onMove = ev => { ev.preventDefault(); onSizeSliderInput(getPct(ev)); };
    const onUp   = () => {
        window._sliderDragging = false;
        if (tip) tip.style.display = 'none';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('touchmove', onMove);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup',   onUp,   {once:true});
    document.addEventListener('touchmove', onMove, {passive:false});
    document.addEventListener('touchend',  onUp,   {once:true});
    onSizeSliderInput(getPct(e));
}

function updateQtyPreview() {
    const sEl = document.getElementById('fs-order-size'), qEl = document.getElementById('fs-qty-preview');
    if (!sEl || !qEl) return;
    const size = parseFloat(sEl.value) || 0;
    const price = _obLivePrice || 0;
    if (!price || !size) { qEl.textContent = ''; return; }
    let qty = size / price;
    if (_instInfo) {
        const step = parseFloat(_instInfo.qtyStep);
        const dec = Math.max(0, -Math.floor(Math.log10(step)));
        qty = parseFloat((Math.floor(qty / step) * step).toFixed(dec));
        const min = parseFloat(_instInfo.minOrderQty);
        if (qty < min) { qEl.textContent = `Min: ${_instInfo.minOrderQty}`; qEl.style.color = '#ef4444'; return; }
    } else { qty = parseFloat(qty.toPrecision(4)); }
    qEl.textContent = `≈ ${qty}`; qEl.style.color = '#9CA3AF';
    const avail = _tradeBalance?.available || 0;
    const lev2 = parseFloat(document.getElementById('fs-order-lev')?.value) || 1;
    if (avail > 0) { const pct = Math.min(100, size / (avail * lev2) * 100); _updateSliderFill(pct); }
    if (_obCandleS && (_fsSlPrice != null || _fsTpPrice != null)) _drawSlTpLines();
}

async function confirmFsOrder() {
    if (!_obSymbol || !_tradeEnabled) return;
    const size = parseFloat(document.getElementById('fs-order-size')?.value) || 0;
    const lev = parseFloat(document.getElementById('fs-order-lev')?.value) || 10;
    const otype = _fsOrderType;
    const limitP = parseFloat(document.getElementById('fs-order-price')?.value) || 0;
    const triggerP  = parseFloat(document.getElementById('fs-order-trigger')?.value) || 0;
    const condExec  = _fsCondExec;
    const condLimP  = parseFloat(document.getElementById('fs-cond-price')?.value) || 0;
    if (!size) return _showTradeMsg(window.t('enter_size'), false);
    if (otype === 'Limit' && !limitP) return _showTradeMsg(window.t('enter_limit_price'), false);
    if (otype === 'Limit' && _tradeSide === 'Buy' && limitP >= (_obLivePrice || 0))
        return _showTradeMsg(window.t('price_high_warn'), false);
    if (otype === 'Limit' && _tradeSide === 'Sell' && limitP <= (_obLivePrice || 0))
        return _showTradeMsg(window.t('price_low_warn'), false);
    if (otype === 'Conditional' && !triggerP) return _showTradeMsg(window.t('enter_trigger'), false);
    if (otype === 'Conditional' && condExec === 'Limit' && !condLimP) return _showTradeMsg(window.t('enter_exec_price'), false);
    const entryP = otype === 'Limit' ? limitP
                 : otype === 'Conditional' ? (condExec === 'Limit' ? condLimP : triggerP)
                 : (_obLivePrice || 0);
    if (!entryP) return _showTradeMsg(window.t('no_price'), false);
    let qty = size / entryP;
    if (_instInfo) {
        const step = parseFloat(_instInfo.qtyStep);
        const dec = Math.max(0, -Math.floor(Math.log10(step)));
        qty = parseFloat((Math.floor(qty / step) * step).toFixed(dec));
        if (qty < parseFloat(_instInfo.minOrderQty)) return _showTradeMsg(`Qty min: ${_instInfo.minOrderQty}`, false);
    } else { qty = parseFloat(qty.toPrecision(4)); }
    const label = _tradeSide === 'Buy' ? 'LONG' : 'SHORT';
    const typeLabel = otype === 'Conditional'
        ? `Conditional trigger@${triggerP} exec:${condExec}${condExec==='Limit'?' @'+condLimP:''}`
        : `${otype}${otype==='Limit'?' @ '+limitP:''}`;
    const slLine = _fsSlPrice != null ? `\nSL: ${parseFloat(_fsSlPrice.toFixed(8))}` : '';
    const tpLine = _fsTpPrice != null ? `\nTP: ${parseFloat(_fsTpPrice.toFixed(8))}` : '';
    const limitNote = '';
    if (!confirm(`${window.t('confirm_order_q')}\n${label} ${_obSymbol}\n${window.t('ord_type_lbl')}: ${typeLabel}\n${window.t('ord_qty_lbl')}: ${qty}\n${window.t('ord_value_lbl')}: $${size.toFixed(2)}\n${window.t('leva_label')}: ${lev}x${slLine}${tpLine}${limitNote}`)) return;
    try {
        const body = { symbol: _obSymbol, side: _tradeSide, orderType: otype === 'Conditional' ? condExec : otype, qty: String(qty), leverage: lev };
        if (otype === 'Limit') body.price = String(limitP);
        if (otype === 'Conditional') {
            body.triggerPrice = String(triggerP);
            body.triggerDirection = triggerP > (_obLivePrice || 0) ? 1 : 2;
            body.orderFilter = 'StopOrder';
            if (condExec === 'Limit') body.price = String(condLimP);
            // SL/TP not included for conditional orders: Bybit would apply them to any
            // existing open position immediately, ignoring the trigger. Set after fill.
        } else {
            if (_fsSlPrice != null) body.stopLoss = parseFloat(_fsSlPrice.toFixed(8));
            if (_fsTpPrice != null) body.takeProfit = parseFloat(_fsTpPrice.toFixed(8));
        }
        const d = await fetch('api/trade/order', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
        if (d.success) {
            _showTradeMsg(`Order OK (${d.orderId?.slice(0,8)}...)`, true);
            if (otype === 'Market') { resetTradeSide('marketOrder'); }
            else if (otype === 'Limit') { _fsPendingOrderId = d.orderId; _fsPendingOrderFilter = 'Order'; _orderSeenInList = false; _pendingOrderSetAt = Date.now(); }
            else {
                _fsPendingOrderId = d.orderId; _fsPendingOrderFilter = 'StopOrder'; _orderSeenInList = false; _pendingOrderSetAt = Date.now();
                // Auto-amend SL/TP on conditional order (not sent at creation to avoid affecting existing positions)
                if ((_fsSlPrice != null || _fsTpPrice != null) && _obSymbol) {
                    const _oid = d.orderId;
                    setTimeout(async () => {
                        if (_fsPendingOrderId !== _oid) return;
                        const ab = { symbol: _obSymbol, orderId: _oid, orderFilter: 'StopOrder' };
                        if (_fsSlPrice != null) ab.stopLoss = parseFloat(_fsSlPrice.toFixed(8));
                        if (_fsTpPrice != null) ab.takeProfit = parseFloat(_fsTpPrice.toFixed(8));
                        _lastAmendAt = Date.now();
                        const da = await fetch('api/trade/amend', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(ab) }).then(r => r.json());
                        if (!da.success) _showTradeMsg(_bybMsg(da), false);
                    }, 1000);
                }
            }
            setTimeout(loadTradeData, 2000);
        }
        else if (d.error && d.error.toLowerCase().includes('agreement')) { _showAgreementError(_obSymbol); }
        else _showTradeMsg(_bybMsg(d), false);
    } catch(e) { _showTradeMsg(window.t('err_net'), false); }
}

async function closeFsPosition() {
    if (!_tradePos || !_obSymbol) return;
    const p = _tradePos;
    if (!confirm(`${window.t('close_pos_q')} ${p.side === 'Buy' ? 'LONG' : 'SHORT'} ${p.size} ${_obSymbol}?`)) return;
    try {
        const d = await fetch('api/trade/close', { method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ symbol: _obSymbol, side: p.side === 'Buy' ? 'Sell' : 'Buy', qty: String(p.size) }) }).then(r => r.json());
        if (d.success) { _showTradeMsg(window.t('position_closed'), true); _tradePos = null; resetTradeSide(); const row = document.getElementById('fs-pos-row'); if (row) row.style.display = 'none'; setTimeout(loadTradeData, 2000); }
        else _showTradeMsg(_bybMsg(d), false);
    } catch(e) { _showTradeMsg(window.t('err_net'), false); }
}

let _pricePickHandler = null;

function startPricePick(inputId) {
    if (_pricePickTarget) _endPricePick();
    if (!_obChart || !_obCandleS) return;
    _pricePickTarget = inputId;
    // hint
    const chartWrap = document.getElementById('ob-chart-container');
    const hint = document.createElement('div');
    hint.id = 'fs-pick-hint';
    hint.textContent = window.t('price_pick_hint');
    hint.style.cssText = 'position:absolute;bottom:10px;left:50%;transform:translateX(-50%);background:rgba(16,16,20,.95);color:#FF9C2E;font-size:11px;padding:5px 14px;border-radius:4px;z-index:20;pointer-events:none;white-space:nowrap;border:1px solid #f59e0b;';
    if (chartWrap) chartWrap.appendChild(hint);
    document.querySelectorAll('.fs-aim-btn').forEach(b => b.style.opacity = b.dataset.target === inputId ? '1' : '0.25');
    _pricePickHandler = (param) => {
        if (!param?.point) return;
        const price = _obCandleS.coordinateToPrice(param.point.y);
        if (price != null) {
            const inp = document.getElementById(_pricePickTarget);
            if (inp) {
                inp.value = parseFloat(price.toPrecision(8));
                inp.dispatchEvent(new Event('input'));
            }
        }
        _endPricePick();
    };
    _obChart.subscribeClick(_pricePickHandler);
}

function _endPricePick() {
    if (_obChart && _pricePickHandler) { try { _obChart.unsubscribeClick(_pricePickHandler); } catch(e){} }
    _pricePickHandler = null;
    _pricePickTarget = null;
    document.getElementById('fs-pick-hint')?.remove();
    document.querySelectorAll('.fs-aim-btn').forEach(b => b.style.opacity = '0.6');
}

document.addEventListener('contextmenu', e => { const inp = e.target; if (inp.tagName === 'INPUT' && inp.step === 'any' && inp.closest('#ob-trade-panel, #fs-trade-panel')) { e.preventDefault(); inp.value = ''; inp.dispatchEvent(new Event('input')); } });

const _BYBIT_CODES = {
    10001: { it:'Parametro non valido',              en:'Invalid parameter' },
    10002: { it:'Richieste troppo frequenti',        en:'Too many requests' },
    10006: { it:'IP non autorizzato',                en:'IP not allowed' },
    110001:{ it:'Ordine non trovato',                en:'Order not found' },
    110004:{ it:'Saldo insufficiente',               en:'Insufficient balance' },
    110007:{ it:'Margine insufficiente',             en:'Insufficient margin' },
    110008:{ it:'Quantità non valida',               en:'Invalid quantity' },
    110009:{ it:'Prezzo non valido',                 en:'Invalid price' },
    110012:{ it:'Saldo disponibile insufficiente',   en:'Insufficient available balance' },
    110013:{ it:'Limite di rischio superato',        en:'Risk limit exceeded' },
    110017:{ it:'Ordine reduce-only rifiutato',      en:'Reduce-only order rejected' },
    110025:{ it:'Quantità supera la posizione',      en:'Qty exceeds position size' },
    110043:{ it:'Ordine già eseguito',               en:'Order already triggered' },
    110064:{ it:'Troppo tardi per modificare',       en:'Too late to amend' },
    110065:{ it:'Nessuna posizione aperta',          en:'No open position' },
    110070:{ it:'Margine insufficiente',             en:'Insufficient margin' },
    110073:{ it:'Prezzo troppo distante dal mercato',en:'Price deviation too large' },
    110074:{ it:'Ordine già cancellato',             en:'Order already cancelled' },
    131001:{ it:'Saldo disponibile insufficiente',   en:'Insufficient available balance' },
    131002:{ it:'Saldo disponibile insufficiente',   en:'Insufficient available balance' },
};
function _bybMsg(d) {
    const lang = window._cspLang || 'en';
    if (d && d.code) {
        if (d.code === 10001 && d.error) return d.error; // show raw Bybit message for generic param error
        const entry = _BYBIT_CODES[d.code];
        if (entry) return entry[lang] || entry.en;
    }
    // Pattern matching for dynamic Bybit messages
    const err = d && d.error;
    if (err) {
        let m;
        m = err.match(/minimum order value\s+(\S+)/i);
        if (m) return lang === 'it'
            ? `Valore dell'ordine inferiore al minimo (${m[1]})`
            : `Order value below minimum (${m[1]})`;
        m = err.match(/minimum order qty\s+(\S+)/i);
        if (m) return lang === 'it'
            ? `Quantità inferiore al minimo (${m[1]})`
            : `Qty below minimum (${m[1]})`;
        m = err.match(/qty step\s+(\S+)/i);
        if (m) return lang === 'it'
            ? `Quantità non multipla del lotto minimo (${m[1]})`
            : `Qty not a multiple of lot step (${m[1]})`;
        m = err.match(/maximum order qty\s+(\S+)/i);
        if (m) return lang === 'it'
            ? `Quantità supera il massimo consentito (${m[1]})`
            : `Qty exceeds maximum (${m[1]})`;
        return err;
    }
    return window.t('err_order');
}
function fmtPrice(p) {
    if (p >= 10000) return p.toLocaleString('en-US', {maximumFractionDigits:1});
    if (p >= 1)     return p.toFixed(3);
    if (p >= 0.01)  return p.toFixed(5);
    return p.toFixed(7);
}
function _showTradeZone(kind) {
    if (!_obCandleS || _fsEntryPrice == null) return;
    const price = kind === 'tp' ? _fsTpPrice : _fsSlPrice;
    if (price == null) return;
    const canvas = document.getElementById('ob-trade-zone-canvas');
    if (!canvas) return;
    canvas.width  = canvas.clientWidth  || canvas.parentElement?.clientWidth  || 100;
    canvas.height = canvas.clientHeight || canvas.parentElement?.clientHeight || 100;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const y1 = _obCandleS.priceToCoordinate(_fsEntryPrice);
    const y2 = _obCandleS.priceToCoordinate(price);
    if (y1 == null || y2 == null) return;
    const long = _tradeSide === 'Buy';
    const isInvalid = kind === 'tp'
        ? (long ? price <= _fsEntryPrice : price >= _fsEntryPrice)
        : (long ? price >= _fsEntryPrice : price <= _fsEntryPrice);
    const isProfit = !isInvalid && kind === 'tp';
    ctx.fillStyle = isInvalid ? 'rgba(239,68,68,0.25)' : (isProfit ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.15)');
    const top = Math.min(y1, y2);
    const h   = Math.max(Math.abs(y2 - y1), 1);
    ctx.fillRect(0, top, canvas.width, h);
    canvas.style.display = 'block';
}
function _hideTradeZone() {
    const canvas = document.getElementById('ob-trade-zone-canvas');
    if (!canvas) return;
    canvas.style.display = 'none';
}
function _showEntryZone() {
    if (!_obCandleS || _fsEntryPrice == null) return;
    const canvas = document.getElementById('ob-trade-zone-canvas');
    if (!canvas) return;
    canvas.width  = canvas.clientWidth  || canvas.parentElement?.clientWidth  || 100;
    canvas.height = canvas.clientHeight || canvas.parentElement?.clientHeight || 100;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const y1 = _obCandleS.priceToCoordinate(_fsEntryPrice);
    if (y1 == null) return;
    const long = _tradeSide === 'Buy';
    if (_fsTpPrice != null) {
        const y2 = _obCandleS.priceToCoordinate(_fsTpPrice);
        if (y2 != null) {
            const isProfit = (long && _fsTpPrice > _fsEntryPrice) || (!long && _fsTpPrice < _fsEntryPrice);
            ctx.fillStyle = isProfit ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.15)';
            ctx.fillRect(0, Math.min(y1, y2), canvas.width, Math.max(Math.abs(y2 - y1), 1));
        }
    }
    if (_fsSlPrice != null) {
        const y2 = _obCandleS.priceToCoordinate(_fsSlPrice);
        if (y2 != null) {
            const isLoss = (long && _fsSlPrice < _fsEntryPrice) || (!long && _fsSlPrice > _fsEntryPrice);
            ctx.fillStyle = isLoss ? 'rgba(239,68,68,0.15)' : 'rgba(16,185,129,0.15)';
            ctx.fillRect(0, Math.min(y1, y2), canvas.width, Math.max(Math.abs(y2 - y1), 1));
        }
    }
    canvas.style.display = 'block';
}
function _showTradeMsg(msg, ok) {
    const el = document.getElementById('fs-trade-msg');
    if (!el) return;
    el.textContent = (ok ? '✓ ' : '⚠ ') + msg; el.style.color = ok ? '#10b981' : '#ef4444'; el.style.display = 'block';
    setTimeout(() => el.style.display = 'none', 4000);
}
function _showAgreementError(symbol) {
    const el = document.getElementById('fs-trade-msg');
    if (!el) return;
    el.innerHTML = `⚠ Agreement required &mdash; <a href="https://www.bybit.com/trade/usdt/${symbol}" target="_blank" style="color:#f59e0b;text-decoration:underline;">Sign on Bybit &rarr;</a>`;
    el.style.color = '#ef4444'; el.style.display = 'block';
    setTimeout(() => el.style.display = 'none', 15000);
}

// ── DRAWING TOOLS ─────────────────────────────────────────────────────────────
let _obRangeActive = false, _obRangeP1 = null, _obRangeMD = null, _obRangeMM = null, _obRangeMU = null, _obRangeCM = null;
let _obRangeHoverLine = null, _obRangeHoverMM = null, _obRangeHoverML = null;
let _obHlineActive = false, _obHlines = [], _obHlineMD = null, _obHlineMM = null, _obHlineMU = null, _obHlineCM = null, _obHlineDragging = null;
let _obHlineHoverLine = null, _obHlineHoverMM = null, _obHlineHoverML = null;
let _obTrendActive = false, _obTrendlines = [], _obTrendP1 = null, _obTrendPrev = null;
let _obTrendMD = null, _obTrendMM = null, _obTrendMU = null, _obTrendCM = null, _obTrendDrag = null, _obTrendRAF = null;
let _obTrendHoverLine = null;

function _drawRangeCanvas(canvas, series, p1, p2) {
    canvas.width  = canvas.clientWidth  || canvas.parentElement?.clientWidth  || 100;
    canvas.height = canvas.clientHeight || canvas.parentElement?.clientHeight || 100;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (p1 === null || p2 === null) return;
    let y1 = series.priceToCoordinate(p1);
    let y2 = series.priceToCoordinate(p2);
    if (y1 === null) y1 = p2 > p1 ? canvas.height : 0;
    if (y2 === null) y2 = p2 > p1 ? 0 : canvas.height;
    const top = Math.min(y1, y2), bottom = Math.max(y1, y2), h = Math.max(bottom - top, 1);
    const isUp = p2 > p1, color = isUp ? '#20B26C' : '#EF454A';
    ctx.fillStyle = isUp ? 'rgba(8,153,129,0.12)' : 'rgba(242,54,69,0.12)';
    ctx.fillRect(0, top, canvas.width, h);
    ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash([5,3]);
    ctx.beginPath(); ctx.moveTo(0, y1); ctx.lineTo(canvas.width, y1); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, y2); ctx.lineTo(canvas.width, y2); ctx.stroke();
    ctx.setLineDash([]);
    const delta = p2 - p1, pct = (delta / p1) * 100, sign = delta >= 0 ? '+' : '';
    const absDelta = Math.abs(delta);
    const dStr = absDelta >= 100 ? delta.toFixed(1) : absDelta >= 1 ? delta.toFixed(2) : delta.toFixed(4);
    const label = `${sign}${dStr}  (${sign}${pct.toFixed(2)}%)`;
    const fs = Math.max(9, Math.min(12, Math.floor(h * 0.35)));
    ctx.font = `bold ${fs}px monospace`;
    const tw = ctx.measureText(label).width, pad = 4;
    const lx = canvas.width - tw - pad * 2 - 6, ly = top + h / 2;
    ctx.fillStyle = isUp ? 'rgba(8,153,129,0.88)' : 'rgba(242,54,69,0.88)';
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(lx - pad, ly - fs, tw + pad * 2, fs + 6, 3)
                  : ctx.rect(lx - pad, ly - fs, tw + pad * 2, fs + 6);
    ctx.fill();
    ctx.fillStyle = '#ffffff'; ctx.fillText(label, lx, ly);
}

function _syncObClearBtn() {
    const b = document.getElementById('ob-clear-draw-btn');
    if (b) b.style.display = (_obHlines.length || _obTrendlines.length) ? '' : 'none';
}

function clearObDrawings() {
    if (!_obCandleS) return;
    for (const hl of _obHlines) { try { _obCandleS.removePriceLine(hl.priceLine); } catch(e) {} }
    _obHlines = [];
    for (const tl of _obTrendlines) {
        if (tl.pl1) try { _obCandleS.removePriceLine(tl.pl1); } catch(e) {}
        if (tl.pl2) try { _obCandleS.removePriceLine(tl.pl2); } catch(e) {}
    }
    _obTrendlines = [];
    try { _obTrendDrawAll(); } catch(e) {}
}

function toggleObRange() {
    if (!_obRangeActive) { if (_obHlineActive) toggleObHline(); if (_obTrendActive) toggleObTrend(); }
    _obRangeActive = !_obRangeActive;
    const btn = document.getElementById('ob-range-btn');
    const canvas = document.getElementById('ob-range-canvas');
    if (!btn || !canvas) return;
    btn.style.color = _obRangeActive ? '#f59e0b' : '#B2B5BE';
    canvas.style.display = _obRangeActive ? 'block' : 'none';
    if (_obRangeMD) { canvas.removeEventListener('mousedown', _obRangeMD); _obRangeMD = null; }
    if (_obRangeMM) { document.removeEventListener('mousemove', _obRangeMM); _obRangeMM = null; }
    if (_obRangeMU) { document.removeEventListener('mouseup', _obRangeMU); _obRangeMU = null; }
    if (_obRangeCM) { canvas.removeEventListener('contextmenu', _obRangeCM); _obRangeCM = null; }
    if (_obRangeHoverMM) { canvas.removeEventListener('mousemove', _obRangeHoverMM); _obRangeHoverMM = null; }
    if (_obRangeHoverML) { canvas.removeEventListener('mouseleave', _obRangeHoverML); _obRangeHoverML = null; }
    if (_obRangeHoverLine) { try { _obCandleS.removePriceLine(_obRangeHoverLine); } catch(e) {} _obRangeHoverLine = null; }
    _obRangeP1 = null;
    try { const rc = canvas.getContext('2d'); rc.clearRect(0,0,canvas.width,canvas.height); } catch(e) {}
    canvas.style.pointerEvents = _obRangeActive ? 'auto' : 'none';
    canvas.style.cursor = _obRangeActive ? 'crosshair' : '';
    if (_obRangeActive) {
        _obRangeMD = e => {
            if (e.button !== 0) return; e.preventDefault();
            const rect = canvas.getBoundingClientRect();
            const price = _obCandleS?.coordinateToPrice(e.clientY - rect.top);
            if (price == null) return;
            _obRangeP1 = price;
        };
        _obRangeMM = e => {
            if (_obRangeP1 === null || !(e.buttons & 1)) return;
            const rect = canvas.getBoundingClientRect();
            const p2 = _obCandleS?.coordinateToPrice(e.clientY - rect.top);
            if (p2 == null) return;
            _drawRangeCanvas(canvas, _obCandleS, _obRangeP1, p2);
        };
        _obRangeMU = e => {
            if (e.button !== 0) return;
            _obRangeP1 = null;
            try { const rc = canvas.getContext('2d'); rc.clearRect(0,0,canvas.width,canvas.height); } catch(e) {}
        };
        _obRangeCM = e => { e.preventDefault(); toggleObRange(); };
        canvas.addEventListener('mousedown', _obRangeMD);
        document.addEventListener('mousemove', _obRangeMM);
        document.addEventListener('mouseup', _obRangeMU);
        canvas.addEventListener('contextmenu', _obRangeCM);
        _obRangeHoverMM = e => {
            if (!_obCandleS) return;
            const rect = canvas.getBoundingClientRect();
            const price = _obCandleS.coordinateToPrice(e.clientY - rect.top);
            if (price == null) return;
            if (_obRangeHoverLine) {
                try { _obRangeHoverLine.applyOptions({ price }); } catch(ex) {}
            } else {
                try { _obRangeHoverLine = _obCandleS.createPriceLine({ price, color: '#f59e0b', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' }); } catch(ex) {}
            }
        };
        _obRangeHoverML = () => {
            if (_obRangeHoverLine) { try { _obCandleS.removePriceLine(_obRangeHoverLine); } catch(e) {} _obRangeHoverLine = null; }
        };
        canvas.addEventListener('mousemove', _obRangeHoverMM);
        canvas.addEventListener('mouseleave', _obRangeHoverML);
    }
}

function _obHlineNearest(clientY) {
    const canvas = document.getElementById('ob-hline-canvas');
    if (!canvas || !_obCandleS) return null;
    const rect = canvas.getBoundingClientRect(), y = clientY - rect.top;
    let best = null, bestDist = 8;
    for (const hl of _obHlines) {
        try { const hy = _obCandleS.priceToCoordinate(hl.price); if (hy != null && Math.abs(hy - y) < bestDist) { bestDist = Math.abs(hy - y); best = hl; } } catch(e) {}
    }
    return best;
}

function toggleObHline() {
    if (!_obHlineActive) { if (_obRangeActive) toggleObRange(); if (_obTrendActive) toggleObTrend(); }
    _obHlineActive = !_obHlineActive;
    const btn = document.getElementById('ob-hline-btn');
    const canvas = document.getElementById('ob-hline-canvas');
    if (!btn || !canvas) return;
    btn.style.color = _obHlineActive ? '#3b82f6' : '#B2B5BE';
    if (_obHlineMD) { canvas.removeEventListener('mousedown', _obHlineMD); _obHlineMD = null; }
    if (_obHlineMM) { document.removeEventListener('mousemove', _obHlineMM); _obHlineMM = null; }
    if (_obHlineMU) { document.removeEventListener('mouseup', _obHlineMU); _obHlineMU = null; }
    if (_obHlineCM) { canvas.removeEventListener('contextmenu', _obHlineCM); _obHlineCM = null; }
    if (_obHlineHoverMM) { canvas.removeEventListener('mousemove', _obHlineHoverMM); _obHlineHoverMM = null; }
    if (_obHlineHoverML) { canvas.removeEventListener('mouseleave', _obHlineHoverML); _obHlineHoverML = null; }
    if (_obHlineHoverLine) { try { _obCandleS?.removePriceLine(_obHlineHoverLine); } catch(e) {} _obHlineHoverLine = null; }
    _obHlineDragging = null;
    canvas.style.pointerEvents = _obHlineActive ? 'auto' : 'none';
    canvas.style.cursor = _obHlineActive ? 'crosshair' : '';
    if (_obHlineActive) {
        _obHlineMD = e => {
            if (e.button !== 0) return; e.preventDefault();
            const nearest = _obHlineNearest(e.clientY);
            if (nearest) { _obHlineDragging = nearest; canvas.style.cursor = 'ns-resize'; }
            else {
                const rect = canvas.getBoundingClientRect();
                const price = _obCandleS?.coordinateToPrice(e.clientY - rect.top);
                if (price != null) { const pl = _obCandleS.createPriceLine({ price, color: '#3b82f6', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' }); _obHlines.push({ priceLine: pl, price }); _syncObClearBtn(); }
            }
        };
        _obHlineMM = e => {
            if (_obHlineDragging) {
                const rect = canvas.getBoundingClientRect();
                const price = _obCandleS?.coordinateToPrice(e.clientY - rect.top);
                if (price != null) { _obHlineDragging.price = price; try { _obHlineDragging.priceLine.applyOptions({ price }); } catch(ex) {} }
            } else {
                canvas.style.cursor = _obHlineNearest(e.clientY) ? 'ns-resize' : 'crosshair';
            }
        };
        _obHlineMU = e => {
            if (e.button !== 0) return;
            _obHlineDragging = null;
            canvas.style.cursor = _obHlineNearest(e.clientY) ? 'ns-resize' : 'crosshair';
        };
        _obHlineCM = e => {
            e.preventDefault();
            const nearest = _obHlineNearest(e.clientY);
            if (nearest) { try { _obCandleS.removePriceLine(nearest.priceLine); } catch(ex) {} _obHlines = _obHlines.filter(h => h !== nearest); _syncObClearBtn(); }
            else toggleObHline();
        };
        canvas.addEventListener('mousedown', _obHlineMD);
        document.addEventListener('mousemove', _obHlineMM);
        document.addEventListener('mouseup', _obHlineMU);
        canvas.addEventListener('contextmenu', _obHlineCM);
        _obHlineHoverMM = e => {
            if (_obHlineDragging || !_obCandleS) return;
            const rect = canvas.getBoundingClientRect();
            const price = _obCandleS.coordinateToPrice(e.clientY - rect.top);
            if (price == null) return;
            if (_obHlineHoverLine) { try { _obHlineHoverLine.applyOptions({ price }); } catch(ex) {} }
            else { try { _obHlineHoverLine = _obCandleS.createPriceLine({ price, color: '#3b82f6', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' }); } catch(ex) {} }
        };
        _obHlineHoverML = () => {
            if (_obHlineHoverLine) { try { _obCandleS.removePriceLine(_obHlineHoverLine); } catch(e) {} _obHlineHoverLine = null; }
        };
        canvas.addEventListener('mousemove', _obHlineHoverMM);
        canvas.addEventListener('mouseleave', _obHlineHoverML);
    } else {
        if (_obHlineHoverLine) { try { _obCandleS?.removePriceLine(_obHlineHoverLine); } catch(e) {} _obHlineHoverLine = null; }
    }
}

const _OB_TREND_HIT = 8;

function _obTrendSync(canvas) { const w = canvas.clientWidth, h = canvas.clientHeight; if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; } }

function _obTrendPriceTag(ctx, x, y, price, W, H) {
    if (y < 0 || y > H) return;
    const label = typeof fmtPrice === 'function' ? fmtPrice(price) : price.toPrecision(6);
    ctx.save();
    ctx.font = '10px -apple-system,BlinkMacSystemFont,sans-serif';
    const tw = ctx.measureText(label).width, pad = 4, bh = 14;
    const bx = Math.min(x + 6, W - tw - pad * 2 - 2);
    const by = y - bh / 2;
    ctx.fillStyle = '#22c55e';
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(bx, by, tw + pad * 2, bh, 2); else ctx.rect(bx, by, tw + pad * 2, bh);
    ctx.fill();
    ctx.fillStyle = '#000';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, bx + pad, y);
    ctx.restore();
}

function _obTrendDrawAll() {
    _syncObClearBtn();
    const canvas = document.getElementById('ob-trend-canvas');
    if (!canvas || !_obCandleS || !_obChart) return;
    _obTrendSync(canvas);
    const ctx = canvas.getContext('2d'), W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    for (const tl of _obTrendlines) {
        try {
            const x1 = _obChart.timeScale().timeToCoordinate(tl.t1), y1 = _obCandleS.priceToCoordinate(tl.p1);
            const x2 = _obChart.timeScale().timeToCoordinate(tl.t2), y2 = _obCandleS.priceToCoordinate(tl.p2);
            if (x1==null||y1==null||x2==null||y2==null) continue;
            ctx.strokeStyle = '#22c55e'; ctx.lineWidth = 1.5; ctx.beginPath();
            if (Math.abs(x2-x1) < 0.5) { ctx.moveTo(x1,0); ctx.lineTo(x1,H); }
            else { const m=(y2-y1)/(x2-x1); ctx.moveTo(0,y1+m*-x1); ctx.lineTo(W,y1+m*(W-x1)); }
            ctx.stroke();
            ctx.fillStyle = '#22c55e';
            ctx.beginPath(); ctx.arc(x1,y1,4,0,Math.PI*2); ctx.fill();
            ctx.beginPath(); ctx.arc(x2,y2,4,0,Math.PI*2); ctx.fill();
            if (y1 >= 0 && y1 <= H) _obTrendPriceTag(ctx, x1, y1, tl.p1, W, H);
            if (y2 >= 0 && y2 <= H) _obTrendPriceTag(ctx, x2, y2, tl.p2, W, H);
        } catch(ex) {}
    }
    if (_obTrendP1 && _obTrendPrev) {
        try {
            const x1 = _obChart.timeScale().timeToCoordinate(_obTrendP1.t), y1 = _obCandleS.priceToCoordinate(_obTrendP1.p);
            if (x1!=null && y1!=null) {
                ctx.strokeStyle = '#22c55e'; ctx.lineWidth = 1; ctx.setLineDash([5,3]);
                ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(_obTrendPrev.x, _obTrendPrev.y); ctx.stroke();
                ctx.setLineDash([]);
                ctx.fillStyle = '#22c55e'; ctx.beginPath(); ctx.arc(x1,y1,4,0,Math.PI*2); ctx.fill();
                if (y1 >= 0 && y1 <= H) _obTrendPriceTag(ctx, x1, y1, _obTrendP1.p, W, H);
            }
        } catch(ex) {}
    }
}

function _obTrendNearest(cx, cy) {
    const canvas = document.getElementById('ob-trend-canvas');
    if (!canvas || !_obCandleS || !_obChart) return null;
    const rect = canvas.getBoundingClientRect(), mx = cx-rect.left, my = cy-rect.top;
    let best = null, bestDist = _OB_TREND_HIT, bestPart = 'line';
    for (const tl of _obTrendlines) {
        try {
            const x1 = _obChart.timeScale().timeToCoordinate(tl.t1), y1 = _obCandleS.priceToCoordinate(tl.p1);
            const x2 = _obChart.timeScale().timeToCoordinate(tl.t2), y2 = _obCandleS.priceToCoordinate(tl.p2);
            if (x1==null||y1==null||x2==null||y2==null) continue;
            const d1=Math.hypot(mx-x1,my-y1), d2=Math.hypot(mx-x2,my-y2);
            const dx=x2-x1, dy=y2-y1, L2=dx*dx+dy*dy;
            const dl = L2 ? Math.hypot(mx-x1-Math.max(0,Math.min(1,((mx-x1)*dx+(my-y1)*dy)/L2))*dx, my-y1-Math.max(0,Math.min(1,((mx-x1)*dx+(my-y1)*dy)/L2))*dy) : Math.hypot(mx-x1,my-y1);
            if (d1 <= _OB_TREND_HIT && d1 < bestDist) { best=tl; bestDist=d1; bestPart='p1'; }
            else if (d2 <= _OB_TREND_HIT && d2 < bestDist) { best=tl; bestDist=d2; bestPart='p2'; }
            else if (dl < bestDist) { best=tl; bestDist=dl; bestPart='line'; }
        } catch(ex) {}
    }
    return best ? { tl: best, part: bestPart } : null;
}

// Ridisegna a ogni frame invece che sul solo evento visibleTimeRangeChange:
// il drag della price-scale e il pan non generano quell'evento in modo
// continuo, quindi le linee restavano ferme o disallineate dalle candele.
function _obTrendEnsureRAF() {
    if (_obTrendRAF) return;
    const tick = () => {
        if (!_obChart || !_obCandleS || (!_obTrendlines.length && !_obTrendActive)) { _obTrendRAF = null; return; }
        _obTrendDrawAll();
        _obTrendRAF = requestAnimationFrame(tick);
    };
    _obTrendRAF = requestAnimationFrame(tick);
}

function toggleObTrend() {
    if (!_obTrendActive) { if (_obRangeActive) toggleObRange(); if (_obHlineActive) toggleObHline(); }
    _obTrendActive = !_obTrendActive;
    const btn = document.getElementById('ob-trend-btn');
    const canvas = document.getElementById('ob-trend-canvas');
    if (!btn || !canvas) return;
    btn.style.color = _obTrendActive ? '#22c55e' : '#B2B5BE';
    if (_obTrendMD) { canvas.removeEventListener('mousedown', _obTrendMD); _obTrendMD = null; }
    if (_obTrendMM) { document.removeEventListener('mousemove', _obTrendMM); _obTrendMM = null; }
    if (_obTrendMU) { document.removeEventListener('mouseup', _obTrendMU); _obTrendMU = null; }
    if (_obTrendCM) { canvas.removeEventListener('contextmenu', _obTrendCM); _obTrendCM = null; }
    _obTrendP1 = null; _obTrendPrev = null; _obTrendDrag = null;
    canvas.style.pointerEvents = _obTrendActive ? 'auto' : 'none';
    canvas.style.cursor = _obTrendActive ? 'crosshair' : '';
    _obTrendEnsureRAF();
    _obTrendDrawAll();
    if (!_obTrendActive) return;
    _obTrendMD = e => {
        if (e.button !== 0) return; e.preventDefault();
        const rect = canvas.getBoundingClientRect(), px = e.clientX-rect.left, py = e.clientY-rect.top;
        const hit = _obTrendNearest(e.clientX, e.clientY);
        if (hit && !_obTrendP1) {
            if (hit.part !== 'line') { _obTrendDrag = { tl: hit.tl, part: hit.part }; canvas.style.cursor = 'crosshair'; }
            else {
                const ox1 = _obChart.timeScale().timeToCoordinate(hit.tl.t1), oy1 = _obCandleS.priceToCoordinate(hit.tl.p1);
                const ox2 = _obChart.timeScale().timeToCoordinate(hit.tl.t2), oy2 = _obCandleS.priceToCoordinate(hit.tl.p2);
                _obTrendDrag = { tl: hit.tl, part: 'line', sx: px, sy: py, ox1, oy1, ox2, oy2 };
                canvas.style.cursor = 'move';
            }
        } else if (!_obTrendP1) {
            const t = _obChart.timeScale().coordinateToTime(px), p = _obCandleS.coordinateToPrice(py);
            if (t != null && p != null) _obTrendP1 = { t, p };
        } else {
            const t2 = _obChart.timeScale().coordinateToTime(px), p2 = _obCandleS.coordinateToPrice(py);
            if (t2 != null && p2 != null) {
                let pl1, pl2;
                try { pl1 = _obCandleS.createPriceLine({ price: _obTrendP1.p, color: '#22c55e', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' }); } catch(ex) {}
                try { pl2 = _obCandleS.createPriceLine({ price: p2,           color: '#22c55e', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' }); } catch(ex) {}
                _obTrendlines.push({ t1: _obTrendP1.t, p1: _obTrendP1.p, t2, p2, pl1, pl2 });
            }
            _obTrendP1 = null; _obTrendPrev = null;
            if (_obTrendHoverLine) { try { _obCandleS?.removePriceLine(_obTrendHoverLine); } catch(e) {} _obTrendHoverLine = null; }
            _obTrendDrawAll();
        }
    };
    _obTrendMM = e => {
        const rect = canvas.getBoundingClientRect(), px = e.clientX-rect.left, py = e.clientY-rect.top;
        if (_obTrendDrag) {
            if (!(e.buttons & 1)) { _obTrendDrag = null; return; }
            const { tl, part } = _obTrendDrag;
            if (part === 'p1') { const t=_obChart.timeScale().coordinateToTime(px), p=_obCandleS.coordinateToPrice(py); if(t&&p){tl.t1=t;tl.p1=p; if(tl.pl1) try{tl.pl1.applyOptions({price:p});}catch(ex){}} }
            else if (part === 'p2') { const t=_obChart.timeScale().coordinateToTime(px), p=_obCandleS.coordinateToPrice(py); if(t&&p){tl.t2=t;tl.p2=p; if(tl.pl2) try{tl.pl2.applyOptions({price:p});}catch(ex){}} }
            else { const dpx=px-_obTrendDrag.sx, dpy=py-_obTrendDrag.sy; const nt1=_obChart.timeScale().coordinateToTime(_obTrendDrag.ox1+dpx), np1=_obCandleS.coordinateToPrice(_obTrendDrag.oy1+dpy); const nt2=_obChart.timeScale().coordinateToTime(_obTrendDrag.ox2+dpx), np2=_obCandleS.coordinateToPrice(_obTrendDrag.oy2+dpy); if(nt1&&np1&&nt2&&np2){tl.t1=nt1;tl.p1=np1;tl.t2=nt2;tl.p2=np2; if(tl.pl1) try{tl.pl1.applyOptions({price:np1});}catch(ex){} if(tl.pl2) try{tl.pl2.applyOptions({price:np2});}catch(ex){}} }
            _obTrendDrawAll();
        } else if (_obTrendP1) {
            _obTrendPrev = { x: px, y: py };
            const hp = _obCandleS?.coordinateToPrice(py);
            if (hp != null) {
                if (_obTrendHoverLine) { try { _obTrendHoverLine.applyOptions({ price: hp }); } catch(ex) {} }
                else { try { _obTrendHoverLine = _obCandleS.createPriceLine({ price: hp, color: '#22c55e', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' }); } catch(ex) {} }
            }
            _obTrendDrawAll();
        } else {
            if (_obTrendHoverLine) { try { _obCandleS?.removePriceLine(_obTrendHoverLine); } catch(e) {} _obTrendHoverLine = null; }
            const hit = _obTrendNearest(e.clientX, e.clientY);
            canvas.style.cursor = hit ? (hit.part === 'line' ? 'move' : 'crosshair') : 'crosshair';
        }
    };
    _obTrendMU = e => { if (e.button !== 0) return; _obTrendDrag = null; canvas.style.cursor = 'crosshair'; };
    _obTrendCM = e => {
        e.preventDefault();
        if (_obTrendP1) { _obTrendP1 = null; _obTrendPrev = null; if (_obTrendHoverLine) { try { _obCandleS?.removePriceLine(_obTrendHoverLine); } catch(e) {} _obTrendHoverLine = null; } _obTrendDrawAll(); return; }
        const hit = _obTrendNearest(e.clientX, e.clientY);
        if (hit) { if(hit.tl.pl1) try{_obCandleS.removePriceLine(hit.tl.pl1);}catch(e){} if(hit.tl.pl2) try{_obCandleS.removePriceLine(hit.tl.pl2);}catch(e){} _obTrendlines = _obTrendlines.filter(tl => tl !== hit.tl); _obTrendDrawAll(); }
        else toggleObTrend();
    };
    canvas.addEventListener('mousedown', _obTrendMD);
    document.addEventListener('mousemove', _obTrendMM);
    document.addEventListener('mouseup', _obTrendMU);
    canvas.addEventListener('contextmenu', _obTrendCM);
}

// ── Vue App ───────────────────────────────────────────────────────────────────
createApp({
    setup() {
        const t = (key) => window.t ? window.t(key) : key;

        const urlParams = new URLSearchParams(window.location.search);
        const symbol    = ref(urlParams.get('symbol') || 'BTCUSDT');
        const isStandalone = ref(window.parent === window);

        const symBase  = computed(() => symbol.value.replace(/USDT$|USDC$|BUSD$|DAI$/, ''));
        const symQuote = computed(() => { const m = symbol.value.match(/(USDT|USDC|BUSD|DAI)$/); return m ? m[1] : 'USDT'; });

        // ── ticker state ──────────────────────────────────────────────────────
        const ticker = ref({ change: null, vol: '' });

        // ── chart state ───────────────────────────────────────────────────────
        const chartContainerEl = ref(null);
        const chartTF  = ref('60');
        const ohlc     = ref({ o: '', h: '', l: '', c: '', pct: '', color: '#9ca3af' });

        let obChart   = null;
        let candleS   = null;
        const emaS    = {};
        const lastEMA = {};
        let chartPollTimer     = null;
        let _cdLastPrice = null, _cdLastOpen = null;
        let _cdRepaintTimer = null;
        let obKlineWS = null, obKlineWSTimer = null, _obTzOffset = 0, _obLiveCandle = null;
        const openMtf = () => window.open('mtf?symbol=' + symbol.value, '_blank');

        const tfCountdowns = ref({});
        const _CD_TF_WARN = new Set(['30','60','240']);
        const _updateTfCountdowns = () => {
            const obj = {};
            for (const tf of ['1','5','30','60','240','D']) {
                const rem = _obCdRemain(tf);
                let warn = false, blink = false;
                if (tf === '1')      { warn = rem <= 10; blink = warn; }
                else if (tf === '5') { warn = rem <= 60; blink = warn; }
                else if (tf === '30' || tf === '60') { warn = rem <= 300; blink = rem <= 60; }
                else if (_CD_TF_WARN.has(tf)) { warn = rem <= 300; }
                obj[tf] = { text: _obCdFmt(rem), color: warn ? '#FF9C2E' : '#F3F4F6', blink };
            }
            tfCountdowns.value = obj;
        };
        let lastConfirmedTime  = 0;
        let obKlineCount = 0;
        let hoverPriceLine = null;
        let obAskLine = null;
        let obBidLine = null;
        let dayHighLine = null, dayLowLine = null, prevHighLine = null, prevLowLine = null;
        let _dayHighPrice = null, _dayLowPrice = null, _prevHighPrice = null, _prevLowPrice = null;
        const showObLines   = ref(false);
        const nakedChart    = ref(false);
        const showTradePanel = ref(true);

        const _clearDayLines = () => {
            for (const ref of [dayHighLine, dayLowLine, prevHighLine, prevLowLine])
                if (ref && candleS) try { candleS.removePriceLine(ref); } catch(e) {}
            dayHighLine = dayLowLine = prevHighLine = prevLowLine = null;
        };

        // ── book state ────────────────────────────────────────────────────────
        const _isPortraitMobile = window.matchMedia('(max-width: 640px), (pointer: coarse)').matches;
        const displayLevels  = ref(_isPortraitMobile ? 10 : 20);
        const grouping       = ref(0);
        const groupingOptions = ref([]);
        const levelsDdOpen    = ref(false);
        const groupingDdOpen  = ref(false);
        const selectLevels = (val) => { displayLevels.value = val; levelsDdOpen.value = false; updateDisplay(); };
        const selectGrouping = (val) => { grouping.value = val; groupingDdOpen.value = false; updateDisplay(); };
        const displayAsks    = ref([]);
        const displayBids    = ref([]);
        const currentPrice   = ref('0.00');
        const spread         = ref('0.00');
        const priceColor     = ref('#9ca3af');
        const loading        = ref(true);
        const error          = ref('');
        const pressure       = ref({ score: 50, label: '—', color: '#6B7280', long: 50, short: 50 });
        const isPaused       = ref(false);
        const showBook       = ref(true);

        const maxLevelDistance = ref({
            askPrice: 0, askPercent: '0.00',
            bidPrice: 0, bidPercent: '0.00'
        });


        const asksMap = new Map();
        const bidsMap = new Map();
        let bookWS = null;
        let reconnectTimer = null;

        // ============================
        //  TICKER
        // ============================
        const fetchTicker = async () => {
            try {
                const r = await fetch(`api/ticker?symbol=${symbol.value}`);
                const d = await r.json();
                if (d.success) {
                    ticker.value = {
                        change: d.change_24h,
                        vol: fmtVol(d.volume_24h),
                    };
                }
            } catch (e) { /* silent */ }
        };

        // ============================
        //  CHART
        // ============================
        const initChart = () => {
            if (!chartContainerEl.value || obChart) return;

            obChart  = makeOBChart(chartContainerEl.value);
            candleS  = addSeries(obChart, 'CandlestickSeries', {
                upColor: '#20B26C', downColor: '#EF454A',
                borderVisible: false, wickUpColor: '#20B26C', wickDownColor: '#EF454A',
            });
            try {
                const _cdSlot = { get curTF() { return chartTF.value; }, get lastPrice() { return _cdLastPrice; }, get lastOpen() { return _cdLastOpen; } };
                candleS.attachPrimitive(new _OBCountdownPrimitive(_cdSlot));
                const _obLvlSlot = { get obActive() { return showObLines.value; }, get obBidVal() { return maxLevelDistance.value.bidPrice; }, get obAskVal() { return maxLevelDistance.value.askPrice; } };
                candleS.attachPrimitive(new _ObBandFillPrimitive(_obLvlSlot));
                candleS.attachPrimitive(new _VolProfilePrimitive(() => _obKlines));
            } catch(e) {}
            const lineBase = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
            for (const { p, color, width, style } of EMA_CFG)
                emaS[p] = addSeries(obChart, 'LineSeries', { ...lineBase, color, lineWidth: width + 0.5, lineStyle: style ?? 0 });

            const _resetChartView = () => { if (obKlineCount) { const n = DEFAULT_CANDLES[chartTF.value]||80; obChart.timeScale().setVisibleLogicalRange({ from: Math.max(0, obKlineCount-n), to: obKlineCount+3 }); obChart.priceScale('right').applyOptions({ autoScale: true }); } };
            obChart.subscribeDblClick(_resetChartView);
            obChart.subscribeCrosshairMove(param => {
                if (param && param.point && param.point.y > 0 && param.seriesData && candleS) {
                    const cd = param.seriesData.get(candleS);
                    if (cd && cd.open != null) {
                        const pct   = ((cd.close - cd.open) / cd.open) * 100;
                        const color = pct >= 0 ? '#10b981' : '#ef4444';
                        ohlc.value  = {
                            o: formatPrice(cd.open),
                            h: formatPrice(cd.high),
                            l: formatPrice(cd.low),
                            c: formatPrice(cd.close),
                            pct: (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%',
                            color,
                        };
                        return;
                    }
                }
                ohlc.value = { o: '', h: '', l: '', c: '', pct: '', color: '#9ca3af' };
            });

            loadChartData(chartTF.value);
            // expose to module-level trade functions and drawing tools
            _obCandleS = candleS;
            _obChart   = obChart;
            _obSymbol  = symbol.value;
            if (_obTrendlines.length || _obTrendActive) _obTrendEnsureRAF();
            initSlTpDrag();
        };

        const loadChartData = async (tf) => {
            if (!candleS) return;
            try {
                const r = await fetch(`api/klines?symbol=${symbol.value}&interval=${tf}`);
                const j = await r.json();
                if (!j.success || !j.data || !j.data.length) return;
                const klines = j.data;
                if (j.utc_offset_s != null) _obTzOffset = j.utc_offset_s;

                candleS.setData(klines);
                if (klines.length) { _cdLastPrice = klines[klines.length-1].close; _cdLastOpen = klines[klines.length-1].open; _obLiveCandle = { ...klines[klines.length-1] }; }
                obKlineCount = klines.length;
                _obKlines = klines;
                if (_obBbActive) _obApplyBB();
                candleS.applyOptions({ priceFormat: getPriceFormat(klines[klines.length - 1]?.close) });

                for (const { p } of EMA_CFG) {
                    const ema = calcEMA(klines, p);
                    emaS[p].setData(ema);
                    lastEMA[p] = ema[ema.length - 1].value;
                }
                if (nakedChart.value)
                    for (const { p } of EMA_CFG)
                        if (emaS[p]) try { emaS[p].applyOptions({ visible: false }); } catch(e) {}
                lastConfirmedTime = klines[klines.length - 1].time;

                const _TF_N = {'1':120,'5':100,'15':80,'30':80,'60':80,'240':60,'D':50,'W':52,'M':24};
                const _rn = _TF_N[tf] || 80;
                const _applyRange = () => { if (obChart) obChart.timeScale().setVisibleLogicalRange({ from: Math.max(0, obKlineCount - _rn), to: obKlineCount + 3 }); };
                _applyRange();
                requestAnimationFrame(_applyRange);
                if (obChart) obChart.priceScale('right').applyOptions({ autoScale: true });

                // Day / Prev H/L lines
                _clearDayLines();
                _dayHighPrice = _dayLowPrice = _prevHighPrice = _prevLowPrice = null;
                try {
                    const rd = await fetch(`api/klines?symbol=${symbol.value}&interval=D`);
                    const jd = await rd.json();
                    if (jd.success && jd.data && jd.data.length) {
                        const lv = getLvCfg();
                        const today = jd.data[jd.data.length - 1];
                        _dayHighPrice = today.high;
                        _dayLowPrice  = today.low;
                        if (!nakedChart.value) {
                            if (lv.dayHigh.vis?.ob !== false) dayHighLine = candleS.createPriceLine({price:today.high, color:lv.dayHigh.color, lineWidth:lv.dayHigh.width, lineStyle:lv.dayHigh.style, axisLabelVisible:false, title:''});
                            if (lv.dayLow.vis?.ob  !== false) dayLowLine  = candleS.createPriceLine({price:today.low,  color:lv.dayLow.color,  lineWidth:lv.dayLow.width,  lineStyle:lv.dayLow.style,  axisLabelVisible:false, title:''});
                        }
                        if (jd.data.length >= 2) {
                            const prev = jd.data[jd.data.length - 2];
                            _prevHighPrice = prev.high;
                            _prevLowPrice  = prev.low;
                            if (!nakedChart.value) {
                                if (lv.prevHigh.vis?.ob !== false) prevHighLine = candleS.createPriceLine({price:prev.high, color:lv.prevHigh.color, lineWidth:lv.prevHigh.width, lineStyle:lv.prevHigh.style, axisLabelVisible:false, title:''});
                                if (lv.prevLow.vis?.ob  !== false) prevLowLine  = candleS.createPriceLine({price:prev.low,  color:lv.prevLow.color,  lineWidth:lv.prevLow.width,  lineStyle:lv.prevLow.style,  axisLabelVisible:false, title:''});
                            }
                        }
                    }
                } catch(e) {}

                startChartPolling(tf);
                startKlineWS(tf);
            } catch (e) { console.error('Chart load error:', e); }
        };

        // WS kline diretto (stesso topic di chart.html/mtf.html): la candela in
        // formazione segue il prezzo tick-by-tick invece di aspettare il poll REST (3s).
        const startKlineWS = (tf) => {
            if (obKlineWS) { try { obKlineWS.close(); } catch(e) {} obKlineWS = null; }
            clearTimeout(obKlineWSTimer);
            obKlineWS = new WebSocket('wss://stream.bybit.com/v5/public/linear');
            obKlineWS.onopen = () => {
                if (obKlineWS) obKlineWS.send(JSON.stringify({ op: 'subscribe', args: [`kline.${tf}.${symbol.value}`] }));
            };
            obKlineWS.onmessage = (event) => {
                if (!candleS) return;
                let msg; try { msg = JSON.parse(event.data); } catch(e) { return; }
                if (!msg.topic || !msg.topic.startsWith('kline.')) return;
                const b = msg.data && msg.data[0];
                if (!b) return;
                const candle = {
                    time:   Math.floor(parseInt(b.start) / 1000) + _obTzOffset,
                    open:   parseFloat(b.open), high: parseFloat(b.high),
                    low:    parseFloat(b.low),  close: parseFloat(b.close),
                    volume: parseFloat(b.volume),
                };
                try { candleS.update(candle); } catch(e) {}
                _cdLastPrice = candle.close; _cdLastOpen = candle.open;
                _obLiveCandle = { ...candle };
            };
            obKlineWS.onerror = () => {};
            obKlineWS.onclose = () => { obKlineWSTimer = setTimeout(() => startKlineWS(chartTF.value), 4000); };
        };

        const stopKlineWS = () => {
            clearTimeout(obKlineWSTimer);
            if (obKlineWS) { try { obKlineWS.close(); } catch(e) {} obKlineWS = null; }
        };

        const startChartPolling = (tf) => {
            stopChartPolling();
            const poll = async () => {
                if (!candleS) return;
                try {
                    const r = await fetch(`api/klines/live?symbol=${symbol.value}&interval=${tf}`);
                    const j = await r.json();
                    if (j.success && j.data && j.data.length) {
                        const candles = j.data;
                        const last    = candles[candles.length - 1];
                        const prev    = candles[candles.length - 2];
                        _cdLastPrice = last.close; _cdLastOpen = last.open; _obLiveCandle = { ...last };
                        // Update the last two candles (current forming + previous if just confirmed)
                        for (const k of candles.slice(-2)) {
                            try { candleS.update(k); } catch(e) {}
                        }
                        // Update EMAs: if a new candle started, lock in the previous EMA
                        if (prev && prev.time > lastConfirmedTime) {
                            for (const { p } of EMA_CFG) {
                                if (lastEMA[p] == null) continue;
                                const ek = 2 / (p + 1);
                                lastEMA[p] = prev.close * ek + lastEMA[p] * (1 - ek);
                            }
                            lastConfirmedTime = prev.time;
                        }
                        // Live EMA for current forming candle
                        for (const { p } of EMA_CFG) {
                            if (lastEMA[p] == null) continue;
                            const ek   = 2 / (p + 1);
                            const live = last.close * ek + lastEMA[p] * (1 - ek);
                            try { emaS[p].update({ time: last.time, value: live }); } catch(e) {}
                        }
                        // Keep _obKlines in sync (rolling window per il ricalcolo BB)
                        if (_obKlines.length) {
                            const lastK = _obKlines[_obKlines.length - 1];
                            if (lastK.time === last.time) _obKlines[_obKlines.length - 1] = { ...last };
                            else _obKlines.push({ ...last });
                        }
                        if (_obBbActive && _obBbSeries.upper) {
                            const bbCfg = getBbCfg();
                            if (_obKlines.length >= bbCfg.period) {
                                const sl = _obKlines.slice(-bbCfg.period);
                                const mean = sl.reduce((a, c) => a + c.close, 0) / bbCfg.period;
                                const std  = Math.sqrt(sl.reduce((a, c) => a + (c.close - mean) ** 2, 0) / bbCfg.period);
                                _obBbSeries.upper.update({ time: last.time, value: mean + bbCfg.mult * std });
                                _obBbSeries.mid.update(  { time: last.time, value: mean });
                                _obBbSeries.lower.update({ time: last.time, value: mean - bbCfg.mult * std });
                            }
                        }
                    }
                } catch(e) {}
                chartPollTimer = setTimeout(poll, 3000);
            };
            poll();
        };

        const stopChartPolling = () => {
            clearTimeout(chartPollTimer);
            chartPollTimer = null;
        };

        const changeChartTF = (tf) => {
            if (tf === chartTF.value || !candleS) return;
            chartTF.value = tf;
            stopChartPolling();
            _clearDayLines();
            candleS.setData([]);
            for (const { p } of EMA_CFG) { emaS[p].setData([]); lastEMA[p] = null; }
            for (const k of ['upper', 'mid', 'lower']) if (_obBbSeries[k]) try { _obBbSeries[k].setData([]); } catch(e) {}
            ohlc.value = { o: '', h: '', l: '', c: '', pct: '', color: '#9ca3af' };
            loadChartData(tf);
        };

        // ============================
        //  GROUPING
        // ============================
        const calculateGroupingOptions = (price) => {
            if (price >= 10000) {
                groupingOptions.value = [0.1, 0.5, 1, 2, 5, 10];
                if (grouping.value === 0) grouping.value = 0.5;
            } else if (price >= 1000) {
                groupingOptions.value = [0.01, 0.05, 0.1, 0.5, 1, 2];
                if (grouping.value === 0) grouping.value = 0.05;
            } else if (price >= 100) {
                groupingOptions.value = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5];
                if (grouping.value === 0) grouping.value = 0.02;
            } else if (price >= 10) {
                groupingOptions.value = [0.001, 0.005, 0.01, 0.02, 0.05, 0.1];
                if (grouping.value === 0) grouping.value = 0.005;
            } else if (price >= 1) {
                groupingOptions.value = [0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01];
                if (grouping.value === 0) grouping.value = 0.0005;
            } else if (price >= 0.01) {
                groupingOptions.value = [0.00001, 0.00005, 0.0001, 0.0002, 0.0005, 0.001];
                if (grouping.value === 0) grouping.value = 0.00005;
            } else if (price >= 0.0001) {
                groupingOptions.value = [0.000001, 0.000005, 0.00001, 0.00002, 0.00005, 0.0001];
                if (grouping.value === 0) grouping.value = 0.000005;
            } else {
                groupingOptions.value = [0.0000001, 0.0000005, 0.000001, 0.000002, 0.000005, 0.00001];
                if (grouping.value === 0) grouping.value = 0.0000005;
            }
        };

        const groupLevels = (levelsMap, tickSize, isAsk = false) => {
            const grouped = new Map();
            levelsMap.forEach((amount, price) => {
                const groupedPrice = isAsk
                    ? Math.ceil(price / tickSize) * tickSize
                    : Math.floor(price / tickSize) * tickSize;
                const roundedPrice = Math.round(groupedPrice * 1e10) / 1e10;
                const existing = grouped.get(roundedPrice) || 0;
                grouped.set(roundedPrice, existing + amount);
            });
            return grouped;
        };

        const generateGroupedLevels = (levelsMap, tickSize, numLevels, isAsk) => {
            const grouped = groupLevels(levelsMap, tickSize, isAsk);
            const prices  = Array.from(levelsMap.keys());
            if (prices.length === 0) return [];
            const bestRawPrice = isAsk ? Math.min(...prices) : Math.max(...prices);
            const bestGroupedPrice = isAsk
                ? Math.ceil(bestRawPrice / tickSize) * tickSize
                : Math.floor(bestRawPrice / tickSize) * tickSize;
            const result = [];
            for (let i = 0; i < numLevels; i++) {
                const price = isAsk
                    ? Math.round((bestGroupedPrice + (i * tickSize)) * 1e10) / 1e10
                    : Math.round((bestGroupedPrice - (i * tickSize)) * 1e10) / 1e10;
                const amount = grouped.get(price) || 0;
                result.push([price, amount]);
            }
            return result;
        };

        // ============================
        //  DISPLAY & RENDER
        // ============================
        const updateDisplay = () => {
            const levels = parseInt(displayLevels.value);
            const tick   = parseFloat(grouping.value);
            let asksArray, bidsArray;
            if (tick > 0) {
                asksArray = generateGroupedLevels(asksMap, tick, levels, true);
                bidsArray = generateGroupedLevels(bidsMap, tick, levels, false);
            } else {
                asksArray = Array.from(asksMap.entries()).sort((a, b) => a[0] - b[0]).slice(0, levels);
                bidsArray = Array.from(bidsMap.entries()).sort((a, b) => b[0] - a[0]).slice(0, levels);
            }
            renderLevels(asksArray, bidsArray);
        };

        const renderLevels = (asksArray, bidsArray) => {
            const asksWithAmount = asksArray.filter(([p, a]) => a > 0);
            const bidsWithAmount = bidsArray.filter(([p, a]) => a > 0);
            const maxAsk = asksWithAmount.length > 0 ? Math.max(...asksWithAmount.map(([p, a]) => a)) : 1;
            const maxBid = bidsWithAmount.length > 0 ? Math.max(...bidsWithAmount.map(([p, a]) => a)) : 1;
            const maxAskLevel = asksWithAmount.find(([p, a]) => a === maxAsk);
            const maxBidLevel = bidsWithAmount.find(([p, a]) => a === maxBid);

            if (asksMap.size > 0 && bidsMap.size > 0) {
                const lowestAsk  = Math.min(...asksMap.keys());
                const highestBid = Math.max(...bidsMap.keys());
                const midPrice   = (lowestAsk + highestBid) / 2;
                if (maxAskLevel && midPrice > 0) {
                    maxLevelDistance.value.askPrice   = maxAskLevel[0];
                    maxLevelDistance.value.askPercent = (((maxAskLevel[0] - midPrice) / midPrice) * 100).toFixed(2);
                }
                if (maxBidLevel && midPrice > 0) {
                    maxLevelDistance.value.bidPrice   = maxBidLevel[0];
                    maxLevelDistance.value.bidPercent = (((midPrice - maxBidLevel[0]) / midPrice) * 100).toFixed(2);
                }
                if (window.parent !== window) {
                    window.parent.postMessage({
                        type: 'ob_levels',
                        bid: maxBidLevel ? maxBidLevel[0] : null,
                        ask: maxAskLevel ? maxAskLevel[0] : null,
                    }, '*');
                }
            }

            const asksReversed = [...asksArray].reverse();
            let cumAsk = 0;
            const askCum = new Array(asksReversed.length);
            for (let i = asksReversed.length - 1; i >= 0; i--) {
                cumAsk += asksReversed[i][0] * asksReversed[i][1];
                askCum[i] = cumAsk;
            }
            displayAsks.value = asksReversed.map(([price, amount], i) => ({
                price:        formatPrice(price),
                rawPrice:     price,
                amount:       amount > 0 ? fmtQty(amount) : '-',
                total:        amount > 0 ? fmtTotal(price * amount) : '-',
                cumTotal:     amount > 0 ? fmtTotal(askCum[i]) : '-',
                depthPercent: amount > 0 ? (amount / maxAsk) * 100 : 0,
                isMaxLevel:   amount === maxAsk && amount > 0,
                isEmpty:      amount === 0,
            }));

            let cumBid = 0;
            displayBids.value = bidsArray.map(([price, amount]) => {
                cumBid += price * amount;
                return {
                    price:        formatPrice(price),
                    rawPrice:     price,
                    amount:       amount > 0 ? fmtQty(amount) : '-',
                    total:        amount > 0 ? fmtTotal(price * amount) : '-',
                    cumTotal:     amount > 0 ? fmtTotal(cumBid) : '-',
                    depthPercent: amount > 0 ? (amount / maxBid) * 100 : 0,
                    isMaxLevel:   amount === maxBid && amount > 0,
                    isEmpty:      amount === 0,
                };
            });

            updateObLines();
            calcPressure(asksArray, bidsArray, maxAsk, maxBid);
        };

        const calcPressure = (asksArray, bidsArray, maxAsk, maxBid) => {
            const mid = _obLivePrice || 0;
            if (!mid || !asksArray.length || !bidsArray.length) return;

            // 1. OBI — volume imbalance (peso 40%)
            const bidVol = bidsArray.reduce((s, [, a]) => s + a, 0);
            const askVol = asksArray.reduce((s, [, a]) => s + a, 0);
            const obi = (bidVol - askVol) / (bidVol + askVol); // -1..+1

            // 2. Depth ratio — USDT cumulativo bid vs ask (peso 30%)
            const bidUsdt = bidsArray.reduce((s, [p, a]) => s + p * a, 0);
            const askUsdt = asksArray.reduce((s, [p, a]) => s + p * a, 0);
            const depthRatio = (bidUsdt - askUsdt) / (bidUsdt + askUsdt); // -1..+1

            // 3. Wall factor — wall più grande: distanza e lato (peso 20%)
            const maxBidLevel = bidsArray.find(([, a]) => a === maxBid);
            const maxAskLevel = asksArray.find(([, a]) => a === maxAsk);
            const bidWallDist = maxBidLevel ? (mid - maxBidLevel[0]) / mid : 0.05;
            const askWallDist = maxAskLevel ? (maxAskLevel[0] - mid) / mid : 0.05;
            const wallBias = maxBid * bidWallDist > maxAsk * askWallDist ? 1 : -1;
            const wallStrength = Math.min(Math.abs(maxBid - maxAsk) / (maxBid + maxAsk), 1);
            const wallScore = wallBias * wallStrength; // -1..+1

            // 4. OHLC trend — bias dal TF selezionato (peso 10%)
            const tfBias = (_cdLastPrice != null && _cdLastOpen != null)
                ? Math.max(-1, Math.min(1, (_cdLastPrice - _cdLastOpen) / (_cdLastOpen * 0.02)))
                : 0;

            // Pesi
            const raw = obi * 0.40 + depthRatio * 0.30 + wallScore * 0.20 + tfBias * 0.10;
            const score = Math.round(((raw + 1) / 2) * 100); // 0-100

            const long  = score;
            const short = 100 - score;
            let label, color;
            if (score >= 65)      { label = 'LONG';    color = '#10b981'; }
            else if (score <= 35) { label = 'SHORT';   color = '#ef4444'; }
            else                  { label = 'NEUTRO';  color = '#f59e0b'; }

            const fmtK = v => v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1000 ? (v / 1000).toFixed(1) + 'K' : v.toFixed(0);
            pressure.value = { score, label, color, long, short,
                bidK: fmtK(bidUsdt), askK: fmtK(askUsdt) };
        };

        const fmtQty = (q) => {
            if (q >= 1000) return Math.round(q).toLocaleString('en-US');
            if (q >= 1)    return parseFloat(q.toFixed(2)).toString();
            return parseFloat(q.toFixed(4)).toString();
        };
        const fmtTotal = (t) => {
            if (t >= 1e6)  return (t / 1e6).toFixed(1) + 'M';
            if (t >= 1000) return (t / 1000).toFixed(1) + 'K';
            if (t >= 1)    return parseFloat(t.toFixed(2)).toString();
            return parseFloat(t.toFixed(4)).toString();
        };
        const formatPrice = (price) => {
            if (price >= 10000)    return price.toFixed(1);
            if (price >= 1000)     return price.toFixed(2);
            if (price >= 100)      return price.toFixed(3);
            if (price >= 10)       return price.toFixed(4);
            if (price >= 1)        return price.toFixed(4);
            if (price >= 0.1)      return price.toFixed(5);
            if (price >= 0.01)     return price.toFixed(6);
            if (price >= 0.001)    return price.toFixed(7);
            if (price >= 0.0001)   return price.toFixed(8);
            if (price >= 0.00001)  return price.toFixed(9);
            if (price >= 0.000001) return price.toFixed(10);
            return price.toPrecision(6);
        };

        // Se l'orologio locale ha superato il boundary della candela in formazione,
        // ne apre subito una nuova lato client invece di aspettare il WS kline
        // (trade-driven: silenzioso se il mercato è calmo) o il poll REST (ogni 3s).
        // WS kline / poll REST arrivano comunque poco dopo e "correggono" i valori
        // sintetici con quelli ufficiali (stesso pattern del patch mid-price sotto).
        const _obRollNewBarIfNeeded = (midPrice) => {
            if (!_obLiveCandle || !candleS) return false;
            const tf = chartTF.value;
            const barSecs = _OB_TF_SECS[tf] || (tf === 'D' ? 86400 : 0);
            if (!barSecs) return false;
            const nowAdj = Math.floor(Date.now() / 1000) + _obTzOffset;
            const expectedOpen = Math.floor(nowAdj / barSecs) * barSecs;
            if (expectedOpen <= _obLiveCandle.time) return false;

            const prevClose = _obLiveCandle.close;
            if (_obLiveCandle.time > lastConfirmedTime) {
                for (const { p } of EMA_CFG) {
                    if (lastEMA[p] == null) continue;
                    const ek = 2 / (p + 1);
                    lastEMA[p] = prevClose * ek + lastEMA[p] * (1 - ek);
                }
                lastConfirmedTime = _obLiveCandle.time;
            }
            if (_obKlines.length) {
                const lastK = _obKlines[_obKlines.length - 1];
                if (lastK.time === _obLiveCandle.time) _obKlines[_obKlines.length - 1] = { ..._obLiveCandle };
                else _obKlines.push({ ..._obLiveCandle });
            }

            const newCandle = {
                time: expectedOpen, open: prevClose,
                high: Math.max(prevClose, midPrice), low: Math.min(prevClose, midPrice),
                close: midPrice, volume: 0,
            };
            _obLiveCandle = newCandle;
            try { candleS.update({ ...newCandle }); } catch(e) {}
            _cdLastPrice = newCandle.close; _cdLastOpen = newCandle.open;

            for (const { p } of EMA_CFG) {
                if (lastEMA[p] == null) continue;
                const ek   = 2 / (p + 1);
                const live = newCandle.close * ek + lastEMA[p] * (1 - ek);
                try { emaS[p].update({ time: newCandle.time, value: live }); } catch(e) {}
            }
            if (_obBbActive && _obBbSeries.upper && _obKlines.length) {
                const bbCfg = getBbCfg();
                const sl = [..._obKlines.slice(-(bbCfg.period - 1)), newCandle];
                if (sl.length >= bbCfg.period) {
                    const mean = sl.reduce((a, c) => a + c.close, 0) / bbCfg.period;
                    const std  = Math.sqrt(sl.reduce((a, c) => a + (c.close - mean) ** 2, 0) / bbCfg.period);
                    _obBbSeries.upper.update({ time: newCandle.time, value: mean + bbCfg.mult * std });
                    _obBbSeries.mid.update(  { time: newCandle.time, value: mean });
                    _obBbSeries.lower.update({ time: newCandle.time, value: mean - bbCfg.mult * std });
                }
            }
            return true;
        };

        // ============================
        //  ORDER BOOK FETCH & WS
        // ============================
        const fetchOrderBook = async () => {
            loading.value = true;
            error.value   = '';
            try {
                const response = await fetch(
                    `https://api.bybit.com/v5/market/orderbook?category=linear&symbol=${symbol.value}&limit=200`
                );
                const data = await response.json();
                if (data.retCode === 0 && data.result) {
                    processOrderBook(data.result, true);
                    loading.value = false;
                    if (!bookWS) connectBookWS();
                    if (!tradeWS) connectTradeWS();
                } else {
                    throw new Error('Failed to fetch order book');
                }
            } catch (err) {
                error.value   = 'Errore nel caricamento dell\'Order Book';
                loading.value = false;
                console.error(err);
            }
        };

        const processOrderBook = (data, isSnapshot = false) => {
            const rawAsks = data.a || [];
            const rawBids = data.b || [];
            if (isSnapshot) { asksMap.clear(); bidsMap.clear(); }

            rawAsks.forEach(([priceStr, amountStr]) => {
                const price  = parseFloat(priceStr);
                const amount = parseFloat(amountStr);
                if (amount === 0) asksMap.delete(price);
                else asksMap.set(price, amount);
            });

            rawBids.forEach(([priceStr, amountStr]) => {
                const price  = parseFloat(priceStr);
                const amount = parseFloat(amountStr);
                if (amount === 0) bidsMap.delete(price);
                else bidsMap.set(price, amount);
            });

            if (asksMap.size > 0 && bidsMap.size > 0) {
                const lowestAsk  = Math.min(...asksMap.keys());
                const highestBid = Math.max(...bidsMap.keys());
                const midPrice   = (lowestAsk + highestBid) / 2;
                const spreadValue   = lowestAsk - highestBid;
                const spreadPercent = (spreadValue / midPrice) * 100;
                currentPrice.value  = formatPrice(midPrice);
                _obLivePrice        = midPrice;
                spread.value        = `${formatPrice(spreadValue)} (${spreadPercent.toFixed(3)}%)`;
                if (groupingOptions.value.length === 0) calculateGroupingOptions(midPrice);
                // Bybit throttla lo stream kline a ~1/s: la candela in formazione segue
                // anche il mid-price del book (molto più frequente) per restare allineata.
                if (_obLiveCandle && candleS && !_obRollNewBarIfNeeded(midPrice)) {
                    _obLiveCandle.close = midPrice;
                    if (midPrice > _obLiveCandle.high) _obLiveCandle.high = midPrice;
                    if (midPrice < _obLiveCandle.low)  _obLiveCandle.low  = midPrice;
                    try { candleS.update({ ..._obLiveCandle }); } catch(e) {}
                    _cdLastPrice = _obLiveCandle.close;
                }
            }

            updateDisplay();
        };

        const setWSDot = (state) => {
            const colors = { connecting:'#F59E0B', live:'#10b981', error:'#ef4444', off:'#6B7280' };
            const d = document.getElementById('ws-dot');
            if (d) { d.style.background = colors[state] || colors.off; d.style.animation = state === 'live' ? 'pulse 2s cubic-bezier(.4,0,.6,1) infinite' : ''; }
        };

        // ============================
        //  CVD — Cumulative Volume Delta
        // ============================
        let tradeWS = null, tradeReconnTimer = null;
        const cvdWindow  = ref('60');   // minuti finestra
        const cvdBuffer  = [];          // { ts, delta }
        const cvdData    = ref({ score: 50, pct: 0, dir: '—', color: '#6B7280', spark: [] });
        const CVD_WINDOWS = [
            { label: '1m',  value: '1'    },
            { label: '5m',  value: '5'    },
            { label: '30m', value: '30'   },
            { label: '1h',  value: '60'   },
            { label: '4h',  value: '240'  },
            { label: 'D',   value: '1440' },
        ];

        const cvdTrim = () => {
            const cutoff = Date.now() - parseInt(cvdWindow.value) * 60000;
            while (cvdBuffer.length && cvdBuffer[0].ts < cutoff) cvdBuffer.shift();
        };

        const cvdCalc = () => {
            cvdTrim();
            if (!cvdBuffer.length) return;
            const windowMs = parseInt(cvdWindow.value) * 60000;
            const now = Date.now();

            // Sparkline: divide window in 30 buckets
            const buckets = 30;
            const bucketMs = windowMs / buckets;
            const bucketArr = new Array(buckets).fill(0);
            for (const { ts, delta } of cvdBuffer) {
                const idx = Math.min(buckets - 1, Math.floor((ts - (now - windowMs)) / bucketMs));
                if (idx >= 0) bucketArr[idx] += delta;
            }
            // Cumulative sum for sparkline
            const spark = [];
            let cum = 0;
            for (const v of bucketArr) { cum += v; spark.push(cum); }

            const total = cvdBuffer.reduce((s, { delta }) => s + delta, 0);
            const totalVol = cvdBuffer.reduce((s, { delta }) => s + Math.abs(delta), 0);
            const pct = totalVol > 0 ? (total / totalVol) * 100 : 0;
            const score = Math.round(((pct + 100) / 200) * 100); // 0-100

            let dir, color;
            if (pct > 10)       { dir = '▲ LONG';  color = '#10b981'; }
            else if (pct < -10) { dir = '▼ SHORT'; color = '#ef4444'; }
            else                { dir = '● NEUTRO'; color = '#f59e0b'; }

            cvdData.value = { score, pct: pct.toFixed(1), dir, color, spark };
        };

        const connectTradeWS = () => {
            if (tradeWS) { tradeWS.close(); tradeWS = null; }
            tradeWS = new WebSocket('wss://stream.bybit.com/v5/public/linear');
            tradeWS.onopen = () => {
                tradeWS.send(JSON.stringify({ op: 'subscribe', args: [`publicTrade.${symbol.value}`] }));
            };
            tradeWS.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (!data.data || !Array.isArray(data.data)) return;
                    for (const t of data.data) {
                        const vol = parseFloat(t.v);
                        const delta = t.S === 'Buy' ? vol : -vol;
                        cvdBuffer.push({ ts: t.T, delta });
                    }
                    cvdCalc();
                } catch(e) {}
            };
            tradeWS.onerror = () => {};
            tradeWS.onclose = () => { tradeReconnTimer = setTimeout(connectTradeWS, 4000); };
        };

        watch(cvdWindow, () => { cvdBuffer.length = 0; cvdCalc(); });

        const connectBookWS = () => {
            setWSDot('connecting');
            bookWS = new WebSocket('wss://stream.bybit.com/v5/public/linear');
            bookWS.onopen = () => {
                bookWS.send(JSON.stringify({ op: 'subscribe', args: [`orderbook.200.${symbol.value}`] }));
                setWSDot('live');
            };
            bookWS.onmessage = (event) => {
                if (isPaused.value) return;
                try {
                    const data = JSON.parse(event.data);
                    if (data.topic && data.topic.startsWith('orderbook') && data.data) {
                        processOrderBook(data.data, data.type === 'snapshot');
                        if (data.data.u) priceColor.value = data.data.u === 'U' ? '#10b981' : '#ef4444';
                    }
                } catch (err) { console.error(err); }
            };
            bookWS.onerror  = (err) => { console.error('OB WS error:', err); setWSDot('error'); };
            bookWS.onclose  = () => { setWSDot('off'); reconnectTimer = setTimeout(connectBookWS, 3000); };
        };

        // ============================
        //  HOVER PRICE LINE
        // ============================
        const setHoverLine = (price, color) => {
            if (!obChart || !candleS) return;
            if (hoverPriceLine) {
                try { candleS.removePriceLine(hoverPriceLine); } catch(e) {}
                hoverPriceLine = null;
            }
            hoverPriceLine = candleS.createPriceLine({
                price,
                color: color || '#94a3b8',
                lineWidth: 1,
                lineStyle: LC.LineStyle ? LC.LineStyle.Solid : 0,
                axisLabelVisible: false,
                title: '',
            });
        };

        const clearHoverLine = () => {
            if (!hoverPriceLine || !candleS) return;
            try { candleS.removePriceLine(hoverPriceLine); } catch(e) {}
            hoverPriceLine = null;
        };

        // ============================
        //  OB LEVEL LINES
        // ============================
        const updateObLines = () => {
            if (!obChart || !candleS || !showObLines.value) return;
            const askPrice = maxLevelDistance.value.askPrice;
            const bidPrice = maxLevelDistance.value.bidPrice;
            if (obAskLine) { try { candleS.removePriceLine(obAskLine); } catch(e) {} obAskLine = null; }
            if (obBidLine) { try { candleS.removePriceLine(obBidLine); } catch(e) {} obBidLine = null; }
            const _oblv = getLvCfg();
            if (askPrice && _oblv.obAsk.vis?.ob !== false) obAskLine = candleS.createPriceLine({ price: askPrice, color: _oblv.obAsk.color, lineWidth: _oblv.obAsk.width, lineStyle: _oblv.obAsk.style, axisLabelVisible: false, title: '' });
            if (bidPrice && _oblv.obBid.vis?.ob !== false) obBidLine = candleS.createPriceLine({ price: bidPrice, color: _oblv.obBid.color, lineWidth: _oblv.obBid.width, lineStyle: _oblv.obBid.style, axisLabelVisible: false, title: '' });
        };

        const clearObLines = () => {
            if (obAskLine && candleS) { try { candleS.removePriceLine(obAskLine); } catch(e) {} obAskLine = null; }
            if (obBidLine && candleS) { try { candleS.removePriceLine(obBidLine); } catch(e) {} obBidLine = null; }
        };

        const toggleObLines = () => {
            showObLines.value = !showObLines.value;
            if (showObLines.value) updateObLines();
            else clearObLines();
        };

        const toggleBookPanel = () => {
            showBook.value = !showBook.value;
        };

        const toggleNakedChart = () => {
            nakedChart.value = !nakedChart.value;

            if (nakedChart.value) {
                for (const { p } of EMA_CFG)
                    if (emaS[p]) try { emaS[p].applyOptions({ visible: false }); } catch(e) {}
                _clearDayLines();
                clearObLines();
            } else {
                for (const { p } of EMA_CFG)
                    if (emaS[p]) try { emaS[p].applyOptions({ visible: true }); } catch(e) {}
                if (candleS) {
                    const lv = getLvCfg();
                    if (_dayHighPrice  && lv.dayHigh.vis?.ob  !== false) dayHighLine  = candleS.createPriceLine({price:_dayHighPrice,  color:lv.dayHigh.color,  lineWidth:lv.dayHigh.width,  lineStyle:lv.dayHigh.style,  axisLabelVisible:false, title:''});
                    if (_dayLowPrice   && lv.dayLow.vis?.ob   !== false) dayLowLine   = candleS.createPriceLine({price:_dayLowPrice,   color:lv.dayLow.color,   lineWidth:lv.dayLow.width,   lineStyle:lv.dayLow.style,   axisLabelVisible:false, title:''});
                    if (_prevHighPrice && lv.prevHigh.vis?.ob !== false) prevHighLine = candleS.createPriceLine({price:_prevHighPrice, color:lv.prevHigh.color, lineWidth:lv.prevHigh.width, lineStyle:lv.prevHigh.style, axisLabelVisible:false, title:''});
                    if (_prevLowPrice  && lv.prevLow.vis?.ob  !== false) prevLowLine  = candleS.createPriceLine({price:_prevLowPrice,  color:lv.prevLow.color,  lineWidth:lv.prevLow.width,  lineStyle:lv.prevLow.style,  axisLabelVisible:false, title:''});
                }
                if (showObLines.value) updateObLines();
            }
        };

        // ============================
        //  CLEANUP
        // ============================
        const cleanup = () => {
            if (bookWS)          { bookWS.close(); bookWS = null; }
            if (tradeWS)         { tradeWS.close(); tradeWS = null; }
            if (reconnectTimer)    clearTimeout(reconnectTimer);
            if (tradeReconnTimer)  clearTimeout(tradeReconnTimer);
            stopChartPolling();
            stopKlineWS();
            _clearDayLines();
            clearObLines();
            asksMap.clear();
            bidsMap.clear();
        };

        watch(showBook, val => {
            if (window.parent !== window) {
                window.parent.postMessage({ type: 'ob_book_toggle', visible: val }, '*');
            }
        });

        function initResizer() {
            const resizer  = document.getElementById('ob-resizer');
            const splitEl  = document.querySelector('.ob-split');
            const bookPnl  = document.querySelector('.ob-book-panel');
            if (!resizer || !splitEl || !bookPnl) return;

            const STORAGE_KEY = 'ob_book_w_pct';
            const MIN_BOOK  = 80;
            const MIN_CHART = 200;

            function calcBookW(pct) {
                const totalW      = splitEl.getBoundingClientRect().width;
                const tradePanelW = document.getElementById('ob-trade-panel')?.offsetWidth || 0;
                const available   = totalW - tradePanelW - 2;
                const maxBook     = Math.max(MIN_BOOK, available - MIN_CHART);
                return Math.max(MIN_BOOK, Math.min(maxBook, Math.round(pct * totalW)));
            }

            let saved = parseFloat(localStorage.getItem(STORAGE_KEY));
            if (isNaN(saved) || saved < 0.05 || saved > 0.6) saved = 1 / 3;
            requestAnimationFrame(() => {
                bookPnl.style.flex = `0 0 ${calcBookW(saved)}px`;
            });

            let startX = 0, startBookW = 0;

            function onMove(clientX) {
                const delta       = clientX - startX;
                const totalW      = splitEl.getBoundingClientRect().width;
                const tradePanelW = document.getElementById('ob-trade-panel')?.offsetWidth || 0;
                const available   = totalW - tradePanelW - 2;
                const maxBook     = Math.max(MIN_BOOK, available - MIN_CHART);
                const bookW       = Math.max(MIN_BOOK, Math.min(maxBook, startBookW - delta));
                bookPnl.style.flex = `0 0 ${bookW}px`;
            }

            function onEnd() {
                resizer.classList.remove('is-dragging');
                document.body.style.userSelect = '';
                document.body.style.cursor     = '';
                const totalW = splitEl.getBoundingClientRect().width;
                const bookW  = bookPnl.getBoundingClientRect().width;
                localStorage.setItem(STORAGE_KEY, (bookW / totalW).toFixed(4));
                document.removeEventListener('mousemove', mmove);
                document.removeEventListener('mouseup',   mup);
            }

            function mmove(e) { onMove(e.clientX); }
            function mup()    { onEnd(); }

            resizer.addEventListener('mousedown', e => {
                e.preventDefault();
                startX     = e.clientX;
                startBookW = bookPnl.getBoundingClientRect().width;
                resizer.classList.add('is-dragging');
                document.body.style.userSelect = 'none';
                document.body.style.cursor     = 'col-resize';
                document.addEventListener('mousemove', mmove);
                document.addEventListener('mouseup',   mup);
            });

            resizer.addEventListener('touchstart', e => {
                e.preventDefault();
                startX     = e.touches[0].clientX;
                startBookW = bookPnl.getBoundingClientRect().width;
                resizer.classList.add('is-dragging');
            }, { passive: false });
            resizer.addEventListener('touchmove', e => {
                e.preventDefault();
                onMove(e.touches[0].clientX);
            }, { passive: false });
            resizer.addEventListener('touchend', () => {
                resizer.classList.remove('is-dragging');
                const totalW = splitEl.getBoundingClientRect().width;
                const bookW  = bookPnl.getBoundingClientRect().width;
                localStorage.setItem(STORAGE_KEY, (bookW / totalW).toFixed(4));
            });
        }

        onMounted(() => {
            document.title = `${symbol.value} Trade`;
            fetchOrderBook();
            if (isStandalone.value) {
                fetchTicker();
                nextTick(() => { initChart(); initResizer(); });
            }
            _updateTfCountdowns();
            _cdRepaintTimer = setInterval(() => {
                if (obChart) try { obChart.applyOptions({}); } catch(e) {}
                _updateTfCountdowns();
            }, 1000);
            // Auth check + trade panel init
            (async () => {
                try {
                    const r = await fetch('/api/auth/status');
                    const d = await r.json();
                    if (d.logged_in) {
                        _isLoggedIn = true;
                        const ordBtn = document.getElementById('fs-order-btn');
                        if (ordBtn) { ordBtn.disabled = false; ordBtn.style.cssText = 'width:100%;padding:8px 0;font-size:12px;font-weight:700;background:#2563eb;color:#fff;border:none;border-radius:4px;cursor:pointer;opacity:1;'; }
                    }
                } catch(e) {}
                try {
                    const cfg = await fetch('/api/trade/config').then(r => r.json());
                    _tradeEnabled = cfg.enabled || false;
                } catch(e) {}
                if (isStandalone.value) {
                    _obSymbol = symbol.value;
                    clearInterval(_tradePollT);
                    loadTradeData();
                    _tradePollT = setInterval(loadTradeData, 3000);
                }
            })();
        });

        onUnmounted(() => {
            cleanup();
            clearInterval(_cdRepaintTimer);
            clearInterval(_tradePollT); _tradePollT = null;
        });

        return {
            t,
            symbol, symBase, symQuote, isStandalone,
            ticker, TF_OPTIONS, chartTF, ohlc, chartContainerEl,
            displayLevels, grouping, groupingOptions,
            levelsDdOpen, groupingDdOpen, selectLevels, selectGrouping,
            displayAsks, displayBids, pressure,
            cvdWindow, cvdData, CVD_WINDOWS,
            currentPrice, spread, priceColor,
            loading, error,
            isPaused,
            maxLevelDistance, showBook,
            fetchOrderBook, updateDisplay, changeChartTF,
            setHoverLine, clearHoverLine,
            showObLines, toggleObLines,
            nakedChart, toggleNakedChart,
            showTradePanel, toggleBookPanel,
            tfCountdowns, openMtf,
        };
    }
}).mount('#app');
