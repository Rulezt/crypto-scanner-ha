"""
BybitPrivateWSPool — real-time position/order updates via Bybit's private
websocket (wss://stream.bybit.com/v5/private), one connection per logged-in
user (each user trades with their own API key/secret).

Read-only: subscribes to "position" and "order" topics, never places orders.
Feeds an in-memory snapshot per user + a pub/sub fanout (Queue per listener)
so the Flask SSE endpoint (/api/trade/stream) can push updates to the browser
the instant Bybit reports a fill/close, instead of waiting on REST polling.

Usage:
    pool = BybitPrivateWSPool()
    conn = pool.ensure(username, api_key, api_secret)
    q = conn.add_listener()
    snap = conn.snapshot(symbol)          # {'position': {...} | None, 'orders': [...]}
    ...
    conn.remove_listener(q)
"""
import json
import time
import hmac
import hashlib
import threading
import queue
import logging

logger = logging.getLogger(__name__)

BYBIT_PRIVATE_WS_URL = 'wss://stream.bybit.com/v5/private'
IDLE_TIMEOUT_S = 600  # close a user's private WS after 10min with no SSE listeners


class _UserConn:
    def __init__(self, username, api_key, api_secret):
        self.username = username
        self._key = api_key
        self._secret = api_secret

        self._lock = threading.RLock()
        self.positions = {}   # symbol -> position dict (absent = flat)
        self.orders = {}      # orderId -> order dict (open orders only)

        self._listeners = []  # list[Queue]
        self._ws = None
        self._running = False
        self._stop = False
        self.last_touch = time.time()
        self.authed = threading.Event()

    # ── public ───────────────────────────────────────────────────────────────

    def touch(self):
        self.last_touch = time.time()

    def idle_seconds(self):
        with self._lock:
            has_listeners = bool(self._listeners)
        return 0 if has_listeners else time.time() - self.last_touch

    @property
    def alive(self):
        return self._running and not self._stop

    def add_listener(self):
        q = queue.Queue(maxsize=100)
        with self._lock:
            self._listeners.append(q)
        self.touch()
        return q

    def remove_listener(self, q):
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)
        self.touch()

    def snapshot(self, symbol):
        with self._lock:
            pos = self.positions.get(symbol)
            orders = [o for o in self.orders.values() if o.get('symbol') == symbol]
            return {'position': dict(pos) if pos else None, 'orders': orders}

    def start(self):
        self._stop = False
        threading.Thread(target=self._run_forever, daemon=True).start()

    def stop(self):
        self._stop = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # ── internal ─────────────────────────────────────────────────────────────

    def _emit(self, event):
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def _run_forever(self):
        while not self._stop:
            try:
                self._connect()
            except Exception as e:
                logger.error(f'PrivateWS[{self.username}] loop error: {e}')
            self._running = False
            self.authed.clear()
            if self._stop:
                return
            time.sleep(5)

    def _connect(self):
        try:
            import websocket as ws_lib
        except ImportError:
            logger.error('websocket-client not installed')
            time.sleep(60)
            return
        ws = ws_lib.WebSocketApp(
            BYBIT_PRIVATE_WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws
        ws.run_forever(ping_interval=20, ping_timeout=10)

    def _on_open(self, ws):
        self._running = True
        expires = int(time.time() * 1000) + 10000
        sign_payload = f'GET/realtime{expires}'
        sig = hmac.new(self._secret.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()
        try:
            ws.send(json.dumps({'op': 'auth', 'args': [self._key, expires, sig]}))
        except Exception as e:
            logger.error(f'PrivateWS[{self.username}] auth send error: {e}')

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except Exception:
            return
        op = data.get('op')
        if op == 'auth':
            if data.get('success'):
                self.authed.set()
                try:
                    ws.send(json.dumps({'op': 'subscribe', 'args': ['position', 'order']}))
                except Exception as e:
                    logger.error(f'PrivateWS[{self.username}] subscribe error: {e}')
            else:
                logger.error(f'PrivateWS[{self.username}] auth failed: {data}')
            return
        topic = data.get('topic', '')
        if topic == 'position':
            self._handle_position(data.get('data', []))
        elif topic == 'order':
            self._handle_order(data.get('data', []))

    def _handle_position(self, items):
        # Un singolo push del topic "position" può contenere PIÙ righe per lo stesso
        # symbol (es. hedge-mode: positionIdx 1=Buy/2=Sell come slot indipendenti, o
        # un simbolo storicamente tradato ma ora flat incluso nello snapshot iniziale).
        # Processare le righe una a una e sovrascrivere self.positions[sym] ad ogni
        # riga è un bug: una riga size=0 relativa a UN slot poteva azzerare in memoria
        # una posizione reale ancora aperta su un altro slot dello stesso symbol,
        # emettendo un falso "posizione chiusa" — causa sospetta di un reset spurio
        # della UI SL/TP con la posizione reale ancora intatta su Bybit. Fix: raggruppa
        # per symbol PRIMA di decidere, e considera il symbol flat solo se TUTTE le
        # righe di questo batch per quel symbol hanno size=0.
        def _fv(v):
            return float(v) if v and v != '0' else None
        by_symbol = {}
        for p in items:
            sym = p.get('symbol')
            if not sym:
                continue
            by_symbol.setdefault(sym, []).append(p)

        for sym, rows in by_symbol.items():
            nonzero = [p for p in rows if float(p.get('size', 0) or 0) != 0]
            with self._lock:
                if not nonzero:
                    self.positions.pop(sym, None)
                    parsed = None
                else:
                    p = nonzero[-1]
                    parsed = {
                        'side': p.get('side'), 'size': float(p.get('size', 0) or 0),
                        'entryPrice': float(p.get('entryPrice') or p.get('avgPrice') or 0),
                        'leverage': float(p.get('leverage') or 0),
                        'unrealizedPnl': float(p.get('unrealisedPnl', 0) or 0),
                        'stopLoss': _fv(p.get('stopLoss')), 'takeProfit': _fv(p.get('takeProfit')),
                        'positionIdx': int(p.get('positionIdx', 0) or 0),
                        'markPrice': float(p.get('markPrice', 0) or 0), 'liqPrice': _fv(p.get('liqPrice')),
                    }
                    self.positions[sym] = parsed
            self._emit({'type': 'position', 'symbol': sym, 'position': parsed})

    def _handle_order(self, items):
        OPEN_STATUSES = {'New', 'PartiallyFilled', 'Untriggered'}
        for o in items:
            sym = o.get('symbol')
            oid = o.get('orderId')
            if not sym or not oid:
                continue
            status = o.get('orderStatus')
            with self._lock:
                if status in OPEN_STATUSES:
                    self.orders[oid] = {
                        'orderId': oid, 'symbol': sym, 'side': o.get('side'),
                        'orderType': o.get('orderType'), 'qty': float(o.get('qty', 0) or 0),
                        'price': float(o.get('price', 0) or 0), 'status': status,
                        'reduceOnly': bool(o.get('reduceOnly')),
                        'triggerPrice': float(o.get('triggerPrice', 0) or 0) or None,
                    }
                else:
                    self.orders.pop(oid, None)
            self._emit({'type': 'orders', 'symbol': sym, 'orders': self.snapshot(sym)['orders']})

    def _on_error(self, ws, error):
        logger.warning(f'PrivateWS[{self.username}] error: {error}')

    def _on_close(self, ws, code, msg):
        self._running = False
        self.authed.clear()


class BybitPrivateWSPool:
    def __init__(self):
        self._conns = {}   # username -> _UserConn
        self._lock = threading.RLock()
        threading.Thread(target=self._reap_loop, daemon=True).start()

    def ensure(self, username, api_key, api_secret):
        with self._lock:
            conn = self._conns.get(username)
            if conn is not None and not conn.alive:
                conn = None
            if conn is None:
                conn = _UserConn(username, api_key, api_secret)
                self._conns[username] = conn
                conn.start()
            else:
                conn.touch()
            return conn

    def _reap_loop(self):
        while True:
            time.sleep(60)
            with self._lock:
                stale = [u for u, c in self._conns.items() if c.idle_seconds() > IDLE_TIMEOUT_S]
                for u in stale:
                    try:
                        self._conns[u].stop()
                    except Exception:
                        pass
                    del self._conns[u]
                    logger.info(f'PrivateWS[{u}] closed (idle)')
