"""Journal — trade MANUALI (quelli aperti dal BOT sono esclusi di proposito,
vedi scanners/bot_engine.BOT_TRADES_LEDGER_FILE).

Fonte dati: /v5/position/closed-pnl di Bybit — è l'exchange stesso a calcolare
PnL/fee reali per ogni posizione chiusa, quindi non c'è bisogno di intercettare
e registrare a mano ogni entrata/uscita lato app (che dovrebbe farlo in più
punti: bot, ordine manuale da trade.html, chiusura manuale, SL/TP nativi
Bybit...). Persistito in locale (JOURNAL_FILE) sia per superare il retention
storico di Bybit sia per calcolare le statistiche senza rifare fetch completi.

Esclusione trade BOT: Bybit non espone nessun ID che leghi il trade chiuso
all'ordine di apertura che lo ha generato (closed-pnl mostra solo l'ordine di
CHIUSURA, spesso il trigger SL/TP nativo, non un ordine nostro) — quindi si
riconcilia per simbolo + orario di chiusura contro il registro locale scritto
da bot_engine.py ad ogni sua uscita reale (tolleranza BOT_MATCH_TOLERANCE_S).
Trade già chiusi PRIMA che questo registro esistesse non possono essere
riconosciuti come BOT e compariranno come manuali.
"""
import bisect
import json
import os
import threading
import time
from collections import defaultdict
from urllib.parse import quote

import requests as rq

_BYB = 'https://api.bybit.com'
JOURNAL_FILE = '/data/journal_trades.json'
BOT_LEDGER_FILE = '/data/bot_trades_history.json'  # scritto da scanners/bot_engine.py
BOT_MATCH_TOLERANCE_S = 90
MIN_SYNC_INTERVAL_S = 20  # come /api/klines: evita di martellare Bybit ad ogni refresh UI

# /v5/position/closed-pnl SENZA startTime/endTime risponde solo con gli ultimi
# giorni (verificato empiricamente: senza questi parametri restituiva solo 16
# record, tutti nell'ultima settimana, mentre l'account aveva trade reali fino
# a ~7 settimane prima) — e comunque una singola richiesta non può coprire più
# di 7 giorni. Serve quindi camminare a ritroso a finestre esplicite.
WINDOW_S = 7 * 86400
FULL_BACKFILL_MAX_DAYS = 730  # ~2 anni: solo al primissimo sync (store vuoto)

_last_sync_ts = 0.0
# Flask gira threaded=True: /api/journal/stats e /api/journal/trades arrivano
# quasi sempre in coppia (fetch in parallelo dal frontend). Senza lock, la
# seconda richiesta concorrente durante il PRIMISSIMO sync (store ancora
# vuoto su disco) supera comunque il rate-limit (basato solo su un timestamp)
# e legge lo store a metà scrittura — risultato: stats vuote nella stessa
# risposta che accompagna una tabella trade piena. Il lock serializza le due
# chiamate così la seconda aspetta che il file sia stato scritto per intero.
_sync_lock = threading.Lock()


def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _fetch_window(bsign_fn, api_key, api_secret, start_ms, end_ms, store, max_pages=20):
    """Fetch completo (con cursor pagination interna) di UNA finestra <=7 giorni
    (limite Bybit), merge nello store. Una finestra reale ha quasi sempre una
    sola pagina — max_pages è solo una guardia contro giornate anomale con
    centinaia di fill."""
    cursor = None
    pages = 0
    while pages < max_pages:
        qs = f'category=linear&limit=100&startTime={start_ms}&endTime={end_ms}'
        if cursor:
            qs += f'&cursor={quote(cursor)}'
        try:
            d = rq.get(f'{_BYB}/v5/position/closed-pnl?{qs}',
                        headers=bsign_fn(api_key, api_secret, qs), timeout=8).json()
        except Exception:
            return
        if d.get('retCode') != 0:
            return
        res = d.get('result', {})
        lst = res.get('list', [])
        for rec in lst:
            oid = rec.get('orderId')
            if oid:
                store[oid] = rec
        pages += 1
        cursor = res.get('nextPageCursor') or None
        if not cursor or not lst:
            return


def sync(bsign_fn, api_key, api_secret, force=False):
    """Store vuoto (primissimo sync): backfill completo camminando a ritroso a
    finestre di WINDOW_S fino a FULL_BACKFILL_MAX_DAYS. Store già popolato:
    sync leggero che cammina in AVANTI dal trade noto più recente fino ad ora
    (copre correttamente anche un gap lungo, es. utente assente per settimane —
    non solo l'ultima finestra fissa). Rate-limited a MIN_SYNC_INTERVAL_S."""
    global _last_sync_ts
    with _sync_lock:
        now = time.time()
        if not force and (now - _last_sync_ts) < MIN_SYNC_INTERVAL_S:
            return _load_json(JOURNAL_FILE, {})
        _last_sync_ts = now

        store = _load_json(JOURNAL_FILE, {})
        now_ms = int(now * 1000)

        if not store:
            start_ms = now_ms - FULL_BACKFILL_MAX_DAYS * 86400 * 1000
        else:
            newest_ms = max(int(r.get('updatedTime', 0)) for r in store.values())
            start_ms = max(0, newest_ms - 60_000)  # piccolo margine di sovrapposizione

        cur = start_ms
        while cur < now_ms:
            chunk_end = min(cur + WINDOW_S * 1000, now_ms)
            _fetch_window(bsign_fn, api_key, api_secret, cur, chunk_end, store)
            cur = chunk_end

        _save_json(JOURNAL_FILE, store)
        return store


