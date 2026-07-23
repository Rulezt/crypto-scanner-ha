"""BOT — Opening Range Breakout (ORB), versione semplice senza filtri

Ogni giorno UTC (00:00→24:00) i primi `orb_minutes` minuti definiscono
l'opening range (massimo/minimo di quella finestra). Una volta chiusa la
finestra il range resta BLOCCATO per il resto della giornata. Il trigger è IN
TEMPO REALE (rottura intrabar, non alla chiusura candela — vedi
_detect_orb_signal):
  - STRONG: il livello era già superato in apertura (gap/continuazione) —
    entry al prezzo di apertura.
  - NORMAL: la candela apre dentro il range e lo rompe durante il suo
    svolgimento (high/low) — entry al livello stesso, nel momento esatto
    della rottura, senza aspettare che la candela chiuda.
Un solo segnale per giorno per range (il primo che rompe, in una direzione o
nell'altra — dopo non si ri-segnala più finché non parte il range del giorno
successivo). Il PRIMO giorno della history fornita viene sempre scartato
(range potenzialmente incompleto, la history potrebbe iniziare a metà finestra).

Entry/SL/TP — un solo livello ciascuno, nessuna milestone TP1/TP2/TP3:
  - Entry = vedi trigger sopra (livello o apertura, mai la chiusura).
  - SL = percentuale fissa dal prezzo di entrata (`sl_pct`, configurabile,
    default 1%) — NON più l'estremo opposto del range: un range molto stretto
    o molto largo non deve più determinare un rischio imprevedibile per trade.
  - TP = percentuale fissa dal prezzo di entrata (`tp_pct`, configurabile,
    default 2%) — NON più il range ribaltato/geometrico, stessa logica dello SL.
Poiché il trigger è intrabar, la STESSA candela che apre il trade può, nel
resto del suo svolgimento, raggiungere già TP/SL/breakeven — viene controllata
anche lei, non solo le candele successive (altrimenti un movimento intero
dentro un'unica candela, dopo la rottura, andrebbe perso).
Chiusura in un colpo solo, a SL o a TP — nessun trailing, nessun filtro
trend/ampiezza/volatilità. Unica eccezione: quando il prezzo raggiunge
`be_trigger_pct` (configurabile, default 25%) della distanza entry→TP, lo SL
si sposta UNA VOLTA SOLA a breakeven (prezzo di entrata, fee-aware) — non
toglie la possibilità di chiudere a TP pieno, protegge solo da un'inversione
dopo aver raggiunto quel punto.

I timestamp delle candele possono arrivare in due convenzioni diverse a
seconda della fonte (vedi app.py): il motore live (ws_manager) usa epoch UTC
puro, mentre le candele del backtest (/api/klines-style fetch) sono shiftate
dell'offset UTC configurato dall'utente. `tz_offset_s` (per-chiamata, MAI un
parametro di configurazione della strategia) dice a `run_engine`/`run_backtest`
di quanto sono shiftate le candele ricevute, così il confine "00:00 UTC" del
giorno viene individuato correttamente in entrambi i casi.

`run_engine()` è l'unica funzione che replica lo stato bar-by-bar: il backtest
la chiama una volta su tutta la history (accumulando i trade chiusi), il motore
live la richiama da zero sull'intera finestra di kline in cache ad ogni nuova
candela chiusa e confronta lo stato risultante con quello della chiamata
precedente per rilevare le transizioni (entrata, chiusura).
"""
import json
import math
import os
import queue
import threading

STATE_FILE = '/data/bot_state.json'

TF_SECONDS = {
    '1': 60, '5': 300, '15': 900, '30': 1800, '60': 3600,
    '240': 14400, 'D': 86400, 'W': 604800, 'M': 2592000,  # M: approssimato a 30gg
}

DAY_SECONDS = 86400

DEFAULT_PARAMS = {
    'orb_minutes': 30,
    'sl_pct': 1.0,           # % fissa dal prezzo di entrata (non più l'estremo del range)
    'tp_pct': 2.0,           # % fissa dal prezzo di entrata (non più il range ribaltato)
    'be_trigger_pct': 25.0,  # % della distanza entry->TP a cui lo SL si sposta a breakeven
}

_EMPTY_ENGINE_STATE = {
    'breakout_dir': 0, 'breakout_bar': 0, 'break_strength': '—',
    'entry_price': 0.0, 'sl_price': 0.0, 'sl_price_orig': 0.0, 'tp_price': 0.0,
    'be_target_price': 0.0,
    'sl_hit': False, 'tp_hit': False, 'be_hit': False, 'trade_open': False,
    'closed_bar': None, 'closed_reason': None, 'closed_dir': 0, 'closed_price': None,
}


