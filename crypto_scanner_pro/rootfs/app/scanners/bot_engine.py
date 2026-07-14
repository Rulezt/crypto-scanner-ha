"""BOT — Breakout Pattern (trigger: canale SMA20 close/high/low, port di channel-breakout.html)

Canale a 2 linee (upper/lower = SMA(period) di high/low, stessa formula
dell'indicatore Canale di chart.html/mtf.html e dello screener
`channel-breakout.html` — qui la linea mid/close non serve al trigger, non
viene calcolata). Segnale sulla candela i confrontato col canale della
candela PRECEDENTE (i-1) per evitare l'auto-riferimento:
  - STRONG: apre già fuori dal canale e chiude dal lato opposto (attraversa
    tutto il canale in una candela).
  - NORMAL: apre dentro il canale e chiude fuori (sopra=Long, sotto=Short).
Scarta le candele di "continuazione" (trend già in corso, non un nuovo
breakout) confrontando anche la candela precedente col canale di due bar fa —
stesso fix anti falsi-segnali di `channel-breakout.html`.

Entry/SL/TP1-3 e gestione del trade (SL spostato a breakeven dopo TP1,
chiusura solo a SL o TP3, nessuna chiusura parziale) sono INVARIATI rispetto
alla strategia precedente (canali convergenti da pivot): SL all'estremo
opposto del canale al momento del segnale, target = estremo rotto + altezza
del canale, TP1/TP2/TP3 a un terzo/due terzi/tutto il movimento.

`run_engine()` è l'unica funzione che replica lo stato bar-by-bar: il backtest
la chiama una volta su tutta la history (accumulando i trade chiusi), il motore
live la richiama da zero sull'intera finestra di kline in cache ad ogni nuova
candela chiusa e confronta lo stato risultante con quello della chiamata
precedente per rilevare le transizioni (entrata, TP1/TP2 toccati, chiusura).
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

DEFAULT_PARAMS = {
    'period': 20,
}

_EMPTY_ENGINE_STATE = {
    'breakout_dir': 0, 'breakout_bar': 0, 'break_strength': '—',
    'entry_price': 0.0, 'sl_price': 0.0, 'sl_price_orig': 0.0,
    'tp1_price': 0.0, 'tp2_price': 0.0, 'tp3_price': 0.0,
    'tp1_hit': False, 'tp2_hit': False, 'tp3_hit': False, 'sl_hit': False, 'trade_open': False,
    'closed_bar': None, 'closed_reason': None, 'closed_dir': 0, 'closed_price': None,
}


def normalize_params(cfg):
    cfg = cfg or {}
    p = dict(DEFAULT_PARAMS)
    for k in DEFAULT_PARAMS:
        if k in cfg:
            p[k] = cfg[k]
    p['period'] = max(5, min(500, int(p['period'])))
    return p


def warmup_bars_for(params):
    return params['period'] + 5


# ── Canale SMA(period) su close/high/low ─────────────────────────────────────

def _calc_sma_plain(values, period):
    n = len(values)
    out = [None] * n
    s = 0.0
    for i in range(n):
        s += values[i] or 0.0
        if i >= period:
            s -= values[i - period] or 0.0
        if i >= period - 1:
            out[i] = s / period
    return out


def _calc_channel(candles, period):
    upper = _calc_sma_plain([c['high']  for c in candles], period)
    lower = _calc_sma_plain([c['low']   for c in candles], period)
    return upper, lower


def _detect_channel_signal(candles, upper, lower, i):
    """Segnale sulla candela i vs. il canale della candela i-1 (vedi docstring
    modulo). Ritorna None oppure {'type': 'strong'|'normal', 'dir': 'bull'|'bear'}."""
    u_prev, l_prev = upper[i - 1], lower[i - 1]
    if u_prev is None or l_prev is None:
        return None
    o, c = candles[i]['open'], candles[i]['close']
    sig_type = sig_dir = None
    if o < l_prev and c > u_prev:
        sig_type, sig_dir = 'strong', 'bull'
    elif o > u_prev and c < l_prev:
        sig_type, sig_dir = 'strong', 'bear'
    elif l_prev <= o <= u_prev and c > u_prev:
        sig_type, sig_dir = 'normal', 'bull'
    elif l_prev <= o <= u_prev and c < l_prev:
        sig_type, sig_dir = 'normal', 'bear'
    if sig_type is None:
        return None
    if i >= 2:
        u_prev2, l_prev2 = upper[i - 2], lower[i - 2]
        prev_close = candles[i - 1]['close']
        if u_prev2 is not None and l_prev2 is not None:
            if sig_dir == 'bull' and prev_close > u_prev2:
                return None
            if sig_dir == 'bear' and prev_close < l_prev2:
                return None
    return {'type': sig_type, 'dir': sig_dir}


# ── Motore breakout — replay bar-by-bar ──────────────────────────────────────

def run_engine(candles, params):
    """Ritorna (stato_finale, trades). `trades` è la lista di tutti i trade
    chiusi durante il replay (per il backtest); `stato_finale` è lo stato "adesso"
    (per il motore live, che lo confronta con la chiamata precedente)."""
    n = len(candles)
    upper_arr, lower_arr = _calc_channel(candles, params['period'])
    warmup = warmup_bars_for(params)

    st = dict(_EMPTY_ENGINE_STATE)
    trades = []
    pending = None  # dettagli del trade attualmente aperto — costruisce il record alla chiusura

    for i in range(n):
        is_warmed_up = i >= warmup

        # trigger — canale SMA(period) close/high/low, vedi _detect_channel_signal
        sig = None
        if not st['trade_open'] and is_warmed_up and i >= 2:
            sig = _detect_channel_signal(candles, upper_arr, lower_arr, i)

        # Entry/SL/TP1-3 INVARIATI: SL all'estremo opposto del canale al momento
        # del segnale, target = estremo rotto + altezza del canale (upper-lower
        # alla candela di segnale), TP1/TP2/TP3 a 1/3, 2/3, tutto il movimento.
        if sig is not None:
            u_prev, l_prev = upper_arr[i - 1], lower_arr[i - 1]
            ch_height = u_prev - l_prev
            entry = candles[i]['close']
            st['entry_price'] = entry
            st['break_strength'] = 'Strong' if sig['type'] == 'strong' else 'Normal'
            if sig['dir'] == 'bull':
                breakout_dir = 1
                sl_price = l_prev
                target_price = u_prev + ch_height
            else:
                breakout_dir = -1
                sl_price = u_prev
                target_price = l_prev - ch_height
            st['breakout_dir'], st['breakout_bar'] = breakout_dir, i
            st['sl_price'] = sl_price
            st['sl_price_orig'] = sl_price
            full_move = abs(target_price - entry)
            if breakout_dir == 1:
                st['tp1_price'] = entry + full_move / 3.0
                st['tp2_price'] = entry + full_move * 2.0 / 3.0
                st['tp3_price'] = entry + full_move
                side = 'long'
            else:
                st['tp1_price'] = entry - full_move / 3.0
                st['tp2_price'] = entry - full_move * 2.0 / 3.0
                st['tp3_price'] = entry - full_move
                side = 'short'
            st['tp1_hit'] = st['tp2_hit'] = st['tp3_hit'] = st['sl_hit'] = False
            st['trade_open'] = True
            pending = {
                'side': side, 'entry_time': candles[i]['time'], 'entry_price': st['entry_price'],
                'sl_orig': st['sl_price_orig'], 'tp1_price': st['tp1_price'], 'tp2_price': st['tp2_price'],
                'tp3_price': st['tp3_price'], 'strength': st['break_strength'],
            }

        # TP/SL hit detection
        if st['trade_open'] and st['breakout_dir'] != 0 and not st['sl_hit'] and i > st['breakout_bar']:
            hi, lo = candles[i]['high'], candles[i]['low']
            tp_side  = (hi >= st['tp1_price']) if st['breakout_dir'] == 1 else (lo <= st['tp1_price'])
            tp2_side = (hi >= st['tp2_price']) if st['breakout_dir'] == 1 else (lo <= st['tp2_price'])
            tp3_side = (hi >= st['tp3_price']) if st['breakout_dir'] == 1 else (lo <= st['tp3_price'])
            sl_side  = (lo <= st['sl_price'])  if st['breakout_dir'] == 1 else (hi >= st['sl_price'])
            if sl_side:
                st['sl_hit'] = True
            else:
                if tp_side and not st['tp1_hit']:
                    st['tp1_hit'] = True
                    st['sl_price'] = st['entry_price']  # breakeven
                if tp2_side and not st['tp2_hit']:
                    st['tp2_hit'] = True
                if tp3_side and not st['tp3_hit']:
                    st['tp3_hit'] = True

        # close trade (solo a SL o TP3 — nessuna chiusura parziale a TP1/TP2)
        if st['trade_open'] and (st['tp3_hit'] or st['sl_hit']):
            reason = ('be' if st['tp1_hit'] else 'sl') if st['sl_hit'] else 'tp3'
            exit_price = st['tp3_price'] if reason == 'tp3' else st['sl_price']
            st['closed_dir'] = st['breakout_dir']
            st['closed_reason'] = reason
            st['closed_bar'] = i
            st['closed_price'] = exit_price
            if pending is not None:
                trades.append({
                    **pending,
                    'exit_time': candles[i]['time'], 'exit_price': exit_price, 'exit_reason': reason,
                    'tp1_hit': st['tp1_hit'], 'tp2_hit': st['tp2_hit'],
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


def run_backtest(candles, params, initial_capital=1000.0, sizing=None, taker_fee_rate=0.00055):
    sizing = sizing or {'type': 'fixed', 'value': 50.0}
    _, raw_trades = run_engine(candles, params)

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
            'sl_orig': rt['sl_orig'], 'tp1_price': rt['tp1_price'], 'tp2_price': rt['tp2_price'], 'tp3_price': rt['tp3_price'],
            'strength': rt['strength'], 'tp1_hit': rt['tp1_hit'], 'tp2_hit': rt['tp2_hit'],
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
        if isinstance(loaded_state, dict) and 'trade_open' in loaded_state:
            self.state.update(loaded_state)
            self.signals = st.get('signals', [])
        else:
            # Stato persistito dalla vecchia strategia (Cross EMA) — forma incompatibile,
            # riparte da zero invece di crashare o mescolare segnali di due strategie diverse.
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
                'tp1': self.state['tp1_price'], 'tp2': self.state['tp2_price'], 'tp3': self.state['tp3_price'],
                'tp1_hit': self.state['tp1_hit'], 'tp2_hit': self.state['tp2_hit'],
                'strength': self.state['break_strength'],
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
        chiusa in più) e ne deriva gli eventi — replica l'ordine Pine: sullo stesso
        bar di un'entrata non si valuta nient'altro."""
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
                            'stop': new['sl_price_orig'], 'tp1': new['tp1_price'], 'tp2': new['tp2_price'],
                            'tp3': new['tp3_price'], 'strength': new['break_strength'],
                            'fresh': new['breakout_bar'] == i})
            return events

        if prev.get('trade_open') and new['trade_open']:
            side = 'long' if new['breakout_dir'] == 1 else 'short'
            if new['tp1_hit'] and not prev.get('tp1_hit'):
                events.append({'type': 'tp1_hit', 'side': side, 'time': t, 'price': new['tp1_price'],
                                'new_stop': new['sl_price']})
            if new['tp2_hit'] and not prev.get('tp2_hit'):
                events.append({'type': 'tp2_hit', 'side': side, 'time': t, 'price': new['tp2_price']})

        if prev.get('trade_open') and not new['trade_open'] and new['closed_bar'] == i:
            side = 'long' if new['closed_dir'] == 1 else 'short'
            events.append({'type': 'exit', 'reason': new['closed_reason'], 'side': side,
                            'time': t, 'price': new['closed_price']})

        return events

    def _on_kline(self, symbol, interval, candle, is_closed):
        if not self.running or not is_closed:
            return
        if symbol != self.symbol or interval != self.tf:
            return
        if not self._ws_manager:
            return

        klines = self._ws_manager.get_klines(symbol, interval)
        needed = warmup_bars_for(self.params) + 5
        if len(klines) < needed:
            return

        with self._lock:
            new_state, _ = run_engine(klines, self.params)
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
            elif ev['type'] == 'tp1_hit':
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
        # SL originale + TP3 come take-profit dell'exchange: TP1/TP2 sono solo
        # milestone informative che spostano lo SL a breakeven (vedi _execute_breakeven).
        ok, order_id, err = self._trade_client.place_order(
            symbol=symbol, side=side, qty=qty, leverage=self.leverage,
            stop_loss=ev['stop'], take_profit=ev['tp3'])
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
                f'🤖 BOT Breakout Pattern {tf_label} — {side_label} [{rec.get("strength", "—")}]',
                '',
                '------------------------------------------------',
                f'- Coin: {self.symbol}',
                f'- Modalità: {mode_label}',
                f'- Prezzo entrata: {rec["price"]}',
                f'- Stop Loss: {rec["stop"]:.6f}',
                f'- TP1: {rec["tp1"]:.6f}  TP2: {rec["tp2"]:.6f}  TP3: {rec["tp3"]:.6f}',
                '------------------------------------------------',
                '',
                f'<a href="https://www.bybit.com/trade/usdt/{self.symbol}">- View Bybit</a>',
            ]
        elif rec['type'] in ('tp1_hit', 'tp2_hit'):
            label = 'TP1 raggiunto — SL spostato a breakeven' if rec['type'] == 'tp1_hit' else 'TP2 raggiunto'
            lines = [
                f'🤖 BOT Breakout Pattern {tf_label} — {label}',
                '',
                '------------------------------------------------',
                f'- Coin: {self.symbol}',
                f'- Modalità: {mode_label}',
                f'- Prezzo: {rec["price"]}',
                '------------------------------------------------',
            ]
        else:
            reason_label = {'sl': 'Stop Loss', 'be': 'Breakeven (dopo TP1)',
                             'tp3': 'Take Profit 3 (target)'}.get(rec['reason'], rec['reason'])
            lines = [
                f'🤖 BOT Breakout Pattern {tf_label} — CHIUSURA',
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
