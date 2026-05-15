// Order Book Standalone Vue App
const { createApp, ref, onMounted, onUnmounted, computed } = Vue;

createApp({
    setup() {
        const urlParams = new URLSearchParams(window.location.search);
        const symbol = ref(urlParams.get('symbol') || 'BTCUSDT');

        const displayLevels = ref(20);
        const grouping = ref(0);
        const groupingOptions = ref([]);
        const displayAsks = ref([]);
        const displayBids = ref([]);
        const currentPrice = ref('0.00');
        const spread = ref('0.00');
        const priceColor = ref('#9ca3af');
        const loading = ref(true);
        const error = ref('');
        const showImbalance = ref(true);
        const isPaused = ref(false);
        const showCVD = ref(true);
        const showBook = ref(true);
        const cvdWidth = ref(180);
        const isResizing = ref(false);

        const maxLevelDistance = ref({
            askPrice: 0, askPercent: '0.00',
            bidPrice: 0, bidPercent: '0.00'
        });

        const imbalance = ref({
            ratio: 0, percent: 50, signal: 'neutral',
            bidTotal: 0, askTotal: 0, direction: '⚪', strength: ''
        });

        // ============================
        //  CVD STATE
        // ============================
        const cvdData = ref({
            cvd: 0,
            delta1m: 0,
            buyVol: 0,
            sellVol: 0,
            lastTrades: [],
            largeThreshold: 0,
            signal: 'neutral',
        });

        const cvdHistory = ref([]);
        const MAX_CVD_HISTORY = 120;

        let wsTrades = null;
        let tradeReconnectTimer = null;
        let cvdChartInstance = null;
        let cvdSeries = null;
        let deltaSeries = null;

        // ============================
        //  ORDER BOOK STATE
        // ============================
        const asksMap = new Map();
        const bidsMap = new Map();
        let ws = null;
        let reconnectTimer = null;

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
            const prices = Array.from(levelsMap.keys());
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
            const tick = parseFloat(grouping.value);
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
                const lowestAsk = Math.min(...asksMap.keys());
                const highestBid = Math.max(...bidsMap.keys());
                const midPrice = (lowestAsk + highestBid) / 2;
                if (maxAskLevel && midPrice > 0) {
                    maxLevelDistance.value.askPrice = maxAskLevel[0];
                    maxLevelDistance.value.askPercent = (((maxAskLevel[0] - midPrice) / midPrice) * 100).toFixed(2);
                }
                if (maxBidLevel && midPrice > 0) {
                    maxLevelDistance.value.bidPrice = maxBidLevel[0];
                    maxLevelDistance.value.bidPercent = (((midPrice - maxBidLevel[0]) / midPrice) * 100).toFixed(2);
                }
                // Send strongest bid/ask levels to parent chart
                if (window.parent !== window) {
                    window.parent.postMessage({
                        type: 'ob_levels',
                        bid: maxBidLevel ? maxBidLevel[0] : null,
                        ask: maxAskLevel ? maxAskLevel[0] : null,
                    }, '*');
                }
            }

            displayAsks.value = [...asksArray].reverse().map(([price, amount]) => ({
                price: formatPrice(price),
                amount: amount > 0 ? amount.toFixed(4) : '-',
                total: amount > 0 ? (price * amount).toFixed(2) : '-',
                depthPercent: amount > 0 ? (amount / maxAsk) * 100 : 0,
                isMaxLevel: amount === maxAsk && amount > 0,
                isEmpty: amount === 0
            }));

            displayBids.value = bidsArray.map(([price, amount]) => ({
                price: formatPrice(price),
                amount: amount.toFixed(4),
                total: (price * amount).toFixed(2),
                depthPercent: (amount / maxBid) * 100,
                isMaxLevel: amount === maxBid
            }));
        };

        const formatPrice = (price) => {
            if (price >= 10000) return price.toFixed(1);
            if (price >= 1000)  return price.toFixed(2);
            if (price >= 100)   return price.toFixed(3);
            if (price >= 10)    return price.toFixed(4);
            if (price >= 1)     return price.toFixed(4);
            if (price >= 0.1)   return price.toFixed(5);
            if (price >= 0.01)  return price.toFixed(6);
            if (price >= 0.001) return price.toFixed(7);
            if (price >= 0.0001) return price.toFixed(8);
            if (price >= 0.00001) return price.toFixed(9);
            if (price >= 0.000001) return price.toFixed(10);
            return price.toPrecision(6);
        };

        // ============================
        //  ORDER BOOK FETCH & WS
        // ============================
        const fetchOrderBook = async () => {
            loading.value = true;
            error.value = '';
            try {
                const response = await fetch(
                    `https://api.bybit.com/v5/market/orderbook?category=linear&symbol=${symbol.value}&limit=200`
                );
                const data = await response.json();
                if (data.retCode === 0 && data.result) {
                    processOrderBook(data.result, true);
                    loading.value = false;
                    if (!ws) connectWebSocket();
                } else {
                    throw new Error('Failed to fetch order book');
                }
            } catch (err) {
                error.value = 'Errore nel caricamento dell\'Order Book';
                loading.value = false;
                console.error(err);
            }
        };

        const processOrderBook = (data, isSnapshot = false) => {
            const rawAsks = data.a || [];
            const rawBids = data.b || [];
            if (isSnapshot) { asksMap.clear(); bidsMap.clear(); }

            rawAsks.forEach(([priceStr, amountStr]) => {
                const price = parseFloat(priceStr);
                const amount = parseFloat(amountStr);
                if (amount === 0) asksMap.delete(price);
                else asksMap.set(price, amount);
            });

            rawBids.forEach(([priceStr, amountStr]) => {
                const price = parseFloat(priceStr);
                const amount = parseFloat(amountStr);
                if (amount === 0) bidsMap.delete(price);
                else bidsMap.set(price, amount);
            });

            if (asksMap.size > 0 && bidsMap.size > 0) {
                const lowestAsk = Math.min(...asksMap.keys());
                const highestBid = Math.max(...bidsMap.keys());
                const midPrice = (lowestAsk + highestBid) / 2;
                const spreadValue = lowestAsk - highestBid;
                const spreadPercent = (spreadValue / midPrice) * 100;
                currentPrice.value = formatPrice(midPrice);
                spread.value = `${formatPrice(spreadValue)} (${spreadPercent.toFixed(3)}%)`;
                if (groupingOptions.value.length === 0) calculateGroupingOptions(midPrice);
            }

            updateDisplay();
            calculateImbalance();
        };

        const calculateImbalance = () => {
            const levels = parseInt(displayLevels.value);
            const tick = parseFloat(grouping.value);
            let asksGrouped = tick > 0 ? groupLevels(asksMap, tick, true) : asksMap;
            let bidsGrouped = tick > 0 ? groupLevels(bidsMap, tick, false) : bidsMap;
            const asksArray = Array.from(asksGrouped.entries()).sort((a, b) => a[0] - b[0]).slice(0, levels);
            const bidsArray = Array.from(bidsGrouped.entries()).sort((a, b) => b[0] - a[0]).slice(0, levels);
            const totalAsk = asksArray.reduce((sum, [p, a]) => sum + (p * a), 0);
            const totalBid = bidsArray.reduce((sum, [p, a]) => sum + (p * a), 0);
            const total = totalAsk + totalBid;
            if (total === 0) return;
            const bidPercent = (totalBid / total) * 100;
            const ratio = totalBid / totalAsk;
            let signal = 'neutral', direction = '⚪', strength = '';
            if      (ratio > 2.0) { signal = 'strong-buy';  direction = '🟢🟢🟢'; strength = 'STRONG BUY'; }
            else if (ratio > 1.5) { signal = 'buy';         direction = '🟢🟢';   strength = 'BUY'; }
            else if (ratio > 1.2) { signal = 'weak-buy';    direction = '🟢';     strength = 'Weak Buy'; }
            else if (ratio < 0.5) { signal = 'strong-sell'; direction = '🔴🔴🔴'; strength = 'STRONG SELL'; }
            else if (ratio < 0.67){ signal = 'sell';        direction = '🔴🔴';   strength = 'SELL'; }
            else if (ratio < 0.83){ signal = 'weak-sell';   direction = '🔴';     strength = 'Weak Sell'; }
            imbalance.value = {
                ratio: ratio.toFixed(2), percent: bidPercent.toFixed(1),
                signal, bidTotal: (totalBid / 1000).toFixed(1) + 'K',
                askTotal: (totalAsk / 1000).toFixed(1) + 'K', direction, strength
            };
        };

        const connectWebSocket = () => {
            ws = new WebSocket('wss://stream.bybit.com/v5/public/linear');
            ws.onopen = () => {
                ws.send(JSON.stringify({ op: 'subscribe', args: [`orderbook.200.${symbol.value}`] }));
            };
            ws.onmessage = (event) => {
                if (isPaused.value) return;
                try {
                    const data = JSON.parse(event.data);
                    if (data.topic && data.topic.startsWith('orderbook') && data.data) {
                        processOrderBook(data.data, data.type === 'snapshot');
                        if (data.data.u) priceColor.value = data.data.u === 'U' ? '#10b981' : '#ef4444';
                    }
                } catch(err) { console.error(err); }
            };
            ws.onerror = (err) => console.error('OB WS error:', err);
            ws.onclose = () => {
                reconnectTimer = setTimeout(connectWebSocket, 3000);
            };
        };

        // ============================
        //  CVD WEBSOCKET (publicTrade)
        // ============================
        let bucketBuy  = 0;
        let bucketSell = 0;
        let bucketTimer = null;

        const flushBucket = () => {
            const delta = bucketBuy - bucketSell;
            const now = Date.now();

            cvdData.value.cvd      += delta;
            cvdData.value.buyVol   += bucketBuy;
            cvdData.value.sellVol  += bucketSell;
            cvdData.value.delta1m   = delta;

            const totalVol = bucketBuy + bucketSell;
            if (totalVol > 0) {
                const ratio = bucketBuy / totalVol;
                if      (ratio > 0.65) cvdData.value.signal = 'bull';
                else if (ratio < 0.35) cvdData.value.signal = 'bear';
                else                   cvdData.value.signal = 'neutral';
            }

            cvdHistory.value.push({ time: now, cvd: cvdData.value.cvd, delta });
            if (cvdHistory.value.length > MAX_CVD_HISTORY) cvdHistory.value.shift();

            drawCVDChart();

            bucketBuy  = 0;
            bucketSell = 0;
        };

        const processTrade = (trade) => {
            const size = parseFloat(trade.v) || 0;
            const price = parseFloat(trade.p) || 0;
            const usdVal = size * price;

            if (trade.S === 'Buy') {
                bucketBuy += usdVal;
            } else {
                bucketSell += usdVal;
            }

            const threshold = cvdData.value.largeThreshold || (price * 10);
            if (usdVal >= threshold) {
                const lt = {
                    side: trade.S,
                    size: size.toFixed(2),
                    usd: usdVal >= 1e6
                        ? (usdVal / 1e6).toFixed(2) + 'M'
                        : (usdVal / 1e3).toFixed(1) + 'K',
                    price: price,
                    time: new Date(trade.T).toLocaleTimeString('it-IT')
                };
                cvdData.value.lastTrades.unshift(lt);
                if (cvdData.value.lastTrades.length > 8) cvdData.value.lastTrades.pop();
            }

            if (cvdData.value.buyVol + cvdData.value.sellVol > 0) {
                const totalTrades = cvdHistory.value.length || 1;
                const avgBucket = (cvdData.value.buyVol + cvdData.value.sellVol) / totalTrades;
                cvdData.value.largeThreshold = avgBucket * 5;
            }
        };

        const connectTradesWS = () => {
            wsTrades = new WebSocket('wss://stream.bybit.com/v5/public/linear');
            wsTrades.onopen = () => {
                wsTrades.send(JSON.stringify({ op: 'subscribe', args: [`publicTrade.${symbol.value}`] }));
                bucketTimer = setInterval(flushBucket, 1000);
            };
            wsTrades.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data);
                    if (msg.topic && msg.topic.startsWith('publicTrade') && Array.isArray(msg.data)) {
                        msg.data.forEach(processTrade);
                    }
                } catch(e) { console.error(e); }
            };
            wsTrades.onerror = (e) => console.error('Trade WS error:', e);
            wsTrades.onclose = () => {
                clearInterval(bucketTimer);
                tradeReconnectTimer = setTimeout(connectTradesWS, 3000);
            };
        };

        // ============================
        //  CVD CANVAS CHART
        // ============================
        const drawCVDChart = () => {
            const canvas = document.getElementById('cvd-canvas');
            if (!canvas) return;
            const ctx = canvas.getContext('2d');
            const W = canvas.width;
            const H = canvas.height;
            ctx.clearRect(0, 0, W, H);

            const hist = cvdHistory.value;
            if (hist.length < 2) return;

            ctx.fillStyle = '#0a0a0b';
            ctx.fillRect(0, 0, W, H);

            const cvdVals = hist.map(h => h.cvd);
            const cvdMin  = Math.min(...cvdVals);
            const cvdMax  = Math.max(...cvdVals);
            const cvdRange = cvdMax - cvdMin || 1;

            const toX = (i) => (i / (hist.length - 1)) * W;
            const toCvdY = (v) => H * 0.7 - ((v - cvdMin) / cvdRange) * (H * 0.6);

            const zeroY = toCvdY(0);
            ctx.beginPath();
            ctx.strokeStyle = 'rgba(255,255,255,0.08)';
            ctx.lineWidth = 1;
            ctx.setLineDash([4, 4]);
            ctx.moveTo(0, zeroY);
            ctx.lineTo(W, zeroY);
            ctx.stroke();
            ctx.setLineDash([]);

            const lastCvd = cvdVals[cvdVals.length - 1];
            const cvdColor = lastCvd >= 0 ? '#10b981' : '#ef4444';
            const cvdGrad = ctx.createLinearGradient(0, 0, 0, H * 0.7);
            cvdGrad.addColorStop(0, lastCvd >= 0 ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.3)');
            cvdGrad.addColorStop(1, 'rgba(0,0,0,0)');

            ctx.beginPath();
            ctx.moveTo(toX(0), toCvdY(cvdVals[0]));
            for (let i = 1; i < hist.length; i++) {
                ctx.lineTo(toX(i), toCvdY(cvdVals[i]));
            }
            ctx.lineTo(toX(hist.length - 1), H * 0.7);
            ctx.lineTo(toX(0), H * 0.7);
            ctx.closePath();
            ctx.fillStyle = cvdGrad;
            ctx.fill();

            ctx.beginPath();
            ctx.strokeStyle = cvdColor;
            ctx.lineWidth = 1.5;
            ctx.moveTo(toX(0), toCvdY(cvdVals[0]));
            for (let i = 1; i < hist.length; i++) {
                ctx.lineTo(toX(i), toCvdY(cvdVals[i]));
            }
            ctx.stroke();

            const barH = H * 0.28;
            const barTop = H * 0.72;
            const deltaVals = hist.map(h => h.delta);
            const maxDelta = Math.max(...deltaVals.map(Math.abs)) || 1;
            const barW = Math.max(1, (W / hist.length) - 1);

            for (let i = 0; i < hist.length; i++) {
                const d = deltaVals[i];
                const bh = (Math.abs(d) / maxDelta) * barH;
                const bx = toX(i) - barW / 2;
                ctx.fillStyle = d >= 0 ? 'rgba(16,185,129,0.7)' : 'rgba(239,68,68,0.7)';
                ctx.fillRect(bx, barTop + (barH - bh), barW, bh);
            }

            ctx.beginPath();
            ctx.strokeStyle = 'rgba(255,255,255,0.06)';
            ctx.lineWidth = 1;
            ctx.moveTo(0, barTop);
            ctx.lineTo(W, barTop);
            ctx.stroke();

            ctx.font = '9px monospace';
            ctx.fillStyle = '#6b7280';
            ctx.fillText('CVD', 4, 10);
            ctx.fillText('Δ', 4, barTop + 10);

            ctx.font = 'bold 10px monospace';
            ctx.fillStyle = cvdColor;
            const cvdLabel = lastCvd >= 1e6
                ? (lastCvd / 1e6).toFixed(2) + 'M'
                : lastCvd >= 1e3
                    ? (lastCvd / 1e3).toFixed(1) + 'K'
                    : lastCvd.toFixed(0);
            ctx.fillText(cvdLabel, W - 50, 10);
        };

        // ============================
        //  CVD PANEL RESIZE
        // ============================
        let resizeStartX = 0;
        let resizeStartWidth = 0;

        const onResize = (e) => {
            const diff = resizeStartX - e.clientX;
            cvdWidth.value = Math.max(140, Math.min(520, resizeStartWidth + diff));
            const canvas = document.getElementById('cvd-canvas');
            if (canvas) {
                canvas.width = cvdWidth.value - 8;
                drawCVDChart();
            }
        };

        const stopResize = () => {
            isResizing.value = false;
            document.removeEventListener('mousemove', onResize);
            document.removeEventListener('mouseup', stopResize);
        };

        const startResize = (e) => {
            isResizing.value = true;
            resizeStartX = e.clientX;
            resizeStartWidth = cvdWidth.value;
            document.addEventListener('mousemove', onResize);
            document.addEventListener('mouseup', stopResize);
        };

        // ============================
        //  CLEANUP
        // ============================
        const cleanup = () => {
            if (ws) { ws.close(); ws = null; }
            if (wsTrades) { wsTrades.close(); wsTrades = null; }
            if (reconnectTimer) clearTimeout(reconnectTimer);
            if (tradeReconnectTimer) clearTimeout(tradeReconnectTimer);
            if (bucketTimer) clearInterval(bucketTimer);
            document.removeEventListener('mousemove', onResize);
            document.removeEventListener('mouseup', stopResize);
            asksMap.clear();
            bidsMap.clear();
        };

        onMounted(() => {
            fetchOrderBook();
            connectTradesWS();
            document.title = `${symbol.value} Order Book`;
        });

        onUnmounted(() => { cleanup(); });

        return {
            symbol,
            displayLevels, grouping, groupingOptions,
            displayAsks, displayBids,
            currentPrice, spread, priceColor,
            loading, error,
            imbalance, showImbalance, isPaused,
            maxLevelDistance,
            cvdData, cvdHistory, showCVD,
            showBook, cvdWidth, isResizing,
            startResize,
            fetchOrderBook, updateDisplay,
        };
    }
}).mount('#app');
