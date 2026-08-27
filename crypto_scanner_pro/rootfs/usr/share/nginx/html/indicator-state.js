// Stato ON/OFF degli indicatori (Canale SMA20, Order Book Levels, GRaB, ROC, BB,
// EMA personalizzate, …) — PER-PAGINA e indipendente.
//
// Storia: prima ogni pagina aveva il proprio flag; poi si era passati a una chiave
// `global_*` unica condivisa fra chart/mtf/trade (attivare un indicatore su una pagina
// lo attivava OVUNQUE). Su richiesta esplicita si torna all'indipendenza per pagina:
// la chiave localStorage è prefissata dal nome della pagina. Stile/parametri restano
// nei rispettivi cfg condivisi (chart_channel_cfg, chart_grab_cfg, …): qui solo l'ON/OFF.
(function () {
    // Nome pagina da location.pathname. Le pagine con indicatori sono chart / mtf /
    // trade. trade.html è servito sia da /trade che da /orderbook → entrambe mappano
    // su 'trade' così lo stato non si sdoppia. Il fullscreen / modal OB di una pagina
    // condividono lo stesso pathname → stesso stato (voluto).
    var seg = ((location.pathname || '/').split('/').filter(Boolean)[0] || 'index')
        .replace(/\.html?$/i, '').toLowerCase();
    var PAGE = (seg === 'orderbook') ? 'trade' : (seg || 'index');

    // base -> nome logico usato da getIndActive/setIndActive
    var BASES = {
        channel:     'channel_active',
        obLevels:    'ob_levels_active',
        lbLevels:    'lb_levels_active',
        grab:        'grab_active',
        bb:          'bb_active',
        roc:         'roc_active',
        grabMidline: 'grab_midline_active',
        hideIndMini: 'hide_ind_mini_active',
        emaCustom:   'ema_custom_active',
        emaCustom2:  'ema_custom2_active',
        // EMA base 5/10/60/223: on/off ora per-pagina (stile/colore restano in chart_ema_cfg)
        ema5:   'ema5_active',
        ema10:  'ema10_active',
        ema60:  'ema60_active',
        ema223: 'ema223_active',
    };

    function keyFor(name) { return PAGE + '__ind_' + BASES[name]; }

    // Migrazione one-time PER PAGINA: se la chiave per-pagina non esiste ancora, eredita
    // UNA VOLTA dal valore condiviso `global_*` (così ogni pagina parte da com'era prima,
    // poi diverge). Nessun altro fallback: le vecchissime chiavi per-pagina (mtf_bb_active,
    // ob_bb_active, …) NON vengono più lette — creavano contaminazione incrociata perché
    // il loro nome coincideva col prefisso di un'altra pagina.
    try {
        for (var name in BASES) {
            var k = keyFor(name);
            if (localStorage.getItem(k) !== null) continue;
            var g = localStorage.getItem('global_' + BASES[name]);
            if (g !== null) localStorage.setItem(k, g === '1' ? '1' : '0');
        }
    } catch (e) {}

    // EMA 5/10/60/223: seed one-time dal vecchio flag CONDIVISO chart_ema_cfg[i].enabled
    // (default true) — così l'utente mantiene lo stato attuale, poi ogni pagina diverge.
    try {
        var emaP = [5, 10, 60, 223];
        var emaCfg = JSON.parse(localStorage.getItem('chart_ema_cfg'));
        for (var i = 0; i < emaP.length; i++) {
            var ek = keyFor('ema' + emaP[i]);
            if (localStorage.getItem(ek) !== null) continue;
            var en = (emaCfg && emaCfg[i]) ? (emaCfg[i].enabled !== false) : true;
            localStorage.setItem(ek, en ? '1' : '0');
        }
    } catch (e) {}

    function getIndActive(name) {
        if (!BASES[name]) return false;
        try { return localStorage.getItem(keyFor(name)) === '1'; } catch (e) { return false; }
    }
    function setIndActive(name, val) {
        if (!BASES[name]) return;
        try { localStorage.setItem(keyFor(name), val ? '1' : '0'); } catch (e) {}
    }

    window.getIndActive = getIndActive;
    window.setIndActive = setIndActive;
})();
