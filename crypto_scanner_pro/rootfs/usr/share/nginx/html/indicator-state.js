// Stato ON/OFF degli indicatori (Canale SMA20, Order Book Levels, Trend Band, GRaB,
// SMC/ICT) — chiave localStorage UNICA condivisa su chart.html/mtf.html/trade.html
// (prima ogni pagina teneva il proprio flag per-pagina: attivare un indicatore su una
// pagina non lo attivava sulle altre, né nel fullscreen di chart.html che non lo
// applicava affatto). Stile/parametri restano nei rispettivi cfg già condivisi
// (chart_channel_cfg, chart_tb_cfg, chart_grab_cfg, chart_smc_cfg): qui si tocca solo
// l'ON/OFF.
(function () {
    const DEFS = {
        channel:  { key: 'global_channel_active',   legacy: ['mtf_channel_active', 'ob_ch_active'] },
        obLevels: { key: 'global_ob_levels_active',  legacy: ['mtf_ob_levels_active', 'ob_lines_active'] },
        tb:       { key: 'global_tb_active',         legacy: ['mtf_tb_active', 'ob_tb_active'] },
        grab:     { key: 'global_grab_active',       legacy: ['mtf_grab_active', 'ob_grab_active'] },
        smc:      { key: 'global_smc_active',        legacy: ['chart_smc_active', 'mtf_smc_active', 'ob_smc_active'] },
        bb:       { key: 'global_bb_active',         legacy: ['mtf_bb_active', 'ob_bb_active'] },
    };

    // Migrazione one-time: se la chiave unica non esiste ancora, eredita '1' da
    // qualunque vecchio flag per-pagina fosse acceso.
    try {
        for (const name in DEFS) {
            const d = DEFS[name];
            if (localStorage.getItem(d.key) === null) {
                if (d.legacy.some(k => localStorage.getItem(k) === '1')) localStorage.setItem(d.key, '1');
            }
        }
    } catch (e) {}

    function getIndActive(name) {
        const d = DEFS[name];
        if (!d) return false;
        try { return localStorage.getItem(d.key) === '1'; } catch (e) { return false; }
    }
    function setIndActive(name, val) {
        const d = DEFS[name];
        if (!d) return;
        try { localStorage.setItem(d.key, val ? '1' : '0'); } catch (e) {}
    }

    window.getIndActive = getIndActive;
    window.setIndActive = setIndActive;
})();
