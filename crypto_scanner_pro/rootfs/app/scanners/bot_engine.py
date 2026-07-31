"""BOT — Trend Band (EMA fast/slow, stessa formula dell'indicatore visuale in
chart.html/mtf.html/trade.js), strategia trend-following pura.

Segnale: spread = EMA(fast_len) - EMA(slow_len) sul close. |spread| < ATR(14)*flat_mult
→ stato FLAT (nessun segnale). Altrimenti BULL (spread>0) o BEAR (spread<0).
Un flip è un cambio di stato rispetto all'ULTIMO stato non-flat visto (lastSignal) —
una candela flat isolata fra due tratti dello stesso colore non genera un nuovo
segnale, esattamente come le frecce dell'indicatore visuale (vedi chart.html
_tbArrowMarker/_tbLastSignal, stessa logica portata qui 1:1).

Entrata/uscita — nessun SL/TP fisso: il bot è SEMPRE in mercato una volta partito
(tranne prima del primo flip), e ogni flip chiude la posizione corrente e ne apre
una opposta nello stesso momento (reverse). Una candela FLAT con posizione aperta
non fa nulla: si resta in posizione finché non arriva un vero flip di direzione
opposta (scelta esplicita dell'utente, stesso comportamento delle frecce).

Il segnale è sulla candela CHIUSA (mai intrabar, a differenza del vecchio ORB):
un EMA cross non ha bisogno di precisione intrabar e aspettare la chiusura evita
falsi flip su un tick che poi rientra prima che la candela finisca.

TF: due concetti separati, come "Calcola su TF" dell'indicatore visuale.
`self.tf` è il TF operativo/mostrato (candele del grafico bot.html, WS pubblico
lato frontend). `self.calc_tf` (opzionale, '' = stesso di `self.tf`) è il TF su
cui gira DAVVERO la strategia — se diverso da `self.tf`, il replay/live gira
sulle candele CHIUSE di `calc_tf` (mai su quelle di `self.tf`): un flip esiste
solo quando chiude una candela di `calc_tf`, indipendentemente da quante
candele di `self.tf` nel frattempo si chiudono. `_strategy_tf()` ritorna
`calc_tf or tf` ed è quello che conta per subscribe/_on_kline/run_engine.

`run_engine()` è l'unica funzione che replica lo stato bar-by-bar: il backtest la
chiama una volta su tutta la history (accumulando i trade chiusi), il motore live
la richiama da zero sull'intera finestra di kline in cache ad ogni nuova candela
CHIUSA e confronta lo stato risultante con quello della chiamata precedente per
rilevare le transizioni (entrata, chiusura/reverse).
"""
import json
import math
import os
import queue
import threading

STATE_FILE = '/data/bot_state.json'

# Registro permanente (mai troncato a 200 come self.signals) delle uscite
# REALI del bot (mode='execution') — usato dal Journal (vedi journal.py) per
# escludere dal conteggio "trade manuali" quelli aperti/chiusi dal bot,
# riconciliando per simbolo+orario contro /v5/position/closed-pnl (Bybit non
# espone un ID che leghi il trade chiuso all'ordine di apertura che lo ha
# generato, quindi serve questo registro locale scritto al momento dell'uscita).
BOT_TRADES_LEDGER_FILE = '/data/bot_trades_history.json'
BOT_TRADES_LEDGER_MAX = 5000


def _append_bot_ledger(rec):
    try:
        os.makedirs(os.path.dirname(BOT_TRADES_LEDGER_FILE), exist_ok=True)
        rows = []
        if os.path.exists(BOT_TRADES_LEDGER_FILE):
            try:
                with open(BOT_TRADES_LEDGER_FILE) as f:
                    rows = json.load(f)
            except Exception:
                rows = []
        rows.append(rec)
        rows = rows[-BOT_TRADES_LEDGER_MAX:]
        tmp = BOT_TRADES_LEDGER_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(rows, f)
        os.replace(tmp, BOT_TRADES_LEDGER_FILE)
    except Exception as e:
        print(f'⚠️ BOT: append ledger: {e}')