def normalize_params(cfg):
    cfg = cfg or {}
    p = dict(DEFAULT_PARAMS)
    for k in DEFAULT_PARAMS:
        if k in cfg:
            p[k] = cfg[k]
    p['orb_minutes'] = max(1, min(1440, int(p['orb_minutes'])))
    p['sl_pct'] = max(0.05, min(20.0, float(p['sl_pct'])))
    p['tp_pct'] = max(0.05, min(50.0, float(p['tp_pct'])))
    p['be_trigger_pct'] = max(0.0, min(100.0, float(p['be_trigger_pct'])))
    return p


def warmup_bars_for(params, tf_seconds=None):
    """L'ORB non ha bisogno di N barre come una SMA: serve solo aver superato
    il PRIMO giorno (potenzialmente incompleto, scartato sempre — vedi
    run_engine) della history fornita. Senza il TF della candela non possiamo
    convertire "1.5 giorni" in barre, quindi usiamo un minimo conservativo."""
    if not tf_seconds:
        return 5
    return max(5, int(DAY_SECONDS * 1.5 / tf_seconds))


# ── Opening Range giornaliero (UTC) ───────────────────────────────────────────

def _day_bucket(t, tz_offset_s):
    """Inizio del giorno UTC contenente il timestamp t, espresso nella STESSA
    convenzione oraria di t (t può già includere l'offset UTC configurato,
    vedi docstring modulo — tz_offset_s dice di quanto)."""
    true_utc = t - tz_offset_s
    return true_utc - (true_utc % DAY_SECONDS) + tz_offset_s


def _calc_orb_ranges(candles, orb_minutes, tz_offset_s):
    """Per ogni candela: (upper, lower) = opening range BLOCCATO della sua
    giornata, o None finché la finestra di apertura è ancora in formazione
    (o se il giorno non ha avuto candele nella finestra, es. gap di history)."""
    n = len(candles)
    upper = [None] * n
    lower = [None] * n
    window_s = orb_minutes * 60

    day_bucket = None
    window_end = None
    day_high = day_low = None
    orb_high = orb_low = None

    for i in range(n):
        t = candles[i]['time']
        db = _day_bucket(t, tz_offset_s)
        if db != day_bucket:
            day_bucket = db
            window_end = day_bucket + window_s
            day_high = day_low = None
            orb_high = orb_low = None

        if t < window_end:
            day_high = candles[i]['high'] if day_high is None else max(day_high, candles[i]['high'])
            day_low  = candles[i]['low']  if day_low  is None else min(day_low,  candles[i]['low'])
        elif orb_high is None and day_high is not None:
            orb_high, orb_low = day_high, day_low

        upper[i], lower[i] = orb_high, orb_low

    return upper, lower


def _detect_orb_signal(candles, upper, lower, i):
    """Segnale sulla candela i vs. l'opening range BLOCCATO della sua stessa
    giornata (fisso per tutto il giorno). Trigger IN TEMPO REALE: non aspetta
    la chiusura della candela, scatta alla rottura del livello non appena
    accade (high/low), esattamente come accadrebbe seguendo il prezzo tick per
    tick — non a un prezzo "confermato" dalla chiusura.
      - STRONG: il livello era già superato in apertura (gap/continuazione) —
        entry al prezzo di apertura (il primo prezzo disponibile quella candela).
      - NORMAL: la candela apre dentro il range e lo rompe durante il suo
        svolgimento (high/low) — entry al livello stesso (il prezzo esatto
        della rottura).
    Ritorna None oppure {'type', 'dir', 'entry'}."""
    o_hi, o_lo = upper[i], lower[i]
    if o_hi is None or o_lo is None:
        return None
    o, h, l = candles[i]['open'], candles[i]['high'], candles[i]['low']
    if o > o_hi:
        return {'type': 'strong', 'dir': 'bull', 'entry': o}
    if o < o_lo:
        return {'type': 'strong', 'dir': 'bear', 'entry': o}
    if h >= o_hi:
        return {'type': 'normal', 'dir': 'bull', 'entry': o_hi}
    if l <= o_lo:
        return {'type': 'normal', 'dir': 'bear', 'entry': o_lo}
    return None


# ── Motore breakout — replay bar-by-bar ──────────────────────────────────────

