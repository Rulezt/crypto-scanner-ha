// Tool di disegno grafico (Range, Linea orizzontale, Trendline fullscreen) condivisi
// fra chart.html e mtf.html — prima erano copiati riga per riga in entrambi i file.
//
// Le funzioni operano su un oggetto "surface" passato come parametro (non su variabili
// globali): per i pannelli in griglia è direttamente l'oggetto slot (`s`), che in
// entrambe le pagine ha già le stesse proprietà (chart, candleS, rangeCanvas, ecc.);
// per la vista fullscreen ogni pagina costruisce un piccolo oggetto con getter/setter
// che leggono/scrivono le variabili globali fs* già esistenti (fsChart, fsRangeActive,
// ...), così tutti gli altri punti del codice che le referenziano direttamente
// continuano a funzionare senza modifiche.
//
// Nota: la trendline dei PANNELLI in griglia non è qui — in mtf.html è condivisa fra
// tutti i pannelli (richiesta esplicita utente) con un rendering in spazio tempo/prezzo
// diverso da chart.html (trendline indipendenti per pannello, spazio pixel); unificarla
// avrebbe richiesto scelte di design che non riguardano solo un refactor. Resta come
// oggi in entrambi i file. La trendline FULLSCREEN invece è identica nei due file ed è
// inclusa qui.
(function () {
    const TREND_HIT = 12;
    const TREND_CLICK_SLOP = 4;
    const HLINE_HIT = 8;
    // Stesso indice usato in chart.html/mtf.html/trade.js per il pane dell'indicatore ROC
    // (unico pane extra esistente oggi oltre al principale). Se in futuro nascono altri
    // indicatori a pannello, questa è l'unica costante da rivedere.
    const ROC_PANE_INDEX = 1;
    function _trendSeriesForPane(s, paneIndex) {
        return paneIndex === ROC_PANE_INDEX ? s.rocSeries : s.candleS;
    }

    // ── geometria condivisa (usata anche dal tool trendline-pannelli, non unificato) ──
    function trendSync(canvas) {
        const w = canvas.clientWidth, h = canvas.clientHeight;
        if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    }

    function trendDrawExt(ctx, x1, y1, x2, y2, W, H, color, lw) {
        ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.beginPath();
        if (Math.abs(x2 - x1) < 0.5) { ctx.moveTo(x1, 0); ctx.lineTo(x1, H); }
        else { const m = (y2 - y1) / (x2 - x1); ctx.moveTo(0, y1 + m * -x1); ctx.lineTo(W, y1 + m * (W - x1)); }
        ctx.stroke();
    }

    function ptSegDist(px, py, x1, y1, x2, y2) {
        const dx = x2 - x1, dy = y2 - y1, L2 = dx * dx + dy * dy;
        if (!L2) return Math.hypot(px - x1, py - y1);
        const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / L2));
        return Math.hypot(px - x1 - t * dx, py - y1 - t * dy);
    }

    // ── Magnete OHLC per gli endpoint della trendline ───────────────────────────────
    // Se il cursore è entro TREND_SNAP_PX da open/high/low/close di una delle candele
    // vicine (in orizzontale, sulle coordinate pixel), il punto si aggancia lì invece
    // che al prezzo/tempo grezzo sotto il cursore. Usato sia per piazzare un nuovo
    // punto sia per trascinare un endpoint esistente (non per il trascinamento
    // dell'intera linea, che resta una traslazione libera).
    //
    // excludeTime: quando si piazza/trascina il SECONDO estremo, esclude la candela
    // già usata dal primo. Senza questo, il magnete tende ad agganciare entrambi i
    // punti alla stessa candela (es. minimo→massimo) — pendenza assurda in tempo/prezzo
    // che su un pannello con TF molto più ampio (es. Weekly) si estrapola fuori da
    // qualsiasi prezzo visibile: la retta esiste ma non è MAI visibile sugli altri
    // pannelli (bug reale riprodotto: trendline 1m invisibile su tutti gli altri TF).
    const TREND_SNAP_PX = 10;
    function trendSnapPoint(chart, candleS, klines, px, py, excludeTime) {
        if (!klines || !klines.length) return null;
        let logical;
        try { logical = chart.timeScale().coordinateToLogical(px); } catch(e) { return null; }
        if (logical == null) return null;
        const idx = Math.round(logical);
        let best = null, bestDist = TREND_SNAP_PX;
        for (let i = idx - 1; i <= idx + 1; i++) {
            const k = klines[i];
            if (!k || k.time === excludeTime) continue;
            for (const price of [k.open, k.high, k.low, k.close]) {
                let y; try { y = candleS.priceToCoordinate(price); } catch(e) { continue; }
                if (y == null) continue;
                const d = Math.abs(y - py);
                if (d < bestDist) { bestDist = d; best = { price, time: k.time }; }
            }
        }
        return best;
    }
    // Punto trendline con fallback al prezzo/tempo grezzo se nessun OHLC è abbastanza vicino.
    function trendPickPoint(chart, candleS, klines, px, py, excludeTime) {
        const snap = trendSnapPoint(chart, candleS, klines, px, py, excludeTime);
        if (snap) return { t: snap.time, p: snap.price };
        const t = chart.timeScale().coordinateToTime(px), p = candleS.coordinateToPrice(py);
        return (t != null && p != null) ? { t, p } : null;
    }

    // ── Multi-pane (es. pannello ROC sotto le candele) ──────────────────────────────
    // Le serie di pane diversi dal principale (indice 0) hanno una price-scale
    // indipendente le cui coordinate priceToCoordinate/coordinateToPrice sono LOCALI
    // al pane (0 = bordo superiore di QUEL pane), non del canvas overlay condiviso che
    // copre tutti i pane impilati. Prima di questo helper il codice passava sempre la Y
    // del mouse (relativa all'intero canvas) a candleS: dentro il pane ROC risultava in
    // un prezzo estrapolato a vanvera e la retta disegnata sbordava sul pane principale.
    // getHTMLElement() è l'API ufficiale (Lightweight Charts v5) per il rettangolo reale
    // di un pane, da cui ricavare offset (per disegnare) e bordi (per il clip).
    function getPaneRect(chart, paneIndex) {
        try {
            const pane = chart.panes()[paneIndex];
            const el = pane && pane.getHTMLElement && pane.getHTMLElement();
            return el ? el.getBoundingClientRect() : null;
        } catch (e) { return null; }
    }

    // Individua in quale pane cade clientY (coordinate viewport, es. e.clientY). extraPanes:
    // [{ index, series }] per pane oltre al principale (es. ROC). Ritorna la serie/rettangolo
    // da usare per le conversioni prezzo e se il magnete OHLC è applicabile (solo sul pane
    // principale, l'unico con candele — agganciarsi a O/H/L/C di un oscillatore non ha senso).
    function resolvePaneAtY(chart, candleS, clientY, extraPanes) {
        for (const ep of (extraPanes || [])) {
            if (!ep.series) continue;
            const rect = getPaneRect(chart, ep.index);
            if (rect && clientY >= rect.top && clientY < rect.bottom) {
                return { index: ep.index, series: ep.series, rect, allowSnap: false };
            }
        }
        return { index: 0, series: candleS, rect: getPaneRect(chart, 0), allowSnap: true };
    }

    // Come trendPickPoint, ma su un pane/serie già risolti da resolvePaneAtY: py è la
    // coordinata Y LOCALE al pane (clientY - rect.top), non quella del canvas overlay.
    function trendPickPointForSeries(chart, series, klines, px, py, excludeTime, allowSnap) {
        if (allowSnap) return trendPickPoint(chart, series, klines, px, py, excludeTime);
        const t = chart.timeScale().coordinateToTime(px), p = series.coordinateToPrice(py);
        return (t != null && p != null) ? { t, p } : null;
    }

    // Wrapper comodi per le implementazioni (griglia/fullscreen/trade.js) che tengono lo
    // stato su un oggetto "surface" `s` con .chart/.candleS/.klines/.rocSeries — in
    // trade.js, che non ha slot/oggetti fullscreen come chart.html/mtf.html, è un piccolo
    // oggetto ad-hoc con getter verso le variabili modulo (_obTrendSurface).
    //
    // Primo punto di una nuova linea: il pane si risolve dalla posizione del mouse.
    function trendPickAtClient(s, clientY, px, excludeTime) {
        const extraPanes = s.rocSeries ? [{ index: ROC_PANE_INDEX, series: s.rocSeries }] : [];
        const resolved = resolvePaneAtY(s.chart, s.candleS, clientY, extraPanes);
        if (!resolved.rect) return null;
        const localPy = clientY - resolved.rect.top;
        const r = trendPickPointForSeries(s.chart, resolved.series, s.klines, px, localPy, excludeTime, resolved.allowSnap);
        return r ? { ...r, pane: resolved.index } : null;
    }
    // Secondo punto / drag di una linea già iniziata in un pane noto: NON si ri-risolve il
    // pane dalla Y corrente (una trendline non può "cambiare pane" a metà tracciamento).
    function trendPickAtPane(s, paneIndex, clientY, px, excludeTime) {
        const series = _trendSeriesForPane(s, paneIndex);
        if (!series) return null;
        const rect = getPaneRect(s.chart, paneIndex);
        if (!rect) return null;
        const localPy = clientY - rect.top;
        return trendPickPointForSeries(s.chart, series, s.klines, px, localPy, excludeTime, paneIndex === 0);
    }

    // ── Touch support ────────────────────────────────────────────────────────────────
    // Tutti i tool sotto sono nati solo per mouse (mousedown/mousemove/mouseup +
    // contextmenu per menu/cancella-riga). Su schermi touch questi eventi non esistono
    // affatto durante un gesto continuo (nessun mousemove sintetico durante un drag), va
    // ripetuto lo stesso schema con touchstart/touchmove/touchend traducendo il touch in
    // un oggetto "mouse-like" così le funzioni handler già scritte per il mouse restano
    // invariate e vengono richiamate identiche anche dal touch.
    function _touchToMouseLike(te) {
        const t = te.touches[0] || te.changedTouches[0];
        return t ? { clientX: t.clientX, clientY: t.clientY, button: 0, buttons: 1, preventDefault: () => te.preventDefault() } : null;
    }
    // Aggancia le controparti touch di mousedown(sul target)/mousemove(su document)/
    // mouseup(su document): stesso pattern usato ovunque per permettere il drag anche
    // fuori dal canvas. Ritorna gli handler per poterli rimuovere nel cleanup (unwireTouch).
    function wireTouch(target, onDown, onMove, onUp) {
        const ts = e => { const m = _touchToMouseLike(e); if (m) { e.preventDefault(); onDown(m); } };
        const tm = e => { const m = _touchToMouseLike(e); if (m) { e.preventDefault(); onMove(m); } };
        const tu = e => { const m = _touchToMouseLike(e); if (m) onUp(m); };
        target.addEventListener('touchstart', ts, { passive: false });
        document.addEventListener('touchmove', tm, { passive: false });
        document.addEventListener('touchend', tu);
        document.addEventListener('touchcancel', tu);
        return { ts, tm, tu };
    }
    function unwireTouch(target, h) {
        if (!h) return;
        target.removeEventListener('touchstart', h.ts);
        document.removeEventListener('touchmove', h.tm);
        document.removeEventListener('touchend', h.tu);
        document.removeEventListener('touchcancel', h.tu);
    }
    // Pressione prolungata (long-press) = equivalente touch del tasto destro, usata SOLO
    // per aprire il menu di scelta tool (attachToolContextMenu) quando nessun tool è
    // attivo. Non si arma affatto se un tool è già attivo (isBlocked): un tool attivo
    // interpreta già touchstart come "piazza/trascina" — far scattare ANCHE un long-press
    // sullo stesso gesto lo cancellerebbe/eliminerebbe a metà, prima ancora che l'utente
    // stacchi il dito. Con isBlocked il long-press resta un entry-point pulito, separato
    // dal disegno vero e proprio (che quando attivo si gestisce già coi touch di wireTouch).
    function wireLongPress(target, { isBlocked, onLongPress }) {
        const DELAY = 550, SLOP = 12;
        let timer = null, sx = 0, sy = 0;
        const clear = () => { if (timer) { clearTimeout(timer); timer = null; } };
        target.addEventListener('touchstart', e => {
            if (isBlocked && isBlocked()) return;
            if (e.touches.length !== 1) { clear(); return; }
            const t = e.touches[0]; sx = t.clientX; sy = t.clientY;
            clear();
            timer = setTimeout(() => { timer = null; onLongPress(sx, sy); }, DELAY);
        }, { passive: true });
        target.addEventListener('touchmove', e => {
            const t = e.touches[0]; if (!t) return;
            if (Math.hypot(t.clientX - sx, t.clientY - sy) > SLOP) clear();
        }, { passive: true });
        target.addEventListener('touchend', clear);
        target.addEventListener('touchcancel', clear);
    }
    function fireContextMenu(el, clientX, clientY) {
        try { el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX, clientY })); } catch (e) {}
    }

    // ── Range (tool di misura, mai persistito) ──────────────────────────────────────
    function drawRangeCanvas(canvas, series, p1, p2) {
        canvas.width  = canvas.clientWidth  || canvas.parentElement.clientWidth  || 100;
        canvas.height = canvas.clientHeight || canvas.parentElement.clientHeight || 100;
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        if (p1 === null || p2 === null) return;

        let y1 = series.priceToCoordinate(p1);
        let y2 = series.priceToCoordinate(p2);
        if (y1 === null) y1 = p2 > p1 ? canvas.height : 0;
        if (y2 === null) y2 = p2 > p1 ? 0 : canvas.height;

        const top    = Math.min(y1, y2);
        const bottom = Math.max(y1, y2);
        const h      = Math.max(bottom - top, 1);
        const isUp   = p2 > p1;
        const color  = isUp ? '#20B26C' : '#EF454A';

        ctx.fillStyle = isUp ? 'rgba(8,153,129,0.12)' : 'rgba(242,54,69,0.12)';
        ctx.fillRect(0, top, canvas.width, h);

        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.setLineDash([5, 3]);
        ctx.beginPath(); ctx.moveTo(0, y1); ctx.lineTo(canvas.width, y1); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(0, y2); ctx.lineTo(canvas.width, y2); ctx.stroke();
        ctx.setLineDash([]);

        const delta    = p2 - p1;
        const pct      = (delta / p1) * 100;
        const sign     = delta >= 0 ? '+' : '';
        const absDelta = Math.abs(delta);
        const dStr     = absDelta >= 100 ? delta.toFixed(1) : absDelta >= 1 ? delta.toFixed(2) : delta.toFixed(4);
        const label    = `${sign}${dStr}  (${sign}${pct.toFixed(2)}%)`;

        const fs  = Math.max(9, Math.min(12, Math.floor(h * 0.35)));
        ctx.font  = `bold ${fs}px monospace`;
        const tw  = ctx.measureText(label).width;
        const pad = 4;
        const lx  = canvas.width - tw - pad * 2 - 6;
        const ly  = top + h / 2;

        ctx.fillStyle = isUp ? 'rgba(8,153,129,0.88)' : 'rgba(242,54,69,0.88)';
        ctx.beginPath();
        ctx.roundRect ? ctx.roundRect(lx - pad, ly - fs, tw + pad * 2, fs + 6, 3)
                      : ctx.rect(lx - pad, ly - fs, tw + pad * 2, fs + 6);
        ctx.fill();
        ctx.fillStyle = '#ffffff';
        ctx.fillText(label, lx, ly);
    }

    function drawRangeLine(canvas, series, price) {
        canvas.width  = canvas.clientWidth  || canvas.parentElement?.clientWidth  || 100;
        canvas.height = canvas.clientHeight || canvas.parentElement?.clientHeight || 100;
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        const y = series.priceToCoordinate(price);
        if (y == null) return;
        ctx.strokeStyle = '#f59e0b';
        ctx.lineWidth = 1;
        ctx.setLineDash([5, 3]);
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(canvas.width, y); ctx.stroke();
        ctx.setLineDash([]);
        const label = price >= 100 ? price.toFixed(2) : price >= 1 ? price.toFixed(4) : price.toFixed(6);
        ctx.font = 'bold 10px monospace';
        const tw = ctx.measureText(label).width;
        const px = canvas.width - tw - 14;
        ctx.fillStyle = 'rgba(245,158,11,0.9)';
        if (ctx.roundRect) ctx.roundRect(px - 4, y - 10, tw + 8, 14, 3); else ctx.rect(px - 4, y - 10, tw + 8, 14);
        ctx.fill();
        ctx.fillStyle = '#fff';
        ctx.fillText(label, px, y);
    }

    // opts: { onDeactivateSiblings } — chiamata se il tool si sta ATTIVANDO, per spegnere
    // gli altri tool attivi sulla stessa surface (la pagina decide quali sono "fratelli").
    function toggleRange(s, opts = {}) {
        if (!s.rangeActive && opts.onDeactivateSiblings) opts.onDeactivateSiblings();
        s.rangeActive = !s.rangeActive;
        s.rangeBtn.style.color = s.rangeActive ? '#f59e0b' : '#4B5563';
        if (s._rangeMD) { s.rangeCanvas.removeEventListener('mousedown', s._rangeMD); s._rangeMD = null; }
        if (s._rangeMM) { document.removeEventListener('mousemove', s._rangeMM); s._rangeMM = null; }
        if (s._rangeMU) { document.removeEventListener('mouseup', s._rangeMU); s._rangeMU = null; }
        if (s._rangeCM) { s.rangeCanvas.removeEventListener('contextmenu', s._rangeCM); s._rangeCM = null; }
        unwireTouch(s.rangeCanvas, s._rangeTouch); s._rangeTouch = null;
        if (s._rangeHoverML) { s.rangeCanvas.removeEventListener('mouseleave', s._rangeHoverML); s._rangeHoverML = null; }
        if (s._rangeHoverLine) { try { s.candleS.removePriceLine(s._rangeHoverLine); } catch(e) {} s._rangeHoverLine = null; }
        s.rangeP1 = null;
        try { const rc = s.rangeCanvas.getContext('2d'); rc.clearRect(0,0,s.rangeCanvas.width,s.rangeCanvas.height); } catch(e) {}
        s.rangeCanvas.style.pointerEvents = s.rangeActive ? 'auto' : 'none';
        s.rangeCanvas.style.cursor = s.rangeActive ? 'crosshair' : '';
        s.rangeCanvas.style.touchAction = s.rangeActive ? 'none' : '';
        if (!s.rangeActive) return;
        s._rangeMD = e => {
            if (e.button !== 0) return;
            e.preventDefault();
            const rect = s.rangeCanvas.getBoundingClientRect();
            const price = s.candleS.coordinateToPrice(e.clientY - rect.top);
            if (price == null) return;
            s.rangeP1 = price;
        };
        s._rangeMM = e => {
            const rect = s.rangeCanvas.getBoundingClientRect();
            const cy = e.clientY - rect.top;
            const p2 = s.candleS.coordinateToPrice(cy);
            if (s.rangeP1 === null) {
                if (cy < 0 || cy > rect.height || p2 == null) {
                    if (s._rangeHoverLine) { try { s.candleS.removePriceLine(s._rangeHoverLine); } catch(ex) {} s._rangeHoverLine = null; }
                    try { const rc = s.rangeCanvas.getContext('2d'); rc.clearRect(0,0,s.rangeCanvas.width,s.rangeCanvas.height); } catch(ex) {}
                    return;
                }
                if (s._rangeHoverLine) { try { s._rangeHoverLine.applyOptions({ price: p2 }); } catch(ex) {} }
                else { try { s._rangeHoverLine = s.candleS.createPriceLine({ price: p2, color: '#f59e0b', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' }); } catch(ex) {} }
                drawRangeLine(s.rangeCanvas, s.candleS, p2);
                return;
            }
            if (!(e.buttons & 1) || p2 == null) return;
            drawRangeCanvas(s.rangeCanvas, s.candleS, s.rangeP1, p2);
        };
        s._rangeMU = e => {
            if (e.button !== 0) return;
            s.rangeP1 = null;
            try { const rc = s.rangeCanvas.getContext('2d'); rc.clearRect(0,0,s.rangeCanvas.width,s.rangeCanvas.height); } catch(e) {}
        };
        s._rangeCM = e => { e.preventDefault(); toggleRange(s, opts); };
        s._rangeHoverML = () => {
            if (s._rangeHoverLine) { try { s.candleS.removePriceLine(s._rangeHoverLine); } catch(e) {} s._rangeHoverLine = null; }
            try { const rc = s.rangeCanvas.getContext('2d'); rc.clearRect(0,0,s.rangeCanvas.width,s.rangeCanvas.height); } catch(e) {}
        };
        s.rangeCanvas.addEventListener('mousedown', s._rangeMD);
        document.addEventListener('mousemove', s._rangeMM);
        document.addEventListener('mouseup', s._rangeMU);
        s.rangeCanvas.addEventListener('contextmenu', s._rangeCM);
        s.rangeCanvas.addEventListener('mouseleave', s._rangeHoverML);
        s._rangeTouch = wireTouch(s.rangeCanvas, s._rangeMD, s._rangeMM, s._rangeMU);
    }

    // ── Hline ────────────────────────────────────────────────────────────────────────
    function hlineNearest(s, clientY) {
        const rect = s.hlineCanvas.getBoundingClientRect();
        const y = clientY - rect.top;
        let best = null, bestDist = HLINE_HIT;
        for (const hl of s.hlines) {
            try { const hy = s.candleS.priceToCoordinate(hl.price); if (hy != null && Math.abs(hy - y) < bestDist) { bestDist = Math.abs(hy - y); best = hl; } } catch(e) {}
        }
        return best;
    }

    // opts: { onDeactivateSiblings, onChange } — onChange chiamata dopo create/drag/delete
    // (mtf.html la valorizza per persistere su localStorage/server, chart.html no).
    function toggleHline(s, opts = {}) {
        if (!s.hlineActive && opts.onDeactivateSiblings) opts.onDeactivateSiblings();
        s.hlineActive = !s.hlineActive;
        s.hlineBtn.style.color = s.hlineActive ? '#3b82f6' : '#4B5563';
        if (s._hlineMD) { s.hlineCanvas.removeEventListener('mousedown', s._hlineMD); s._hlineMD = null; }
        if (s._hlineMM) { document.removeEventListener('mousemove', s._hlineMM); s._hlineMM = null; }
        if (s._hlineMU) { document.removeEventListener('mouseup', s._hlineMU); s._hlineMU = null; }
        if (s._hlineCM) { s.hlineCanvas.removeEventListener('contextmenu', s._hlineCM); s._hlineCM = null; }
        unwireTouch(s.hlineCanvas, s._hlineTouch); s._hlineTouch = null;
        if (s._hlineHoverML) { s.hlineCanvas.removeEventListener('mouseleave', s._hlineHoverML); s._hlineHoverML = null; }
        if (s._hlineHoverLine) { try { s.candleS.removePriceLine(s._hlineHoverLine); } catch(e) {} s._hlineHoverLine = null; }
        s._hlineDragging = null;
        s.hlineCanvas.style.pointerEvents = s.hlineActive ? 'auto' : 'none';
        s.hlineCanvas.style.cursor = s.hlineActive ? 'crosshair' : '';
        s.hlineCanvas.style.touchAction = s.hlineActive ? 'none' : '';
        if (!s.hlineActive) return;
        s._hlineMD = e => {
            if (e.button !== 0) return;
            e.preventDefault();
            const nearest = hlineNearest(s, e.clientY);
            if (nearest) { s._hlineDragging = nearest; s.hlineCanvas.style.cursor = 'ns-resize'; }
            else {
                const rect = s.hlineCanvas.getBoundingClientRect();
                const price = s.candleS.coordinateToPrice(e.clientY - rect.top);
                if (price != null) {
                    const pl = s.candleS.createPriceLine({ price, color: '#3b82f6', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' });
                    s.hlines.push({ priceLine: pl, price });
                    opts.onSyncClearBtn?.();
                    opts.onChange?.();
                }
            }
        };
        s._hlineMM = e => {
            if (s._hlineDragging) {
                const rect = s.hlineCanvas.getBoundingClientRect();
                const price = s.candleS.coordinateToPrice(e.clientY - rect.top);
                if (price != null) { s._hlineDragging.price = price; try { s._hlineDragging.priceLine.applyOptions({ price }); } catch(ex) {} }
            } else {
                const rect = s.hlineCanvas.getBoundingClientRect();
                const price = s.candleS.coordinateToPrice(e.clientY - rect.top);
                if (price != null) {
                    if (s._hlineHoverLine) { try { s._hlineHoverLine.applyOptions({ price }); } catch(ex) {} }
                    else { try { s._hlineHoverLine = s.candleS.createPriceLine({ price, color: '#3b82f6', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' }); } catch(ex) {} }
                }
                s.hlineCanvas.style.cursor = hlineNearest(s, e.clientY) ? 'ns-resize' : 'crosshair';
            }
        };
        s._hlineMU = e => {
            if (e.button !== 0) return;
            if (s._hlineDragging) opts.onChange?.();
            s._hlineDragging = null;
            s.hlineCanvas.style.cursor = hlineNearest(s, e.clientY) ? 'ns-resize' : 'crosshair';
        };
        s._hlineCM = e => {
            e.preventDefault();
            const nearest = hlineNearest(s, e.clientY);
            if (nearest) {
                try { s.candleS.removePriceLine(nearest.priceLine); } catch(ex) {}
                s.hlines = s.hlines.filter(h => h !== nearest);
                opts.onSyncClearBtn?.();
                opts.onChange?.();
            }
            else toggleHline(s, opts);
        };
        s._hlineHoverML = () => { if (s._hlineHoverLine) { try { s.candleS.removePriceLine(s._hlineHoverLine); } catch(e) {} s._hlineHoverLine = null; } };
        s.hlineCanvas.addEventListener('mousedown', s._hlineMD);
        document.addEventListener('mousemove', s._hlineMM);
        document.addEventListener('mouseup', s._hlineMU);
        s.hlineCanvas.addEventListener('contextmenu', s._hlineCM);
        s.hlineCanvas.addEventListener('mouseleave', s._hlineHoverML);
        s._hlineTouch = wireTouch(s.hlineCanvas, s._hlineMD, s._hlineMM, s._hlineMU);
    }

    function clearHlines(s, opts = {}) {
        for (const hl of s.hlines) { try { s.candleS.removePriceLine(hl.priceLine); } catch(e) {} }
        s.hlines = [];
        opts.onChange?.();
    }

    // timeToCoordinate() nativo torna null se tl.t1/t2 (timestamp esatti delle candele
    // del TF con cui la linea è stata tracciata) non combaciano con nessuna candela del
    // TF corrente (es. minuto preciso tracciato a 1m, poi il fullscreen passa a 5m) — la
    // trendline spariva cambiando TF pur restando in s.trendlines. Fallback: interpola
    // tra le due candele più vicine del TF corrente (richiede s.klines sulla surface).
    function timeToXRobust(s, t) {
        const direct = s.chart.timeScale().timeToCoordinate(t);
        if (direct != null) return direct;
        const kl = s.klines;
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
            const xLo = s.chart.timeScale().logicalToCoordinate(loIdx);
            const xHi = s.chart.timeScale().logicalToCoordinate(hiIdx);
            if (xLo == null || xHi == null) return null;
            return xLo + (xHi - xLo) * frac;
        } catch(e) { return null; }
    }

    // Coordinate Y di una trendline nello spazio del canvas overlay condiviso (che copre
    // tutti i pane impilati): la serie del pane di tl.pane ritorna una Y LOCALE a quel
    // pane, va sommato l'offset del pane rispetto al canvas. Ritorna anche il rettangolo
    // del pane (per il clip in trendDrawAll) — null se il pane non esiste più (es. ROC
    // disattivato dopo che la linea era stata tracciata lì).
    function _trendCanvasY(s, canvas, paneIndex, price) {
        const series = _trendSeriesForPane(s, paneIndex);
        if (!series) return null;
        const paneRect = getPaneRect(s.chart, paneIndex);
        if (!paneRect) return null;
        const local = series.priceToCoordinate(price);
        if (local == null) return null;
        const canvasRect = canvas.getBoundingClientRect();
        return { y: local + (paneRect.top - canvasRect.top), paneRect, canvasRect };
    }

    // ── Trendline — SOLO fullscreen (i pannelli in griglia restano non unificati) ────
    function trendDrawAll(s) {
        s.syncClearBtn?.();
        const canvas = s.trendCanvas; if (!canvas || !s.candleS || !s.chart) return;
        trendSync(canvas);
        const ctx = canvas.getContext('2d'), W = canvas.width, H = canvas.height;
        // La retta estesa si ferma al bordo del pannello candele, PRIMA della colonna
        // prezzo (il canvas del tool è largo quanto l'intero container, asse compreso).
        let paneW = W;
        try { const aw = s.chart.priceScale('right').width(); if (Number.isFinite(aw)) paneW = Math.max(0, W - aw); } catch(e) {}
        ctx.clearRect(0, 0, W, H);
        for (const tl of s.trendlines) {
            try {
                const paneIndex = tl.pane || 0;
                const c1 = _trendCanvasY(s, canvas, paneIndex, tl.p1), c2 = _trendCanvasY(s, canvas, paneIndex, tl.p2);
                const x1 = timeToXRobust(s, tl.t1), x2 = timeToXRobust(s, tl.t2);
                if (x1==null||!c1||x2==null||!c2) continue;
                const y1 = c1.y, y2 = c2.y, canvasRect = c1.canvasRect;
                const paneTop = c1.paneRect.top - canvasRect.top;
                // Confina la retta estesa (e i marker) dentro il rettangolo del proprio
                // pane, così non sbrodola sull'altro pane quando la pendenza è estrema.
                ctx.save();
                ctx.beginPath(); ctx.rect(0, paneTop, paneW, c1.paneRect.height); ctx.clip();
                trendDrawExt(ctx, x1, y1, x2, y2, paneW, H, '#22c55e', 1.5);
                ctx.fillStyle = '#22c55e';
                ctx.beginPath(); ctx.arc(x1, y1, 4, 0, Math.PI*2); ctx.fill();
                ctx.beginPath(); ctx.arc(x2, y2, 4, 0, Math.PI*2); ctx.fill();
                ctx.restore();
            } catch(ex) {}
        }
        if (s._trendP1 && s._trendPrev) {
            try {
                const paneIndex = s._trendPane || 0;
                const x1 = s.chart.timeScale().timeToCoordinate(s._trendP1.t);
                const c1 = _trendCanvasY(s, canvas, paneIndex, s._trendP1.p);
                if (x1!=null && c1) {
                    ctx.strokeStyle = '#22c55e'; ctx.lineWidth = 1; ctx.setLineDash([5,3]);
                    ctx.beginPath(); ctx.moveTo(x1, c1.y); ctx.lineTo(s._trendPrev.x, s._trendPrev.y); ctx.stroke();
                    ctx.setLineDash([]);
                    ctx.fillStyle = '#22c55e'; ctx.beginPath(); ctx.arc(x1, c1.y, 4, 0, Math.PI*2); ctx.fill();
                }
            } catch(ex) {}
        }
    }

    function trendEnsureRAF(s) {
        if (s._trendRAF) return;
        const tick = () => {
            if (!s.chart || !s.candleS || (!s.trendlines.length && !s.trendActive)) { s._trendRAF = null; return; }
            trendDrawAll(s);
            s._trendRAF = requestAnimationFrame(tick);
        };
        s._trendRAF = requestAnimationFrame(tick);
    }

    function trendNearest(s, cx, cy) {
        const canvas = s.trendCanvas; if (!canvas) return null;
        const rect = canvas.getBoundingClientRect(), mx = cx-rect.left, my = cy-rect.top;
        let best = null, bestDist = TREND_HIT, bestPart = 'line';
        for (const tl of s.trendlines) {
            try {
                const paneIndex = tl.pane || 0;
                const c1 = _trendCanvasY(s, canvas, paneIndex, tl.p1), c2 = _trendCanvasY(s, canvas, paneIndex, tl.p2);
                const x1 = timeToXRobust(s, tl.t1), x2 = timeToXRobust(s, tl.t2);
                if (x1==null||!c1||x2==null||!c2) continue;
                const y1 = c1.y, y2 = c2.y;
                const d1 = Math.hypot(mx-x1, my-y1), d2 = Math.hypot(mx-x2, my-y2);
                if (d1 <= TREND_HIT && d1 < bestDist) { best = tl; bestDist = d1; bestPart = 'p1'; }
                else if (d2 <= TREND_HIT && d2 < bestDist) { best = tl; bestDist = d2; bestPart = 'p2'; }
                else { const dl = ptSegDist(mx, my, x1, y1, x2, y2); if (dl < bestDist) { best = tl; bestDist = dl; bestPart = 'line'; } }
            } catch(ex) {}
        }
        return best ? { tl: best, part: bestPart } : null;
    }

    // opts: { onDeactivateSiblings }
    function toggleFsTrend(s, opts = {}) {
        if (!s.trendActive && opts.onDeactivateSiblings) opts.onDeactivateSiblings();
        s.trendActive = !s.trendActive;
        s.trendBtn.style.color = s.trendActive ? '#22c55e' : '#B2B5BE';
        if (s._trendMD) { s.trendCanvas.removeEventListener('mousedown', s._trendMD); s._trendMD = null; }
        if (s._trendMM) { document.removeEventListener('mousemove', s._trendMM); s._trendMM = null; }
        if (s._trendMU) { document.removeEventListener('mouseup', s._trendMU); s._trendMU = null; }
        if (s._trendCM) { s.trendCanvas.removeEventListener('contextmenu', s._trendCM); s._trendCM = null; }
        unwireTouch(s.trendCanvas, s._trendTouch); s._trendTouch = null;
        s._trendP1 = null; s._trendPrev = null; s._trendPane = null; s._trendDrag = null; s._trendPending = null;
        s.trendCanvas.style.pointerEvents = s.trendActive ? 'auto' : 'none';
        s.trendCanvas.style.cursor = s.trendActive ? 'crosshair' : '';
        s.trendCanvas.style.touchAction = s.trendActive ? 'none' : '';
        trendEnsureRAF(s);
        trendDrawAll(s);
        if (!s.trendActive) return;
        const canvas = s.trendCanvas;
        s._trendMD = e => {
            if (e.button !== 0) return; e.preventDefault();
            const rect = canvas.getBoundingClientRect(), px = e.clientX-rect.left, py = e.clientY-rect.top;
            const hit = trendNearest(s, e.clientX, e.clientY);
            if (hit && !s._trendP1) {
                // Non decidere subito se è drag o un click per iniziare una nuova linea:
                // serve poter piazzare due trendline dallo stesso punto esatto (vedi mousemove/mouseup).
                s._trendPending = { hit, px, py };
            } else if (!s._trendP1) {
                const r = trendPickAtClient(s, e.clientY, px);
                if (r) { s._trendP1 = r; s._trendPane = r.pane; }
            } else {
                const r2 = trendPickAtPane(s, s._trendPane || 0, e.clientY, px, s._trendP1.t);
                if (r2) { s.trendlines.push({ t1: s._trendP1.t, p1: s._trendP1.p, t2: r2.t, p2: r2.p, pane: s._trendPane || 0 }); opts.onChange?.(); }
                s._trendP1 = null; s._trendPrev = null; s._trendPane = null;
                trendDrawAll(s);
            }
        };
        s._trendMM = e => {
            const rect = canvas.getBoundingClientRect(), px = e.clientX-rect.left, py = e.clientY-rect.top;
            if (s._trendPending) {
                if (Math.hypot(px - s._trendPending.px, py - s._trendPending.py) <= TREND_CLICK_SLOP) return;
                const { hit, px: dpx0, py: dpy0 } = s._trendPending;
                if (hit.part !== 'line') { s._trendDrag = { tl: hit.tl, part: hit.part }; canvas.style.cursor = 'grabbing'; }
                else {
                    const dragPane = hit.tl.pane || 0;
                    const c1 = _trendCanvasY(s, canvas, dragPane, hit.tl.p1), c2 = _trendCanvasY(s, canvas, dragPane, hit.tl.p2);
                    const ox1 = timeToXRobust(s, hit.tl.t1), ox2 = timeToXRobust(s, hit.tl.t2);
                    s._trendDrag = { tl: hit.tl, part: 'line', sx: dpx0, sy: dpy0, ox1, oy1: c1 && c1.y, ox2, oy2: c2 && c2.y };
                    canvas.style.cursor = 'move';
                }
                s._trendPending = null;
            }
            if (s._trendDrag) {
                if (!(e.buttons & 1)) { s._trendDrag = null; return; }
                const { tl, part } = s._trendDrag;
                const dragPane = tl.pane || 0;
                if (part === 'p1') { const r = trendPickAtPane(s, dragPane, e.clientY, px, tl.t2); if(r){tl.t1=r.t;tl.p1=r.p;} }
                else if (part === 'p2') { const r = trendPickAtPane(s, dragPane, e.clientY, px, tl.t1); if(r){tl.t2=r.t;tl.p2=r.p;} }
                else {
                    // oy1/oy2 sono già in spazio canvas condiviso (vedi _trendCanvasY sopra):
                    // per riconvertirli in prezzo serve la serie del pane della linea, sulla
                    // coordinata LOCALE al pane — sottrarre l'offset del pane dal canvas.
                    const series = _trendSeriesForPane(s, dragPane), paneRect = getPaneRect(s.chart, dragPane);
                    if (series && paneRect) {
                        const canvasRect = canvas.getBoundingClientRect(), offsetY = paneRect.top - canvasRect.top;
                        const dpx=px-s._trendDrag.sx, dpy=py-s._trendDrag.sy;
                        const nt1=s.chart.timeScale().coordinateToTime(s._trendDrag.ox1+dpx), np1=series.coordinateToPrice(s._trendDrag.oy1+dpy-offsetY);
                        const nt2=s.chart.timeScale().coordinateToTime(s._trendDrag.ox2+dpx), np2=series.coordinateToPrice(s._trendDrag.oy2+dpy-offsetY);
                        if(nt1&&np1&&nt2&&np2){tl.t1=nt1;tl.p1=np1;tl.t2=nt2;tl.p2=np2;}
                    }
                }
                trendDrawAll(s);
            } else if (s._trendP1) {
                s._trendPrev = { x: px, y: py };
                trendDrawAll(s);
            } else {
                const hit = trendNearest(s, e.clientX, e.clientY);
                canvas.style.cursor = hit ? (hit.part === 'line' ? 'move' : 'grab') : 'crosshair';
            }
        };
        s._trendMU = e => {
            if (e.button !== 0) return;
            if (s._trendPending) {
                // Click senza drag su un punto/linea esistente → inizia una nuova trendline da qui.
                const { px: cpx } = s._trendPending;
                const r = trendPickAtClient(s, e.clientY, cpx);
                if (r) { s._trendP1 = r; s._trendPane = r.pane; }
                s._trendPending = null;
                return;
            }
            if (s._trendDrag) { s._trendDrag = null; opts.onChange?.(); }
            canvas.style.cursor = 'crosshair';
        };
        s._trendCM = e => {
            e.preventDefault();
            if (s._trendP1) { s._trendP1 = null; s._trendPrev = null; s._trendPane = null; trendDrawAll(s); return; }
            const hit = trendNearest(s, e.clientX, e.clientY);
            if (hit) { s.trendlines = s.trendlines.filter(tl => tl !== hit.tl); trendDrawAll(s); opts.onChange?.(); }
            else toggleFsTrend(s, opts);
        };
        canvas.addEventListener('mousedown', s._trendMD);
        document.addEventListener('mousemove', s._trendMM);
        document.addEventListener('mouseup', s._trendMU);
        canvas.addEventListener('contextmenu', s._trendCM);
        s._trendTouch = wireTouch(canvas, s._trendMD, s._trendMM, s._trendMU);
    }

    // ── Menu contestuale (tasto destro nel grafico quando nessun tool è attivo) ────────
    // Entry point aggiuntivo per attivare Range/Hline/Trendline oltre ai bottoni header
    // (richiesto su index/chart/mtf/trade, griglia+fullscreen). Quando un tool è già
    // attivo il tasto destro resta al listener del tool stesso (cancella la riga più
    // vicina) — qui si esce subito per non sovrapporre le due azioni.
    let _dtMenu = null;
    function _dtMenuEsc(e) { if (e.key === 'Escape') closeToolMenu(); }
    function closeToolMenu() {
        if (_dtMenu) { try { _dtMenu.remove(); } catch(e) {} _dtMenu = null; }
        document.removeEventListener('keydown', _dtMenuEsc);
    }
    function showToolMenu(clientX, clientY, items) {
        closeToolMenu();
        const menu = document.createElement('div');
        menu.style.cssText = 'position:fixed;z-index:9999;background:#1E222D;border:1px solid #374151;border-radius:6px;overflow:hidden;box-shadow:0 6px 20px rgba(0,0,0,.7);min-width:170px;font-family:inherit;';
        menu.style.left = clientX + 'px';
        menu.style.top = clientY + 'px';
        for (const { label, icon, onClick } of items) {
            if (!onClick) continue;
            const item = document.createElement('div');
            item.innerHTML = (icon || '') + '<span>' + label + '</span>';
            item.style.cssText = 'padding:8px 14px;font-size:12px;color:#E5E7EB;cursor:pointer;white-space:nowrap;display:flex;align-items:center;gap:8px;';
            item.onmouseenter = () => item.style.background = '#2A2E39';
            item.onmouseleave = () => item.style.background = '';
            item.onmousedown = e => e.stopPropagation();
            item.onclick = e => { e.stopPropagation(); closeToolMenu(); onClick(); };
            menu.appendChild(item);
        }
        document.body.appendChild(menu);
        _dtMenu = menu;
        requestAnimationFrame(() => {
            if (!_dtMenu) return;
            const r = menu.getBoundingClientRect();
            if (r.right > window.innerWidth) menu.style.left = Math.max(4, window.innerWidth - r.width - 8) + 'px';
            if (r.bottom > window.innerHeight) menu.style.top = Math.max(4, window.innerHeight - r.height - 8) + 'px';
        });
        setTimeout(() => document.addEventListener('click', closeToolMenu, { once: true }), 0);
        document.addEventListener('keydown', _dtMenuEsc);
    }

    // container: elemento su cui intercettare il tasto destro (l'intero pannello/grafico).
    //
    // Listener in fase di CAPTURE (non bubble): se un tool è già attivo ma il click cade
    // dove non c'è nessuna riga, il listener del tool stesso (canvas sopra, target
    // dell'evento) lo interpreta come "annulla/disattiva" — in fase di bubble lo stato
    // sarebbe già cambiato PRIMA che questo listener lo leggesse (falso negativo:
    // isAnyToolActive() tornerebbe false e il menu apparirebbe comunque). In capture si
    // legge lo stato PRIMA che il listener del tool (in fase target/bubble) lo tocchi.
    const ICON_TREND = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 28" width="15" height="15" style="flex-shrink:0"><path d="M5 22 L23 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/><circle cx="5" cy="22" r="2.5" fill="currentColor"/><circle cx="23" cy="6" r="2.5" fill="currentColor"/></svg>';
    const ICON_HLINE = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 28" width="15" height="15" fill="currentColor" style="flex-shrink:0"><rect x="2" y="13" width="7" height="2" rx="1"/><circle cx="13" cy="14" r="3"/><rect x="19" y="13" width="7" height="2" rx="1"/></svg>';
    const ICON_RANGE = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 28" width="15" height="15" style="flex-shrink:0"><g fill="currentColor"><path fill-rule="nonzero" d="M4 5h16.5v-1h-16.5zM25 24h-16.5v1h16.5z"></path><path fill-rule="nonzero" d="M6.5 26c.828 0 1.5-.672 1.5-1.5s-.672-1.5-1.5-1.5-1.5.672-1.5 1.5.672 1.5 1.5 1.5zm0 1c-1.381 0-2.5-1.119-2.5-2.5s1.119-2.5 2.5-2.5 2.5 1.119 2.5 2.5-1.119 2.5-2.5 2.5zM22.5 6c.828 0 1.5-.672 1.5-1.5s-.672-1.5-1.5-1.5-1.5.672-1.5 1.5.672 1.5 1.5 1.5zm0 1c-1.381 0-2.5-1.119-2.5-2.5z"></path><path fill-rule="nonzero" d="M14 9v14h1v-14z"></path><path d="M14.5 6l2.5 3h-5z"></path></g></svg>';
    const ICON_CLEAR = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="15" height="15" style="flex-shrink:0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m7 21-4.3-4.3c-1-1-1-2.5 0-3.4l9.6-9.6c1-1 2.5-1 3.4 0l5.6 5.6c1 1 1 2.5 0 3.4L13 21"/><path d="M22 21H7"/><path d="m5 11 9 9"/></svg>';

    // opts: { isAnyToolActive(): bool, onTrend, onHline, onRange, onClear?, hasDrawings()?: bool,
    //         labels?: {trend,hline,range,clear} }
    // "Cancella disegni" compare solo se hasDrawings() torna true (se omesso, voce sempre visibile).
    function attachToolContextMenu(container, opts) {
        if (!container || !opts) return;
        container.addEventListener('contextmenu', e => {
            if (opts.isAnyToolActive && opts.isAnyToolActive()) return;
            e.preventDefault();
            const tt = (k, fb) => (window.t ? window.t(k) : '') || fb;
            const showClear = opts.onClear && (!opts.hasDrawings || opts.hasDrawings());
            showToolMenu(e.clientX, e.clientY, [
                { label: (opts.labels && opts.labels.trend) || tt('trend_tool', 'Trendline'), icon: ICON_TREND, onClick: opts.onTrend },
                { label: (opts.labels && opts.labels.hline) || tt('hline_tool', 'Linea orizzontale'), icon: ICON_HLINE, onClick: opts.onHline },
                { label: (opts.labels && opts.labels.range) || tt('price_range', 'Range prezzo'), icon: ICON_RANGE, onClick: opts.onRange },
                { label: (opts.labels && opts.labels.clear) || tt('clear_drawings', 'Cancella disegni'), icon: ICON_CLEAR, onClick: showClear ? opts.onClear : null },
            ]);
        }, true);
        // Equivalente touch del tasto destro: pressione prolungata su un punto vuoto del
        // grafico (nessun tool attivo) apre lo stesso menu. Non si arma se un tool è già
        // attivo — quel gesto è già interpretato come "piazza/trascina" (vedi wireLongPress).
        wireLongPress(container, {
            isBlocked: () => opts.isAnyToolActive && opts.isAnyToolActive(),
            onLongPress: (x, y) => fireContextMenu(container, x, y),
        });
    }

    window.DrawTools = {
        TREND_HIT, TREND_CLICK_SLOP, HLINE_HIT,
        trendSync, trendDrawExt, ptSegDist, trendSnapPoint, trendPickPoint,
        getPaneRect, resolvePaneAtY, trendPickPointForSeries, trendPickAtClient, trendPickAtPane,
        trendSeriesForPane: _trendSeriesForPane, trendCanvasY: _trendCanvasY,
        drawRangeCanvas, drawRangeLine,
        toggleRange,
        hlineNearest, toggleHline, clearHlines,
        toggleFsTrend, trendDrawAll, trendEnsureRAF,
        showToolMenu, closeToolMenu, attachToolContextMenu,
        wireTouch, unwireTouch, wireLongPress, fireContextMenu,
    };
})();