TF_SECONDS = {
    '1': 60, '5': 300, '15': 900, '30': 1800, '60': 3600,
    '240': 14400, 'D': 86400, 'W': 604800, 'M': 2592000,  # M: approssimato a 30gg
}

TB_ATR_LEN = 14  # stesso periodo ATR fisso dell'indicatore visuale (calcTrendBand)

DEFAULT_PARAMS = {
    'fast_len': 5,    # stessi default dell'indicatore visuale (_DEFAULT_TB_CFG)
    'slow_len': 10,
    'flat_mult': 0.25,
}

_EMPTY_ENGINE_STATE = {
    'direction': 0,  # 0 nessuna posizione, 1 long, -1 short
    'entry_bar': 0, 'entry_price': 0.0, 'trade_open': False,
    'closed_bar': None, 'closed_reason': None, 'closed_dir': 0, 'closed_price': None,
}


def normalize_params(cfg):
    cfg = cfg or {}
    p = dict(DEFAULT_PARAMS)
    for k in DEFAULT_PARAMS:
        if k in cfg:
            p[k] = cfg[k]
    p['fast_len'] = max(2, min(200, int(p['fast_len'])))
    p['slow_len'] = max(2, min(400, int(p['slow_len'])))
    p['flat_mult'] = max(0.0, min(5.0, float(p['flat_mult'])))
    return p


def warmup_bars_for(params):
    """Barre necessarie prima che EMA/ATR siano affidabili — non più legato ai
    giorni come l'ORB (nessun concetto di 'giorno' per un EMA cross)."""
    return max(params['fast_len'], params['slow_len'], TB_ATR_LEN) + 2


# ── Trend Band — stessa formula di calcTrendBand (chart.html/mtf.html/trade.js) ──

def _ema_series(values, period):
    """EMA ricorsiva seminata dal primo valore (non SMA) — identica a calcEMAField."""
    k = 2.0 / (period + 1)
    v = values[0]
    out = [v]
    for i in range(1, len(values)):
        v = values[i] * k + v * (1.0 - k)
        out.append(v)
    return out


def _true_range_series(candles):
    out = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c['high'] - c['low'])
        else:
            pc = candles[i - 1]['close']
            out.append(max(c['high'] - c['low'], abs(c['high'] - pc), abs(c['low'] - pc)))
    return out


def _rma_series(values, length):
    """Wilder's smoothing seminato da una SMA sui primi `length` valori — identica a calcRMA."""
    n = len(values)
    out = [None] * n
    if n < length:
        return out
    s = sum(values[:length])
    out[length - 1] = s / length
    for i in range(length, n):
        out[i] = (values[i] - out[i - 1]) / length + out[i - 1]
    return out


def _tb_color(spread, atr, flat_mult):
    if atr is not None and abs(spread) < atr * flat_mult:
        return 'flat'
    return 'bull' if spread > 0 else 'bear'


def _calc_trend_band_colors(candles, params):
    closes = [c['close'] for c in candles]
    ema_fast = _ema_series(closes, params['fast_len'])
    ema_slow = _ema_series(closes, params['slow_len'])
    tr = _true_range_series(candles)
    atr = _rma_series(tr, TB_ATR_LEN)
    return [_tb_color(ema_fast[i] - ema_slow[i], atr[i], params['flat_mult']) for i in range(len(candles))]


# ── Motore — replay bar-by-bar ───────────────────────────────────────────────