def run_engine(candles, params, tz_offset_s=0, taker_fee_rate=0.00055):
    """Ritorna (stato_finale, trades). `trades` è la lista di tutti i trade
    chiusi durante il replay (per il backtest); `stato_finale` è lo stato "adesso"
    (per il motore live, che lo confronta con la chiamata precedente).
    `tz_offset_s`: di quanto le candele sono shiftate rispetto a UTC puro (vedi
    docstring modulo) — SEMPRE passato esplicitamente dal chiamante, mai una
    strategia-param persistita. `taker_fee_rate`: fee taker per lato (round-trip
    = 2x), usata per il breakeven fee-aware — anch'essa sempre esterna, mai
    parte di `params` (dipende dall'account/VIP tier, non dalla strategia)."""
    n = len(candles)
    upper_arr, lower_arr = _calc_orb_ranges(candles, params['orb_minutes'], tz_offset_s)

    st = dict(_EMPTY_ENGINE_STATE)
    trades = []
    pending = None  # dettagli del trade attualmente aperto — costruisce il record alla chiusura

    first_day_bucket = None   # il primo giorno della history è sempre scartato (range forse incompleto)
    triggered_bucket = None   # giorno per cui è già scattato un segnale (max 1 trade/giorno)

    for i in range(n):
        day_bucket = _day_bucket(candles[i]['time'], tz_offset_s)
        if first_day_bucket is None:
            first_day_bucket = day_bucket
        # A differenza di una SMA, l'ORB non ha bisogno di un numero minimo di
        # barre: basta essere oltre il primo giorno (range potenzialmente
        # incompleto) — nessun altro warmup serve.
        is_warmed_up = day_bucket != first_day_bucket

        # trigger — opening range giornaliero UTC, vedi _detect_orb_signal
        sig = None
        if (not st['trade_open'] and is_warmed_up and triggered_bucket != day_bucket):
            sig = _detect_orb_signal(candles, upper_arr, lower_arr, i)
            if sig is not None:
                triggered_bucket = day_bucket

        # Entry/SL/TP — un solo livello ciascuno, entrambi a percentuale fissa
        # dal prezzo di entrata (`sl_pct`/`tp_pct`), non più dalla geometria
        # del range (che serve solo a individuare il trigger, non più i livelli).
        if sig is not None:
            entry = sig['entry']
            st['entry_price'] = entry
            st['break_strength'] = 'Strong' if sig['type'] == 'strong' else 'Normal'
            sl_frac = params['sl_pct'] / 100.0
            tp_frac = params['tp_pct'] / 100.0
            if sig['dir'] == 'bull':
                breakout_dir = 1
                sl_price = entry * (1.0 - sl_frac)
                tp_price = entry * (1.0 + tp_frac)
                side = 'long'
            else:
                breakout_dir = -1
                sl_price = entry * (1.0 + sl_frac)
                tp_price = entry * (1.0 - tp_frac)
                side = 'short'
            st['breakout_dir'], st['breakout_bar'] = breakout_dir, i
            st['sl_price'] = sl_price
            st['sl_price_orig'] = sl_price
            st['tp_price'] = tp_price
            st['sl_hit'] = st['tp_hit'] = st['be_hit'] = False
            st['trade_open'] = True
            # Breakeven "vero" = recupera anche le fee di andata+ritorno (entrambe
            # taker su Bybit derivati), non il puro prezzo di entrata — altrimenti
            # dopo le fee sarebbe comunque una piccola perdita.
            round_trip_fee = 2.0 * taker_fee_rate
            st['be_target_price'] = entry * (1.0 + round_trip_fee if breakout_dir == 1 else 1.0 - round_trip_fee)
            pending = {
                'side': side, 'entry_time': candles[i]['time'], 'entry_price': st['entry_price'],
                'sl_price_orig': st['sl_price_orig'], 'tp_price': st['tp_price'], 'strength': st['break_strength'],
            }

        # TP/SL hit detection — chiusura in un colpo solo. Unica eccezione: al 25%
        # della distanza entry->TP lo SL si sposta una volta a breakeven (vedi
        # docstring modulo), ma la chiusura resta comunque solo a SL o TP pieno.
        # i >= breakout_bar (non solo >): il trigger è ora intrabar, quindi la
        # STESSA candela che apre il trade può, nel resto del suo svolgimento,
        # anche già raggiungere TP/SL/breakeven — non va ignorata.
        if st['trade_open'] and st['breakout_dir'] != 0 and i >= st['breakout_bar']:
            hi, lo = candles[i]['high'], candles[i]['low']
            tp_side = (hi >= st['tp_price']) if st['breakout_dir'] == 1 else (lo <= st['tp_price'])
            sl_side = (lo <= st['sl_price']) if st['breakout_dir'] == 1 else (hi >= st['sl_price'])
            if sl_side:
                st['sl_hit'] = True
            elif tp_side:
                st['tp_hit'] = True
            elif not st['be_hit']:
                be_trigger_price = st['entry_price'] + (st['tp_price'] - st['entry_price']) * (params['be_trigger_pct'] / 100.0)
                be_trigger_side = (hi >= be_trigger_price) if st['breakout_dir'] == 1 else (lo <= be_trigger_price)
                if be_trigger_side:
                    st['be_hit'] = True
                    st['sl_price'] = st['be_target_price']

        # close trade (a SL o TP — chiusura unica; 'be' = SL colpito dopo essere
        # stato spostato a breakeven, distinto da 'sl' = SL originale mai raggiunto il 25%)
        if st['trade_open'] and (st['tp_hit'] or st['sl_hit']):
            reason = ('be' if st['be_hit'] else 'sl') if st['sl_hit'] else 'tp'
            exit_price = st['tp_price'] if reason == 'tp' else st['sl_price']
            st['closed_dir'] = st['breakout_dir']
            st['closed_reason'] = reason
            st['closed_bar'] = i
            st['closed_price'] = exit_price
            if pending is not None:
                trades.append({
                    **pending,
                    'exit_time': candles[i]['time'], 'exit_price': exit_price, 'exit_reason': reason,
                })
                pending = None
            st['trade_open'] = False
            st['breakout_dir'] = 0

    return st, trades


