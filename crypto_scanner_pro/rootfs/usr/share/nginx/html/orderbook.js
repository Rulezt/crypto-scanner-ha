// Order Book Standalone Vue App
const { createApp, ref, computed, onMounted, onUnmounted, watch, nextTick } = Vue;

// ── Lightweight Charts ────────────────────────────────────────────────────────
const LC = window.LightweightCharts;

const EMA_CFG = [
    { p: 5,   color: '#ef4444', width: 2   },
    { p: 10,  color: '#fbbf24', width: 2   },
    { p: 60,  color: '#3b82f6', width: 3   },
    { p: 223, color: '#a855f7', width: 2.5 },
];

const TF_OPTIONS = [
    { v: '5',   l: '5m'  },
    { v: '30',  l: '30m' },
    { v: '60',  l: '1h'  },
    { v: '240', l: '4h'  },
    { v: 'D',   l: '1D'  },
];

const DEFAULT_CANDLES = { '5': 100, '30': 80, '60': 80, '240': 60, 'D': 50 };

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
    return LC.createChart(el, {
        autoSize: true,
        layout: { background: { color: '#0F0F0F' }, textColor: '#B2B5BE', fontSize: 13 },
        grid:    { vertLines: { color: '#FFFFFF0F' }, horzLines: { color: '#FFFFFF0F' } },
        crosshair: { mode: LC.CrosshairMode ? LC.CrosshairMode.Normal : 1 },
        rightPriceScale: { borderColor: '#2A2E39', scaleMargins: { top: 0.05, bottom: 0.05 } },
        timeScale: { borderColor: '#2A2E39', visible: true, timeVisible: true, secondsVisible: false,
                     barSpacing: 6, rightOffset: 30 },
    });
}

function addSeries(chart, type, opts) {
    if (typeof chart.addSeries === 'function' && LC[type]) return chart.addSeries(LC[type], opts);
    const legacy = { CandlestickSeries: 'addCandlestickSeries', LineSeries: 'addLineSeries' };
    return chart[legacy[type]](opts);
}

function fmtVol(v) {
    if (!v && v !== 0) return '';
    if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K';
    return v.toFixed(0);
}

