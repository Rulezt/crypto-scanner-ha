// Order Book Standalone Vue App
const { createApp, ref, computed, onMounted, onUnmounted, watch, nextTick } = Vue;

// ── Lightweight Charts ────────────────────────────────────────────────────────
const LC = window.LightweightCharts;

const DEFAULT_EMA_CFG = [
    { p: 5,   color: '#ef4444', style: 0, width: 2,   enabled: true },
    { p: 10,  color: '#fbbf24', style: 0, width: 2,   enabled: true },
    { p: 60,  color: '#3b82f6', style: 0, width: 3,   enabled: true },
    { p: 223, color: '#a855f7', style: 0, width: 2.5, enabled: true },
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
// ── Size massima stimabile dal book senza slippage eccessivo ──────────────────
// Accumula liquidità dal miglior prezzo in poi (asks per Buy, bids per Sell,
// entrambi già ordinati best-first) finché il prezzo medio ponderato (VWAP)
// non si allontana dal miglior prezzo oltre MAX_SIZE_SLIPPAGE_PCT. Si ferma
// PRIMA di aggiungere il livello che farebbe superare la soglia (non lo
// riempie parzialmente) — stima volutamente conservativa: un'indicazione,
// non un limite hard imposto sull'ordine.
const MAX_SIZE_SLIPPAGE_PCT = 0.15;
function computeMaxSafeSize(sortedLevels) {
    if (!sortedLevels.length) return null;
    const best = sortedLevels[0][0];
    if (!(best > 0)) return null;
    const maxDev = best * MAX_SIZE_SLIPPAGE_PCT / 100;
    let cumQty = 0, cumNotional = 0;
    for (const [price, qty] of sortedLevels) {
        if (!(qty > 0)) continue;
        const nQty = cumQty + qty, nNotional = cumNotional + price * qty;
        if (Math.abs(nNotional / nQty - best) > maxDev) break;
        cumQty = nQty; cumNotional = nNotional;
    }
    return cumQty > 0 ? { qty: cumQty, notional: cumNotional } : null;
}
function getEmaCfg() {
    try {
        const s = JSON.parse(localStorage.getItem('chart_ema_cfg'));
        if (s && s.length === 4) return s.map((e, i) => ({ ...DEFAULT_EMA_CFG[i], ...e, enabled: e.enabled !== false }));
    } catch(e) {}
    return DEFAULT_EMA_CFG.map(x => ({...x}));
}
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
let EMA_CFG = getEmaCfg();
let _obEmaS = {}, _obLastEMA = {};
function toggleEmaAll(period) {
    const cfg = getEmaCfg();
    const entry = cfg.find(e => e.p === period);
    entry.enabled = !(entry.enabled !== false);
    try { localStorage.setItem('chart_ema_cfg', JSON.stringify(cfg)); } catch(e) {}
    EMA_CFG = cfg;
    if (!_obKlines || !_obKlines.length) return;
    for (const {p, enabled} of EMA_CFG) {
        if (!_obEmaS[p]) continue;
        if (enabled === false) {
            _obEmaS[p].setData([]);
            _obLastEMA[p] = null;
        } else {
            const ema = calcEMA(_obKlines, p);
            _obEmaS[p].setData(ema);
            _obLastEMA[p] = ema[ema.length - 1].value;
        }
    }
}

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

// ── ROC (Rate Of Change) — porting da Pine: 100 * (close - close[length]) / close[length].
// A differenza degli overlay (BB/Canale/GRaB) è un oscillatore senza relazione di scala
// col prezzo, quindi va in un pannello dedicato sotto le candele (Panes API di
// Lightweight Charts v5, mai usata altrove nel sito — vedi addSeries/paneIndex).
const ROC_PANE_INDEX = 1;
const DEFAULT_ROC_CFG = { length: 9, color: '#2962FF', areaFill: false, upColor: '#20B26C', downColor: '#EF454A', showExtremes: false };
function getRocCfg() {
    try { const s = JSON.parse(localStorage.getItem('chart_roc_cfg')); if (s) return { ...DEFAULT_ROC_CFG, ...s }; } catch(e) {}
    return { ...DEFAULT_ROC_CFG };
}
function calcRoc(klines, length) {
    const out = [];
    for (let i = length; i < klines.length; i++) {
        const prev = klines[i - length].close;
        out.push({ time: klines[i].time, value: 100 * (klines[i].close - prev) / prev });
    }
    return out;
}
// Vedi chart.html: la serie ROC è sempre una BaselineSeries, fill trasparenti quando
// areaFill è OFF, così si evita di ricreare serie/pane al toggle dell'opzione.
function _rocFillOpts(cfg) {
    const on = !!cfg.areaFill;
    return {
        baseValue: { type: 'price', price: 0 },
        topLineColor: cfg.color, bottomLineColor: cfg.color,
        topFillColor1: on ? _hexToRgba(cfg.upColor, 0.35) : 'rgba(0,0,0,0)',
        topFillColor2: on ? _hexToRgba(cfg.upColor, 0.05) : 'rgba(0,0,0,0)',
        bottomFillColor1: on ? _hexToRgba(cfg.downColor, 0.05) : 'rgba(0,0,0,0)',
        bottomFillColor2: on ? _hexToRgba(cfg.downColor, 0.35) : 'rgba(0,0,0,0)',
    };
}

// ── GRaB (Buy green Sell Red) — porting Pine "BGSR": Murrey Math midline/range
// (highest/lowest a `murreyLength` candele) + wave EMA(high/low/close, `emaPeriod`)
// + ricolorazione candela in base a close/open vs wave (barcolor originale, sempre
// attiva quando il toggle è ON — showWave controlla solo le 3 linee wave, non la
// ricolorazione, esattamente come nel Pine di origine).
const _DEFAULT_GRAB_CFG = {
    murreyLength: 100, showMidline: true, showRange: true,
    emaPeriod: 34, showWave: false,
    midlineColor: '#000000', rangeColor: '#FF00FF',
    emaHighColor: '#FF0000', emaLowColor: '#008000', emaCloseColor: '#C0C0C0',
    colorAboveBull: '#00FF00', colorAboveBear: '#008000',
    colorBelowBull: '#FF0000', colorBelowBear: '#800000',
    colorMidBull:   '#C0C0C0', colorMidBear:   '#808080',
};
function getGrabCfg() {
    try { const s = JSON.parse(localStorage.getItem('chart_grab_cfg')); if (s) return { ..._DEFAULT_GRAB_CFG, ...s }; } catch(e) {}
    return { ..._DEFAULT_GRAB_CFG };
}
function setGrabCfg(cfg) { try { localStorage.setItem('chart_grab_cfg', JSON.stringify(cfg)); } catch(e) {} }
function calcEMAField(bars, period, field) {
    const k = 2 / (period + 1); let v = bars[0][field];
    return bars.map((b, i) => { if (i > 0) v = b[field] * k + v * (1 - k); return { time: b.time, value: v }; });
}
function calcMurrey(klines, length) {
    const mid = [], lo = [], hi = [];
    for (let i = length - 1; i < klines.length; i++) {
        let h = -Infinity, l = Infinity;
        for (let j = i - length + 1; j <= i; j++) { h = Math.max(h, klines[j].high); l = Math.min(l, klines[j].low); }
        mid.push({ time: klines[i].time, value: l + (h - l) / 2 });
        lo.push( { time: klines[i].time, value: l });
        hi.push( { time: klines[i].time, value: h });
    }
    return { mid, lo, hi };
}
function grabBarColor(close, open, eHigh, eLow, cfg) {
    if (close < eLow)  return close > open ? cfg.colorBelowBull : cfg.colorBelowBear;
    if (close > eHigh) return close > open ? cfg.colorAboveBull : cfg.colorAboveBear;
    return close > open ? cfg.colorMidBull : cfg.colorMidBear;
}
// Stato semantico GRaB (per tooltip strip TF): stessa logica di grabBarColor, ma
// restituisce una chiave testuale invece del colore, per poter mostrare all'utente
// cosa indica il colore (posizione rispetto alla wave + direzione della candela).
function grabBarState(close, open, eHigh, eLow) {
    const bull = close > open;
    if (close < eLow)  return bull ? 'belowBull' : 'belowBear';
    if (close > eHigh) return bull ? 'aboveBull' : 'aboveBear';
    return bull ? 'midBull' : 'midBear';
}
const GRAB_STATE_LABEL = {
    aboveBull: 'Trend rialzista forte',
    aboveBear: 'Trend rialzista in pausa',
    belowBull: 'Trend ribassista in pausa',
    belowBear: 'Trend ribassista forte',
    midBull:   'Laterale, spinta rialzista',
    midBear:   'Laterale, spinta ribassista',
};

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

function addSeries(chart, type, opts, paneIndex) {
    // Le LineSeries qui sono sempre indicatori overlay (EMA/canale/BB), mai la serie
    // principale: escluderle dall'autoscale evita che un EMA223 lontano dal prezzo
    // schiacci le candele in una riga piatta illeggibile — la scala verticale segue
    // solo le candele. Non si applica quando la serie va in un pannello dedicato
    // (paneIndex esplicito, es. ROC): lì la scala prezzi è indipendente e deve
    // autoscalare normalmente sui valori dell'oscillatore.
    if (type === 'LineSeries' && paneIndex == null) opts = { ...opts, autoscaleInfoProvider: () => null };
    if (typeof chart.addSeries === 'function' && LC[type]) return paneIndex != null ? chart.addSeries(LC[type], opts, paneIndex) : chart.addSeries(LC[type], opts);
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
function _obCdFmt(s, hideSecAboveHour) {
    if (s <= 0) return '0:00';
    if (s >= 3600) {
        const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=s%60;
        if (hideSecAboveHour) return `${h}:${String(m).padStart(2,'0')}`;
        return `${h}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`;
    }
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
        const hideSec = s.curTF === 'D' || s.curTF === '240';
        return [{ coordinate: () => y + 17, text: () => _obCdFmt(rem, hideSec), textColor: () => '#FFFFFF', backColor: () => bull ? '#20B26C' : '#EF454A' }];
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
let _obCandleS = null, _obChart = null, _obSymbol = '', _obLivePrice = 0, _obChartTF = '', _obTzOffsetG = 0;
let _obLoadSeq = 0;
let _isLoggedIn = false;
// "Colora candele" — toggle GLOBALE condiviso su tutte le pagine (vedi candle-color.js).
const _applyCandleStyle = window.applyCandleColorStyle;
function toggleObCandleColor() {
    window.toggleCandleColorGlobal(() => { _applyCandleStyle(_obCandleS); });
}

// ── Bollinger Bands (BB) ───────────────────────────────────────────────────────
// Stato ON/OFF persistito in localStorage (chiavi ob_*_active) — a differenza del resto di
// questo file, qui vogliamo che sopravviva ai cambi coin dal tasto ricerca, che fanno un
// window.location.href (reload completo di pagina), non un cambio in-place.
let _obBbActive = window.getIndActive('bb'), _obBbSeries = { upper: null, mid: null, lower: null }, _obKlines = [];

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
    window.setIndActive('bb', _obBbActive);
    const btn = document.getElementById('ob-bb-btn');
    if (btn) btn.style.color = _obBbActive ? '#60a5fa' : '#B2B5BE';
    _obApplyBB();
}

// ── ROC (Rate Of Change) ────────────────────────────────────────────────────────
let _obRocActive = window.getIndActive('roc'), _obRocSeries = null, _obRocZeroLine = null, _obRocMaxLine = null, _obRocMinLine = null;

// Vedi chart.html: linee su min/max effettivi dei dati caricati, ricreate ad ogni
// refresh (non aggiornate in-place).
function _updateRocExtremeLines(cfg, data) {
    if (_obRocMaxLine) { try { _obRocSeries.removePriceLine(_obRocMaxLine); } catch(e) {} _obRocMaxLine = null; }
    if (_obRocMinLine) { try { _obRocSeries.removePriceLine(_obRocMinLine); } catch(e) {} _obRocMinLine = null; }
    if (!cfg.showExtremes || !data.length) return;
    let max = data[0].value, min = data[0].value;
    for (const d of data) { if (d.value > max) max = d.value; if (d.value < min) min = d.value; }
    _obRocMaxLine = _obRocSeries.createPriceLine({ price: max, color: cfg.upColor, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'Max' });
    _obRocMinLine = _obRocSeries.createPriceLine({ price: min, color: cfg.downColor, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'Min' });
}

function _obApplyRoc() {
    // removePane() è un'operazione di layout pesante: farla ad ogni refresh dati/cambio
    // TF (invece che solo su vera transizione ON<->OFF) col tempo destabilizza il chart
    // (smette di renderizzare le candele dopo alcuni cambi TF) — vedi chart.html applyRocToSlot.
    const cfg = getRocCfg();
    if (!_obRocActive || !_obChart || !_obKlines.length || _obKlines.length <= cfg.length) {
        if (_obRocSeries) { try { _obChart.removePane(ROC_PANE_INDEX); } catch(e) {} _obRocSeries = null; _obRocZeroLine = null; _obRocMaxLine = null; _obRocMinLine = null; }
        return;
    }
    if (!_obRocSeries) {
        _obRocSeries = addSeries(_obChart, 'BaselineSeries', {
            priceLineVisible: true, lastValueVisible: true, crosshairMarkerVisible: true,
            lineWidth: 1, ..._rocFillOpts(cfg),
            priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        }, ROC_PANE_INDEX);
        _obRocZeroLine = _obRocSeries.createPriceLine({ price: 0, color: '#787B86', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' });
        try { _obChart.panes()[ROC_PANE_INDEX].setStretchFactor(0.3); } catch(e) {}
    } else {
        _obRocSeries.applyOptions(_rocFillOpts(cfg));
    }
    const _rocData = calcRoc(_obKlines, cfg.length);
    _obRocSeries.setData(_rocData);
    _updateRocExtremeLines(cfg, _rocData);
}

function toggleObRoc() {
    _obRocActive = !_obRocActive;
    window.setIndActive('roc', _obRocActive);
    const btn = document.getElementById('ob-roc-btn');
    if (btn) btn.style.color = _obRocActive ? '#2962FF' : '#B2B5BE';
    _obApplyRoc();
}

// ── EMA personalizzata (overlay singolo, lunghezza configurabile — porting Pine
// "Moving Average Exponential", solo il core: lunghezza/colore/stile, niente
// smoothing/BB secondari, scope confermato con l'utente) ───────────────────────
const DEFAULT_EMA_CUSTOM_CFG = {
    length: 9, color: '#00E5FF', width: 1.5, style: 0, waitClose: true,
    // TF di calcolo indipendente per ogni TF del grafico OB (chiave 'tf_<TF grafico>'):
    // '' = segue il grafico (comportamento storico), altrimenti proietta da quel TF fisso.
    tf_1: '', tf_5: '', tf_30: '', tf_60: '', tf_240: '', tf_D: '', tf_W: '', tf_M: '',
};
function getEmaCustomCfg() {
    try {
        const s = JSON.parse(localStorage.getItem('chart_ema_custom_cfg'));
        if (s) {
            // Migrazione one-time: prima esisteva un solo campo `tf` globale (vedi chart.html).
            if (s.tf !== undefined && s.tf_1 === undefined) {
                for (const k of ['tf_1','tf_5','tf_30','tf_60','tf_240','tf_D','tf_W','tf_M']) s[k] = s.tf;
                delete s.tf;
                try { localStorage.setItem('chart_ema_custom_cfg', JSON.stringify(s)); } catch(e) {}
            }
            return { ...DEFAULT_EMA_CUSTOM_CFG, ...s };
        }
    } catch(e) {}
    return { ...DEFAULT_EMA_CUSTOM_CFG };
}
// ── Timeframe di calcolo (porting Pine indicator(timeframe=..., timeframe_gaps=...)):
// vedi commento gemello in chart.html per il dettaglio. Qui il grafico order book è
// sempre "a schermo singolo" (nessuna griglia con più istanze), quindi il polling di
// aggiornamento (_syncObEmaCustomMtfTimer) resta sempre attivo mentre il TF MTF è attivo.
function _projectMtfEma(hostKlines, auxKlines, auxEma, waitClose) {
    if (!hostKlines.length || !auxKlines.length || !auxEma.length) return [];
    const out = [];
    let ai = 0;
    for (const hk of hostKlines) {
        while (ai + 1 < auxKlines.length && auxKlines[ai + 1].time <= hk.time) ai++;
        const isLastAux = ai === auxKlines.length - 1;
        const idx = (waitClose && isLastAux) ? ai - 1 : ai;
        if (idx >= 0 && auxEma[idx]) out.push({ time: hk.time, value: auxEma[idx].value });
    }
    return out;
}
async function _fetchAuxKlines(sym, tf) {
    try {
        const r = await fetch(`api/klines?symbol=${sym}&interval=${tf}`);
        const j = await r.json();
        return (j.success && j.data) ? j.data : [];
    } catch(e) { return []; }
}
let _obEmaCustomActive = window.getIndActive('emaCustom'), _obEmaCustomSeries = null, _obLastEmaCustom = null;
let _obEmaCustomMtfSeq = 0, _obEmaCustomMtfTimer = null;
function _syncObEmaCustomMtfTimer() {
    clearInterval(_obEmaCustomMtfTimer); _obEmaCustomMtfTimer = null;
    const cfg = getEmaCustomCfg();
    const effectiveTf = cfg['tf_' + _obChartTF] || '';
    if (_obEmaCustomActive && effectiveTf && _obChart) {
        _obEmaCustomMtfTimer = setInterval(_obApplyEmaCustom, 5000);
    }
}
function _obApplyEmaCustom() {
    if (_obEmaCustomSeries) { try { _obChart.removeSeries(_obEmaCustomSeries); } catch(e) {} _obEmaCustomSeries = null; }
    _obLastEmaCustom = null;
    _obEmaCustomMtfSeq++;
    if (!_obEmaCustomActive || !_obChart || !_obKlines.length) return;
    const cfg = getEmaCustomCfg();
    const lb = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    _obEmaCustomSeries = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.color, lineWidth: cfg.width, lineStyle: cfg.style ?? 0 });
    // TF di calcolo indipendente per TF del grafico OB (vedi DEFAULT_EMA_CUSTOM_CFG):
    // rilegge sempre la voce giusta da _obChartTF, così cambiare TF sul grafico applica in
    // automatico il TF di calcolo configurato per quel TF, senza toccare le impostazioni.
    const effectiveTf = cfg['tf_' + _obChartTF] || '';
    if (!effectiveTf) {
        const ema = calcEMA(_obKlines, cfg.length);
        _obEmaCustomSeries.setData(ema);
        _obLastEmaCustom = ema[ema.length - 1].value;
        return;
    }
    const mySeq = _obEmaCustomMtfSeq;
    const sym = _obSymbol;
    if (!sym) return;
    (async () => {
      try {
        const auxKlines = await _fetchAuxKlines(sym, effectiveTf);
        if (_obEmaCustomMtfSeq !== mySeq || !_obEmaCustomSeries) return;
        if (!auxKlines.length) { console.warn('EMA personalizzata: nessun dato per TF', effectiveTf, sym); return; }
        const auxEma = calcEMA(auxKlines, cfg.length);
        const projected = _projectMtfEma(_obKlines, auxKlines, auxEma, cfg.waitClose);
        _obEmaCustomSeries.setData(projected);
      } catch(e) { console.error('EMA personalizzata (MTF) errore:', e); }
    })();
}
function toggleObEmaCustom() {
    _obEmaCustomActive = !_obEmaCustomActive;
    window.setIndActive('emaCustom', _obEmaCustomActive);
    _obApplyEmaCustom();
    _syncObEmaCustomMtfTimer();
}

// ── EMA personalizzata 2 — seconda istanza indipendente, copia 1:1 della prima
// (vedi commenti sopra) con suffisso "2", stessi helper _projectMtfEma/_fetchAuxKlines.
const DEFAULT_EMA_CUSTOM2_CFG = {
    length: 21, color: '#FF6EC7', width: 1.5, style: 0, waitClose: true,
    tf_1: '', tf_5: '', tf_30: '', tf_60: '', tf_240: '', tf_D: '', tf_W: '', tf_M: '',
};
function getEmaCustom2Cfg() {
    try {
        const s = JSON.parse(localStorage.getItem('chart_ema_custom2_cfg'));
        if (s) return { ...DEFAULT_EMA_CUSTOM2_CFG, ...s };
    } catch(e) {}
    return { ...DEFAULT_EMA_CUSTOM2_CFG };
}
let _obEmaCustom2Active = window.getIndActive('emaCustom2'), _obEmaCustom2Series = null, _obLastEmaCustom2 = null;
let _obEmaCustom2MtfSeq = 0, _obEmaCustom2MtfTimer = null;
function _syncObEmaCustom2MtfTimer() {
    clearInterval(_obEmaCustom2MtfTimer); _obEmaCustom2MtfTimer = null;
    const cfg = getEmaCustom2Cfg();
    const effectiveTf = cfg['tf_' + _obChartTF] || '';
    if (_obEmaCustom2Active && effectiveTf && _obChart) {
        _obEmaCustom2MtfTimer = setInterval(_obApplyEmaCustom2, 5000);
    }
}
function _obApplyEmaCustom2() {
    if (_obEmaCustom2Series) { try { _obChart.removeSeries(_obEmaCustom2Series); } catch(e) {} _obEmaCustom2Series = null; }
    _obLastEmaCustom2 = null;
    _obEmaCustom2MtfSeq++;
    if (!_obEmaCustom2Active || !_obChart || !_obKlines.length) return;
    const cfg = getEmaCustom2Cfg();
    const lb = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    _obEmaCustom2Series = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.color, lineWidth: cfg.width, lineStyle: cfg.style ?? 0 });
    const effectiveTf = cfg['tf_' + _obChartTF] || '';
    if (!effectiveTf) {
        const ema = calcEMA(_obKlines, cfg.length);
        _obEmaCustom2Series.setData(ema);
        _obLastEmaCustom2 = ema[ema.length - 1].value;
        return;
    }
    const mySeq = _obEmaCustom2MtfSeq;
    const sym = _obSymbol;
    if (!sym) return;
    (async () => {
      try {
        const auxKlines = await _fetchAuxKlines(sym, effectiveTf);
        if (_obEmaCustom2MtfSeq !== mySeq || !_obEmaCustom2Series) return;
        if (!auxKlines.length) { console.warn('EMA personalizzata 2: nessun dato per TF', effectiveTf, sym); return; }
        const auxEma = calcEMA(auxKlines, cfg.length);
        const projected = _projectMtfEma(_obKlines, auxKlines, auxEma, cfg.waitClose);
        _obEmaCustom2Series.setData(projected);
      } catch(e) { console.error('EMA personalizzata 2 (MTF) errore:', e); }
    })();
}
function toggleObEmaCustom2() {
    _obEmaCustom2Active = !_obEmaCustom2Active;
    window.setIndActive('emaCustom2', _obEmaCustom2Active);
    _obApplyEmaCustom2();
    _syncObEmaCustom2MtfTimer();
}