def _max_drawdown_pct(curve):
    if not curve:
        return 0.0
    peak = curve[0]['equity']
    max_dd = 0.0
    for pt in curve:
        peak = max(peak, pt['equity'])
        if peak > 0:
            max_dd = max(max_dd, (peak - pt['equity']) / peak * 100.0)
    return round(max_dd, 2)


def run_backtest(candles, params, initial_capital=1000.0, sizing=None, taker_fee_rate=0.00055, tz_offset_s=0):
    sizing = sizing or {'type': 'fixed', 'value': 50.0}
    _, raw_trades = run_engine(candles, params, tz_offset_s, taker_fee_rate)

    trades = []
    equity = float(initial_capital)
    equity_curve = [{'time': candles[0]['time'] if candles else 0, 'equity': round(equity, 4)}]

    for rt in raw_trades:
        side, entry_price, exit_price = rt['side'], rt['entry_price'], rt['exit_price']
        direction = 1 if side == 'long' else -1
        gross_pnl_pct = direction * (exit_price - entry_price) / entry_price * 100.0

        if sizing.get('type') == 'pct_balance':
            notional = equity * (float(sizing.get('value', 0)) / 100.0)
        else:
            notional = float(sizing.get('value', 50.0))
        gross_pnl_usdt = notional * (gross_pnl_pct / 100.0)
        # Entrata e uscita sono entrambe ordini a mercato (taker) sui derivati Bybit.
        fee_usdt = notional * taker_fee_rate * 2
        pnl_usdt = gross_pnl_usdt - fee_usdt
        pnl_pct = (pnl_usdt / notional * 100.0) if notional else 0.0
        equity += pnl_usdt

        trades.append({
            'side': side, 'entry_time': rt['entry_time'], 'entry_price': entry_price,
            'exit_time': rt['exit_time'], 'exit_price': exit_price, 'exit_reason': rt['exit_reason'],
            'pnl_pct': round(pnl_pct, 4), 'pnl_usdt': round(pnl_usdt, 4), 'fee_usdt': round(fee_usdt, 4),
            'notional': round(notional, 2),
            'sl_price_orig': rt['sl_price_orig'], 'tp_price': rt['tp_price'], 'strength': rt['strength'],
        })
        equity_curve.append({'time': rt['exit_time'], 'equity': round(equity, 4)})

    total  = len(trades)
    wins   = [tr for tr in trades if tr['pnl_usdt'] > 0]
    losses = [tr for tr in trades if tr['pnl_usdt'] <= 0]
    gross_profit = sum(tr['pnl_usdt'] for tr in wins)
    gross_loss   = -sum(tr['pnl_usdt'] for tr in losses)
    net_profit   = equity - initial_capital

    stats = {
        'net_profit': round(net_profit, 2),
        'net_profit_pct': round(net_profit / initial_capital * 100.0, 2) if initial_capital else 0.0,
        'total_trades': total,
        'win_rate': round(len(wins) / total * 100.0, 2) if total else 0.0,
        'profit_factor': (round(gross_profit / gross_loss, 4) if gross_loss > 0
                           else (None if gross_profit == 0 else float('inf'))),
        'avg_trade_pct': round(sum(tr['pnl_pct'] for tr in trades) / total, 4) if total else 0.0,
        'max_drawdown_pct': _max_drawdown_pct(equity_curve),
    }
    if stats['profit_factor'] == float('inf'):
        stats['profit_factor'] = None  # nessuna perdita — non rappresentabile in JSON

    return {'trades': trades, 'equity_curve': equity_curve, 'stats': stats}