def _bot_ledger_by_symbol():
    ledger = _load_json(BOT_LEDGER_FILE, [])
    by_sym = defaultdict(list)
    for rec in ledger:
        sym = rec.get('symbol', '')
        t = rec.get('exit_time')
        if sym and t is not None:
            by_sym[sym].append(int(t))
    for sym in by_sym:
        by_sym[sym].sort()
    return by_sym


def _is_bot_trade(symbol, close_time_s, by_sym):
    times = by_sym.get(symbol)
    if not times:
        return False
    i = bisect.bisect_left(times, close_time_s)
    for j in (i - 1, i):
        if 0 <= j < len(times) and abs(times[j] - close_time_s) <= BOT_MATCH_TOLERANCE_S:
            return True
    return False


def _normalize(rec):
    open_fee = float(rec.get('openFee', 0) or 0)
    close_fee = float(rec.get('closeFee', 0) or 0)
    return {
        'order_id': rec.get('orderId', ''),
        'symbol': rec.get('symbol', ''),
        'side': rec.get('side', ''),
        'qty': float(rec.get('qty', 0) or 0),
        'leverage': rec.get('leverage', ''),
        'avg_entry': float(rec.get('avgEntryPrice', 0) or 0),
        'avg_exit': float(rec.get('avgExitPrice', 0) or 0),
        'entry_value': float(rec.get('cumEntryValue', 0) or 0),
        'exit_value': float(rec.get('cumExitValue', 0) or 0),
        'pnl': float(rec.get('closedPnl', 0) or 0),
        'fees': open_fee + close_fee,
        'fill_count': int(rec.get('fillCount', 0) or 0),
        'open_time': int(rec.get('createdTime', 0)) // 1000,
        'close_time': int(rec.get('updatedTime', 0)) // 1000,
    }


def manual_trades(symbol=None, since=None, until=None):
    """Trade chiusi, esclusi quelli riconciliati col registro BOT — vedi
    docstring modulo. Ordinati per orario di chiusura discendente."""
    store = _load_json(JOURNAL_FILE, {})
    by_sym = _bot_ledger_by_symbol()
    out = []
    for rec in store.values():
        sym = rec.get('symbol', '')
        if symbol and sym != symbol:
            continue
        close_t = int(rec.get('updatedTime', 0)) // 1000
        if since is not None and close_t < since:
            continue
        if until is not None and close_t > until:
            continue
        if _is_bot_trade(sym, close_t, by_sym):
            continue
        out.append(_normalize(rec))
    out.sort(key=lambda t: t['close_time'], reverse=True)
    return out


def distinct_symbols():
    """Coin presenti nello storico manuale (BOT escluso), per popolare il
    selettore filtro — su TUTTO lo storico, non solo il periodo selezionato in
    UI, altrimenti cambiando periodo la tendina perderebbe/aggiungerebbe voci."""
    return sorted({t['symbol'] for t in manual_trades()})


def compute_stats(trades):
    n = len(trades)
    if n == 0:
        return {
            'count': 0, 'wins': 0, 'losses': 0, 'win_rate': 0.0,
            'total_pnl': 0.0, 'total_fees': 0.0, 'avg_win': 0.0, 'avg_loss': 0.0,
            'profit_factor': None, 'expectancy': 0.0, 'best_trade': 0.0,
            'worst_trade': 0.0, 'max_drawdown': 0.0, 'equity_curve': [],
        }
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] < 0]
    total_pnl = sum(t['pnl'] for t in trades)
    total_fees = sum(t['fees'] for t in trades)
    gross_win = sum(t['pnl'] for t in wins)
    gross_loss = abs(sum(t['pnl'] for t in losses))

    ordered = sorted(trades, key=lambda t: t['close_time'])
    curve = []
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in ordered:
        running += t['pnl']
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
        # Lightweight Charts richiede timestamp strettamente crescenti nella
        # serie — con molti trade capita che due chiudano nello stesso secondo
        # (es. fill multipli sullo stesso simbolo ravvicinati). Se il punto
        # precedente ha lo stesso 't' lo si aggiorna invece di duplicarlo,
        # altrimenti setData() lancia un errore e il grafico resta vuoto.
        if curve and curve[-1]['t'] == t['close_time']:
            curve[-1]['equity'] = round(running, 4)
        else:
            curve.append({'t': t['close_time'], 'equity': round(running, 4)})

    return {
        'count': n, 'wins': len(wins), 'losses': len(losses),
        'win_rate': len(wins) / n * 100,
        'total_pnl': total_pnl, 'total_fees': total_fees,
        'avg_win': (gross_win / len(wins)) if wins else 0.0,
        'avg_loss': (gross_loss / len(losses)) if losses else 0.0,
        # None = nessuna perdita registrata: se ci sono vincite è un profit
        # factor "infinito" (la UI lo mostra come ∞), altrimenti è indefinito.
        'profit_factor': (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win > 0 else 0.0),
        'expectancy': total_pnl / n,
        'best_trade': max(t['pnl'] for t in trades),
        'worst_trade': min(t['pnl'] for t in trades),
        'max_drawdown': max_dd,
        'equity_curve': curve,
    }