// ── Canale SMA 20 (SMA di high/close/low → 3 linee + riempimento) ──────────────
// Config condivisa con chart.html/mtf.html (stessa chiave localStorage
// chart_channel_cfg) — OB_CH_COLOR resta solo per il colore fisso del bottone
// nella toolbar (stesso pattern del bottone Canale su chart.html/mtf.html, che
// non riflette il colore configurato della linea).
const OB_CH_COLOR = '#22d3ee';
const DEFAULT_CHANNEL_CFG = { period: 20, color: '#ffffff', width: 1, style: 0, bgColor: '#ffffff', bgOpacity: 10 };
function getChannelCfg() {
    try { const s = JSON.parse(localStorage.getItem('chart_channel_cfg')); if (s) return { ...DEFAULT_CHANNEL_CFG, ...s }; } catch(e) {}
    return { ...DEFAULT_CHANNEL_CFG };
}
let _obChActive = window.getIndActive('channel'), _obChSeries = { upper: null, mid: null, lower: null }, _obChData = null;

function calcSmaChannel(klines, period) {
    const mid = [], upper = [], lower = [];
    for (let i = period - 1; i < klines.length; i++) {
        let sumC = 0, sumH = 0, sumL = 0;
        for (let j = i - period + 1; j <= i; j++) { sumC += klines[j].close; sumH += klines[j].high; sumL += klines[j].low; }
        mid.push(  { time: klines[i].time, value: sumC / period });
        upper.push({ time: klines[i].time, value: sumH / period });
        lower.push({ time: klines[i].time, value: sumL / period });
    }
    return { mid, upper, lower };
}

// Ricalcola l'intero canale e restituisce solo l'ultimo punto (per gli update live,
// stesso approccio di ricalcolo-finestra già usato per BB — costo trascurabile su
// _obKlines che è comunque limitato a poche decine/centinaia di candele).
function _obChTail(klines) {
    const period = getChannelCfg().period;
    if (klines.length < period) return null;
    const c = calcSmaChannel(klines, period);
    return { upper: c.upper[c.upper.length - 1], mid: c.mid[c.mid.length - 1], lower: c.lower[c.lower.length - 1] };
}

function _obChUpdateTail(klines) {
    if (!_obChActive || !_obChSeries.upper) return;
    const t = _obChTail(klines);
    if (!t) return;
    _obChSeries.upper.update(t.upper);
    _obChSeries.mid.update(t.mid);
    _obChSeries.lower.update(t.lower);
    if (_obChData) {
        const upd = (arr, pt) => { if (arr.length && arr[arr.length - 1].time === pt.time) arr[arr.length - 1] = pt; else arr.push(pt); };
        upd(_obChData.upper, t.upper);
        upd(_obChData.lower, t.lower);
    }
}

class _ObChannelFillPrimitive {
    constructor() {
        this._series = null;
        const renderer = {
            draw: target => target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio, verticalPixelRatio }) => {
                const series = this._series;
                if (!series || !_obChActive || !_obChData || !_obChart) return;
                const { upper, lower } = _obChData;
                if (!upper.length || !lower.length) return;
                const ts = _obChart.timeScale();
                ctx.save();
                ctx.beginPath();
                let started = false;
                for (let i = 0; i < upper.length; i++) {
                    const x = ts.timeToCoordinate(upper[i].time);
                    const y = series.priceToCoordinate(upper[i].value);
                    if (x == null || y == null) continue;
                    const xp = x * horizontalPixelRatio, yp = y * verticalPixelRatio;
                    if (!started) { ctx.moveTo(xp, yp); started = true; } else ctx.lineTo(xp, yp);
                }
                for (let i = lower.length - 1; i >= 0; i--) {
                    const x = ts.timeToCoordinate(lower[i].time);
                    const y = series.priceToCoordinate(lower[i].value);
                    if (x == null || y == null) continue;
                    ctx.lineTo(x * horizontalPixelRatio, y * verticalPixelRatio);
                }
                if (started) { const _chCfg = getChannelCfg(); ctx.closePath(); ctx.fillStyle = _hexToRgba(_chCfg.bgColor, (_chCfg.bgOpacity ?? 10) / 100); ctx.fill(); }
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

function _obApplyChannel() {
    for (const k of ['upper', 'mid', 'lower']) {
        if (_obChSeries[k]) { try { _obChart.removeSeries(_obChSeries[k]); } catch(e) {} _obChSeries[k] = null; }
    }
    _obChData = null;
    if (!_obChActive || !_obChart) return;
    const cfg = getChannelCfg();
    const lb = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    _obChSeries.upper = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.color, lineWidth: cfg.width, lineStyle: cfg.style });
    _obChSeries.mid   = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.color, lineWidth: cfg.width, lineStyle: 2 });
    _obChSeries.lower = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.color, lineWidth: cfg.width, lineStyle: cfg.style });
    if (_obKlines.length >= cfg.period) {
        const c = calcSmaChannel(_obKlines, cfg.period);
        _obChSeries.upper.setData(c.upper);
        _obChSeries.mid.setData(c.mid);
        _obChSeries.lower.setData(c.lower);
        _obChData = { upper: c.upper, lower: c.lower };
    }
}

function toggleObChannel() {
    _obChActive = !_obChActive;
    window.setIndActive('channel', _obChActive);
    const btn = document.getElementById('ob-ch-btn');
    if (btn) btn.style.color = _obChActive ? OB_CH_COLOR : '#B2B5BE';
    _obApplyChannel();
}

// ── GRaB (Buy green Sell Red) ───────────────────────────────────────────────────
let _obGrabActive = window.getIndActive('grab');
let _obGrabMidlineActive = window.getIndActive('grabMidline'); // toggle indipendente per vedere solo la midline GRaB senza attivare tutto l'indicatore
let _obGrabWaveSeries = { high: null, low: null, close: null };
let _obGrabMurreySeries = { mid: null, lo: null, hi: null };
let _obGrabState = null;

function _obApplyGrab() {
    for (const k of ['high', 'low', 'close']) {
        if (_obGrabWaveSeries[k]) { try { _obChart.removeSeries(_obGrabWaveSeries[k]); } catch(e) {} _obGrabWaveSeries[k] = null; }
    }
    for (const k of ['mid', 'lo', 'hi']) {
        if (_obGrabMurreySeries[k]) { try { _obChart.removeSeries(_obGrabMurreySeries[k]); } catch(e) {} _obGrabMurreySeries[k] = null; }
    }
    if (!_obChart) { _obGrabState = null; return; }
    const cfg = getGrabCfg();
    // Se il GRaB completo è OFF, la midline resta comunque disponibile tramite il
    // toggle indipendente _obGrabMidlineActive (pannello Indicatori, sotto EMA 223).
    const midlineOn = _obGrabActive ? cfg.showMidline : _obGrabMidlineActive;
    if (!_obGrabActive) {
        if (_obCandleS && _obKlines.length) _obCandleS.setData(_obKlines);
        _obGrabState = null;
        if (!midlineOn || !_obKlines.length) return;
        const lb = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
        _obGrabMurreySeries.mid = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.midlineColor, lineWidth: 1 });
        if (_obKlines.length >= cfg.murreyLength) _obGrabMurreySeries.mid.setData(calcMurrey(_obKlines, cfg.murreyLength).mid);
        return;
    }
    const lb = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    if (cfg.showWave) {
        _obGrabWaveSeries.high  = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.emaHighColor,  lineWidth: 1 });
        _obGrabWaveSeries.low   = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.emaLowColor,   lineWidth: 1 });
        _obGrabWaveSeries.close = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.emaCloseColor, lineWidth: 1 });
    }
    if (midlineOn) _obGrabMurreySeries.mid = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.midlineColor, lineWidth: 1 });
    if (cfg.showRange) {
        _obGrabMurreySeries.lo = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.rangeColor, lineWidth: 1 });
        _obGrabMurreySeries.hi = addSeries(_obChart, 'LineSeries', { ...lb, color: cfg.rangeColor, lineWidth: 1 });
    }
    if (!_obKlines.length) return;
    const period = cfg.emaPeriod;
    const eh = calcEMAField(_obKlines, period, 'high');
    const el = calcEMAField(_obKlines, period, 'low');
    const ec = calcEMAField(_obKlines, period, 'close');
    const colored = _obKlines.map((k, i) => {
        const c = grabBarColor(k.close, k.open, eh[i].value, el[i].value, cfg);
        return { ...k, color: c, wickColor: c, borderColor: c };
    });
    _obCandleS.setData(colored);
    _obGrabState = { emaHigh: eh[eh.length - 1].value, emaLow: el[el.length - 1].value, emaClose: ec[ec.length - 1].value };
    if (cfg.showWave) {
        _obGrabWaveSeries.high.setData(eh);
        _obGrabWaveSeries.low.setData(el);
        _obGrabWaveSeries.close.setData(ec);
    }
    if ((midlineOn || cfg.showRange) && _obKlines.length >= cfg.murreyLength) {
        const m = calcMurrey(_obKlines, cfg.murreyLength);
        if (midlineOn) _obGrabMurreySeries.mid.setData(m.mid);
        if (cfg.showRange) { _obGrabMurreySeries.lo.setData(m.lo); _obGrabMurreySeries.hi.setData(m.hi); }
    }
}

function toggleObGrab() {
    _obGrabActive = !_obGrabActive;
    window.setIndActive('grab', _obGrabActive);
    const btn = document.getElementById('ob-grab-btn');
    if (btn) btn.style.color = _obGrabActive ? '#f59e0b' : '#B2B5BE';
    const cfgBtn = document.getElementById('ob-grab-cfg-btn');
    if (cfgBtn) cfgBtn.style.display = _obGrabActive ? 'inline-flex' : 'none';
    _obApplyGrab();
}
function toggleObGrabMidline() {
    _obGrabMidlineActive = !_obGrabMidlineActive;
    window.setIndActive('grabMidline', _obGrabMidlineActive);
    _obApplyGrab();
}

// ── Pannello impostazioni GRaB ──────────────────────────────────────────────────
function openGrabCfgPanel() {
    const cfg = getGrabCfg();
    for (const k of Object.keys(_DEFAULT_GRAB_CFG)) {
        const el = document.getElementById('grab-cfg-' + k);
        if (!el) continue;
        if (el.type === 'checkbox') el.checked = !!cfg[k];
        else el.value = cfg[k];
    }
    document.getElementById('grab-cfg-panel').style.display = 'flex';
}
function closeGrabCfgPanel() { document.getElementById('grab-cfg-panel').style.display = 'none'; }
function saveGrabCfgPanel() {
    const cfg = {};
    for (const k of Object.keys(_DEFAULT_GRAB_CFG)) {
        const el = document.getElementById('grab-cfg-' + k);
        if (!el) { cfg[k] = _DEFAULT_GRAB_CFG[k]; continue; }
        if (el.type === 'checkbox') cfg[k] = el.checked;
        else if (el.type === 'number') cfg[k] = parseFloat(el.value);
        else cfg[k] = el.value;
    }
    if (!isFinite(cfg.murreyLength)) cfg.murreyLength = _DEFAULT_GRAB_CFG.murreyLength;
    if (!isFinite(cfg.emaPeriod))    cfg.emaPeriod    = _DEFAULT_GRAB_CFG.emaPeriod;
    cfg.murreyLength = Math.max(10, Math.min(500, Math.round(cfg.murreyLength)));
    cfg.emaPeriod    = Math.max(2,  Math.min(500, Math.round(cfg.emaPeriod)));
    setGrabCfg(cfg);
    closeGrabCfgPanel();
    if (_obGrabActive || _obGrabMidlineActive) _obApplyGrab();
}
function resetGrabCfgPanel() {
    localStorage.removeItem('chart_grab_cfg');
    openGrabCfgPanel();
    if (_obGrabActive || _obGrabMidlineActive) _obApplyGrab();
}