def run_engine(candles, params, taker_fee_rate=0.00055):
    """Ritorna (stato_finale, trades). `trades` è la lista di tutti i trade
    chiusi durante il replay (per il backtest); `stato_finale` è lo stato "adesso"
    (per il motore live, che lo confronta con la chiamata precedente).
    `taker_fee_rate` non è usato dal motore stesso (nessun breakeven fee-aware
    con questa strategia) — resta nella firma solo per compatibilità con
    run_backtest/BotEngine, che lo passano sempre esplicitamente."""
    n = len(candles)
    st = dict(_EMPTY_ENGINE_STATE)
    trades = []
    if n == 0:
        return st, trades

    colors = _calc_trend_band_colors(candles, params)
    warmup = warmup_bars_for(params)
    pending = None       # dettagli del trade attualmente aperto — costruisce il record alla chiusura
    last_signal = None   # ultimo colore NON-flat confermato — vedi docstring modulo

    for i in range(n):
        if i < warmup:
            if colors[i] in ('bull', 'bear'):
                last_signal = colors[i]
            continue

        color = colors[i]
        is_flip = color in ('bull', 'bear') and color != last_signal

        if is_flip:
            new_dir = 1 if color == 'bull' else -1
            entry_price = candles[i]['close']

            # Reverse: chiude la posizione opposta eventualmente aperta SULLA
            # STESSA barra in cui si apre la nuova — un flip, per costruzione
            # (last_signal si aggiorna solo sui colori non-flat), è sempre nella
            # direzione opposta a quella corrente, mai una ripetizione.
            if st['trade_open']:
                st['closed_dir'] = st['direction']
                st['closed_reason'] = 'reverse'
                st['closed_bar'] = i
                st['closed_price'] = entry_price
                if pending is not None:
                    trades.append({**pending, 'exit_time': candles[i]['time'],
                                   'exit_price': entry_price, 'exit_reason': 'reverse'})
                    pending = None
                st['trade_open'] = False

            side = 'long' if new_dir == 1 else 'short'
            st['direction'] = new_dir
            st['entry_bar'] = i
            st['entry_price'] = entry_price
            st['trade_open'] = True
            pending = {'side': side, 'entry_time': candles[i]['time'], 'entry_price': entry_price}

        if color in ('bull', 'bear'):
            last_signal = color

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


