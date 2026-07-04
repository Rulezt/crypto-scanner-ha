"""BOT — Cross EMA Vincente (Pine Script strategy port)

Incrocio SMA "Lenta" (default periodo 10) / SMA "Veloce" (default periodo 60),
filtrato da una SMA "Filtro" (default periodo 200), con filtro opzionale
"chiudi se le prime due candele dopo l'entrata vanno contro" e SL/TP fissi in %.

Le etichette dei gruppi ricalcano fedelmente lo script Pine originale (nel quale
"SMA Lenta" ha periodo 10 e "SMA Veloce" ha periodo 60 — invertito rispetto a
quanto suggerirebbero i nomi, ma mantenuto identico per fedeltà alla strategia).

`step()` è l'unica funzione che decide entrate/uscite bar-by-bar: viene usata
sia dal backtest (`run_backtest`) sia dal motore live (`BotEngine`), così i due
condividono esattamente la stessa logica.
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
    'sma_lenta_period': 10, 'sma_lenta_source': 'close',
    'sma_veloce_period': 60, 'sma_veloce_source': 'close',
    'filter_enabled': True, 'filter_period': 200, 'filter_source': 'close',
    'candle_filter_enabled': False,
    'sl_pct': 1.0, 'tp_pct': 1.7,
}

_EMPTY_POSITION_STATE = {'position': None, 'entry_time': None, 'entry_price': None,
                          'stop': None, 'take': None}


def normalize_params(cfg):
    cfg = cfg or {}
    p = dict(DEFAULT_PARAMS)
    for k in DEFAULT_PARAMS:
        if k in cfg:
            p[k] = cfg[k]
    p['sma_lenta_period']  = int(p['sma_lenta_period'])
    p['sma_veloce_period'] = int(p['sma_veloce_period'])
    p['filter_period']     = int(p['filter_period'])
    p['filter_enabled']    = bool(p['filter_enabled'])
    p['candle_filter_enabled'] = bool(p['candle_filter_enabled'])
    p['sl_pct'] = float(p['sl_pct'])
    p['tp_pct'] = float(p['tp_pct'])
    return p


# ── Strategy core (condiviso da backtest e live) ────────────────────────────

def price_of(candle, source):
    o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
    if source == 'open':  return o
    if source == 'high':  return h
    if source == 'low':   return l
    if source == 'hl2':   return (h + l) / 2
    if source == 'hlc3':  return (h + l + c) / 3
    if source == 'ohlc4': return (o + h + l + c) / 4
    return c  # 'close' e fallback


def sma_series(candles, period, source):
    """Serie SMA allineata a `candles` (rolling sum, O(n)). None finché non ci sono
    abbastanza barre."""
    n = len(candles)
    out = [None] * n
    if period <= 0 or n < period:
        return out
    vals = [price_of(c, source) for c in candles]
    s = sum(vals[:period])
    out[period - 1] = s / period
    for i in range(period, n):
        s += vals[i] - vals[i - period]
        out[i] = s / period
    return out


def crossover(prev_a, prev_b, a, b):
    return (prev_a is not None and prev_b is not None and a is not None and b is not None
            and prev_a <= prev_b and a > b)


def crossunder(prev_a, prev_b, a, b):
    return (prev_a is not None and prev_b is not None and a is not None and b is not None
            and prev_a >= prev_b and a < b)


def step(state, params, prev_s1, s1, prev_s2, s2, sfilt, candle, interval_seconds):
    """Transizione di un singolo bar chiuso. Ritorna (nuovo_state, eventi).

    Replica l'ordine esatto dello script Pine:
      1. se flat: valuta entrata (crossover/crossunder + filtro SMA200) — se scatta,
         niente altro viene controllato su questo stesso bar (Pine non valuta il
         filtro-candele/SL/TP sul bar di entrata).
      2. se in posizione: filtro-candele SOLO su entry_time+1*iv o +2*iv (if/else-if,
         il primo che matcha vince — esattamente come il codice Pine).
      3. se ancora in posizione: SL/TP contro high/low del bar. Se un bar tocca
         entrambi i livelli, si risolve lo Stop Loss per primo (assunzione
         conservativa dichiarata — non derivabile da OHLC puro).
    """
    state = dict(state)
    events = []
    t = candle['time']

    if state['position'] is None:
        long_cond  = crossover(prev_s1, prev_s2, s1, s2)
        short_cond = crossunder(prev_s1, prev_s2, s1, s2)
        if params['filter_enabled']:
            filt_ok_long  = sfilt is not None and s1 is not None and s2 is not None and s1 > sfilt and s2 > sfilt
            filt_ok_short = sfilt is not None and s1 is not None and s2 is not None and s1 < sfilt and s2 < sfilt
            long_cond  = long_cond and filt_ok_long
            short_cond = short_cond and filt_ok_short

        if long_cond or short_cond:
            side  = 'long' if long_cond else 'short'
            price = candle['close']
            sl = params['sl_pct'] / 100.0
            tp = params['tp_pct'] / 100.0
            stop = price * (1 - sl) if side == 'long' else price * (1 + sl)
            take = price * (1 + tp) if side == 'long' else price * (1 - tp)
            state.update(position=side, entry_time=t, entry_price=price, stop=stop, take=take)
            events.append({'type': 'entry', 'side': side, 'time': t, 'price': price,
                            'stop': stop, 'take': take})
        return state, events

    # ── in posizione ────────────────────────────────────────────────────────
    side     = state['position']
    entry_t  = state['entry_time']
    entry_p  = state['entry_price']

    if params['candle_filter_enabled']:
        against = (side == 'long' and candle['close'] < entry_p) or \
                  (side == 'short' and candle['close'] > entry_p)
        if t == entry_t + interval_seconds:
            if against:
                events.append({'type': 'exit', 'reason': 'candle_filter_1', 'side': side,
                                'time': t, 'price': candle['close']})
                return dict(_EMPTY_POSITION_STATE), events
        elif t == entry_t + 2 * interval_seconds:
            if against:
                events.append({'type': 'exit', 'reason': 'candle_filter_2', 'side': side,
                                'time': t, 'price': candle['close']})
                return dict(_EMPTY_POSITION_STATE), events

    stop, take = state['stop'], state['take']
    hit_stop = (side == 'long' and candle['low'] <= stop) or (side == 'short' and candle['high'] >= stop)
    hit_take = (side == 'long' and candle['high'] >= take) or (side == 'short' and candle['low'] <= take)
    if hit_stop or hit_take:
        if hit_stop:
            reason, price = 'sl', stop
        else:
            reason, price = 'tp', take
        events.append({'type': 'exit', 'reason': reason, 'side': side, 'time': t, 'price': price})
        return dict(_EMPTY_POSITION_STATE), events

    return state, events


def simulate(candles, params):
    """Replay completo su tutta la history. Ritorna (eventi, stato_finale)."""
    n = len(candles)
    s1 = sma_series(candles, params['sma_lenta_period'], params['sma_lenta_source'])
    s2 = sma_series(candles, params['sma_veloce_period'], params['sma_veloce_source'])
    sf = (sma_series(candles, params['filter_period'], params['filter_source'])
          if params['filter_enabled'] else [None] * n)

    start_idx = max(params['sma_lenta_period'], params['sma_veloce_period'],
                     params['filter_period'] if params['filter_enabled'] else 0)
    iv_s = params.get('_interval_seconds', 3600)

    state = dict(_EMPTY_POSITION_STATE)
    all_events = []
    for i in range(max(1, start_idx), n):
        state, events = step(state, params, s1[i - 1], s1[i], s2[i - 1], s2[i], sf[i],
                              candles[i], iv_s)
        all_events.extend(events)
    return all_events, state


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


def run_backtest(candles, params, initial_capital=1000.0, sizing=None):
    sizing = sizing or {'type': 'fixed', 'value': 50.0}
    events, _ = simulate(candles, params)

    trades = []
    equity = float(initial_capital)
    equity_curve = [{'time': candles[0]['time'] if candles else 0, 'equity': round(equity, 4)}]
    pending_entry = None

    for ev in events:
        if ev['type'] == 'entry':
            pending_entry = ev
        elif ev['type'] == 'exit' and pending_entry:
            side        = pending_entry['side']
            entry_price = pending_entry['price']
            exit_price  = ev['price']
            direction   = 1 if side == 'long' else -1
            pnl_pct     = direction * (exit_price - entry_price) / entry_price * 100.0

            if sizing.get('type') == 'pct_balance':
                notional = equity * (float(sizing.get('value', 0)) / 100.0)
            else:
                notional = float(sizing.get('value', 50.0))
            pnl_usdt = notional * (pnl_pct / 100.0)
            equity  += pnl_usdt

            trades.append({
                'side': side, 'entry_time': pending_entry['time'], 'entry_price': entry_price,
                'exit_time': ev['time'], 'exit_price': exit_price, 'exit_reason': ev['reason'],
                'pnl_pct': round(pnl_pct, 4), 'pnl_usdt': round(pnl_usdt, 4),
                'notional': round(notional, 2),
            })
            equity_curve.append({'time': ev['time'], 'equity': round(equity, 4)})
            pending_entry = None

    total   = len(trades)
    wins    = [tr for tr in trades if tr['pnl_usdt'] > 0]
    losses  = [tr for tr in trades if tr['pnl_usdt'] <= 0]
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

        st = self._load_state()
        self.running = bool(st.get('running', False))
        self.state   = st.get('position_state') or dict(_EMPTY_POSITION_STATE)
        self.signals = st.get('signals', [])

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
            self.state = dict(_EMPTY_POSITION_STATE)
            self._save_state()
        if self._ws_manager:
            self._ws_manager.subscribe_klines([self.symbol], intervals=[self.tf])
        return True, None

    def stop(self, close_position=False):
        result = {'success': True}
        with self._lock:
            self.running = False
            if close_position and self.mode == 'execution' and self.state.get('position') and self._trade_client:
                pos = self._trade_client.get_position(self.symbol)
                if pos:
                    ok, err = self._trade_client.close_position(self.symbol, pos['side'], pos['size'])
                    result['closed'] = ok
                    if err:
                        result['error'] = err
                self.state = dict(_EMPTY_POSITION_STATE)
            self._save_state()
        return result

    def status(self):
        live_pos = None
        if self.mode == 'execution' and self._trade_client and self.symbol:
            live_pos = self._trade_client.get_position(self.symbol)
        return {
            'running': self.running, 'mode': self.mode, 'symbol': self.symbol, 'tf': self.tf,
            'position': dict(self.state) if self.state.get('position') else None,
            'exchange_position': live_pos,
            'last_signal_time': self.signals[-1]['time'] if self.signals else None,
        }

    def get_signals(self, limit=100):
        return self.signals[-limit:]

    # ── WS callback ──────────────────────────────────────────────────────────

    def _on_kline(self, symbol, interval, candle, is_closed):
        if not self.running or not is_closed:
            return
        if symbol != self.symbol or interval != self.tf:
            return
        if not self._ws_manager:
            return

        klines = self._ws_manager.get_klines(symbol, interval)
        p = self.params
        needed = max(p['sma_lenta_period'], p['sma_veloce_period'],
                     p['filter_period'] if p['filter_enabled'] else 0) + 2
        if len(klines) < needed:
            return

        s1 = sma_series(klines, p['sma_lenta_period'], p['sma_lenta_source'])
        s2 = sma_series(klines, p['sma_veloce_period'], p['sma_veloce_source'])
        sf = (sma_series(klines, p['filter_period'], p['filter_source'])
              if p['filter_enabled'] else [None] * len(klines))
        i = len(klines) - 1
        iv_s = TF_SECONDS.get(interval, 3600)

        with self._lock:
            new_state, events = step(self.state, p, s1[i - 1], s1[i], s2[i - 1], s2[i],
                                      sf[i], klines[i], iv_s)
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
                self._execute_entry(ev)
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
            return

        side = 'Buy' if ev['side'] == 'long' else 'Sell'
        ok, order_id, err = self._trade_client.place_order(
            symbol=symbol, side=side, qty=qty, leverage=self.leverage,
            stop_loss=ev['stop'], take_profit=ev['take'])
        if not ok:
            print(f'❌ BOT order failed: {err}')

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
                f'🤖 BOT Cross EMA {tf_label} — {side_label}',
                '',
                '------------------------------------------------',
                f'- Coin: {self.symbol}',
                f'- Modalità: {mode_label}',
                f'- Prezzo entrata: {rec["price"]}',
                f'- Stop Loss: {rec["stop"]:.6f}',
                f'- Take Profit: {rec["take"]:.6f}',
                '------------------------------------------------',
                '',
                f'<a href="https://www.bybit.com/trade/usdt/{self.symbol}">- View Bybit</a>',
            ]
        else:
            reason_label = {'sl': 'Stop Loss', 'tp': 'Take Profit',
                             'candle_filter_1': 'Filtro 1a candela',
                             'candle_filter_2': 'Filtro 2a candela'}.get(rec['reason'], rec['reason'])
            lines = [
                f'🤖 BOT Cross EMA {tf_label} — CHIUSURA',
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
