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
        if (s._rangeHoverML) { s.rangeCanvas.removeEventListener('mouseleave', s._rangeHoverML); s._rangeHoverML = null; }
        if (s._rangeHoverLine) { try { s.candleS.removePriceLine(s._rangeHoverLine); } catch(e) {} s._rangeHoverLine = null; }
        s.rangeP1 = null;
        try { const rc = s.rangeCanvas.getContext('2d'); rc.clearRect(0,0,s.rangeCanvas.width,s.rangeCanvas.height); } catch(e) {}
        s.rangeCanvas.style.pointerEvents = s.rangeActive ? 'auto' : 'none';
        s.rangeCanvas.style.cursor = s.rangeActive ? 'crosshair' : '';
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
        if (s._hlineHoverML) { s.hlineCanvas.removeEventListener('mouseleave', s._hlineHoverML); s._hlineHoverML = null; }
        if (s._hlineHoverLine) { try { s.candleS.removePriceLine(s._hlineHoverLine); } catch(e) {} s._hlineHoverLine = null; }
        s._hlineDragging = null;
        s.hlineCanvas.style.pointerEvents = s.hlineActive ? 'auto' : 'none';
        s.hlineCanvas.style.cursor = s.hlineActive ? 'crosshair' : '';
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
    }

    function clearHlines(s, opts = {}) {
        for (const hl of s.hlines) { try { s.candleS.removePriceLine(hl.priceLine); } catch(e) {} }
        s.hlines = [];
        opts.onChange?.();
    }

    // ── Trendline — SOLO fullscreen (i pannelli in griglia restano non unificati) ────
    function trendDrawAll(s) {
        s.syncClearBtn?.();
        const canvas = s.trendCanvas; if (!canvas || !s.candleS || !s.chart) return;
        trendSync(canvas);
        const ctx = canvas.getContext('2d'), W = canvas.width, H = canvas.height;
        ctx.clearRect(0, 0, W, H);
        for (const tl of s.trendlines) {
            try {
                const x1 = s.chart.timeScale().timeToCoordinate(tl.t1), y1 = s.candleS.priceToCoordinate(tl.p1);
                const x2 = s.chart.timeScale().timeToCoordinate(tl.t2), y2 = s.candleS.priceToCoordinate(tl.p2);
                if (x1==null||y1==null||x2==null||y2==null) continue;
                trendDrawExt(ctx, x1, y1, x2, y2, W, H, '#22c55e', 1.5);
                ctx.fillStyle = '#22c55e';
                ctx.beginPath(); ctx.arc(x1, y1, 4, 0, Math.PI*2); ctx.fill();
                ctx.beginPath(); ctx.arc(x2, y2, 4, 0, Math.PI*2); ctx.fill();
            } catch(ex) {}
        }
        if (s._trendP1 && s._trendPrev) {
            try {
                const x1 = s.chart.timeScale().timeToCoordinate(s._trendP1.t), y1 = s.candleS.priceToCoordinate(s._trendP1.p);
                if (x1!=null && y1!=null) {
                    ctx.strokeStyle = '#22c55e'; ctx.lineWidth = 1; ctx.setLineDash([5,3]);
                    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(s._trendPrev.x, s._trendPrev.y); ctx.stroke();
                    ctx.setLineDash([]);
                    ctx.fillStyle = '#22c55e'; ctx.beginPath(); ctx.arc(x1, y1, 4, 0, Math.PI*2); ctx.fill();
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
                const x1 = s.chart.timeScale().timeToCoordinate(tl.t1), y1 = s.candleS.priceToCoordinate(tl.p1);
                const x2 = s.chart.timeScale().timeToCoordinate(tl.t2), y2 = s.candleS.priceToCoordinate(tl.p2);
                if (x1==null||y1==null||x2==null||y2==null) continue;
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
        s._trendP1 = null; s._trendPrev = null; s._trendDrag = null; s._trendPending = null;
        s.trendCanvas.style.pointerEvents = s.trendActive ? 'auto' : 'none';
        s.trendCanvas.style.cursor = s.trendActive ? 'crosshair' : '';
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
                const t = s.chart.timeScale().coordinateToTime(px), p = s.candleS.coordinateToPrice(py);
                if (t != null && p != null) s._trendP1 = { t, p };
            } else {
                const t2 = s.chart.timeScale().coordinateToTime(px), p2 = s.candleS.coordinateToPrice(py);
                if (t2 != null && p2 != null) s.trendlines.push({ t1: s._trendP1.t, p1: s._trendP1.p, t2, p2 });
                s._trendP1 = null; s._trendPrev = null;
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
                    const ox1 = s.chart.timeScale().timeToCoordinate(hit.tl.t1), oy1 = s.candleS.priceToCoordinate(hit.tl.p1);
                    const ox2 = s.chart.timeScale().timeToCoordinate(hit.tl.t2), oy2 = s.candleS.priceToCoordinate(hit.tl.p2);
                    s._trendDrag = { tl: hit.tl, part: 'line', sx: dpx0, sy: dpy0, ox1, oy1, ox2, oy2 };
                    canvas.style.cursor = 'move';
                }
                s._trendPending = null;
            }
            if (s._trendDrag) {
                if (!(e.buttons & 1)) { s._trendDrag = null; return; }
                const { tl, part } = s._trendDrag;
                if (part === 'p1') { const t=s.chart.timeScale().coordinateToTime(px),p=s.candleS.coordinateToPrice(py); if(t&&p){tl.t1=t;tl.p1=p;} }
                else if (part === 'p2') { const t=s.chart.timeScale().coordinateToTime(px),p=s.candleS.coordinateToPrice(py); if(t&&p){tl.t2=t;tl.p2=p;} }
                else { const dpx=px-s._trendDrag.sx, dpy=py-s._trendDrag.sy; const nt1=s.chart.timeScale().coordinateToTime(s._trendDrag.ox1+dpx), np1=s.candleS.coordinateToPrice(s._trendDrag.oy1+dpy); const nt2=s.chart.timeScale().coordinateToTime(s._trendDrag.ox2+dpx), np2=s.candleS.coordinateToPrice(s._trendDrag.oy2+dpy); if(nt1&&np1&&nt2&&np2){tl.t1=nt1;tl.p1=np1;tl.t2=nt2;tl.p2=np2;} }
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
                const { px: cpx, py: cpy } = s._trendPending;
                const t = s.chart.timeScale().coordinateToTime(cpx), p = s.candleS.coordinateToPrice(cpy);
                if (t != null && p != null) s._trendP1 = { t, p };
                s._trendPending = null;
                return;
            }
            s._trendDrag = null; canvas.style.cursor = 'crosshair';
        };
        s._trendCM = e => {
            e.preventDefault();
            if (s._trendP1) { s._trendP1 = null; s._trendPrev = null; trendDrawAll(s); return; }
            const hit = trendNearest(s, e.clientX, e.clientY);
            if (hit) { s.trendlines = s.trendlines.filter(tl => tl !== hit.tl); trendDrawAll(s); }
            else toggleFsTrend(s, opts);
        };
        canvas.addEventListener('mousedown', s._trendMD);
        document.addEventListener('mousemove', s._trendMM);
        document.addEventListener('mouseup', s._trendMU);
        canvas.addEventListener('contextmenu', s._trendCM);
    }

    window.DrawTools = {
        TREND_HIT, TREND_CLICK_SLOP, HLINE_HIT,
        trendSync, trendDrawExt, ptSegDist,
        drawRangeCanvas, drawRangeLine,
        toggleRange,
        hlineNearest, toggleHline, clearHlines,
        toggleFsTrend, trendDrawAll,
    };
})();