// Ricolora la candela riusando l'ultimo stato noto (no recompute) — per i punti ad
// alta frequenza (WS kline raw, patch mid-price book) dove basta evitare che
// l'update "spoglio" (senza color) faccia sfarfallare via il colore GRaB.
function _obGrabColorCandle(candle) {
    if (!_obGrabActive || !_obGrabState || !_obCandleS) return;
    const cfg = getGrabCfg();
    const color = grabBarColor(candle.close, candle.open, _obGrabState.emaHigh, _obGrabState.emaLow, cfg);
    try { _obCandleS.update({ ...candle, color, wickColor: color, borderColor: color }); } catch(e) {}
}
// Full recompute (live emaHigh/Low/Close + ricolora + wave/murrey) — per i punti
// "ufficiali" (poll REST 3s, roll-forward nuova candela), mirror di _obChUpdateTail.
function _obGrabUpdateTail(candle, confirmed) {
    const cfg = getGrabCfg();
    const midlineOn = _obGrabActive ? cfg.showMidline : _obGrabMidlineActive;
    if (_obGrabActive && _obGrabState && _obCandleS) {
        const k = 2 / (cfg.emaPeriod + 1);
        const liveEmaHigh  = candle.high  * k + _obGrabState.emaHigh  * (1 - k);
        const liveEmaLow   = candle.low   * k + _obGrabState.emaLow   * (1 - k);
        const liveEmaClose = candle.close * k + _obGrabState.emaClose * (1 - k);
        const color = grabBarColor(candle.close, candle.open, liveEmaHigh, liveEmaLow, cfg);
        try { _obCandleS.update({ ...candle, color, wickColor: color, borderColor: color }); } catch(e) {}
        if (cfg.showWave && _obGrabWaveSeries.high) {
            _obGrabWaveSeries.high.update({ time: candle.time, value: liveEmaHigh });
            _obGrabWaveSeries.low.update( { time: candle.time, value: liveEmaLow });
            _obGrabWaveSeries.close.update({ time: candle.time, value: liveEmaClose });
        }
        if (confirmed) { _obGrabState.emaHigh = liveEmaHigh; _obGrabState.emaLow = liveEmaLow; _obGrabState.emaClose = liveEmaClose; }
    }
    if ((midlineOn || (_obGrabActive && cfg.showRange)) && _obKlines.length >= cfg.murreyLength) {
        const win = _obKlines.slice(-cfg.murreyLength);
        let h = -Infinity, l = Infinity;
        for (const c of win) { h = Math.max(h, c.high); l = Math.min(l, c.low); }
        if (midlineOn && _obGrabMurreySeries.mid) _obGrabMurreySeries.mid.update({ time: candle.time, value: l + (h - l) / 2 });
        if (_obGrabActive && cfg.showRange && _obGrabMurreySeries.lo) { _obGrabMurreySeries.lo.update({ time: candle.time, value: l }); _obGrabMurreySeries.hi.update({ time: candle.time, value: h }); }
    }
}
// Conferma lo stato EMA-H/L/C sulla candela appena chiusa — usa gli high/low REALI
// della candela (non solo close, a differenza di lastEMA che è EMA-di-close):
// _obGrabState traccia EMA(high)/EMA(low) separate, come nel Pine originale.
function _obGrabConfirmPrev(prevCandle) {
    if (!_obGrabActive || !_obGrabState || !prevCandle) return;
    const k = 2 / (getGrabCfg().emaPeriod + 1);
    _obGrabState.emaHigh  = prevCandle.high  * k + _obGrabState.emaHigh  * (1 - k);
    _obGrabState.emaLow   = prevCandle.low   * k + _obGrabState.emaLow   * (1 - k);
    _obGrabState.emaClose = prevCandle.close * k + _obGrabState.emaClose * (1 - k);
}

// I 4 bottoni rapidi in header (Canale/BB/GRaB/ROC) colorano se stessi solo
// dentro il proprio toggleObX(), che gira al click — ma con lo stato ora persistito e
// ripristinato al load (vedi i _obXActive = localStorage.getItem(...) più sopra), il
// colore va risincronizzato manualmente una volta all'avvio, altrimenti l'indicatore
// risulta attivo ma il bottone appare ancora spento. OB Levels non serve: è legato via
// :class Vue a showObLines, si aggiorna da solo.
function _obSyncIndicatorButtonColors() {
    const set = (id, active, color) => { const btn = document.getElementById(id); if (btn) btn.style.color = active ? color : '#B2B5BE'; };
    set('ob-ch-btn', _obChActive, OB_CH_COLOR);
    set('ob-bb-btn', _obBbActive, '#60a5fa');
    set('ob-grab-btn', _obGrabActive, '#f59e0b');
    set('ob-roc-btn', _obRocActive, '#2962FF');
}
let _tradeEnabled = false, _tradePos = null, _tradeSide = null, _hadPosition = false;
let _tradeBalance = null, _instInfo = null, _tradePollT = null;
let _tradeSSE = null;
let _fsOrderType = 'Limit', _fsCondExec = 'Market';
let _fsPendingOrderId = null, _fsPendingOrderFilter = null, _orderSeenInList = false, _pendingOrderSetAt = 0, _lastAmendAt = 0, _orderMissingCount = 0, _posMissingCount = 0;
let _pricePickTarget = null;
let _fsSlLine = null, _fsTpLine = null, _fsSlPrice = null, _fsTpPrice = null;
// TP può essere Market (default, alla trigger) o Limit (ordine limite alla trigger,
// stesso prezzo). SL resta sempre market — un limit SL rischia di non riempirsi.
let _fsTpOrderType = 'Market';
let _fsSlLabel = null, _fsTpLabel = null, _fsExecLabel = null;
let _fsEntryLine = null, _fsEntryPrice = null, _fsEntryLabel = null, _fsEntryIsPosition = false;
let _fsExecLine = null, _fsExecPrice = null;
let _fsChartSpacingLocked = false;
// Distanza voluta in PIXEL (non barre): il numero di barre necessarie a coprirla varia
// con barSpacing, che a sua volta cambia per TF (finestre di zoom diverse in _TF_N/DEFAULT_CANDLES
// comprimono/allargano le barre nella stessa larghezza di canvas) — fissare un numero di barre
// dava quindi una distanza in pixel diversa da TF a TF, mentre le label sono ancorate in pixel.
const _FS_MIN_RIGHT_PX = 330;
function _fsMinOffsetBars(chart) {
    chart = chart || _obChart;
    try {
        const bs = chart?.timeScale()?.options()?.barSpacing || 6;
        return Math.max(1, Math.round(_FS_MIN_RIGHT_PX / bs));
    } catch(e) { return 22; }
}
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
    if (!open) { clearInterval(_tradePollT); _tradePollT = null; loadTradeData(); _tradePollT = setInterval(loadTradeData, 3000); _startTradeSSE(); }
    else { clearInterval(_tradePollT); _tradePollT = null; _stopTradeSSE(); resetTradeSide('panelClose'); }
}

// ── Push posizione/ordini via websocket privata Bybit (backend, private_ws_manager.py)
// invece di aspettare il prossimo poll REST — vedi indagine "lentezza P&L/chiusura TP-SL".
// Il poll REST (loadTradeData, sopra) resta attivo come fallback/resync (balance,
// istrumento, lista ordini pendenti) — qui copriamo solo il percorso rapido posizione.
function _startTradeSSE() {
    _stopTradeSSE();
    if (!_tradeEnabled || !_isLoggedIn || !_obSymbol) return;
    try {
        const es = new EventSource(`api/trade/stream?symbol=${_obSymbol}`);
        _tradeSSE = es;
        es.addEventListener('snapshot', ev => {
            try {
                const d = JSON.parse(ev.data);
                if (d.position) { _tradePos = d.position; _hadPosition = true; _posMissingCount = 0; _renderPosition(); }
            } catch(e) {}
        });
        es.addEventListener('position', ev => {
            try {
                const d = JSON.parse(ev.data);
                // SOLO percorso "posizione aggiornata/apertao" — l'aggiornamento veloce
                // di P&L che serviva. La CHIUSURA resta gestita esclusivamente dal poll
                // REST con debounce a 5 poll (vedi _renderPosition): un push "size=0" da
                // qui si è rivelato un falso positivo (probabile riga con size=0 non
                // relativa alla posizione reale, es. hedge-mode/altro positionIdx) che
                // ha azzerato la UI SL/TP senza che la posizione fosse davvero chiusa.
                // Da NON riattivare senza aver capito la causa esatta.
                if (d.position) {
                    _tradePos = d.position; _hadPosition = true; _posMissingCount = 0;
                    _renderPosition();
                }
            } catch(e) {}
        });
    } catch(e) {}
}
function _stopTradeSSE() {
    if (_tradeSSE) { try { _tradeSSE.close(); } catch(e) {} _tradeSSE = null; }
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
    const qtyEl = document.getElementById('fs-pos-qty');
    if (qtyEl) qtyEl.textContent = `Qty: ${p.size}`;
    const liqEl = document.getElementById('fs-pos-liq');
    if (liqEl) liqEl.textContent = p.liqPrice ? `Liq: ${fmtPrice(p.liqPrice)}` : '';
    _syncPositionEntryMarker(p);
    if (!_tradeSide) { _fsSlPrice = p.stopLoss || null; _fsTpPrice = p.takeProfit || null; _drawSlTpLines(); }
}

// Una volta eseguito l'ordine (posizione aperta), la label Entry deve mostrare e restare
// fissa sul prezzo medio di ingresso REALE riportato da Bybit (p.entryPrice) — non sul
// prezzo richiesto/stimato in fase di piazzamento, che può differire per slippage
// (Market/Conditional-Market) o non avere avuto affatto una label (gli ordini Market non
// disegnano la preview, vedi setFsSide). Idempotente: se il prezzo non cambia non ridisegna.
function _syncPositionEntryMarker(p) {
    if (!_obCandleS || !p || !p.entryPrice) return;
    // Il lock scatta subito al primo poll che vede la posizione — non va dedotto da
    // _fsPendingOrderId, che si azzera solo dopo 5 poll (~15s) di assenza dell'ordine
    // dalla lista Bybit: per tutta quella finestra il drag restava permesso.
    _fsEntryIsPosition = true;
    const sideEl = document.getElementById('fs-el-side');
    if (sideEl) sideEl.style.cursor = 'default';
    if (_fsEntryLine && _fsEntryPrice === p.entryPrice) return;
    const long = p.side === 'Buy';
    _fsEntryPrice = p.entryPrice;
    if (_fsEntryLine) { try { _obCandleS.removePriceLine(_fsEntryLine); } catch(e) {} }
    _fsEntryLine = _obCandleS.createPriceLine({ price: _fsEntryPrice, color: long ? '#10b981' : '#ef4444', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
    if (!_fsEntryLabel) {
        if (!_tradeSide) _tradeSide = p.side;
        _createEntryLabel(long);
        _ensureChartRightSpace();
    }
    _updateEntryLabelPos();
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
    // "TP" resta fisso (fa parte della maniglia di drag, cursore ns-resize); solo il
    // suffisso MKT/LMT è cliccabile per scegliere il tipo — cursore a manina (pointer)
    // e stopPropagation sul mousedown così il click non fa partire anche il drag.
    const tpTypeSpan = `<span onmousedown="event.stopPropagation()" onclick="_toggleTpTypeMenu(event)" title="${window.t('tp_type_hint')}" style="cursor:pointer;">${_fsTpOrderType === 'Limit' ? 'LMT' : 'MKT'}</span>`;
    const kindLabel = kind === 'tp' ? `TP ${tpTypeSpan}` : 'SL';
    const alertIcon = `<svg width="13" height="13" viewBox="0 0 24 24" style="display:block;flex-shrink:0"><circle cx="12" cy="12" r="11" fill="#ef4444"/><rect x="11" y="5" width="2" height="9" rx="1" fill="white"/><rect x="11" y="16" width="2" height="2.5" rx="1" fill="white"/></svg>`;
    const leftContent = isInvalid ? alertIcon : kindLabel;
    const leftBg      = isInvalid ? '#3d0a0a' : '#1E222D';
    const leftColor   = isInvalid ? '#ef4444' : color;
    const label = document.createElement('div');
    label.id = `fs-${kind}-label`;
    label.style.cssText = `position:absolute;right:180px;z-index:20;display:flex;align-items:stretch;border:1px solid ${border};border-radius:4px;overflow:hidden;font-size:11px;font-weight:600;pointer-events:all;white-space:nowrap;user-select:none;transform:translateY(-50%);box-shadow:0 2px 8px rgba(0,0,0,.5);cursor:ns-resize;`;
    label.innerHTML = `
        <div onmousedown="_startSlTpLabelDrag(event,'${kind}')" ontouchstart="_startSlTpLabelDrag(event,'${kind}')" style="padding:5px 8px;background:${leftBg};color:${leftColor};cursor:ns-resize;touch-action:none;display:flex;align-items:center;gap:3px;">${leftContent}</div>
        ${infoHtml ? `<div onmousedown="_startSlTpLabelDrag(event,'${kind}')" ontouchstart="_startSlTpLabelDrag(event,'${kind}')" style="padding:5px 8px;background:${isInvalid ? '#2d0a0a' : '#1E222D'};color:${isInvalid ? '#f87171' : color};display:flex;gap:6px;align-items:center;cursor:ns-resize;touch-action:none;border-left:1px solid #2A2E39;">${infoHtml}</div>` : ''}
        <div onclick="_removeSlTpLine('${kind}')" title="${window.t('remove_sltp')}" style="padding:5px 7px;color:#6B7280;cursor:pointer;border-left:1px solid #2A2E39;background:#1E222D;" onmouseenter="this.style.background='#2A2E39'" onmouseleave="this.style.background='#1E222D'">✕</div>
    `;
    label.onmouseenter = () => _showTradeZone(kind);
    label.onmouseleave = () => _hideTradeZone();
    chartEl.appendChild(label);
    return label;
}

// ── Scelta tipo TP (Market/Limit) — click sull'etichetta "TP MKT/LMT" sul grafico.
// Il prezzo limite coincide col prezzo di trigger del TP (nessuna seconda linea
// separata da gestire) — copre il caso comune "TP come limit invece che market".
function _toggleTpTypeMenu(e) {
    e.stopPropagation();
    const existing = document.getElementById('fs-tp-type-menu');
    if (existing) { existing.remove(); return; }
    const menu = document.createElement('div');
    menu.id = 'fs-tp-type-menu';
    menu.style.cssText = `position:fixed;left:${e.clientX}px;top:${e.clientY + 10}px;z-index:9999;background:#1E222D;border:1px solid #2A2E39;border-radius:4px;overflow:hidden;font-size:11px;font-weight:600;box-shadow:0 4px 12px rgba(0,0,0,.6);min-width:90px;`;
    const opt = (type, label) => `<div onclick="_setTpOrderType('${type}')" style="padding:7px 12px;color:${_fsTpOrderType===type?'#10b981':'#B2B5BE'};cursor:pointer;" onmouseenter="this.style.background='#2A2E39'" onmouseleave="this.style.background='none'">${label}</div>`;
    menu.innerHTML = opt('Market', 'TP MKT') + `<div style="border-top:1px solid #2A2E39;"></div>` + opt('Limit', 'TP LMT');
    document.body.appendChild(menu);
    const closeOnce = ev => { if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('mousedown', closeOnce, true); } };
    setTimeout(() => document.addEventListener('mousedown', closeOnce, true), 0);
}
function _setTpOrderType(type) {
    _fsTpOrderType = type;
    document.getElementById('fs-tp-type-menu')?.remove();
    _drawSlTpLines();
    _pushTpOrderType();
}
function _pushTpOrderType() {
    if (_fsTpPrice == null || !_obSymbol) return;
    const tp = parseFloat(_fsTpPrice.toFixed(8));
    if (_tradePos) {
        const body = { symbol: _obSymbol, positionIdx: _tradePos.positionIdx ?? 0, takeProfit: tp, tpOrderType: _fsTpOrderType };
        if (_fsTpOrderType === 'Limit') body.tpLimitPrice = tp;
        fetch('api/trade/set-sltp', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) })
            .then(r => r.json()).then(d => { if (!d.success) _showTradeMsg(_bybMsg(d), false); })
            .catch(() => _showTradeMsg(window.t('err_net'), false));
    } else if (_fsPendingOrderId) {
        // Ordine ancora pendente (non riempito): stesso cambio ma via amend ordine,
        // non trading-stop posizione (non esiste ancora nessuna posizione).
        const body = { symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'Order', takeProfit: tp, tpOrderType: _fsTpOrderType };
        if (_fsTpOrderType === 'Limit') body.tpLimitPrice = tp;
        _lastAmendAt = Date.now();
        fetch('api/trade/amend', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) })
            .then(r => r.json()).then(d => { if (!d.success) _showTradeMsg(_bybMsg(d), false); })
            .catch(() => _showTradeMsg(window.t('err_net'), false));
    }
    // else: bozza pre-ordine, senza posizione né ordine pendente — il tipo scelto
    // verrà incluso al momento della creazione dell'ordine (vedi confirmFsOrder).
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
        _fsTpPrice = null; _fsTpOrderType = 'Market';
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
    _fsSlPrice = null; _fsTpPrice = null; _fsTpOrderType = 'Market';
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
    _fsTpOrderType = 'Market';
    _drawSlTpLines();
    if (_obSymbol) {
        fetch('api/trade/set-sltp', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ symbol: _obSymbol, positionIdx: _tradePos.positionIdx ?? 0, takeProfit: parseFloat(_fsTpPrice.toFixed(8)), tpOrderType: 'Market' }) })
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
                if (tpVal) {
                    body.takeProfit = parseFloat(tpVal.toFixed(8));
                    body.tpOrderType = _fsTpOrderType;
                    if (_fsTpOrderType === 'Limit') body.tpLimitPrice = parseFloat(tpVal.toFixed(8));
                }
                const d = await fetch('api/trade/set-sltp', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
                if (!d.success) _showTradeMsg(_bybMsg(d), false);
            } else if (_fsPendingOrderId) {
                // Pending order: amend
                _lastAmendAt = Date.now();
                const body = { symbol: _obSymbol, orderId: _fsPendingOrderId, orderFilter: _fsPendingOrderFilter || 'Order' };
                if (slVal) body.stopLoss = parseFloat(slVal.toFixed(8));
                if (tpVal) {
                    body.takeProfit = parseFloat(tpVal.toFixed(8));
                    body.tpOrderType = _fsTpOrderType;
                    if (_fsTpOrderType === 'Limit') body.tpLimitPrice = parseFloat(tpVal.toFixed(8));
                }
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
    const prevSide = _tradeSide;
    _tradeSide = side;
    const l = document.getElementById('fs-btn-long'), s = document.getElementById('fs-btn-short');
    if (l) { l.style.background = side === 'Buy' ? '#065f46' : '#0d1a14'; l.style.color = '#10b981'; l.style.borderColor = side === 'Buy' ? '#10b981' : '#1e3d2a'; }
    if (s) { s.style.background = side === 'Sell' ? '#7f1d1d' : '#1a0d0d'; s.style.color = '#ef4444'; s.style.borderColor = side === 'Sell' ? '#ef4444' : '#3d1e1e'; }
    if (!_obCandleS) return;
    const long = side === 'Buy';
    _removeEntryLine();
    // Cambio Long<->Short: i livelli SL/TP disegnati appartenevano all'ordine bozza nel verso
    // precedente e non hanno senso nel nuovo verso — azzerati solo qui (client-side, nessuna
    // chiamata API: se c'è già una posizione reale aperta il suo SL/TP resta invariato sull'exchange).
    if (prevSide && prevSide !== side) {
        _removeSlTpLines();
        const si = document.getElementById('fs-sl-input'); if (si) si.value = '';
        const ti = document.getElementById('fs-tp-input'); if (ti) ti.value = '';
        _hideTradeZone();
    }
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
            const drawP = price > 0 ? price : liveP;
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
        _ensureChartRightSpace();
    }
}

