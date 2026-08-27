// candle-feed.js — sorgente UNICA delle candele per tutte le pagine con grafici.
//
// Perché esiste: prima ogni pagina (chart/mtf/trade/bot) aveva la sua implementazione
// separata di "scarica storico + segui il WS kline + apri la barra nuova", con fix
// divergenti. Ne derivavano candele che si congelavano/sparivano e candele rosse mentre
// il prezzo saliva. Cause:
//   - roll sintetico basato sull'orologio LOCALE del client: se avanti di pochi secondi
//     apriva una barra a un timestamp futuro rispetto all'exchange, poi scartava come
//     "stale" ogni kline reale successiva → grafico congelato sulla barra fantasma;
//   - la barra in formazione prendeva open dai trade ma close dal MID del book
//     (bid+ask)/2 → in salita il mid sta sotto il prezzo dei trade → barra rossa.
//
// Questo modulo: SOLO `api/klines` (storico) + UN WebSocket `kline.<tf>.<symbol>`
// multiplexato per pagina. Niente book, niente poll REST, niente Date.now() per i
// boundary, niente roll sintetico. La barra si muove esclusivamente ai messaggi kline
// di Bybit; a mercato fermo la barra nuova compare al primo tick (~1s di ritardo max).
//
// API:
//   const feed = window.CandleFeed.open({
//     symbol, tf,
//     onReady(bars, { reason }),                     // reason: 'history' | 'resync'
//     onUpdate(bar, { bars, confirmed, isNewBar }),
//   });
//   feed.bars                 // array canonico (tempo già shiftato TZ, non decrescente)
//   feed.lastPrice
//   feed.setSymbolTF(symbol, tf)
//   feed.resync()
//   feed.close()
//
//   await window.CandleFeed.history(symbol, tf)  ->  { bars, tzOffsetS }
(function () {
    'use strict';

    var WS_URL = 'wss://stream.bybit.com/v5/public/linear';

    // ── Hub WebSocket condiviso (una connessione per pagina) ────────────────────
    var hub = {
        sock: null,
        connected: false,
        subs: Object.create(null),   // topic -> Set<fn(barMsg)>
        reconnectCbs: new Set(),      // fn() chiamate ad ogni (ri)connessione riuscita
        _reconnTimer: null,
        _pingTimer: null,
        _everConnected: false,
    };

    function hubEnsure() {
        if (hub.sock &&
            (hub.sock.readyState === WebSocket.OPEN || hub.sock.readyState === WebSocket.CONNECTING)) {
            return;
        }
        var sock;
        try { sock = new WebSocket(WS_URL); }
        catch (e) { hubScheduleReconnect(); return; }
        hub.sock = sock;

        sock.onopen = function () {
            hub.connected = true;
            var topics = Object.keys(hub.subs);
            if (topics.length) {
                try { sock.send(JSON.stringify({ op: 'subscribe', args: topics })); } catch (e) {}
            }
            clearInterval(hub._pingTimer);
            hub._pingTimer = setInterval(function () {
                if (sock.readyState === WebSocket.OPEN) {
                    try { sock.send('{"op":"ping"}'); } catch (e) {}
                }
            }, 20000);
            // Alla RIconnessione (non alla prima) i feed devono risincronizzare: durante
            // il buco possono essersi chiuse una o più barre mai ricevute.
            if (hub._everConnected) {
                hub.reconnectCbs.forEach(function (fn) { try { fn(); } catch (e) {} });
            }
            hub._everConnected = true;
        };

        sock.onmessage = function (ev) {
            var msg;
            try { msg = JSON.parse(ev.data); } catch (e) { return; }
            if (!msg || !msg.topic || !msg.data) return;
            var set = hub.subs[msg.topic];
            if (!set || !set.size) return;
            set.forEach(function (fn) { try { fn(msg); } catch (e) {} });
        };

        sock.onerror = function () { /* onclose fa il resto */ };

        sock.onclose = function () {
            hub.connected = false;
            clearInterval(hub._pingTimer); hub._pingTimer = null;
            hubScheduleReconnect();
        };
    }

    function hubScheduleReconnect() {
        if (hub._reconnTimer) return;
        hub._reconnTimer = setTimeout(function () {
            hub._reconnTimer = null;
            if (Object.keys(hub.subs).length) hubEnsure();
        }, 4000);
    }

    function hubSubscribe(topic, fn) {
        var set = hub.subs[topic];
        var isNew = !set;
        if (isNew) { set = hub.subs[topic] = new Set(); }
        set.add(fn);
        hubEnsure();
        if (isNew && hub.connected && hub.sock && hub.sock.readyState === WebSocket.OPEN) {
            try { hub.sock.send(JSON.stringify({ op: 'subscribe', args: [topic] })); } catch (e) {}
        }
    }

    function hubUnsubscribe(topic, fn) {
        var set = hub.subs[topic];
        if (!set) return;
        set.delete(fn);
        if (!set.size) {
            delete hub.subs[topic];
            if (hub.connected && hub.sock && hub.sock.readyState === WebSocket.OPEN) {
                try { hub.sock.send(JSON.stringify({ op: 'unsubscribe', args: [topic] })); } catch (e) {}
            }
        }
    }

    // ── Fetch storico (unico punto) ────────────────────────────────────────────
    function fetchHistory(symbol, tf) {
        return fetch('api/klines?symbol=' + encodeURIComponent(symbol) + '&interval=' + encodeURIComponent(tf))
            .then(function (r) { return r.json(); })
            .then(function (j) {
                if (!j || !j.success || !Array.isArray(j.data)) throw new Error('klines fetch failed');
                var tz = j.utc_offset_s || 0;
                // difesa: righe corrotte / non ordinate / non monotòne
                var bars = [];
                var lastT = -Infinity;
                for (var i = 0; i < j.data.length; i++) {
                    var k = j.data[i];
                    if (!k || !isFinite(k.open) || !isFinite(k.close) ||
                        !isFinite(k.high) || !isFinite(k.low) || k.high < k.low) continue;
                    if (k.time <= lastT) continue;
                    lastT = k.time;
                    bars.push({ time: k.time, open: +k.open, high: +k.high,
                                low: +k.low, close: +k.close, volume: +(k.volume || 0) });
                }
                return { bars: bars, tzOffsetS: tz };
            });
    }

    // ── Feed ───────────────────────────────────────────────────────────────────
    function Feed(opts) {
        this.symbol = opts.symbol;
        this.tf = String(opts.tf);
        this.onReady = typeof opts.onReady === 'function' ? opts.onReady : function () {};
        this.onUpdate = typeof opts.onUpdate === 'function' ? opts.onUpdate : function () {};
        this.bars = [];
        this.lastPrice = null;
        this.tzOffsetS = 0;
        this._seq = 0;
        this._topic = null;
        this._closed = false;
        this._onMsg = this._handleMsg.bind(this);
        this._onReconnect = this.resync.bind(this);

        hub.reconnectCbs.add(this._onReconnect);
        this._subscribe();
        this._loadHistory('history');

        if (!Feed._visBound) {
            Feed._visBound = true;
            Feed._hiddenAt = 0;
            document.addEventListener('visibilitychange', function () {
                if (document.hidden) { Feed._hiddenAt = Date.now(); return; }
                if (Feed._hiddenAt && Date.now() - Feed._hiddenAt > 20000) {
                    Feed._all.forEach(function (f) { if (!f._closed) f.resync(); });
                }
                Feed._hiddenAt = 0;
            });
        }
        Feed._all.add(this);
    }
    Feed._all = new Set();

    Feed.prototype._topicFor = function () {
        return 'kline.' + this.tf + '.' + this.symbol;
    };

    Feed.prototype._subscribe = function () {
        var t = this._topicFor();
        if (this._topic === t) return;
        if (this._topic) hubUnsubscribe(this._topic, this._onMsg);
        this._topic = t;
        hubSubscribe(t, this._onMsg);
    };

    Feed.prototype._loadHistory = function (reason) {
        var self = this;
        var mySeq = ++this._seq;
        fetchHistory(this.symbol, this.tf).then(function (res) {
            if (self._closed || mySeq !== self._seq) return;   // superata da un setSymbolTF/resync
            self.bars = res.bars;
            self.tzOffsetS = res.tzOffsetS;
            self.lastPrice = res.bars.length ? res.bars[res.bars.length - 1].close : null;
            try { self.onReady(self.bars, { reason: reason }); } catch (e) {}
        }).catch(function () {
            if (self._closed || mySeq !== self._seq) return;
            // riprova una volta a breve: senza storico il feed è inutile
            setTimeout(function () { if (!self._closed && mySeq === self._seq) self._loadHistory(reason); }, 3000);
        });
    };

    Feed.prototype._handleMsg = function (msg) {
        if (this._closed) return;
        if (!msg.topic || msg.topic !== this._topic) return;   // vecchio TF/symbol in volo
        var b = msg.data && msg.data[0];
        if (!b) return;
        var o = parseFloat(b.open), h = parseFloat(b.high), l = parseFloat(b.low), c = parseFloat(b.close);
        if (!isFinite(o) || !isFinite(h) || !isFinite(l) || !isFinite(c)) return;

        var bar = {
            time: Math.floor(parseInt(b.start, 10) / 1000) + this.tzOffsetS,
            open: o, high: h, low: l, close: c,
            volume: parseFloat(b.volume) || 0,
        };
        var confirmed = b.confirm === true || b.confirm === 'true';

        // Prima del primo storico: ignora (evita di seminare una serie da un solo tick).
        if (!this.bars.length) return;

        var last = this.bars[this.bars.length - 1];
        var isNewBar = false;
        if (bar.time < last.time) {
            return;                       // stale / fuori ordine — scartato in un solo punto
        } else if (bar.time === last.time) {
            this.bars[this.bars.length - 1] = bar;
        } else {
            this.bars.push(bar);
            isNewBar = true;
        }
        this.lastPrice = bar.close;

        try {
            this.onUpdate(bar, { bars: this.bars, confirmed: confirmed, isNewBar: isNewBar });
        } catch (e) {}
    };

    Feed.prototype.setSymbolTF = function (symbol, tf) {
        if (this._closed) return;
        tf = String(tf);
        if (symbol === this.symbol && tf === this.tf) return;
        this.symbol = symbol;
        this.tf = tf;
        this.bars = [];
        this.lastPrice = null;
        this._subscribe();
        this._loadHistory('history');
    };

    Feed.prototype.resync = function () {
        if (this._closed) return;
        this._loadHistory('resync');
    };

    Feed.prototype.close = function () {
        if (this._closed) return;
        this._closed = true;
        if (this._topic) hubUnsubscribe(this._topic, this._onMsg);
        this._topic = null;
        hub.reconnectCbs.delete(this._onReconnect);
        Feed._all.delete(this);
        this.bars = [];
    };

    window.CandleFeed = {
        open: function (opts) { return new Feed(opts); },
        history: fetchHistory,
    };
})();