// ── Vue App ───────────────────────────────────────────────────────────────────
createApp({
    setup() {
        const urlParams = new URLSearchParams(window.location.search);
        const symbol    = ref(urlParams.get('symbol') || 'BTCUSDT');
        const isStandalone = ref(window.parent === window);

        const symBase = computed(() => symbol.value.replace('USDT', ''));

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
        let chartWS   = null;
        let chartWsTimer = null;
        let obKlineCount = 0;

        // ── book state ────────────────────────────────────────────────────────
        const displayLevels  = ref(20);
        const grouping       = ref(0);
        const groupingOptions = ref([]);
        const displayAsks    = ref([]);
        const displayBids    = ref([]);
        const currentPrice   = ref('0.00');
        const spread         = ref('0.00');
        const priceColor     = ref('#9ca3af');
        const loading        = ref(true);
        const error          = ref('');
        const showImbalance  = ref(true);
        const isPaused       = ref(false);
        const showBook       = ref(true);

        const maxLevelDistance = ref({
            askPrice: 0, askPercent: '0.00',
            bidPrice: 0, bidPercent: '0.00'
        });

        const imbalance = ref({
            ratio: 0, percent: '50.0', signal: 'neutral',
            bidTotal: '0K', askTotal: '0K', direction: '⚪', strength: ''
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
                upColor: '#089981', downColor: '#F23645',
                borderVisible: false, wickUpColor: '#089981', wickDownColor: '#F23645',
            });
            const lineBase = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
            for (const { p, color, width } of EMA_CFG)
                emaS[p] = addSeries(obChart, 'LineSeries', { ...lineBase, color, lineWidth: width + 0.5 });

            obChart.subscribeDblClick(() => { if (obKlineCount) obChart.timeScale().setVisibleLogicalRange({ from: obKlineCount - (DEFAULT_CANDLES[chartTF.value] || 80), to: obKlineCount + 3 }); });
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
        };

        const loadChartData = async (tf) => {
            if (!candleS) return;
            try {
                const r = await fetch(`api/klines?symbol=${symbol.value}&interval=${tf}`);
                const j = await r.json();
                if (!j.success || !j.data || !j.data.length) return;
                const klines = j.data;

                candleS.setData(klines);
                obKlineCount = klines.length;
                candleS.applyOptions({ priceFormat: getPriceFormat(klines[klines.length - 1]?.close) });

                for (const { p } of EMA_CFG) {
                    const ema = calcEMA(klines, p);
                    emaS[p].setData(ema);
                    lastEMA[p] = ema[ema.length - 1].value;
                }

                const n = DEFAULT_CANDLES[tf] || 80;
                obChart.timeScale().setVisibleLogicalRange({ from: klines.length - n, to: klines.length + 3 });

                connectChartWS(tf);
            } catch (e) { console.error('Chart load error:', e); }
        };

        const connectChartWS = (tf) => {
            closeChartWS();
            chartWS = new WebSocket('wss://stream.bybit.com/v5/public/linear');
            chartWS.onopen = () => {
                chartWS.send(JSON.stringify({ op: 'subscribe', args: [`kline.${tf}.${symbol.value}`] }));
            };
            chartWS.onmessage = (ev) => {
                let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
                if (!msg.topic || !msg.topic.startsWith('kline.') || !candleS) return;
                const b = msg.data[0];
                const confirmed = b.confirm === true || b.confirm === 'true';
                const candle = {
                    time: Math.floor(parseInt(b.start) / 1000),
                    open: parseFloat(b.open), high: parseFloat(b.high),
                    low:  parseFloat(b.low),  close: parseFloat(b.close),
                };
                candleS.update(candle);
                for (const { p } of EMA_CFG) {
                    if (lastEMA[p] == null) continue;
                    const k    = 2 / (p + 1);
                    const live = candle.close * k + lastEMA[p] * (1 - k);
                    emaS[p].update({ time: candle.time, value: live });
                    if (confirmed) lastEMA[p] = live;
                }
            };
            chartWS.onerror = () => {};
            chartWS.onclose = () => { chartWsTimer = setTimeout(() => connectChartWS(chartTF.value), 5000); };
        };

        const closeChartWS = () => {
            clearTimeout(chartWsTimer);
            if (chartWS) { try { chartWS.close(); } catch (e) {} chartWS = null; }
        };

        const changeChartTF = (tf) => {
            if (tf === chartTF.value || !candleS) return;
            chartTF.value = tf;
            closeChartWS();
            candleS.setData([]);
            for (const { p } of EMA_CFG) { emaS[p].setData([]); lastEMA[p] = null; }
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

            displayAsks.value = [...asksArray].reverse().map(([price, amount]) => ({
                price:        formatPrice(price),
                amount:       amount > 0 ? amount.toFixed(4) : '-',
                total:        amount > 0 ? (price * amount).toFixed(2) : '-',
                depthPercent: amount > 0 ? (amount / maxAsk) * 100 : 0,
                isMaxLevel:   amount === maxAsk && amount > 0,
                isEmpty:      amount === 0,
            }));

            displayBids.value = bidsArray.map(([price, amount]) => ({
                price:        formatPrice(price),
                amount:       amount.toFixed(4),
                total:        (price * amount).toFixed(2),
                depthPercent: (amount / maxBid) * 100,
                isMaxLevel:   amount === maxBid,
            }));
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
                spread.value        = `${formatPrice(spreadValue)} (${spreadPercent.toFixed(3)}%)`;
                if (groupingOptions.value.length === 0) calculateGroupingOptions(midPrice);
            }

            updateDisplay();
            calculateImbalance();
        };

        const calculateImbalance = () => {
            const levels = parseInt(displayLevels.value);
            const tick   = parseFloat(grouping.value);
            let asksGrouped = tick > 0 ? groupLevels(asksMap, tick, true)  : asksMap;
            let bidsGrouped = tick > 0 ? groupLevels(bidsMap, tick, false) : bidsMap;
            const asksArray = Array.from(asksGrouped.entries()).sort((a, b) => a[0] - b[0]).slice(0, levels);
            const bidsArray = Array.from(bidsGrouped.entries()).sort((a, b) => b[0] - a[0]).slice(0, levels);
            const totalAsk  = asksArray.reduce((sum, [p, a]) => sum + (p * a), 0);
            const totalBid  = bidsArray.reduce((sum, [p, a]) => sum + (p * a), 0);
            const total     = totalAsk + totalBid;
            if (total === 0) return;
            const bidPercent = (totalBid / total) * 100;
            const ratio      = totalBid / totalAsk;
            let signal = 'neutral', direction = '⚪', strength = '';
            if      (ratio > 2.0)  { signal = 'strong-buy';  direction = '🟢🟢🟢'; strength = 'STRONG BUY';  }
            else if (ratio > 1.5)  { signal = 'buy';         direction = '🟢🟢';   strength = 'BUY';         }
            else if (ratio > 1.2)  { signal = 'weak-buy';    direction = '🟢';     strength = 'Weak Buy';    }
            else if (ratio < 0.5)  { signal = 'strong-sell'; direction = '🔴🔴🔴'; strength = 'STRONG SELL'; }
            else if (ratio < 0.67) { signal = 'sell';        direction = '🔴🔴';   strength = 'SELL';        }
            else if (ratio < 0.83) { signal = 'weak-sell';   direction = '🔴';     strength = 'Weak Sell';   }
            imbalance.value = {
                ratio: ratio.toFixed(2), percent: bidPercent.toFixed(1),
                signal, bidTotal: (totalBid / 1000).toFixed(1) + 'K',
                askTotal: (totalAsk / 1000).toFixed(1) + 'K', direction, strength,
            };
        };

        const connectBookWS = () => {
            bookWS = new WebSocket('wss://stream.bybit.com/v5/public/linear');
            bookWS.onopen = () => {
                bookWS.send(JSON.stringify({ op: 'subscribe', args: [`orderbook.200.${symbol.value}`] }));
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
            bookWS.onerror  = (err) => console.error('OB WS error:', err);
            bookWS.onclose  = () => { reconnectTimer = setTimeout(connectBookWS, 3000); };
        };

        // ============================
        //  CLEANUP
        // ============================
        const cleanup = () => {
            if (bookWS)        { bookWS.close(); bookWS = null; }
            if (reconnectTimer)  clearTimeout(reconnectTimer);
            closeChartWS();
            asksMap.clear();
            bidsMap.clear();
        };

        watch(showBook, val => {
            if (window.parent !== window) {
                window.parent.postMessage({ type: 'ob_book_toggle', visible: val }, '*');
            }
        });

        onMounted(() => {
            document.title = `${symbol.value} Order Book`;
            fetchOrderBook();
            if (isStandalone.value) {
                fetchTicker();
                nextTick(() => { initChart(); });
            }
        });

        onUnmounted(() => {
            cleanup();
        });

        return {
            symbol, symBase, isStandalone,
            ticker, TF_OPTIONS, chartTF, ohlc, chartContainerEl,
            displayLevels, grouping, groupingOptions,
            displayAsks, displayBids,
            currentPrice, spread, priceColor,
            loading, error,
            imbalance, showImbalance, isPaused,
            maxLevelDistance, showBook,
            fetchOrderBook, updateDisplay, changeChartTF,
        };
    }
}).mount('#app');
