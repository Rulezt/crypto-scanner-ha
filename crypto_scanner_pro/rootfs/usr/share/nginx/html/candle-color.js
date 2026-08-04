// Colora candele: toggle GLOBALE unico condiviso su tutte le pagine con grafici a
// candele (stessa chiave localStorage ovunque — non più una per pagina). Ogni pagina,
// subito dopo aver creato/ricaricato una CandlestickSeries, chiama
// window.applyCandleColorStyle(series) per applicare lo stile corrente (corpo
// semi-trasparente, contorno/stoppino bianco quando attivo).
(function () {
    const KEY = 'global_candle_color_active';
    const NORMAL_STYLE = { upColor: '#20B26C', downColor: '#EF454A', borderVisible: false, wickUpColor: '#20B26C', wickDownColor: '#EF454A' };
    const COLOR_STYLE = { upColor: 'rgba(255,255,255,0.12)', downColor: 'rgba(255,255,255,0.12)', borderVisible: true, wickUpColor: '#ffffff', wickDownColor: '#ffffff' };

    // Migrazione one-time dai vecchi toggle per-pagina (chart/mtf/ob) alla chiave unica.
    try {
        if (localStorage.getItem(KEY) === null) {
            const legacy = ['chart_candle_color_active', 'mtf_candle_color_active', 'ob_candle_color_active'];
            if (legacy.some(k => localStorage.getItem(k) === '1')) localStorage.setItem(KEY, '1');
        }
    } catch (e) {}

    function isCandleColorActive() {
        try { return localStorage.getItem(KEY) === '1'; } catch (e) { return false; }
    }
    function applyCandleColorStyle(series) {
        if (!series) return;
        try { series.applyOptions(isCandleColorActive() ? COLOR_STYLE : NORMAL_STYLE); } catch (e) {}
    }
    // `onToggle` (opzionale) riceve il nuovo stato per far ridisegnare la pagina chiamante
    // (griglia + fullscreen, o qualunque altro contesto locale) subito dopo lo switch.
    function toggleCandleColorGlobal(onToggle) {
        const next = !isCandleColorActive();
        try { localStorage.setItem(KEY, next ? '1' : '0'); } catch (e) {}
        if (typeof onToggle === 'function') onToggle(next);
        return next;
    }

    window.isCandleColorActive = isCandleColorActive;
    window.applyCandleColorStyle = applyCandleColorStyle;
    window.toggleCandleColorGlobal = toggleCandleColorGlobal;
})();