def run_backtest(candles, params, initial_capital=1000.0, sizing=None, taker_fee_rate=0.00055):
    sizing = sizing or {'type': 'fixed', 'value': 50.0}
    _, raw_trades = run_engine(candles, params, taker_fee_rate)

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
                 trade_client=None, symbol='', tf='60', calc_tf='', mode='signal', sizing=None,
                 leverage=1, **params_cfg):
        telegram_config = telegram_config or {}
        self.telegram_token   = telegram_config.get('token', '')
        self.telegram_chat_id = telegram_config.get('chat_id', '')
        self._ws_manager  = ws_manager
        self._live_config = live_config
        self._trade_client = trade_client

        self.symbol  = (symbol or '').upper()
        self.tf      = str(tf or '60')       # TF operativo/mostrato (grafico) — vedi docstring modulo
        self.calc_tf = str(calc_tf or '')    # '' = stesso di tf; vedi _strategy_tf()
        self.mode   = mode if mode in ('signal', 'execution') else 'signal'
        self.sizing = sizing or {'type': 'fixed', 'value': 50.0}
        self.leverage = int(leverage or 1)
        self.params = normalize_params(params_cfg)

        # Fee reale dell'account (VIP tier incluso), usata dal backtest per stimare
        # le fee round-trip. Un solo fetch qui: la reinit avviene ad ogni salvataggio
        # config di un qualsiasi scanner, non serve richiamarla ad ogni candela.
        self.taker_fee_rate = 0.00055
        if trade_client and self.symbol:
            try:
                self.taker_fee_rate = trade_client.get_taker_fee_rate(self.symbol)
            except Exception:
                pass

        self._lock = threading.Lock()
        self._alert_queue = queue.Queue(maxsize=50)
        threading.Thread(target=self._alert_worker, daemon=True).start()

        # entry_bar del trade la cui apertura reale è fallita su Bybit — finché
        # vale, il replay bar-by-bar continua a "riconoscerlo" (run_engine è
        # stateless), ma va ignorato come fantasma invece di ritentare l'ordine o
        # segnalarne una falsa chiusura. Resettato quando quel trade si chiude da
        # solo nel replay (vedi _on_kline).
        self._exec_fail_bar = None

        st = self._load_state()
        self.running = bool(st.get('running', False))
        loaded_state = st.get('position_state') or {}
        self.state = dict(_EMPTY_ENGINE_STATE)
        if isinstance(loaded_state, dict) and 'trade_open' in loaded_state and 'direction' in loaded_state:
            self.state.update(loaded_state)
            self.signals = st.get('signals', [])
        else:
            # Stato persistito da una strategia precedente (forma incompatibile,
            # es. il vecchio ORB con sl_price/tp_price) — riparte da zero invece
            # di crashare o mescolare segnali di due strategie diverse.
            self.signals = []

        # Sicurezza: la modalità esecuzione reale non riprende mai da sola dopo
        # un riavvio del container — richiede sempre uno Start manuale.
        if self.running and self.mode == 'execution':
            self.running = False
            self._save_state()

        if ws_manager is not None:
            ws_manager.add_kline_callback(self._on_kline)
            if self.running and self.symbol:
                ws_manager.subscribe_klines([self.symbol], intervals=[self._strategy_tf()])

        print(f'🤖 BOT init — symbol={self.symbol or "-"} tf={self.tf} calc_tf={self.calc_tf or "(stesso)"} '
              f'mode={self.mode} running={self.running}')

    def _strategy_tf(self):
        """TF su cui gira davvero la strategia — vedi docstring modulo."""
        return self.calc_tf or self.tf

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
            self._ws_manager.subscribe_klines([self.symbol], intervals=[self._strategy_tf()])
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
                'side': 'long' if self.state['direction'] == 1 else 'short',
                'entry_price': self.state['entry_price'],
            }
        return {
            'running': self.running, 'mode': self.mode, 'symbol': self.symbol, 'tf': self.tf,
            'calc_tf': self.calc_tf, 'position': position, 'exchange_position': live_pos,
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
        chiusa in più) e ne deriva gli eventi: uscita (reverse) ed entrata. A
        differenza del vecchio ORB, un reverse chiude la posizione precedente e ne
        apre una nuova SULLA STESSA barra — 'trade_open' resta True nello stato
        nuovo anche quando è appena avvenuta una chiusura, quindi le due condizioni
        vanno controllate in modo indipendente (closed_bar per l'uscita, entry_bar
        per l'entrata), non con un early-return come nel motore precedente."""
        events = []
        t = klines[i]['time']

        if new.get('closed_bar') == i and prev.get('closed_bar') != i:
            side = 'long' if new['closed_dir'] == 1 else 'short'
            events.append({'type': 'exit', 'reason': new['closed_reason'], 'side': side,
                            'time': t, 'price': new['closed_price']})

        if new['trade_open'] and (not prev.get('trade_open') or new['entry_bar'] != prev.get('entry_bar')):
            side = 'long' if new['direction'] == 1 else 'short'
            # 'fresh' = il flip è avvenuto proprio su questa candela (i), non
            # ereditato dal replay dello storico (es. subito dopo uno Start con un
            # trend già in corso) — vedi _execute_entry, che in quel caso rifiuta
            # di piazzare un ordine reale su un prezzo ormai superato dal mercato.
            events.append({'type': 'entry', 'side': side, 'time': t, 'price': new['entry_price'],
                            'fresh': new['entry_bar'] == i})

        return events

    def _on_kline(self, symbol, interval, candle, is_closed):
        # Il segnale (EMA cross) è sulla candela CHIUSA — a differenza del vecchio
        # ORB non serve precisione intrabar, anzi reagire a un tick in corso
        # esporrebbe a falsi flip che rientrano prima della chiusura.
        if not is_closed:
            return
        if not self.running:
            return
        if symbol != self.symbol or interval != self._strategy_tf():
            return
        if not self._ws_manager:
            return

        klines = self._ws_manager.get_klines(symbol, interval)
        needed = warmup_bars_for(self.params) + 5
        if len(klines) < needed:
            return

        with self._lock:
            new_state, _ = run_engine(klines, self.params, taker_fee_rate=self.taker_fee_rate)
            i = len(klines) - 1

            # Trade la cui apertura reale è fallita su Bybit (vedi _execute_entry):
            # run_engine è stateless e lo "ritroverebbe" identico ad ogni candela
            # finché non si chiude da solo — va ignorato come fantasma (niente
            # ri-tentativi, niente falsa entrata/chiusura) finché non si risolve.
            if (self._exec_fail_bar is not None and new_state.get('trade_open')
                    and new_state.get('entry_bar') == self._exec_fail_bar):
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
                          f'entrata reale saltata — il prezzo sarebbe ormai superato dal mercato')
                    # Nessuna posizione reale aperta di proposito — marca come
                    # "fantasma" (stesso _on_kline che gestisce gli ordini falliti)
                    # così quando questo trade si chiude da solo nel replay non
                    # parte un falso alert di chiusura.
                    self._exec_fail_bar = self.state.get('entry_bar')
                    return
                self._execute_entry(ev)
            elif ev['type'] == 'exit':
                self._execute_exit(ev)
                # Registrato SEMPRE (anche se _execute_exit non ha dovuto inviare
                # un ordine perché era già stata chiusa da un reverse precedente)
                # — questo evento rappresenta comunque la chiusura reale della
                # posizione del bot, candela in cui il nostro replay l'ha rilevata.
                _append_bot_ledger({'symbol': self.symbol, 'side': ev.get('side', ''),
                                     'exit_time': ev['time'], 'reason': ev.get('reason', '')})
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
            self._exec_fail_bar = self.state.get('entry_bar')
            return

        side = 'Buy' if ev['side'] == 'long' else 'Sell'
        # Nessun SL/TP fisso (trend-following puro, esce solo sul flip opposto).
        ok, order_id, err = self._trade_client.place_order(
            symbol=symbol, side=side, qty=qty, leverage=self.leverage)
        if not ok:
            print(f'❌ BOT order failed: {err}')
            # Nessuna posizione reale aperta — marca questo trade come "fantasma"
            # (stesso entry_bar) così _on_kline non lo ritratta come nuova
            # entrata né segnala una falsa chiusura quando si risolve da solo.
            self._exec_fail_bar = self.state.get('entry_bar')

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
        # TF di calcolo (non quello mostrato in grafico se diverso) — è quello che
        # ha davvero generato il segnale, vedi _strategy_tf().
        strategy_tf = self._strategy_tf()
        tf_label = {'D': '1D', '240': '4h', '60': '1h', '30': '30m', '15': '15m',
                    '5': '5m', '1': '1m', 'W': '1W', 'M': '1M'}.get(strategy_tf, strategy_tf)
        mode_label = 'ESECUZIONE REALE' if rec.get('mode') == 'execution' else 'Solo Segnale'

        if rec['type'] == 'entry':
            side_label = 'LONG' if rec['side'] == 'long' else 'SHORT'
            lines = [
                f'🤖 BOT Trend Band {tf_label} — {side_label}',
                '',
                '------------------------------------------------',
                f'- Coin: {self.symbol}',
                f'- Modalità: {mode_label}',
                f'- Prezzo entrata: {rec["price"]}',
                '------------------------------------------------',
                '',
                f'<a href="https://www.bybit.com/trade/usdt/{self.symbol}">- View Bybit</a>',
            ]
        else:
            lines = [
                f'🤖 BOT Trend Band {tf_label} — CHIUSURA (reverse)',
                '',
                '------------------------------------------------',
                f'- Coin: {self.symbol}',
                f'- Modalità: {mode_label}',
                f'- Prezzo uscita: {rec["price"]}',
                '------------------------------------------------',
            ]
        caption = '\n'.join(lines)
        send_text(self.telegram_token, self.telegram_chat_id, caption)
        log_alert(self.symbol, 'BOT', emoji='🤖',
                  note=f'{rec["type"]} {rec.get("reason", "")}'.strip(), tf=tf_label)