// Le label Entry/SL/TP sono ancorate a `right:180px` nel container del grafico:
// spinge le candele a sinistra (rightOffset) così non finiscono coperte dalle label,
// e blocca il pan (vedi subscribeVisibleLogicalRangeChange in initChart) sotto quella soglia.
// In portrait mobile lo schermo è troppo stretto per riservare 330px fissi (comprime le
// candele in una fetta minuscola): la spaziatura fissa resta disattivata, vedi [[feedback_portrait_only_scope]].
function _ensureChartRightSpace() {
    if (window.matchMedia('(max-width: 640px), (pointer: coarse)').matches) return;
    _fsChartSpacingLocked = true;
    if (!_obChart) return;
    try { _obChart.timeScale().applyOptions({ rightOffset: _fsMinOffsetBars() }); } catch(e) {}
}

function _removeEntryLine() {
    if (_fsEntryLine && _obCandleS) { try { _obCandleS.removePriceLine(_fsEntryLine); } catch(e) {} }
    _fsEntryLine = null; _fsEntryPrice = null; _fsEntryIsPosition = false;
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
    const isFilled = _fsEntryIsPosition;
    const slTpBtns = isMarket ? '' : `
        <div id="fs-el-tp" onmousedown="_startLabelDrag('tp',event)" ontouchstart="_startLabelDrag('tp',event)" title="${window.t('drag_tp')}" style="${cell}${sep}color:#10b981;cursor:grab;touch-action:none;${_fsTpPrice != null ? 'display:none;' : ''}" onmouseenter="this.style.background='#1a3028'" onmouseleave="this.style.background='#1E222D'">TP</div>
        <div id="fs-el-sl" onmousedown="_startLabelDrag('sl',event)" ontouchstart="_startLabelDrag('sl',event)" title="${window.t('drag_sl')}" style="${cell}${sep}color:#ef4444;cursor:grab;touch-action:none;${_fsSlPrice != null ? 'display:none;' : ''}" onmouseenter="this.style.background='#2d1717'" onmouseleave="this.style.background='#1E222D'">SL</div>`;
    label.innerHTML = `
        <div id="fs-el-side" onmousedown="_startEntryDrag(event)" ontouchstart="_startEntryDrag(event)" style="padding:5px 9px;background:${sideBg};color:${sideColor};cursor:${(isMarket || isFilled) ? 'default' : 'ns-resize'};touch-action:none;">${sideText}</div>
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
    // Ordine già eseguito (posizione live): il prezzo di ingresso è quello reale
    // riportato da Bybit e non è più modificabile col drag.
    if (!_obCandleS || !_tradeSide || _fsOrderType === 'Market' || _fsEntryIsPosition) return;
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
    _fsChartSpacingLocked = false;
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
            if (_fsTpPrice != null) {
                body.takeProfit = parseFloat(_fsTpPrice.toFixed(8));
                body.tpOrderType = _fsTpOrderType;
                if (_fsTpOrderType === 'Limit') body.tpLimitPrice = parseFloat(_fsTpPrice.toFixed(8));
            }
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
                        if (_fsTpPrice != null) {
                            ab.takeProfit = parseFloat(_fsTpPrice.toFixed(8));
                            ab.tpOrderType = _fsTpOrderType;
                            if (_fsTpOrderType === 'Limit') ab.tpLimitPrice = parseFloat(_fsTpPrice.toFixed(8));
                        }
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
    try {
        const d = await fetch('api/trade/close', { method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ symbol: _obSymbol, side: p.side === 'Buy' ? 'Sell' : 'Buy', qty: String(p.size) }) }).then(r => r.json());
        if (d.success) { _showTradeMsg(window.t('position_closed'), true); _tradePos = null; resetTradeSide(); const row = document.getElementById('fs-pos-row'); if (row) row.style.display = 'none'; setTimeout(loadTradeData, 2000); }
        else _showTradeMsg(_bybMsg(d), false);
    } catch(e) { _showTradeMsg(window.t('err_net'), false); }
}

async function reverseFsPosition() {
    if (!_tradePos || !_obSymbol) return;
    try {
        // qty/side vengono ricalcolati server-side dalla posizione reale su Bybit
        // (non dal client) — vedi /api/trade/reverse, stesso principio di sicurezza
        // già applicato altrove dopo l'incidente SL/TP.
        const d = await fetch('api/trade/reverse', { method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ symbol: _obSymbol }) }).then(r => r.json());
        if (d.success) { _showTradeMsg(window.t('position_reversed'), true); resetTradeSide('reverseOrder'); setTimeout(loadTradeData, 2000); }
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
let _obRangeActive = false, _obRangeP1 = null, _obRangeMD = null, _obRangeMM = null, _obRangeMU = null, _obRangeCM = null, _obRangeTouch = null;
let _obRangeHoverLine = null, _obRangeHoverMM = null, _obRangeHoverML = null;
let _obHlineActive = false, _obHlines = [], _obHlineMD = null, _obHlineMM = null, _obHlineMU = null, _obHlineCM = null, _obHlineDragging = null, _obHlineTouch = null;
let _obHlineHoverLine = null, _obHlineHoverMM = null, _obHlineHoverML = null;
let _obTrendActive = false, _obTrendlines = [], _obTrendP1 = null, _obTrendPrev = null, _obTrendPending = null, _obTrendPane = null;
let _obTrendMD = null, _obTrendMM = null, _obTrendMU = null, _obTrendCM = null, _obTrendDrag = null, _obTrendRAF = null, _obTrendTouch = null;
// Surface minimale per riusare gli helper multi-pane di draw-tools.js (getPaneRect/
// resolvePaneAtY/trendPickAtClient/...), che si aspettano un oggetto con questa forma.
const _obTrendSurface = {
    get chart() { return _obChart; },
    get candleS() { return _obCandleS; },
    get klines() { return _obKlines; },
    get rocSeries() { return _obRocSeries; },
};

// Come _timeToXRobust in mtf.html: timeToCoordinate torna null se il timestamp non
// combacia esattamente con una candela dell'asse attuale — cosa che succede sempre
// dopo un cambio TF (la trendline resta al TF su cui è stata disegnata). Si interpola
// in pixel fra i due indici logici interi che la racchiudono, così la linea resta
// visibile e trascinabile anche dopo un cambio TF invece di sparire silenziosamente.
function _obTimeToXRobust(t) {
    const direct = _obChart.timeScale().timeToCoordinate(t);
    if (direct != null) return direct;
    const kl = _obKlines;
    if (!kl || kl.length < 2) return null;
    let loIdx, hiIdx, frac;
    if (t <= kl[0].time) {
        loIdx = 0; hiIdx = 1;
        const dt = kl[1].time - kl[0].time;
        frac = dt ? (t - kl[0].time) / dt : 0;
    } else if (t >= kl[kl.length - 1].time) {
        const n = kl.length;
        loIdx = n - 2; hiIdx = n - 1;
        const dt = kl[n-1].time - kl[n-2].time;
        frac = dt ? 1 + (t - kl[n-1].time) / dt : 1;
    } else {
        let lo = 0, hi = kl.length - 1;
        while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (kl[mid].time <= t) lo = mid; else hi = mid; }
        loIdx = lo; hiIdx = hi;
        const dt = kl[hi].time - kl[lo].time;
        frac = dt ? (t - kl[lo].time) / dt : 0;
    }
    try {
        const xLo = _obChart.timeScale().logicalToCoordinate(loIdx);
        const xHi = _obChart.timeScale().logicalToCoordinate(hiIdx);
        if (xLo == null || xHi == null) return null;
        return xLo + (xHi - xLo) * frac;
    } catch(e) { return null; }
}

// Persistenza disegni (hline + trendline) per simbolo, stesso pattern di
// _mtfSaveDrawings/_mtfRestoreDrawings in mtf.html: senza questo, un refresh della
// pagina perdeva tutti i disegni (erano solo variabili JS in memoria).
function _obLoadDrawings() {
    try { return JSON.parse(localStorage.getItem('ob_drawings') || '{}'); } catch(e) { return {}; }
}
function _obSaveDrawings() {
    let json;
    try {
        const all = _obLoadDrawings();
        all[_obSymbol] = {
            trendlines: _obTrendlines.map(tl => ({ t1: tl.t1, p1: tl.p1, t2: tl.t2, p2: tl.p2, pane: tl.pane || 0 })),
            hlines: _obHlines.map(h => h.price),
        };
        json = JSON.stringify(all);
        localStorage.setItem('ob_drawings', json);
    } catch(e) { return; }
    // PUT sincrono (non il debounce di prefs-sync.js): un'azione discreta e rara come
    // completare un disegno non deve rischiare di perderlo a un refresh immediato.
    try {
        const x = new XMLHttpRequest();
        x.open('PUT', '/api/prefs/' + encodeURIComponent('ob_drawings'), false);
        x.setRequestHeader('Content-Type', 'application/json');
        x.send(JSON.stringify({ value: json }));
    } catch(e) {}
}
function _obRestoreDrawings() {
    const rec = _obLoadDrawings()[_obSymbol];
    if (!rec) return;
    if (Array.isArray(rec.trendlines) && rec.trendlines.length) {
        _obTrendlines.push(...rec.trendlines);
        _obTrendEnsureRAF();
    }
    if (Array.isArray(rec.hlines) && _obCandleS) {
        for (const price of rec.hlines) {
            try {
                const pl = _obCandleS.createPriceLine({ price, color: '#3b82f6', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
                _obHlines.push({ priceLine: pl, price });
            } catch(e) {}
        }
    }
    _syncObClearBtn();
}

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
    _obSaveDrawings();
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
    DrawTools.unwireTouch(canvas, _obRangeTouch); _obRangeTouch = null;
    if (_obRangeHoverMM) { canvas.removeEventListener('mousemove', _obRangeHoverMM); _obRangeHoverMM = null; }
    if (_obRangeHoverML) { canvas.removeEventListener('mouseleave', _obRangeHoverML); _obRangeHoverML = null; }
    if (_obRangeHoverLine) { try { _obCandleS.removePriceLine(_obRangeHoverLine); } catch(e) {} _obRangeHoverLine = null; }
    _obRangeP1 = null;
    try { const rc = canvas.getContext('2d'); rc.clearRect(0,0,canvas.width,canvas.height); } catch(e) {}
    canvas.style.pointerEvents = _obRangeActive ? 'auto' : 'none';
    canvas.style.cursor = _obRangeActive ? 'crosshair' : '';
    canvas.style.touchAction = _obRangeActive ? 'none' : '';
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
        _obRangeTouch = DrawTools.wireTouch(canvas, _obRangeMD, _obRangeMM, _obRangeMU);
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
    DrawTools.unwireTouch(canvas, _obHlineTouch); _obHlineTouch = null;
    if (_obHlineHoverMM) { canvas.removeEventListener('mousemove', _obHlineHoverMM); _obHlineHoverMM = null; }
    if (_obHlineHoverML) { canvas.removeEventListener('mouseleave', _obHlineHoverML); _obHlineHoverML = null; }
    if (_obHlineHoverLine) { try { _obCandleS?.removePriceLine(_obHlineHoverLine); } catch(e) {} _obHlineHoverLine = null; }
    _obHlineDragging = null;
    canvas.style.pointerEvents = _obHlineActive ? 'auto' : 'none';
    canvas.style.cursor = _obHlineActive ? 'crosshair' : '';
    canvas.style.touchAction = _obHlineActive ? 'none' : '';
    if (_obHlineActive) {
        _obHlineMD = e => {
            if (e.button !== 0) return; e.preventDefault();
            const nearest = _obHlineNearest(e.clientY);
            if (nearest) { _obHlineDragging = nearest; canvas.style.cursor = 'ns-resize'; }
            else {
                const rect = canvas.getBoundingClientRect();
                const price = _obCandleS?.coordinateToPrice(e.clientY - rect.top);
                if (price != null) { const pl = _obCandleS.createPriceLine({ price, color: '#3b82f6', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' }); _obHlines.push({ priceLine: pl, price }); _syncObClearBtn(); _obSaveDrawings(); }
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
            if (_obHlineDragging) { _obHlineDragging = null; _obSaveDrawings(); }
            canvas.style.cursor = _obHlineNearest(e.clientY) ? 'ns-resize' : 'crosshair';
        };
        _obHlineCM = e => {
            e.preventDefault();
            const nearest = _obHlineNearest(e.clientY);
            if (nearest) { try { _obCandleS.removePriceLine(nearest.priceLine); } catch(ex) {} _obHlines = _obHlines.filter(h => h !== nearest); _syncObClearBtn(); _obSaveDrawings(); }
            else toggleObHline();
        };
        canvas.addEventListener('mousedown', _obHlineMD);
        document.addEventListener('mousemove', _obHlineMM);
        document.addEventListener('mouseup', _obHlineMU);
        canvas.addEventListener('contextmenu', _obHlineCM);
        _obHlineTouch = DrawTools.wireTouch(canvas, _obHlineMD, _obHlineMM, _obHlineMU);
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
const _OB_TREND_CLICK_SLOP = 4;

function _obTrendSync(canvas) { const w = canvas.clientWidth, h = canvas.clientHeight; if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; } }

function _obTrendDrawAll() {
    _syncObClearBtn();
    const canvas = document.getElementById('ob-trend-canvas');
    if (!canvas || !_obCandleS || !_obChart) return;
    _obTrendSync(canvas);
    const ctx = canvas.getContext('2d'), W = canvas.width, H = canvas.height;
    // La retta si ferma al bordo del pannello candele, prima della colonna prezzo
    // (il canvas del tool è largo quanto l'intero container, asse compreso).
    let paneW = W;
    try { const aw = _obChart.priceScale('right').width(); if (Number.isFinite(aw)) paneW = Math.max(0, W - aw); } catch(e) {}
    ctx.clearRect(0, 0, W, H);
    for (const tl of _obTrendlines) {
        try {
            const paneIndex = tl.pane || 0;
            const c1 = DrawTools.trendCanvasY(_obTrendSurface, canvas, paneIndex, tl.p1), c2 = DrawTools.trendCanvasY(_obTrendSurface, canvas, paneIndex, tl.p2);
            const x1 = _obTimeToXRobust(tl.t1), x2 = _obTimeToXRobust(tl.t2);
            if (x1==null||!c1||x2==null||!c2) continue;
            const y1 = c1.y, y2 = c2.y, paneTop = c1.paneRect.top - c1.canvasRect.top;
            // Confina la retta estesa (e i marker) dentro il proprio pane, così non
            // sbrodola sull'altro pane quando la pendenza è estrema (es. pane ROC piccolo).
            ctx.save();
            ctx.beginPath(); ctx.rect(0, paneTop, W, c1.paneRect.height); ctx.clip();
            ctx.strokeStyle = '#22c55e'; ctx.lineWidth = 1.5; ctx.beginPath();
            if (Math.abs(x2-x1) < 0.5) { ctx.moveTo(x1,paneTop); ctx.lineTo(x1,paneTop+c1.paneRect.height); }
            else { const m=(y2-y1)/(x2-x1); ctx.moveTo(0,y1+m*-x1); ctx.lineTo(paneW,y1+m*(paneW-x1)); }
            ctx.stroke();
            ctx.fillStyle = '#22c55e';
            ctx.beginPath(); ctx.arc(x1,y1,4,0,Math.PI*2); ctx.fill();
            ctx.beginPath(); ctx.arc(x2,y2,4,0,Math.PI*2); ctx.fill();
            ctx.restore();
        } catch(ex) {}
    }
    if (_obTrendP1 && _obTrendPrev) {
        try {
            const paneIndex = _obTrendPane || 0;
            const x1 = _obChart.timeScale().timeToCoordinate(_obTrendP1.t);
            const c1 = DrawTools.trendCanvasY(_obTrendSurface, canvas, paneIndex, _obTrendP1.p);
            if (x1!=null && c1) {
                ctx.strokeStyle = '#22c55e'; ctx.lineWidth = 1; ctx.setLineDash([5,3]);
                ctx.beginPath(); ctx.moveTo(x1,c1.y); ctx.lineTo(_obTrendPrev.x, _obTrendPrev.y); ctx.stroke();
                ctx.setLineDash([]);
                ctx.fillStyle = '#22c55e'; ctx.beginPath(); ctx.arc(x1,c1.y,4,0,Math.PI*2); ctx.fill();
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
            const paneIndex = tl.pane || 0;
            const c1 = DrawTools.trendCanvasY(_obTrendSurface, canvas, paneIndex, tl.p1), c2 = DrawTools.trendCanvasY(_obTrendSurface, canvas, paneIndex, tl.p2);
            const x1 = _obTimeToXRobust(tl.t1), x2 = _obTimeToXRobust(tl.t2);
            if (x1==null||!c1||x2==null||!c2) continue;
            const y1 = c1.y, y2 = c2.y;
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
    DrawTools.unwireTouch(canvas, _obTrendTouch); _obTrendTouch = null;
    _obTrendP1 = null; _obTrendPrev = null; _obTrendPane = null; _obTrendDrag = null; _obTrendPending = null;
    canvas.style.pointerEvents = _obTrendActive ? 'auto' : 'none';
    canvas.style.cursor = _obTrendActive ? 'crosshair' : '';
    canvas.style.touchAction = _obTrendActive ? 'none' : '';
    _obTrendEnsureRAF();
    _obTrendDrawAll();
    if (!_obTrendActive) return;
    _obTrendMD = e => {
        if (e.button !== 0) return; e.preventDefault();
        const rect = canvas.getBoundingClientRect(), px = e.clientX-rect.left, py = e.clientY-rect.top;
        const hit = _obTrendNearest(e.clientX, e.clientY);
        if (hit && !_obTrendP1) {
            // Non decidere subito se è drag o un click per iniziare una nuova linea:
            // serve poter piazzare due trendline dallo stesso punto esatto (vedi mousemove/mouseup).
            _obTrendPending = { hit, px, py };
        } else if (!_obTrendP1) {
            const r = DrawTools.trendPickAtClient(_obTrendSurface, e.clientY, px);
            if (r) { _obTrendP1 = r; _obTrendPane = r.pane; }
        } else {
            const r2 = DrawTools.trendPickAtPane(_obTrendSurface, _obTrendPane || 0, e.clientY, px, _obTrendP1.t);
            if (r2) {
                _obTrendlines.push({ t1: _obTrendP1.t, p1: _obTrendP1.p, t2: r2.t, p2: r2.p, pane: _obTrendPane || 0 });
                _obSaveDrawings();
            }
            _obTrendP1 = null; _obTrendPrev = null; _obTrendPane = null;
            _obTrendDrawAll();
        }
    };
    _obTrendMM = e => {
        const rect = canvas.getBoundingClientRect(), px = e.clientX-rect.left, py = e.clientY-rect.top;
        if (_obTrendPending) {
            if (Math.hypot(px - _obTrendPending.px, py - _obTrendPending.py) <= _OB_TREND_CLICK_SLOP) return;
            const { hit, px: dpx0, py: dpy0 } = _obTrendPending;
            if (hit.part !== 'line') { _obTrendDrag = { tl: hit.tl, part: hit.part }; canvas.style.cursor = 'grabbing'; }
            else {
                const dragPane = hit.tl.pane || 0;
                const c1 = DrawTools.trendCanvasY(_obTrendSurface, canvas, dragPane, hit.tl.p1), c2 = DrawTools.trendCanvasY(_obTrendSurface, canvas, dragPane, hit.tl.p2);
                const ox1 = _obTimeToXRobust(hit.tl.t1), ox2 = _obTimeToXRobust(hit.tl.t2);
                _obTrendDrag = { tl: hit.tl, part: 'line', sx: dpx0, sy: dpy0, ox1, oy1: c1 && c1.y, ox2, oy2: c2 && c2.y };
                canvas.style.cursor = 'move';
            }
            _obTrendPending = null;
        }
        if (_obTrendDrag) {
            if (!(e.buttons & 1)) { _obTrendDrag = null; return; }
            const { tl, part } = _obTrendDrag;
            const dragPane = tl.pane || 0;
            if (part === 'p1') { const r = DrawTools.trendPickAtPane(_obTrendSurface, dragPane, e.clientY, px, tl.t2); if (r) { tl.t1=r.t; tl.p1=r.p; } }
            else if (part === 'p2') { const r = DrawTools.trendPickAtPane(_obTrendSurface, dragPane, e.clientY, px, tl.t1); if (r) { tl.t2=r.t; tl.p2=r.p; } }
            else {
                const series = DrawTools.trendSeriesForPane(_obTrendSurface, dragPane), paneRect = DrawTools.getPaneRect(_obChart, dragPane);
                if (series && paneRect) {
                    const canvasRect = canvas.getBoundingClientRect(), offsetY = paneRect.top - canvasRect.top;
                    const dpx=px-_obTrendDrag.sx, dpy=py-_obTrendDrag.sy;
                    const nt1=_obChart.timeScale().coordinateToTime(_obTrendDrag.ox1+dpx), np1=series.coordinateToPrice(_obTrendDrag.oy1+dpy-offsetY);
                    const nt2=_obChart.timeScale().coordinateToTime(_obTrendDrag.ox2+dpx), np2=series.coordinateToPrice(_obTrendDrag.oy2+dpy-offsetY);
                    if(nt1&&np1&&nt2&&np2){tl.t1=nt1;tl.p1=np1;tl.t2=nt2;tl.p2=np2;}
                }
            }
            _obTrendDrawAll();
        } else if (_obTrendP1) {
            _obTrendPrev = { x: px, y: py };
            _obTrendDrawAll();
        } else {
            const hit = _obTrendNearest(e.clientX, e.clientY);
            canvas.style.cursor = hit ? (hit.part === 'line' ? 'move' : 'grab') : 'crosshair';
        }
    };
    _obTrendMU = e => {
        if (e.button !== 0) return;
        if (_obTrendPending) {
            // Click senza drag su un punto/linea esistente → inizia una nuova trendline da qui.
            const { px: cpx } = _obTrendPending;
            const r = DrawTools.trendPickAtClient(_obTrendSurface, e.clientY, cpx);
            if (r) { _obTrendP1 = r; _obTrendPane = r.pane; }
            _obTrendPending = null;
            return;
        }
        if (_obTrendDrag) { _obTrendDrag = null; _obSaveDrawings(); }
        canvas.style.cursor = 'crosshair';
    };
    _obTrendCM = e => {
        e.preventDefault();
        if (_obTrendP1) { _obTrendP1 = null; _obTrendPrev = null; _obTrendPane = null; _obTrendDrawAll(); return; }
        const hit = _obTrendNearest(e.clientX, e.clientY);
        if (hit) { _obTrendlines = _obTrendlines.filter(tl => tl !== hit.tl); _obTrendDrawAll(); _obSaveDrawings(); }
        else toggleObTrend();
    };
    canvas.addEventListener('mousedown', _obTrendMD);
    document.addEventListener('mousemove', _obTrendMM);
    document.addEventListener('mouseup', _obTrendMU);
    canvas.addEventListener('contextmenu', _obTrendCM);
    _obTrendTouch = DrawTools.wireTouch(canvas, _obTrendMD, _obTrendMM, _obTrendMU);
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
                obj[tf] = { text: _obCdFmt(rem, tf === 'D' || tf === '240'), color: warn ? '#FF9C2E' : '#F3F4F6', blink };
            }
            tfCountdowns.value = obj;
        };

        // Striscia colori GRaB per TF (una candela per ciascuno dei 6 TF standard,
        // indipendente dal TF mostrato sul grafico principale) — fetch leggero via
        // api/klines (cache 15s lato server) invece di mantenere stato WS live per
        // ogni TF: aggiornamento "quasi in tempo reale" (ogni 5s), non tick-by-tick,
        // sufficiente per una barra di sintesi sotto ai bottoni TF. SEMPRE attiva,
        // indipendente dal bottone GRaB (che controlla solo la ricolorazione delle
        // candele sul grafico) — richiesta esplicita dell'utente 2026-07-24.
        const grabTfColors = ref({});
        const grabTfTitle = (tf) => {
            const g = grabTfColors.value[tf.v];
            return g ? GRAB_STATE_LABEL[g.state] : '';
        };
        let _grabTfTick = 0;
        const _updateGrabTfStrip = async () => {
            const cfg = getGrabCfg();
            const period = cfg.emaPeriod;
            const need = period + 5;
            const sym = symbol.value;
            const results = {};
            await Promise.all(TF_OPTIONS.map(async (tf) => {
                try {
                    const res = await fetch(`api/klines?symbol=${sym}&interval=${tf.v}`);
                    const data = await res.json();
                    if (!data.success || !data.data || data.data.length < need) return;
                    const candles = data.data;
                    const eh = calcEMAField(candles, period, 'high');
                    const el = calcEMAField(candles, period, 'low');
                    const last = candles[candles.length - 1];
                    const eHigh = eh[eh.length - 1].value, eLow = el[el.length - 1].value;
                    results[tf.v] = {
                        color: grabBarColor(last.close, last.open, eHigh, eLow, cfg),
                        state: grabBarState(last.close, last.open, eHigh, eLow),
                    };
                } catch(e) { /* skip tf */ }
            }));
            if (sym === symbol.value) grabTfColors.value = results;
        };

        // Striscia colori Canale SMA 20 per TF, stesso pattern/frequenza della striscia
        // GRaB sopra: verde (lime GRaB) se il prezzo è sopra il canale, rosso se sotto.
        // Se è dentro il canale, si riusano gli stessi colori/etichette configurati per
        // GRaB "dentro range" (colorMidBull/colorMidBear), scegliendo bull/bear in base
        // alla posizione rispetto alla linea mediana (mid, la SMA di riferimento del
        // canale) anziché a close vs open come fa GRaB. SEMPRE attiva indipendente dal
        // toggle Canale sul grafico, per lo stesso motivo della striscia GRaB.
        const CHANNEL_TF_COLORS = { above: '#00FF00', below: '#EF454A' };
        const CHANNEL_TF_LABEL  = { above: 'Prezzo sopra il canale', below: 'Prezzo sotto il canale', insideBull: 'Dentro range (rialzo)', insideBear: 'Dentro range (ribasso)' };
        const channelTfColors = ref({});
        const channelTfTitle = (tf) => {
            const c = channelTfColors.value[tf.v];
            return c ? CHANNEL_TF_LABEL[c.state] : '';
        };
        const _updateChannelTfStrip = async () => {
            const period = getChannelCfg().period;
            const need = period + 1;
            const sym = symbol.value;
            const grabCfg = getGrabCfg();
            const results = {};
            await Promise.all(TF_OPTIONS.map(async (tf) => {
                try {
                    const res = await fetch(`api/klines?symbol=${sym}&interval=${tf.v}`);
                    const data = await res.json();
                    if (!data.success || !data.data || data.data.length < need) return;
                    const candles = data.data;
                    const ch = calcSmaChannel(candles, period);
                    if (!ch.upper.length) return;
                    const last = candles[candles.length - 1];
                    const upper = ch.upper[ch.upper.length - 1].value, lower = ch.lower[ch.lower.length - 1].value;
                    const mid = ch.mid[ch.mid.length - 1].value;
                    let state, color;
                    if (last.close > upper) { state = 'above'; color = CHANNEL_TF_COLORS.above; }
                    else if (last.close < lower) { state = 'below'; color = CHANNEL_TF_COLORS.below; }
                    else if (last.close >= mid) { state = 'insideBull'; color = grabCfg.colorMidBull; }
                    else { state = 'insideBear'; color = grabCfg.colorMidBear; }
                    results[tf.v] = { color, state };
                } catch(e) { /* skip tf */ }
            }));
            if (sym === symbol.value) channelTfColors.value = results;
        };

        // Striscia colori ROC per TF, stesso pattern/frequenza delle due strisce sopra:
        // verde se l'ultimo valore ROC è sopra lo zero (momentum rialzista), rosso se
        // sotto. Riusa i colori configurati dall'utente per l'indicatore ROC stesso
        // (upColor/downColor di getRocCfg()) così resta coerente con eventuali
        // personalizzazioni fatte nel pannello Indicatori. SEMPRE attiva, indipendente
        // dal toggle ROC sul grafico, stesso motivo delle altre due strisce.
        const ROC_TF_LABEL = { above: 'ROC sopra 0 (momentum rialzista)', below: 'ROC sotto 0 (momentum ribassista)' };
        const rocTfColors = ref({});
        const rocTfTitle = (tf) => {
            const r = rocTfColors.value[tf.v];
            return r ? ROC_TF_LABEL[r.state] : '';
        };
        const _updateRocTfStrip = async () => {
            const cfg = getRocCfg();
            const length = cfg.length;
            const need = length + 2;
            const sym = symbol.value;
            const results = {};
            await Promise.all(TF_OPTIONS.map(async (tf) => {
                try {
                    const res = await fetch(`api/klines?symbol=${sym}&interval=${tf.v}`);
                    const data = await res.json();
                    if (!data.success || !data.data || data.data.length < need) return;
                    const roc = calcRoc(data.data, length);
                    if (!roc.length) return;
                    const last = roc[roc.length - 1].value;
                    const state = last >= 0 ? 'above' : 'below';
                    results[tf.v] = { color: state === 'above' ? cfg.upColor : cfg.downColor, state };
                } catch(e) { /* skip tf */ }
            }));
            if (sym === symbol.value) rocTfColors.value = results;
        };
        let lastConfirmedTime  = 0;
        let obKlineCount = 0;
        let hoverPriceLine = null;
        let obAskLine = null;
        let obBidLine = null;
        let dayHighLine = null, dayLowLine = null, prevHighLine = null, prevLowLine = null;
        let _dayHighPrice = null, _dayLowPrice = null, _prevHighPrice = null, _prevLowPrice = null;
        const showObLines   = ref(window.getIndActive('obLevels'));
        const nakedChart    = ref(false);
        const showTradePanel = ref(true);

        const _clearDayLines = () => {
            for (const ref of [dayHighLine, dayLowLine, prevHighLine, prevLowLine])
                if (ref && candleS) try { candleS.removePriceLine(ref); } catch(e) {}
            dayHighLine = dayLowLine = prevHighLine = prevLowLine = null;
        };
        const _applyDayPrevLines = () => {
            _clearDayLines();
            if (!candleS || nakedChart.value) return;
            const lv = getLvCfg();
            if (_dayHighPrice  != null && lv.dayHigh.vis?.ob  !== false) dayHighLine  = candleS.createPriceLine({price:_dayHighPrice,  color:lv.dayHigh.color,  lineWidth:lv.dayHigh.width,  lineStyle:lv.dayHigh.style,  axisLabelVisible:false, title:''});
            if (_dayLowPrice   != null && lv.dayLow.vis?.ob   !== false) dayLowLine   = candleS.createPriceLine({price:_dayLowPrice,   color:lv.dayLow.color,   lineWidth:lv.dayLow.width,   lineStyle:lv.dayLow.style,   axisLabelVisible:false, title:''});
            if (_prevHighPrice != null && lv.prevHigh.vis?.ob !== false) prevHighLine = candleS.createPriceLine({price:_prevHighPrice, color:lv.prevHigh.color, lineWidth:lv.prevHigh.width, lineStyle:lv.prevHigh.style, axisLabelVisible:false, title:''});
            if (_prevLowPrice  != null && lv.prevLow.vis?.ob  !== false) prevLowLine  = candleS.createPriceLine({price:_prevLowPrice,  color:lv.prevLow.color,  lineWidth:lv.prevLow.width,  lineStyle:lv.prevLow.style,  axisLabelVisible:false, title:''});
        };
        const toggleDayLevelsAll = () => {
            const lv = getLvCfg();
            const newVal = !lv.dayHigh.vis.ob;
            lv.dayHigh.vis.ob = newVal; lv.dayLow.vis.ob = newVal;
            try { localStorage.setItem('chart_levels_cfg', JSON.stringify(lv)); } catch(e) {}
            _applyDayPrevLines();
        };
        const togglePrevLevelsAll = () => {
            const lv = getLvCfg();
            const newVal = !lv.prevHigh.vis.ob;
            lv.prevHigh.vis.ob = newVal; lv.prevLow.vis.ob = newVal;
            try { localStorage.setItem('chart_levels_cfg', JSON.stringify(lv)); } catch(e) {}
            _applyDayPrevLines();
        };
        let _athVal = null, _atlVal = null, athLine = null, atlLine = null;
        const _applyAthAtlLines = () => {
            if (athLine && candleS) { try { candleS.removePriceLine(athLine); } catch(e) {} athLine = null; }
            if (atlLine && candleS) { try { candleS.removePriceLine(atlLine); } catch(e) {} atlLine = null; }
            if (!candleS || nakedChart.value) return;
            const lv = getLvCfg();
            if (_athVal != null && lv.ath.vis?.ob !== false) athLine = candleS.createPriceLine({price:_athVal, color:lv.ath.color, lineWidth:lv.ath.width, lineStyle:lv.ath.style, axisLabelVisible:false, title:''});
            if (_atlVal != null && lv.atl.vis?.ob !== false) atlLine = candleS.createPriceLine({price:_atlVal, color:lv.atl.color, lineWidth:lv.atl.width, lineStyle:lv.atl.style, axisLabelVisible:false, title:''});
        };
        const toggleAthAtlAll = () => {
            const lv = getLvCfg();
            const newVal = !lv.ath.vis.ob;
            lv.ath.vis.ob = newVal; lv.atl.vis.ob = newVal;
            try { localStorage.setItem('chart_levels_cfg', JSON.stringify(lv)); } catch(e) {}
            _applyAthAtlLines();
        };
        async function _fetchAthAtl(sym) {
            try {
                const r = await fetch(`api/ath-atl?symbol=${sym}`);
                const j = await r.json();
                if (j.status === 'pending') { setTimeout(() => { if (symbol.value === sym) _fetchAthAtl(sym); }, 3000); return; }
                if (j.status !== 'ok' || symbol.value !== sym) return;
                _athVal = j.ath; _atlVal = j.atl;
                _applyAthAtlLines();
            } catch(e) {}
        }

        // ── book state ────────────────────────────────────────────────────────
        const _isPortraitMobile = window.matchMedia('(max-width: 640px), (pointer: coarse)').matches;
        const displayLevels  = ref(_isPortraitMobile ? 10 : 20);
        const grouping       = ref(0);
        const groupingOptions = ref([]);
        const levelsDdOpen    = ref(false);
        const groupingDdOpen  = ref(false);
        const tfDdOpen        = ref(false);
        const isPortraitMobile = _isPortraitMobile;
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
        const showBook       = ref(localStorage.getItem('ob_show_book') !== '0');

        const maxLevelDistance = ref({
            askPrice: 0, askPercent: '0.00',
            bidPrice: 0, bidPercent: '0.00'
        });


        const asksMap = new Map();
        const bidsMap = new Map();
        let bookWS = null;
        let reconnectTimer = null;

        // Coalesce updateDisplay()/renderMaxSafeSize() (sort O(200) + rebuild array
        // reattivi Vue + calcPressure) a 1 per frame invece che 1 per messaggio WS:
        // su un book liquido i messaggi orderbook.200 arrivano molto più spesso di
        // 60/s, e il rendering non serve più frequente di quanto l'occhio percepisca.
        // asksMap/bidsMap restano aggiornate in modo sincrono ad ogni messaggio
        // (processOrderBook) — solo il render pesante viene rimandato al prossimo frame.
        let _obRenderPending = false;
        const _obScheduleRender = () => {
            if (_obRenderPending) return;
            _obRenderPending = true;
            requestAnimationFrame(() => {
                _obRenderPending = false;
                updateDisplay();
                renderMaxSafeSize();
            });
        };

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

            _obSyncIndicatorButtonColors();
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
                candleS.attachPrimitive(new _ObChannelFillPrimitive());
            } catch(e) {}
            const lineBase = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
            for (const { p, color, width, style } of EMA_CFG)
                emaS[p] = addSeries(obChart, 'LineSeries', { ...lineBase, color, lineWidth: width + 0.5, lineStyle: style ?? 0 });

            const _resetChartView = () => { if (obKlineCount) { const n = DEFAULT_CANDLES[chartTF.value]||80; const off = _fsChartSpacingLocked ? _fsMinOffsetBars(obChart) : 3; obChart.timeScale().setVisibleLogicalRange({ from: Math.max(0, obKlineCount-n), to: obKlineCount+off }); obChart.priceScale('right').applyOptions({ autoScale: true }); } };
            obChart.subscribeDblClick(_resetChartView);
            // Con le label Entry/SL/TP attive (_fsChartSpacingLocked), impedisce col pan di
            // avvicinare le candele all'area riservata alle label oltre la distanza minima (in pixel).
            obChart.timeScale().subscribeVisibleLogicalRangeChange(() => {
                if (!_fsChartSpacingLocked) return;
                const minBars = _fsMinOffsetBars(obChart);
                const pos = obChart.timeScale().scrollPosition();
                if (pos < minBars) obChart.timeScale().scrollToPosition(minBars, false);
            });
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
            _obEmaS    = emaS;
            _obLastEMA = lastEMA;
            window.toggleDayLevelsAll  = toggleDayLevelsAll;
            window.togglePrevLevelsAll = togglePrevLevelsAll;
            window.toggleAthAtlAll     = toggleAthAtlAll;
            window.toggleObLines       = toggleObLines;
            window._obShowLinesValue   = () => showObLines.value;
            window._obApplyDayPrevLines = _applyDayPrevLines;
            window._obApplyAthAtlLines  = _applyAthAtlLines;
            window._obUpdateObLines     = updateObLines;
            _obRestoreDrawings();
            if (_obTrendlines.length || _obTrendActive) _obTrendEnsureRAF();
            initSlTpDrag();
            DrawTools.attachToolContextMenu(document.getElementById('ob-chart-container'), {
                isAnyToolActive: () => _obRangeActive || _obHlineActive || _obTrendActive,
                onTrend: toggleObTrend, onHline: toggleObHline, onRange: toggleObRange,
                onClear: clearObDrawings,
                hasDrawings: () => !!(_obHlines.length || _obTrendlines.length),
            });
        };

        const loadChartData = async (tf) => {
            if (!candleS) return;
            _obChartTF = tf;
            _obLoadSeq++;
            const _mySeq = _obLoadSeq;
            try {
                // Le linee Day/Prev H/L usano solo le ultime 2 candele D: partite in parallelo
                // al fetch principale, e su /api/klines/live (una sola chiamata Bybit, cache
                // ws_manager) invece di /api/klines (pagina fino a 5 richieste Bybit in sequenza
                // per l'intero storico daily) — prima girava DOPO il fetch principale sullo
                // stesso endpoint pesante, raddoppiando l'attesa ad ogni cambio TF.
                const dayPromise = fetch(`api/klines/live?symbol=${symbol.value}&interval=D`).then(r => r.json()).catch(() => null);

                const r = await fetch(`api/klines?symbol=${symbol.value}&interval=${tf}`);
                const j = await r.json();
                if (!j.success || !j.data || !j.data.length) return;
                // Se nel frattempo è partito un altro changeChartTF (TF cambiato di nuovo
                // prima che questa fetch finisse), questa risposta è superata: scartarla
                // evita di sovrascrivere le candele del TF corrente con dati vecchi.
                if (_obLoadSeq !== _mySeq) return;
                const klines = j.data;
                if (j.utc_offset_s != null) { _obTzOffset = j.utc_offset_s; _obTzOffsetG = j.utc_offset_s; }

                candleS.setData(klines);
                if (klines.length) { _cdLastPrice = klines[klines.length-1].close; _cdLastOpen = klines[klines.length-1].open; _obLiveCandle = { ...klines[klines.length-1] }; }
                obKlineCount = klines.length;
                _obKlines = klines;
                if (_obBbActive) _obApplyBB();
                if (_obRocActive) _obApplyRoc();
                if (_obEmaCustomActive) _obApplyEmaCustom();
                _syncObEmaCustomMtfTimer();
                if (_obEmaCustom2Active) _obApplyEmaCustom2();
                _syncObEmaCustom2MtfTimer();
                if (_obChActive) _obApplyChannel();
                if (_obGrabActive || _obGrabMidlineActive) _obApplyGrab();
                _applyCandleStyle(candleS);
                candleS.applyOptions({ priceFormat: getPriceFormat(klines[klines.length - 1]?.close) });

                for (const { p, enabled } of EMA_CFG) {
                    if (enabled === false) { emaS[p].setData([]); lastEMA[p] = null; continue; }
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
                const _applyRange = () => {
                    if (obChart) { const off = _fsChartSpacingLocked ? _fsMinOffsetBars(obChart) : 3; obChart.timeScale().setVisibleLogicalRange({ from: Math.max(0, obKlineCount - _rn), to: obKlineCount + off }); }
                    // Le label Entry/SL/TP/Exec sono div assoluti posizionati via priceToCoordinate:
                    // il cambio TF sposta la price scale (nuovi dati + autoScale), vanno riallineate.
                    _updateAllLabels();
                };
                _applyRange();
                requestAnimationFrame(_applyRange);
                if (obChart) obChart.priceScale('right').applyOptions({ autoScale: true });

                // Day / Prev H/L lines
                _clearDayLines();
                _dayHighPrice = _dayLowPrice = _prevHighPrice = _prevLowPrice = null;
                if (athLine && candleS) { try { candleS.removePriceLine(athLine); } catch(e) {} athLine = null; }
                if (atlLine && candleS) { try { candleS.removePriceLine(atlLine); } catch(e) {} atlLine = null; }
                _athVal = _atlVal = null;
                _fetchAthAtl(symbol.value);
                try {
                    const jd = await dayPromise;
                    if (_obLoadSeq !== _mySeq) return;
                    if (jd && jd.success && jd.data && jd.data.length) {
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
            const sock = new WebSocket('wss://stream.bybit.com/v5/public/linear');
            obKlineWS = sock;
            sock.onopen = () => {
                sock.send(JSON.stringify({ op: 'subscribe', args: [`kline.${tf}.${symbol.value}`] }));
            };
            sock.onmessage = (event) => {
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
                // Il confirm=true della barra appena chiusa può arrivare da Bybit con un
                // filo di ritardo rispetto al roll lato client (_obRollNewBarIfNeeded, guidato
                // dal book molto più frequente della kline WS): se nel frattempo _obLiveCandle
                // è già avanzata alla barra nuova, questo messaggio "vecchio" va scartato —
                // altrimenti riporta _obLiveCandle indietro e il prossimo roll riapre la barra
                // nuova da zero, cancellando il movimento reale già accumulato (doji fantasma
                // seguito dalla barra successiva coi dati veri, bug segnalato 2026-08-24).
                if (_obLiveCandle && candle.time < _obLiveCandle.time) return;
                if (_obGrabActive) _obGrabColorCandle(candle);
                else try { candleS.update(candle); } catch(e) {}
                // Canale SMA20: prima veniva aggiornato SOLO dal polling REST ogni 3s (sotto),
                // mai da qui — quindi la banda restava ferma per secondi mentre candela/prezzo
                // si muovevano col book (più frequente), poi "scattava" di colpo al valore
                // corretto: il salto ripetuto è il disegno seghettato segnalato sulle ultime
                // candele.
                if (_obChActive) _obChUpdateTail([..._obKlines, candle]);
                _cdLastPrice = candle.close; _cdLastOpen = candle.open;
                _obLiveCandle = { ...candle };
            };
            sock.onerror = () => {};
            sock.onclose = () => { obKlineWSTimer = setTimeout(() => startKlineWS(chartTF.value), 4000); };
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
                        // Il buffer server-side (ws_manager) crea la barra nuova solo al primo
                        // tick Bybit ricevuto per quel bar_start — per ~1-1.5s dopo ogni boundary
                        // può quindi rispondere ancora con la barra VECCHIA come "last". Se nel
                        // frattempo il roll lato client (_obRollNewBarIfNeeded, guidato dal book,
                        // più reattivo) è già avanzato, applicare questa risposta stale
                        // riporterebbe indietro _obLiveCandle: il prossimo roll riaprirebbe la
                        // barra nuova da zero, cancellando il movimento reale già accumulato
                        // (stesso bug/fix della kline WS diretta sopra, bug segnalato 2026-08-24,
                        // qui però proveniva dal poll REST — non coperto dal primo fix).
                        if (_obLiveCandle && last.time < _obLiveCandle.time) { chartPollTimer = setTimeout(poll, 3000); return; }
                        _cdLastPrice = last.close; _cdLastOpen = last.open; _obLiveCandle = { ...last };
                        // Update the last two candles (current forming + previous if just confirmed)
                        for (const k of candles.slice(-2)) {
                            try { candleS.update(k); } catch(e) {}
                        }
                        // Update EMAs: if a new candle started, lock in the previous EMA
                        const justConfirmed = prev && prev.time > lastConfirmedTime;
                        if (justConfirmed) {
                            for (const { p } of EMA_CFG) {
                                if (lastEMA[p] == null) continue;
                                const ek = 2 / (p + 1);
                                lastEMA[p] = prev.close * ek + lastEMA[p] * (1 - ek);
                            }
                            if (_obLastEmaCustom != null) {
                                const eck = 2 / (getEmaCustomCfg().length + 1);
                                _obLastEmaCustom = prev.close * eck + _obLastEmaCustom * (1 - eck);
                            }
                            if (_obLastEmaCustom2 != null) {
                                const eckB = 2 / (getEmaCustom2Cfg().length + 1);
                                _obLastEmaCustom2 = prev.close * eckB + _obLastEmaCustom2 * (1 - eckB);
                            }
                            _obGrabConfirmPrev(prev);
                            lastConfirmedTime = prev.time;
                        }
                        // Live EMA for current forming candle
                        for (const { p } of EMA_CFG) {
                            if (lastEMA[p] == null) continue;
                            const ek   = 2 / (p + 1);
                            const live = last.close * ek + lastEMA[p] * (1 - ek);
                            try { emaS[p].update({ time: last.time, value: live }); } catch(e) {}
                        }
                        if (_obEmaCustomActive && _obEmaCustomSeries && _obLastEmaCustom != null) {
                            const eck2 = 2 / (getEmaCustomCfg().length + 1);
                            const live2 = last.close * eck2 + _obLastEmaCustom * (1 - eck2);
                            try { _obEmaCustomSeries.update({ time: last.time, value: live2 }); } catch(e) {}
                        }
                        if (_obEmaCustom2Active && _obEmaCustom2Series && _obLastEmaCustom2 != null) {
                            const eckB2 = 2 / (getEmaCustom2Cfg().length + 1);
                            const liveB2 = last.close * eckB2 + _obLastEmaCustom2 * (1 - eckB2);
                            try { _obEmaCustom2Series.update({ time: last.time, value: liveB2 }); } catch(e) {}
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
                        if (_obRocActive && _obRocSeries) {
                            const rocCfg = getRocCfg();
                            if (_obKlines.length > rocCfg.length) {
                                const rocPrev = _obKlines[_obKlines.length - 1 - rocCfg.length].close;
                                _obRocSeries.update({ time: last.time, value: 100 * (last.close - rocPrev) / rocPrev });
                            }
                        }
                        _obGrabUpdateTail(last, false);
                        // NON richiamare _obChUpdateTail qui: il WS (startKlineWS) aggiorna già
                        // il canale in tempo reale ad ogni tick sulla candela in formazione. Farlo
                        // anche qui con il close del polling REST (fino a 3s più vecchio) creava un
                        // dente di sega — il valore veniva tirato indietro ogni 3s prima che il
                        // prossimo tick WS lo ricorreggesse. Bug reale segnalato con screenshot,
                        // vedi _obChUpdateTail in startKlineWS.
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
            stopKlineWS();
            _clearDayLines();
            candleS.setData([]);
            // La candela in formazione del TF precedente resta valorizzata finché loadChartData
            // non risponde: senza azzerarla, il patch mid-price del book (riga ~3052) la
            // reinietta sulla serie appena svuotata — un'unica barra con l'H/L del vecchio TF,
            // che sull'autoScale appare come una candela enorme finché il fetch non la sostituisce.
            _obLiveCandle = null;
            for (const { p } of EMA_CFG) { emaS[p].setData([]); lastEMA[p] = null; }
            for (const k of ['upper', 'mid', 'lower']) if (_obBbSeries[k]) try { _obBbSeries[k].setData([]); } catch(e) {}
            if (_obRocSeries) try { _obRocSeries.setData([]); } catch(e) {}
            if (_obEmaCustomSeries) try { _obEmaCustomSeries.setData([]); } catch(e) {}
            _obLastEmaCustom = null;
            if (_obEmaCustom2Series) try { _obEmaCustom2Series.setData([]); } catch(e) {}
            _obLastEmaCustom2 = null;
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

        // Usa SEMPRE asksMap/bidsMap grezze (fino a 200 livelli dalla sub WS
        // orderbook.200), non gli array già tagliati a displayLevels (10-20 righe
        // visibili) — altrimenti con una soglia di slippage larga o un book
        // profondo il calcolo si fermerebbe all'ultima riga mostrata invece che
        // alla vera liquidità disponibile, sottostimando la size senza dirlo.
        const renderMaxSafeSize = () => {
            const buyEl  = document.getElementById('fs-maxsize-buy');
            const sellEl = document.getElementById('fs-maxsize-sell');
            if (!buyEl && !sellEl) return;
            const asksFull = Array.from(asksMap.entries()).sort((a, b) => a[0] - b[0]);
            const bidsFull = Array.from(bidsMap.entries()).sort((a, b) => b[0] - a[0]);
            const maxBuy  = computeMaxSafeSize(asksFull);
            const maxSell = computeMaxSafeSize(bidsFull);
            if (buyEl)  buyEl.textContent  = maxBuy  ? fmtTotal(maxBuy.notional)  : '—';
            if (sellEl) sellEl.textContent = maxSell ? fmtTotal(maxSell.notional) : '—';
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
            // Bybit allinea i bar boundary all'epoch UTC: bisogna arrotondare PRIMA in UTC
            // e solo dopo applicare _obTzOffset. Arrotondare il tempo già shiftato (come
            // prima) è sbagliato quando l'offset non è multiplo esatto di barSecs (es.
            // offset +2h su barSecs=4h): produce un boundary sfasato di
            // "_obTzOffset % barSecs" secondi rispetto a quello reale, e la candela WS
            // autentica (correttamente allineata) arriva poi con un time "più vecchio" del
            // boundary sintetico già scritto sulla serie — stesso bug/fix di
            // _expectedBarOpen in mtf.html, mai portato qui.
            const nowUtc = Math.floor(Date.now() / 1000);
            const expectedOpen = Math.floor(nowUtc / barSecs) * barSecs + _obTzOffset;
            if (expectedOpen <= _obLiveCandle.time) return false;

            // Se il book resta silenzioso per più di un boundary (mercato calmo, tab in
            // background, WS momentaneamente muto), saltare direttamente a "adesso" lasciava
            // un buco visivo sulla serie: le barre intermedie mai aperte semplicemente non
            // esistevano fra l'ultima candela reale e quella corrente (bug segnalato
            // 2026-08-25, screenshot). Fix: si itera un boundary alla volta fino a
            // raggiungere expectedOpen, riempiendo ogni barra mancante con una candela
            // piatta al prezzo di chiusura precedente — solo l'ULTIMA (quella corrente)
            // riflette davvero midPrice.
            // Cap di sicurezza: un tab lasciato in background per ore/giorni potrebbe
            // richiedere migliaia di barre sintetiche in un colpo solo (freeze). Oltre la
            // soglia si riempiono solo le ultime MAX_FILL_BARS, accettando un buco residuo
            // in quel caso estremo (si autocorregge comunque al prossimo cambio TF/reload,
            // che rifà un fetch REST completo).
            const MAX_FILL_BARS = 300;
            const missingBars = (expectedOpen - _obLiveCandle.time) / barSecs;
            const fillStart = missingBars > MAX_FILL_BARS
                ? expectedOpen - MAX_FILL_BARS * barSecs
                : _obLiveCandle.time + barSecs;
            for (let boundary = fillStart; boundary <= expectedOpen; boundary += barSecs) {
                const isNowBar = boundary === expectedOpen;
                const prevClose = _obLiveCandle.close;
                if (_obLiveCandle.time > lastConfirmedTime) {
                    for (const { p } of EMA_CFG) {
                        if (lastEMA[p] == null) continue;
                        const ek = 2 / (p + 1);
                        lastEMA[p] = prevClose * ek + lastEMA[p] * (1 - ek);
                    }
                    if (_obLastEmaCustom != null) {
                        const eck = 2 / (getEmaCustomCfg().length + 1);
                        _obLastEmaCustom = prevClose * eck + _obLastEmaCustom * (1 - eck);
                    }
                    if (_obLastEmaCustom2 != null) {
                        const eckB = 2 / (getEmaCustom2Cfg().length + 1);
                        _obLastEmaCustom2 = prevClose * eckB + _obLastEmaCustom2 * (1 - eckB);
                    }
                    _obGrabConfirmPrev(_obLiveCandle);
                    lastConfirmedTime = _obLiveCandle.time;
                }
                if (_obKlines.length) {
                    const lastK = _obKlines[_obKlines.length - 1];
                    if (lastK.time === _obLiveCandle.time) _obKlines[_obKlines.length - 1] = { ..._obLiveCandle };
                    else _obKlines.push({ ..._obLiveCandle });
                }

                const newCandle = isNowBar ? {
                    time: boundary, open: prevClose,
                    high: Math.max(prevClose, midPrice), low: Math.min(prevClose, midPrice),
                    close: midPrice, volume: 0,
                } : {
                    time: boundary, open: prevClose, high: prevClose, low: prevClose, close: prevClose, volume: 0,
                };
                _obLiveCandle = newCandle;
                // Prima, con GRaB attivo, la barra appena aperta dal roll non veniva disegnata
                // qui (il chiamante in processOrderBook salta il ramo _obGrabColorCandle quando
                // il roll ha già "consumato" il tick con return true) — restava assente dalla
                // serie fino al tick successivo del book, un giro perso non necessario.
                if (_obGrabActive) _obGrabColorCandle(newCandle);
                else { try { candleS.update({ ...newCandle }); } catch(e) {} }
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
                if (_obRocActive && _obRocSeries && _obKlines.length >= getRocCfg().length) {
                    const rocCfg = getRocCfg();
                    const rocPrev = _obKlines[_obKlines.length - rocCfg.length].close;
                    _obRocSeries.update({ time: newCandle.time, value: 100 * (newCandle.close - rocPrev) / rocPrev });
                }
                if (_obEmaCustomActive && _obEmaCustomSeries && _obLastEmaCustom != null) {
                    const eck2 = 2 / (getEmaCustomCfg().length + 1);
                    const live2 = newCandle.close * eck2 + _obLastEmaCustom * (1 - eck2);
                    try { _obEmaCustomSeries.update({ time: newCandle.time, value: live2 }); } catch(e) {}
                }
                if (_obEmaCustom2Active && _obEmaCustom2Series && _obLastEmaCustom2 != null) {
                    const eckB2 = 2 / (getEmaCustom2Cfg().length + 1);
                    const liveB2 = newCandle.close * eckB2 + _obLastEmaCustom2 * (1 - eckB2);
                    try { _obEmaCustom2Series.update({ time: newCandle.time, value: liveB2 }); } catch(e) {}
                }
                if (_obKlines.length) _obChUpdateTail([..._obKlines, newCandle]);
                _obGrabUpdateTail(newCandle, false);
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
                    if (_obGrabActive) _obGrabColorCandle(_obLiveCandle);
                    else try { candleS.update({ ..._obLiveCandle }); } catch(e) {}
                    _cdLastPrice = _obLiveCandle.close;
                }
            }

            _obScheduleRender();
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
            const sock = new WebSocket('wss://stream.bybit.com/v5/public/linear');
            tradeWS = sock;
            sock.onopen = () => {
                sock.send(JSON.stringify({ op: 'subscribe', args: [`publicTrade.${symbol.value}`] }));
            };
            sock.onmessage = (event) => {
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
            sock.onerror = () => {};
            sock.onclose = () => { tradeReconnTimer = setTimeout(connectTradeWS, 4000); };
        };

        watch(cvdWindow, () => { cvdBuffer.length = 0; cvdCalc(); });

        const connectBookWS = () => {
            setWSDot('connecting');
            const sock = new WebSocket('wss://stream.bybit.com/v5/public/linear');
            bookWS = sock;
            sock.onopen = () => {
                sock.send(JSON.stringify({ op: 'subscribe', args: [`orderbook.200.${symbol.value}`] }));
                setWSDot('live');
            };
            sock.onmessage = (event) => {
                if (isPaused.value) return;
                try {
                    const data = JSON.parse(event.data);
                    if (data.topic && data.topic.startsWith('orderbook') && data.data) {
                        processOrderBook(data.data, data.type === 'snapshot');
                        if (data.data.u) priceColor.value = data.data.u === 'U' ? '#10b981' : '#ef4444';
                    }
                } catch (err) { console.error(err); }
            };
            sock.onerror  = (err) => { console.error('OB WS error:', err); setWSDot('error'); };
            sock.onclose  = () => { setWSDot('off'); reconnectTimer = setTimeout(connectBookWS, 3000); };
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
            window.setIndActive('obLevels', showObLines.value);
            if (showObLines.value) updateObLines();
            else clearObLines();
        };

        const toggleBookPanel = () => {
            showBook.value = !showBook.value;
            try { localStorage.setItem('ob_show_book', showBook.value ? '1' : '0'); } catch(e) {}
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
                // Sempre attiva, indipendente dal bottone GRaB (quello colora solo
                // le candele sul grafico) — vedi commento su _updateGrabTfStrip.
                _grabTfTick++;
                if (_grabTfTick % 5 === 1) _updateGrabTfStrip();
                if (_grabTfTick % 5 === 3) _updateChannelTfStrip();
                if (_grabTfTick % 5 === 0) _updateRocTfStrip();
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
            clearInterval(_obEmaCustomMtfTimer); _obEmaCustomMtfTimer = null;
            clearInterval(_obEmaCustom2MtfTimer); _obEmaCustom2MtfTimer = null;
        });

        return {
            t,
            symbol, symBase, symQuote, isStandalone,
            ticker, TF_OPTIONS, chartTF, ohlc, chartContainerEl, grabTfColors, grabTfTitle, channelTfColors, channelTfTitle, rocTfColors, rocTfTitle,
            displayLevels, grouping, groupingOptions,
            levelsDdOpen, groupingDdOpen, selectLevels, selectGrouping,
            tfDdOpen, isPortraitMobile,
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

// ── Indicatori panel (popup unico, stesso pattern di chart.html/mtf.html) ──────
const _GEAR_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';
const IND_LIST = [
    { label: 'EMA 5',   isOn: () => getEmaCfg().find(e=>e.p===5).enabled,   toggle: () => toggleEmaAll(5),   cfgKey: 'ema5',   col: 1 },
    { label: 'EMA 10',  isOn: () => getEmaCfg().find(e=>e.p===10).enabled,  toggle: () => toggleEmaAll(10),  cfgKey: 'ema10',  col: 1 },
    { label: 'EMA 60',  isOn: () => getEmaCfg().find(e=>e.p===60).enabled,  toggle: () => toggleEmaAll(60),  cfgKey: 'ema60',  col: 1 },
    { label: 'EMA 223', isOn: () => getEmaCfg().find(e=>e.p===223).enabled, toggle: () => toggleEmaAll(223), cfgKey: 'ema223', col: 1 },
    { label: 'Midline GRaB', isOn: () => _obGrabMidlineActive, toggle: toggleObGrabMidline, col: 1 },
    { label: 'EMA personalizzata', isOn: () => _obEmaCustomActive, toggle: toggleObEmaCustom, cfgOpen: () => openIndCfgPanel('emaCustom'), col: 1 },
    { label: 'EMA personalizzata 2', isOn: () => _obEmaCustom2Active, toggle: toggleObEmaCustom2, cfgOpen: () => openIndCfgPanel('emaCustom2'), col: 1 },
    { label: 'Canale SMA 20',   isOn: () => _obChActive,  toggle: toggleObChannel, cfgKey: 'channel', col: 2 },
    { label: 'Bollinger Bands', isOn: () => _obBbActive,  toggle: toggleObBB,      cfgKey: 'bb', col: 2 },
    { label: 'GRaB',            isOn: () => _obGrabActive,toggle: toggleObGrab,    cfgOpen: openGrabCfgPanel, col: 2 },
    { label: 'ROC',             isOn: () => _obRocActive, toggle: toggleObRoc,     cfgKey: 'roc', col: 2 },
    { label: 'Order Book Levels', isOn: () => window._obShowLinesValue ? window._obShowLinesValue() : false, toggle: () => window.toggleObLines && window.toggleObLines(), cfgKey: 'obLevels', col: 2 },
    { label: 'Livelli Daily',          isOn: () => getLvCfg().dayHigh.vis.ob,  toggle: () => window.toggleDayLevelsAll  && window.toggleDayLevelsAll(),  cfgKey: 'dayLevels',  col: 3 },
    { label: 'Massimi/Minimi Storici', isOn: () => getLvCfg().ath.vis.ob,     toggle: () => window.toggleAthAtlAll     && window.toggleAthAtlAll(),     cfgKey: 'athAtl',     col: 3 },
    { label: 'Livelli Giorno Prec.',   isOn: () => getLvCfg().prevHigh.vis.ob,toggle: () => window.togglePrevLevelsAll && window.togglePrevLevelsAll(), cfgKey: 'prevLevels', col: 3 },
    { label: 'Colora candele',         isOn: () => window.isCandleColorActive(), toggle: toggleObCandleColor, col: 3 },
];
function renderIndPanel() {
    const el = document.getElementById('ind-rows');
    if (!el) return;
    const renderRow = (it, idx) => {
        const on = it.isOn();
        const gear = (it.cfgKey || it.cfgOpen) ? `<span data-ind-idx-gear="${idx}" title="Impostazioni" style="margin-right:6px;color:#6B7280;cursor:pointer;display:flex;align-items:center;">${_GEAR_SVG}</span>` : '';
        return `<div data-ind-idx="${idx}" style="display:flex;align-items:center;justify-content:space-between;padding:7px 10px;cursor:pointer;gap:6px;">
            <span style="font-size:11px;color:#E5E7EB;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${it.label}</span>
            <span style="display:flex;align-items:center;flex-shrink:0;">
                ${gear}
                <span style="width:30px;height:16px;border-radius:8px;background:${on ? '#2563EB' : '#2A2E39'};position:relative;flex-shrink:0;transition:background .15s;">
                    <span style="position:absolute;top:2px;left:${on ? '16px' : '2px'};width:12px;height:12px;border-radius:50%;background:#fff;transition:left .15s;"></span>
                </span>
            </span>
        </div>`;
    };
    const cols = [[], [], []];
    IND_LIST.forEach((it, idx) => cols[(it.col||1)-1].push(renderRow(it, idx)));
    el.innerHTML = `<div style="display:flex;">
        ${cols.map((rows, ci) => `<div style="flex:1;min-width:0;${ci>0?'border-left:1px solid #2A2E39;':''}">${rows.join('')}</div>`).join('')}
    </div>`;
    el.onclick = ev => {
        const gearEl = ev.target.closest('[data-ind-idx-gear]');
        if (gearEl) {
            ev.stopPropagation();
            const it = IND_LIST[+gearEl.dataset.indIdxGear];
            if (it.cfgOpen) it.cfgOpen(); else openIndCfgPanel(it.cfgKey);
            closeIndPanel();
            return;
        }
        const row = ev.target.closest('[data-ind-idx]');
        if (!row) return;
        IND_LIST[+row.dataset.indIdx].toggle();
        renderIndPanel();
    };
}
function openIndPanel() { renderIndPanel(); document.getElementById('ind-panel').style.display = 'block'; }
function closeIndPanel() { document.getElementById('ind-panel').style.display = 'none'; }
function closeIndPanelOutside(ev) { if (ev.target.id === 'ind-panel') closeIndPanel(); }

// ── Pannello impostazioni generico per gli indicatori configurabili (porting da
// chart.html) ───────────────────────────────────────────────────────────────
function _emaCfgSchema(period) {
    const d = DEFAULT_EMA_CFG.find(x => x.p === period);
    return {
        title: `EMA ${period}`,
        getCfg: () => { const e = getEmaCfg().find(x => x.p === period); return { color: e.color, width: e.width, style: e.style }; },
        defaults: { color: d.color, width: d.width, style: d.style },
        fields: [
            { key:'color', label:'Colore', type:'color' },
            { key:'width', label:'Spessore', type:'number', min:1, max:5, step:0.5 },
            { key:'style', label:'Stile linea', type:'select', options:[[0,'Solido'],[1,'Tratteggiato'],[2,'Punteggiato']] },
        ],
        apply: cfg => {
            const arr = getEmaCfg();
            const e = arr.find(x => x.p === period);
            e.color = cfg.color; e.width = cfg.width; e.style = cfg.style;
            try { localStorage.setItem('chart_ema_cfg', JSON.stringify(arr)); } catch(err) {}
            EMA_CFG = arr;
            if (_obEmaS[period]) _obEmaS[period].applyOptions({ color: cfg.color, lineWidth: cfg.width + 0.5, lineStyle: cfg.style });
        },
    };
}
const IND_CFG_SCHEMAS = {
    channel: {
        title: 'Canale SMA 20', getCfg: getChannelCfg, defaults: DEFAULT_CHANNEL_CFG,
        fields: [
            { key:'period', label:'Periodo', type:'number', min:2, max:300, step:1 },
            { key:'color', label:'Colore linee', type:'color' },
            { key:'width', label:'Spessore', type:'number', min:1, max:5, step:1 },
            { key:'style', label:'Stile linea', type:'select', options:[[0,'Solido'],[1,'Tratteggiato'],[2,'Punteggiato']] },
            { key:'bgColor', label:'Colore sfondo', type:'color' },
            { key:'bgOpacity', label:'Opacità sfondo %', type:'number', min:0, max:100, step:1 },
        ],
        apply: cfg => {
            try { localStorage.setItem('chart_channel_cfg', JSON.stringify(cfg)); } catch(e) {}
            _obApplyChannel();
        },
    },
    bb: {
        title: 'Bollinger Bands', getCfg: getBbCfg, defaults: DEFAULT_BB_CFG,
        fields: [
            { key:'period', label:'Periodo', type:'number', min:2, max:300, step:1 },
            { key:'mult', label:'Moltiplicatore Dev.Std', type:'number', min:0.5, max:5, step:0.1 },
            { key:'color', label:'Colore', type:'color' },
            { key:'width', label:'Spessore', type:'number', min:1, max:5, step:1 },
            { key:'style', label:'Stile linea', type:'select', options:[[0,'Solido'],[1,'Tratteggiato'],[2,'Punteggiato']] },
        ],
        apply: cfg => {
            try { localStorage.setItem('chart_bb_cfg', JSON.stringify(cfg)); } catch(e) {}
            _obApplyBB();
        },
    },
    roc: {
        title: 'ROC (Rate Of Change)', getCfg: getRocCfg, defaults: DEFAULT_ROC_CFG,
        fields: [
            { key:'length', label:'Periodo', type:'number', min:1, max:300, step:1 },
            { key:'color', label:'Colore', type:'color' },
            { key:'areaFill', label:'Area colorata sopra/sotto 0', type:'checkbox' },
            { key:'upColor', label:'Colore area sopra 0', type:'color' },
            { key:'downColor', label:'Colore area sotto 0', type:'color' },
            { key:'showExtremes', label:'Linee min/max raggiunti', type:'checkbox' },
        ],
        apply: cfg => {
            try { localStorage.setItem('chart_roc_cfg', JSON.stringify(cfg)); } catch(e) {}
            _obApplyRoc();
        },
    },
    dayLevels: {
        title: 'Livelli Daily',
        getCfg: () => { const lv = getLvCfg(); return { highColor: lv.dayHigh.color, lowColor: lv.dayLow.color, width: lv.dayHigh.width, style: lv.dayHigh.style }; },
        defaults: { highColor: DEFAULT_LEVELS_CFG.dayHigh.color, lowColor: DEFAULT_LEVELS_CFG.dayLow.color, width: DEFAULT_LEVELS_CFG.dayHigh.width, style: DEFAULT_LEVELS_CFG.dayHigh.style },
        fields: [
            { key:'highColor', label:'Colore Massimo', type:'color' },
            { key:'lowColor', label:'Colore Minimo', type:'color' },
            { key:'width', label:'Spessore', type:'number', min:1, max:5, step:1 },
            { key:'style', label:'Stile linea', type:'select', options:[[0,'Solido'],[1,'Tratteggiato'],[2,'Punteggiato']] },
        ],
        apply: cfg => {
            const lv = getLvCfg();
            lv.dayHigh.color = cfg.highColor; lv.dayHigh.width = cfg.width; lv.dayHigh.style = cfg.style;
            lv.dayLow.color  = cfg.lowColor;  lv.dayLow.width  = cfg.width; lv.dayLow.style  = cfg.style;
            try { localStorage.setItem('chart_levels_cfg', JSON.stringify(lv)); } catch(e) {}
            if (window._obApplyDayPrevLines) window._obApplyDayPrevLines();
        },
    },
    prevLevels: {
        title: 'Livelli Giorno Precedente',
        getCfg: () => { const lv = getLvCfg(); return { highColor: lv.prevHigh.color, lowColor: lv.prevLow.color, width: lv.prevHigh.width, style: lv.prevHigh.style }; },
        defaults: { highColor: DEFAULT_LEVELS_CFG.prevHigh.color, lowColor: DEFAULT_LEVELS_CFG.prevLow.color, width: DEFAULT_LEVELS_CFG.prevHigh.width, style: DEFAULT_LEVELS_CFG.prevHigh.style },
        fields: [
            { key:'highColor', label:'Colore Massimo', type:'color' },
            { key:'lowColor', label:'Colore Minimo', type:'color' },
            { key:'width', label:'Spessore', type:'number', min:1, max:5, step:1 },
            { key:'style', label:'Stile linea', type:'select', options:[[0,'Solido'],[1,'Tratteggiato'],[2,'Punteggiato']] },
        ],
        apply: cfg => {
            const lv = getLvCfg();
            lv.prevHigh.color = cfg.highColor; lv.prevHigh.width = cfg.width; lv.prevHigh.style = cfg.style;
            lv.prevLow.color  = cfg.lowColor;  lv.prevLow.width  = cfg.width; lv.prevLow.style  = cfg.style;
            try { localStorage.setItem('chart_levels_cfg', JSON.stringify(lv)); } catch(e) {}
            if (window._obApplyDayPrevLines) window._obApplyDayPrevLines();
        },
    },
    athAtl: {
        title: 'Massimi/Minimi Storici',
        getCfg: () => { const lv = getLvCfg(); return { athColor: lv.ath.color, atlColor: lv.atl.color, width: lv.ath.width, style: lv.ath.style }; },
        defaults: { athColor: DEFAULT_LEVELS_CFG.ath.color, atlColor: DEFAULT_LEVELS_CFG.atl.color, width: DEFAULT_LEVELS_CFG.ath.width, style: DEFAULT_LEVELS_CFG.ath.style },
        fields: [
            { key:'athColor', label:'Colore ATH', type:'color' },
            { key:'atlColor', label:'Colore ATL', type:'color' },
            { key:'width', label:'Spessore', type:'number', min:1, max:5, step:1 },
            { key:'style', label:'Stile linea', type:'select', options:[[0,'Solido'],[1,'Tratteggiato'],[2,'Punteggiato']] },
        ],
        apply: cfg => {
            const lv = getLvCfg();
            lv.ath.color = cfg.athColor; lv.ath.width = cfg.width; lv.ath.style = cfg.style;
            lv.atl.color = cfg.atlColor; lv.atl.width = cfg.width; lv.atl.style = cfg.style;
            try { localStorage.setItem('chart_levels_cfg', JSON.stringify(lv)); } catch(e) {}
            if (window._obApplyAthAtlLines) window._obApplyAthAtlLines();
        },
    },
    obLevels: {
        title: 'Order Book Levels',
        getCfg: () => { const lv = getLvCfg(); return { bidColor: lv.obBid.color, askColor: lv.obAsk.color, width: lv.obBid.width, style: lv.obBid.style }; },
        defaults: { bidColor: DEFAULT_LEVELS_CFG.obBid.color, askColor: DEFAULT_LEVELS_CFG.obAsk.color, width: DEFAULT_LEVELS_CFG.obBid.width, style: DEFAULT_LEVELS_CFG.obBid.style },
        fields: [
            { key:'bidColor', label:'Colore Bid', type:'color' },
            { key:'askColor', label:'Colore Ask', type:'color' },
            { key:'width', label:'Spessore', type:'number', min:1, max:5, step:1 },
            { key:'style', label:'Stile linea', type:'select', options:[[0,'Solido'],[1,'Tratteggiato'],[2,'Punteggiato']] },
        ],
        apply: cfg => {
            const lv = getLvCfg();
            lv.obBid.color = cfg.bidColor; lv.obBid.width = cfg.width; lv.obBid.style = cfg.style;
            lv.obAsk.color = cfg.askColor; lv.obAsk.width = cfg.width; lv.obAsk.style = cfg.style;
            try { localStorage.setItem('chart_levels_cfg', JSON.stringify(lv)); } catch(e) {}
            if (window._obUpdateObLines) window._obUpdateObLines();
        },
    },
    ema5:   _emaCfgSchema(5),
    ema10:  _emaCfgSchema(10),
    ema60:  _emaCfgSchema(60),
    ema223: _emaCfgSchema(223),
    emaCustom: {
        title: 'EMA personalizzata', getCfg: getEmaCustomCfg, defaults: DEFAULT_EMA_CUSTOM_CFG,
        fields: [
            { key:'length', label:'Lunghezza', type:'number', min:1, max:500, step:1 },
            { key:'color', label:'Colore', type:'color' },
            { key:'width', label:'Spessore', type:'number', min:1, max:5, step:0.5 },
            { key:'style', label:'Stile linea', type:'select', options:[[0,'Solido'],[1,'Tratteggiato'],[2,'Punteggiato']] },
            { key:'tf_1',   label:'TF calcolo — grafico 1m',  type:'select', raw:true, options:[['','Uguale al grafico'],['1','1m'],['5','5m'],['30','30m'],['60','1h'],['240','4h'],['D','D'],['W','W'],['M','M']] },
            { key:'tf_5',   label:'TF calcolo — grafico 5m',  type:'select', raw:true, options:[['','Uguale al grafico'],['1','1m'],['5','5m'],['30','30m'],['60','1h'],['240','4h'],['D','D'],['W','W'],['M','M']] },
            { key:'tf_30',  label:'TF calcolo — grafico 30m', type:'select', raw:true, options:[['','Uguale al grafico'],['1','1m'],['5','5m'],['30','30m'],['60','1h'],['240','4h'],['D','D'],['W','W'],['M','M']] },
            { key:'tf_60',  label:'TF calcolo — grafico 1h',  type:'select', raw:true, options:[['','Uguale al grafico'],['1','1m'],['5','5m'],['30','30m'],['60','1h'],['240','4h'],['D','D'],['W','W'],['M','M']] },
            { key:'tf_240', label:'TF calcolo — grafico 4h',  type:'select', raw:true, options:[['','Uguale al grafico'],['1','1m'],['5','5m'],['30','30m'],['60','1h'],['240','4h'],['D','D'],['W','W'],['M','M']] },
            { key:'tf_D',   label:'TF calcolo — grafico D',   type:'select', raw:true, options:[['','Uguale al grafico'],['1','1m'],['5','5m'],['30','30m'],['60','1h'],['240','4h'],['D','D'],['W','W'],['M','M']] },
            { key:'waitClose', label:'Attendi la chiusura del timeframe', type:'checkbox' },
        ],
        apply: cfg => {
            try { localStorage.setItem('chart_ema_custom_cfg', JSON.stringify(cfg)); } catch(e) {}
            _obApplyEmaCustom();
            _syncObEmaCustomMtfTimer();
        },
    },
    emaCustom2: {
        title: 'EMA personalizzata 2', getCfg: getEmaCustom2Cfg, defaults: DEFAULT_EMA_CUSTOM2_CFG,
        fields: [
            { key:'length', label:'Lunghezza', type:'number', min:1, max:500, step:1 },
            { key:'color', label:'Colore', type:'color' },
            { key:'width', label:'Spessore', type:'number', min:1, max:5, step:0.5 },
            { key:'style', label:'Stile linea', type:'select', options:[[0,'Solido'],[1,'Tratteggiato'],[2,'Punteggiato']] },
            { key:'tf_1',   label:'TF calcolo — grafico 1m',  type:'select', raw:true, options:[['','Uguale al grafico'],['1','1m'],['5','5m'],['30','30m'],['60','1h'],['240','4h'],['D','D'],['W','W'],['M','M']] },
            { key:'tf_5',   label:'TF calcolo — grafico 5m',  type:'select', raw:true, options:[['','Uguale al grafico'],['1','1m'],['5','5m'],['30','30m'],['60','1h'],['240','4h'],['D','D'],['W','W'],['M','M']] },
            { key:'tf_30',  label:'TF calcolo — grafico 30m', type:'select', raw:true, options:[['','Uguale al grafico'],['1','1m'],['5','5m'],['30','30m'],['60','1h'],['240','4h'],['D','D'],['W','W'],['M','M']] },
            { key:'tf_60',  label:'TF calcolo — grafico 1h',  type:'select', raw:true, options:[['','Uguale al grafico'],['1','1m'],['5','5m'],['30','30m'],['60','1h'],['240','4h'],['D','D'],['W','W'],['M','M']] },
            { key:'tf_240', label:'TF calcolo — grafico 4h',  type:'select', raw:true, options:[['','Uguale al grafico'],['1','1m'],['5','5m'],['30','30m'],['60','1h'],['240','4h'],['D','D'],['W','W'],['M','M']] },
            { key:'tf_D',   label:'TF calcolo — grafico D',   type:'select', raw:true, options:[['','Uguale al grafico'],['1','1m'],['5','5m'],['30','30m'],['60','1h'],['240','4h'],['D','D'],['W','W'],['M','M']] },
            { key:'waitClose', label:'Attendi la chiusura del timeframe', type:'checkbox' },
        ],
        apply: cfg => {
            try { localStorage.setItem('chart_ema_custom2_cfg', JSON.stringify(cfg)); } catch(e) {}
            _obApplyEmaCustom2();
            _syncObEmaCustom2MtfTimer();
        },
    },
};
let _indCfgKey = null, _indCfgWorking = null;
function openIndCfgPanel(key) {
    _indCfgKey = key;
    const schema = IND_CFG_SCHEMAS[key];
    _indCfgWorking = schema.getCfg();
    const titleEl = document.getElementById('ind-cfg-title');
    if (titleEl) titleEl.textContent = schema.title;
    renderIndCfgRows(schema);
    document.getElementById('ind-cfg-panel').style.display = 'block';
}
function renderIndCfgRows(schema) {
    const el = document.getElementById('ind-cfg-rows');
    if (!el) return;
    el.innerHTML = schema.fields.map(f => {
        const val = _indCfgWorking[f.key];
        let input;
        if (f.type === 'color') input = `<input type="color" data-cfg-key="${f.key}" value="${val}" style="width:36px;height:22px;border:none;background:none;cursor:pointer;">`;
        else if (f.type === 'checkbox') input = `<input type="checkbox" data-cfg-key="${f.key}" ${val ? 'checked' : ''} style="width:16px;height:16px;cursor:pointer;">`;
        else if (f.type === 'select') input = `<select data-cfg-key="${f.key}" style="background:#1E222D;color:#E5E7EB;border:1px solid #2A2E39;border-radius:4px;font-size:11px;padding:2px 4px;">${f.options.map(([v,l]) => `<option value="${v}" ${v==val?'selected':''}>${l}</option>`).join('')}</select>`;
        else input = `<input type="number" data-cfg-key="${f.key}" value="${val}" min="${f.min ?? ''}" max="${f.max ?? ''}" step="${f.step ?? 1}" style="width:64px;background:#1E222D;color:#E5E7EB;border:1px solid #2A2E39;border-radius:4px;font-size:11px;padding:2px 4px;">`;
        return `<div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;">
            <span style="font-size:11px;color:#9CA3AF;">${f.label}</span>
            ${input}
        </div>`;
    }).join('');
}
function closeIndCfgPanel() { document.getElementById('ind-cfg-panel').style.display = 'none'; }
function closeIndCfgOutside(ev) { if (ev.target.id === 'ind-cfg-panel') closeIndCfgPanel(); }
function resetIndCfg() {
    const schema = IND_CFG_SCHEMAS[_indCfgKey];
    _indCfgWorking = { ...schema.defaults };
    renderIndCfgRows(schema);
}
function saveIndCfgPanel() {
    const schema = IND_CFG_SCHEMAS[_indCfgKey];
    const el = document.getElementById('ind-cfg-rows');
    el.querySelectorAll('[data-cfg-key]').forEach(input => {
        const key = input.dataset.cfgKey;
        const field = schema.fields.find(f => f.key === key);
        let v;
        if (field.type === 'checkbox') v = input.checked;
        else if (field.type === 'select') v = field.raw ? input.value : parseInt(input.value, 10);
        else if (field.type === 'number') v = parseFloat(input.value);
        else v = input.value;
        _indCfgWorking[key] = v;
    });
    schema.apply(_indCfgWorking);
    closeIndCfgPanel();
}
