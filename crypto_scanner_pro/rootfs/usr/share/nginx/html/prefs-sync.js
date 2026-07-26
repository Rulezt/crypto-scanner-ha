/* Sincronizza le impostazioni (scanner/indicatori/preferenze) per utente loggato.
   Ospiti: nessuna modifica al comportamento, tutto resta locale come oggi. */
(function () {
  var FIXED_KEYS = [
    'csp_lang', 'chart_source', 'chart_candles_per_tf', 'ibkr_symbols',
    'chart_ema_cfg', 'chart_levels_cfg', 'chart_bb_cfg', 'chart_channel_cfg', 'chart_grab_cfg',
    'bot_cfg_draft', 'bot_channel_visibility', 'ob_book_w_pct',
    'ath_atl_cfg', 'chb_cfg', 'bbsq_cfg', 'e223f_cfg', 'ico_cfg',
    'midbrk_cfg', 'grb_cfg', 'rvol_cfg', 'vinef_cfg', 'third_touch_cfg', 'ema60_cfg', 'trdln_cfg',
    'confl_cfg', 'mtf_drawings'
  ];
  var SUFFIX_MATCH = '_columns_order';

  function isKnownKey(k) {
    if (!k) return false;
    if (FIXED_KEYS.indexOf(k) !== -1) return true;
    return k.indexOf(SUFFIX_MATCH) !== -1;
  }

  // 1. Stato login + prefs correnti, in un'unica chiamata sincrona (bloccante:
  //    deve completare prima che qualsiasi altro <script> della pagina legga localStorage).
  var xhr = new XMLHttpRequest();
  var data = null;
  try {
    xhr.open('GET', '/api/prefs', false);
    xhr.send(null);
    if (xhr.status === 200) data = JSON.parse(xhr.responseText);
  } catch (e) { /* rete/server irraggiungibile: fail-open, nessuna modifica */ }

  if (!data || !data.logged_in) return; // ospite o errore: comportamento invariato

  var serverPrefs = data.prefs || {};

  // 2. Il valore del server vince su quello locale.
  for (var k in serverPrefs) {
    if (Object.prototype.hasOwnProperty.call(serverPrefs, k)) {
      try { window.localStorage.setItem(k, serverPrefs[k]); } catch (e) {}
    }
  }

  // 3. Seed-upload: chiavi note presenti solo in locale vengono caricate una volta sull'account.
  try {
    var toSeed = [];
    for (var i = 0; i < window.localStorage.length; i++) {
      var lk = window.localStorage.key(i);
      if (isKnownKey(lk) && !Object.prototype.hasOwnProperty.call(serverPrefs, lk)) {
        toSeed.push(lk);
      }
    }
    toSeed.forEach(function (lk) {
      pushToServer(lk, window.localStorage.getItem(lk));
    });
  } catch (e) {}

  // 4. Ogni scrittura futura su una chiave nota viene specchiata sul server (debounce 400ms).
  var _origSetItem = window.localStorage.setItem.bind(window.localStorage);
  var _origRemoveItem = window.localStorage.removeItem.bind(window.localStorage);
  var _timers = {};
  var _pending = {}; // pkey -> {value, deleted} in attesa di essere inviata (per il flush a pagehide)

  window.localStorage.setItem = function (key, value) {
    _origSetItem(key, value);
    if (isKnownKey(key)) scheduleSync(key, value, false);
  };
  window.localStorage.removeItem = function (key) {
    _origRemoveItem(key);
    if (isKnownKey(key)) scheduleSync(key, null, true);
  };

  function scheduleSync(key, value, deleted) {
    _pending[key] = { value: value, deleted: deleted };
    if (_timers[key]) clearTimeout(_timers[key]);
    _timers[key] = setTimeout(function () {
      delete _timers[key];
      var item = _pending[key];
      delete _pending[key];
      if (!item) return;
      if (item.deleted) deleteFromServer(key); else pushToServer(key, item.value);
    }, 400);
  }

  function pushToServer(key, value) {
    fetch('/api/prefs/' + encodeURIComponent(key), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: value }),
      credentials: 'same-origin'
    }).catch(function () {});
  }
  function deleteFromServer(key) {
    fetch('/api/prefs/' + encodeURIComponent(key), {
      method: 'DELETE', credentials: 'same-origin'
    }).catch(function () {});
  }

  // 5. Alla chiusura/navigazione fuori dalla pagina, flush immediato di eventuali
  //    scritture ancora in debounce, cosi' non si perdono modifiche fatte negli ultimi 400ms.
  document.addEventListener('pagehide', function () {
    var items = [];
    for (var key in _pending) {
      if (!Object.prototype.hasOwnProperty.call(_pending, key)) continue;
      var item = _pending[key];
      items.push({ pkey: key, value: item.value, deleted: !!item.deleted });
      if (_timers[key]) { clearTimeout(_timers[key]); delete _timers[key]; }
    }
    if (!items.length) return;
    try {
      var blob = new Blob([JSON.stringify(items)], { type: 'application/json' });
      navigator.sendBeacon('/api/prefs/flush', blob);
    } catch (e) {}
  });
})();