# ── Live engine ──────────────────────────────────────────────────────────────

class BotEngine:
    def __init__(self, telegram_config=None, ws_manager=None, live_config=None,
                 trade_client=None, symbol='', tf='60', mode='signal', sizing=None,
                 leverage=1, **params_cfg):
        telegram_config = telegram_config or {}
        self.telegram_token   = telegram_config.get('token', '')
        self.telegram_chat_id = telegram_config.get('chat_id', '')
        self._ws_manager  = ws_manager
        self._live_config = live_config
        self._trade_client = trade_client

        self.symbol = (symbol or '').upper()
        self.tf     = str(tf or '60')
        self.mode   = mode if mode in ('signal', 'execution') else 'signal'
        self.sizing = sizing or {'type': 'fixed', 'value': 50.0}
        self.leverage = int(leverage or 1)
        self.params = normalize_params(params_cfg)

        # Fee reale dell'account (VIP tier incluso) — usata per il breakeven
        # fee-aware del motore (vedi run_engine). Un solo fetch qui: la reinit
        # avviene ad ogni salvataggio config di un qualsiasi scanner, non serve
        # richiamarla ad ogni candela (rischio rate-limit + fee tier non cambia
        # praticamente mai durante una sessione).
        self.taker_fee_rate = 0.00055
        if trade_client and self.symbol:
            try:
                self.taker_fee_rate = trade_client.get_taker_fee_rate(self.symbol)
            except Exception:
                pass

        self._lock = threading.Lock()
        self._alert_queue = queue.Queue(maxsize=50)
        threading.Thread(target=self._alert_worker, daemon=True).start()

        # breakout_bar del trade la cui apertura reale è fallita su Bybit — finché
        # vale, il replay bar-by-bar continua a "riconoscerlo" (run_engine è
        # stateless), ma va ignorato come fantasma invece di ritentare l'ordine o
        # segnalarne una falsa chiusura. Resettato quando quel trade si chiude da
        # solo nel replay (vedi _on_kline).
        self._exec_fail_bar = None

        st = self._load_state()
        self.running = bool(st.get('running', False))
        loaded_state = st.get('position_state') or {}
        self.state = dict(_EMPTY_ENGINE_STATE)
        if isinstance(loaded_state, dict) and 'trade_open' in loaded_state and 'tp_price' in loaded_state:
            self.state.update(loaded_state)
            self.signals = st.get('signals', [])
        else:
            # Stato persistito da una strategia precedente (forma incompatibile,
            # es. TP1/TP2/TP3) — riparte da zero invece di crashare o mescolare
            # segnali di due strategie diverse.
            self.signals = []

        # Sicurezza: la modalità esecuzione reale non riprende mai da sola dopo
        # un riavvio del container — richiede sempre uno Start manuale.
        if self.running and self.mode == 'execution':
            self.running = False
            self._save_state()

        if ws_manager is not None:
            ws_manager.add_kline_callback(self._on_kline)
            if self.running and self.symbol:
                ws_manager.subscribe_klines([self.symbol], intervals=[self.tf])

        print(f'🤖 BOT init — symbol={self.symbol or "-"} tf={self.tf} mode={self.mode} '
              f'running={self.running}')

    # ── State persistence ────────────────────────────────────────────────────

    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    return json.load(f)
        except Exception as e:
            print(f'⚠️ BOT: load state: {e}')
        return {}

    def _save_state(self):
        snapshot = {
            'running': self.running, 'mode': self.mode, 'symbol': self.symbol, 'tf': self.tf,
            'position_state': self.state, 'signals': self.signals[-200:],
        }
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(snapshot, f)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            print(f'⚠️ BOT: save state: {e}')

    # ── Start/Stop/Status ────────────────────────────────────────────────────

    def start(self):
        if not self.symbol:
            return False, 'Nessun simbolo configurato'
        tf_seconds = TF_SECONDS.get(self.tf) or (int(self.tf) * 60 if self.tf.isdigit() else 3600)
        if tf_seconds >= DAY_SECONDS:
            return False, (f"TF {self.tf} troppo largo per un opening range giornaliero — "
                            f"serve un TF intraday (max 4h/240)")
        if self.mode == 'execution' and self._trade_client:
            pos = self._trade_client.get_position(self.symbol)
            if pos:
                return False, (f'Posizione già aperta su {self.symbol} — chiudila '
                                f'manualmente prima di avviare il bot')
        with self._lock:
            self.running = True
            self.state = dict(_EMPTY_ENGINE_STATE)
            self._exec_fail_bar = None
            # Il feed segnali non andava mai svuotato da Start/Stop — un vecchio
            # segnale (magari ereditato da un warm-start precedente) restava visibile
            # indefinitamente confondendolo con uno nuovo. Ogni Start riparte pulito.
            self.signals = []
            self._save_state()
        if self._ws_manager:
            self._ws_manager.subscribe_klines([self.symbol], intervals=[self.tf])
        return True, None

    def clear_signals(self):
        with self._lock:
            self.signals = []
            self._save_state()

    def stop(self, close_position=False):
        result = {'success': True}
        with self._lock:
            self.running = False
            if close_position and self.mode == 'execution' and self.state.get('trade_open') and self._trade_client:
                pos = self._trade_client.get_position(self.symbol)
                if pos:
                    ok, err = self._trade_client.close_position(self.symbol, pos['side'], pos['size'])
                    result['closed'] = ok
                    if err:
                        result['error'] = err
                self.state = dict(_EMPTY_ENGINE_STATE)
            self._save_state()
        return result

    def status(self):
        live_pos = None
        if self.mode == 'execution' and self._trade_client and self.symbol:
            live_pos = self._trade_client.get_position(self.symbol)

        position = None
        if self.state.get('trade_open'):
            position = {
                'side': 'long' if self.state['breakout_dir'] == 1 else 'short',
                'entry_price': self.state['entry_price'], 'stop': self.state['sl_price'],
                'target': self.state['tp_price'], 'strength': self.state['break_strength'],
            }
        return {
            'running': self.running, 'mode': self.mode, 'symbol': self.symbol, 'tf': self.tf,
            'position': position, 'exchange_position': live_pos,
            'last_signal_time': self.signals[-1]['time'] if self.signals else None,
        }

    def get_signals(self, limit=100):
        return self.signals[-limit:]

    def get_exchange_alerts(self, limit=20):
        """Fill reali dall'exchange per il simbolo configurato — vedi
        _BotTradeClient.get_recent_executions per il motivo (confronto col
        motore, non un doppione dei segnali calcolati)."""
        if not self._trade_client or not self.symbol:
            return []
        return self._trade_client.get_recent_executions(self.symbol, limit)

    # ── WS callback ──────────────────────────────────────────────────────────

    def _diff_events(self, prev, new, klines, i):
        """Confronta lo stato del motore tra due chiamate consecutive (una candela
        chiusa in più) e ne deriva gli eventi: entrata, spostamento a breakeven
        (25% verso il TP) e chiusura."""
        events = []
        t = klines[i]['time']

        if new['trade_open'] and (not prev.get('trade_open') or new['breakout_bar'] != prev.get('breakout_bar')):
            side = 'long' if new['breakout_dir'] == 1 else 'short'
            # 'fresh' = il breakout è avvenuto proprio su questa candela (i), non
            # ereditato dal replay dello storico (es. subito dopo uno Start con un
            # segnale già a metà strada) — vedi _execute_entry, che rifiuta di
            # piazzare un ordine reale con Entry/SL/TP calcolati su un prezzo ormai
            # superato dal mercato.
            events.append({'type': 'entry', 'side': side, 'time': t, 'price': new['entry_price'],
                            'stop': new['sl_price_orig'], 'target': new['tp_price'], 'strength': new['break_strength'],
                            'fresh': new['breakout_bar'] == i})
            return events

        if prev.get('trade_open') and new['trade_open'] and new['be_hit'] and not prev.get('be_hit'):
            side = 'long' if new['breakout_dir'] == 1 else 'short'
            events.append({'type': 'breakeven', 'side': side, 'time': t,
                            'price': new['entry_price'], 'new_stop': new['sl_price']})

        if prev.get('trade_open') and not new['trade_open'] and new['closed_bar'] == i:
            side = 'long' if new['closed_dir'] == 1 else 'short'
            events.append({'type': 'exit', 'reason': new['closed_reason'], 'side': side,
                            'time': t, 'price': new['closed_price']})

        return events

    def _on_kline(self, symbol, interval, candle, is_closed):
        # Trigger intrabar (vedi _detect_orb_signal): reagisce anche alla candela
        # ANCORA IN FORMAZIONE, non solo a quella chiusa — altrimenti il segnale
        # scatterebbe comunque solo alla chiusura, vanificando il senso di un
        # trigger "in tempo reale". ws_manager aggiorna in-place l'ultima candela
        # del buffer ad ogni tick (vedi ws_manager._handle_kline), quindi
        # get_klines() riflette già il prezzo più recente anche a candela aperta.
        if not self.running:
            return
        if symbol != self.symbol or interval != self.tf:
            return
        if not self._ws_manager:
            return

        klines = self._ws_manager.get_klines(symbol, interval)
        tf_seconds = TF_SECONDS.get(self.tf) or (int(self.tf) * 60 if self.tf.isdigit() else 3600)
        needed = warmup_bars_for(self.params, tf_seconds) + 5
        if len(klines) < needed:
            return

        with self._lock:
            # ws_manager fornisce sempre epoch UTC puro (nessuno shift di
            # timezone), a differenza delle candele del backtest — vedi
            # docstring modulo su tz_offset_s.
            new_state, _ = run_engine(klines, self.params, tz_offset_s=0, taker_fee_rate=self.taker_fee_rate)
            i = len(klines) - 1

            # Trade la cui apertura reale è fallita su Bybit (vedi _execute_entry):
            # run_engine è stateless e lo "ritroverebbe" identico ad ogni candela
            # finché non si chiude da solo — va ignorato come fantasma (niente
            # ri-tentativi, niente falsa entrata/chiusura) finché non si risolve.
            if (self._exec_fail_bar is not None and new_state.get('trade_open')
                    and new_state.get('breakout_bar') == self._exec_fail_bar):
                new_state = dict(_EMPTY_ENGINE_STATE)
            elif self._exec_fail_bar is not None and not new_state.get('trade_open'):
                self._exec_fail_bar = None

            events = self._diff_events(self.state, new_state, klines, i)
            self.state = new_state
            if events:
                for ev in events:
                    rec = dict(ev)
                    rec['mode'] = self.mode
                    self.signals.append(rec)
                self.signals = self.signals[-200:]
            self._save_state()

        for ev in events:
            self._handle_event(ev)

    def _handle_event(self, ev):
        self._queue_alert(dict(ev, mode=self.mode))
        if self.mode != 'execution' or not self._trade_client:
            return
        try:
            if ev['type'] == 'entry':
                if not ev.get('fresh', True):
                    print(f'⚠️ BOT: segnale non fresco (ereditato dallo storico) su {self.symbol}, '
                          f'entrata reale saltata — Entry/SL/TP sarebbero calcolati su un prezzo ormai superato')
                    # Nessuna posizione reale aperta di proposito — marca come
                    # "fantasma" (stesso _on_kline che gestisce gli ordini falliti)
                    # così quando questo trade si chiude da solo nel replay non
                    # parte un falso alert di chiusura.
                    self._exec_fail_bar = self.state.get('breakout_bar')
                    return
                self._execute_entry(ev)
            elif ev['type'] == 'breakeven':
                self._execute_breakeven(ev)
            elif ev['type'] == 'exit':
                self._execute_exit(ev)
        except Exception as e:
            print(f'❌ BOT execution error: {e}')

    # ── Esecuzione reale ─────────────────────────────────────────────────────

    def _execute_entry(self, ev):
        symbol = self.symbol
        pos = self._trade_client.get_position(symbol)
        if pos:
            print(f'⚠️ BOT: posizione già aperta su {symbol}, entrata ignorata (anti doppia-entrata)')
            return

        price = ev['price']
        if self.sizing.get('type') == 'pct_balance':
            bal = self._trade_client.get_balance()
            notional = (bal.get('available', 0) or 0) * (float(self.sizing.get('value', 0)) / 100.0)
        else:
            notional = float(self.sizing.get('value', 50.0))

        instr = self._trade_client.get_instrument(symbol)
        qty = self._round_qty(notional / price if price else 0, instr)
        if qty <= 0:
            print(f'⚠️ BOT: qty calcolata 0 per {symbol}, entrata saltata')
            self._exec_fail_bar = self.state.get('breakout_bar')
            return

        side = 'Buy' if ev['side'] == 'long' else 'Sell'
        ok, order_id, err = self._trade_client.place_order(
            symbol=symbol, side=side, qty=qty, leverage=self.leverage,
            stop_loss=ev['stop'], take_profit=ev['target'])
        if not ok:
            print(f'❌ BOT order failed: {err}')
            # Nessuna posizione reale aperta — marca questo trade come "fantasma"
            # (stesso breakout_bar) così _on_kline non lo ritratta come nuova
            # entrata né segnala una falsa chiusura quando si risolve da solo.
            self._exec_fail_bar = self.state.get('breakout_bar')

    def _execute_breakeven(self, ev):
        pos = self._trade_client.get_position(self.symbol)
        if not pos:
            return
        ok, err = self._trade_client.modify_stop_loss(self.symbol, ev['new_stop'], pos.get('positionIdx', 0))
        if not ok:
            print(f'❌ BOT breakeven SL move failed: {err}')

    def _execute_exit(self, ev):
        pos = self._trade_client.get_position(self.symbol)
        if not pos:
            return
        ok, err = self._trade_client.close_position(self.symbol, pos['side'], pos['size'])
        if not ok:
            print(f'❌ BOT close failed: {err}')

    @staticmethod
    def _round_qty(raw_qty, instr):
        step_v = float((instr or {}).get('qtyStep', 0.001)) or 0.001
        min_q  = float((instr or {}).get('minOrderQty', 0.001)) or 0.001
        qty = math.floor(raw_qty / step_v) * step_v
        if qty < min_q:
            return 0.0
        decimals = len(str(step_v).split('.')[1].rstrip('0')) if '.' in str(step_v) else 0
        return round(qty, decimals)

    # ── Alert ────────────────────────────────────────────────────────────────

    def _queue_alert(self, rec):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            self._alert_queue.put_nowait(rec)
        except queue.Full:
            pass

    def _alert_worker(self):
        while True:
            try:
                rec = self._alert_queue.get(timeout=5)
                self._send_alert(rec)
                self._alert_queue.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                print(f'❌ BOT alert worker: {e}')

    def _send_alert(self, rec):
        try:
            from alert_utils import send_text, log_alert
        except ImportError:
            return
        tf_label = {'D': '1D', '240': '4h', '60': '1h', '30': '30m', '15': '15m',
                    '5': '5m', '1': '1m', 'W': '1W', 'M': '1M'}.get(self.tf, self.tf)
        mode_label = 'ESECUZIONE REALE' if rec.get('mode') == 'execution' else 'Solo Segnale'

        if rec['type'] == 'entry':
            side_label = 'LONG' if rec['side'] == 'long' else 'SHORT'
            lines = [
                f'🤖 BOT ORB {tf_label} — {side_label} [{rec.get("strength", "—")}]',
                '',
                '------------------------------------------------',
                f'- Coin: {self.symbol}',
                f'- Modalità: {mode_label}',
                f'- Prezzo entrata: {rec["price"]}',
                f'- Stop Loss: {rec["stop"]:.6f}',
                f'- Take Profit: {rec["target"]:.6f}',
                '------------------------------------------------',
                '',
                f'<a href="https://www.bybit.com/trade/usdt/{self.symbol}">- View Bybit</a>',
            ]
        elif rec['type'] == 'breakeven':
            lines = [
                f'🤖 BOT ORB {tf_label} — Breakeven (25% verso TP)',
                '',
                '------------------------------------------------',
                f'- Coin: {self.symbol}',
                f'- Modalità: {mode_label}',
                f'- SL spostato a: {rec["new_stop"]:.6f}',
                '------------------------------------------------',
            ]
        else:
            reason_label = {'sl': 'Stop Loss', 'be': 'Breakeven', 'tp': 'Take Profit (target)'}.get(rec['reason'], rec['reason'])
            lines = [
                f'🤖 BOT ORB {tf_label} — CHIUSURA',
                '',
                '------------------------------------------------',
                f'- Coin: {self.symbol}',
                f'- Modalità: {mode_label}',
                f'- Motivo: {reason_label}',
                f'- Prezzo uscita: {rec["price"]}',
                '------------------------------------------------',
            ]
        caption = '\n'.join(lines)
        send_text(self.telegram_token, self.telegram_chat_id, caption)
        log_alert(self.symbol, 'BOT', emoji='🤖',
                  note=f'{rec["type"]} {rec.get("reason", "")}'.strip(), tf=tf_label)
