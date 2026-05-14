"""
BybitWSManager — Real-time Bybit data via WebSocket.

Ticker stream:  wss://stream.bybit.com/v5/public/linear → tickers.{symbol}
Kline stream:   same endpoint                           → kline.{interval}.{symbol}

Usage:
    mgr = BybitWSManager()
    mgr.add_tick_callback(fn)          # fn(symbol, ticker_dict)
    mgr.add_kline_callback(fn)         # fn(symbol, interval, candle_dict, is_closed)
    mgr.subscribe_klines(['BTCUSDT'], intervals=['30'])
    mgr.start()
"""
import json
import time
import threading
import logging
import requests

logger = logging.getLogger(__name__)

BYBIT_WS_URL = 'wss://stream.bybit.com/v5/public/linear'
TOP_N_TICKERS = 200


class BybitWSManager:

    def __init__(self):
        self._tickers = {}          # {symbol: {price, change_24h, volume_24h, ...}}
        self._klines  = {}          # {(symbol, interval): [candle, ...]}  oldest-first
        self._lock    = threading.RLock()

        self._tick_callbacks  = []  # fn(symbol, data)
        self._kline_callbacks = []  # fn(symbol, interval, candle, is_closed)

        self._pending_topics = set()
        self._ws    = None
        self._running = False
        self.ready  = threading.Event()  # set once ≥10 tickers received

    # ── public ───────────────────────────────────────────────────────────────

    def get_ticker(self, symbol):
        with self._lock:
            return dict(self._tickers.get(symbol, {}))

    def get_all_tickers(self):
        with self._lock:
            return {s: dict(d) for s, d in self._tickers.items()}

    def get_klines(self, symbol, interval):
        with self._lock:
            return list(self._klines.get((symbol, str(interval)), []))

    def add_tick_callback(self, fn):
        self._tick_callbacks.append(fn)

    def add_kline_callback(self, fn):
        self._kline_callbacks.append(fn)

    def subscribe_klines(self, symbols, intervals=('30',)):
        """Subscribe to kline streams and pre-seed cache from REST."""
        new_topics = []
        for sym in symbols:
            for iv in map(str, intervals):
                topic = f'kline.{iv}.{sym}'
                if topic not in self._pending_topics:
                    self._pending_topics.add(topic)
                    new_topics.append(topic)
                    threading.Thread(
                        target=self._seed_klines, args=(sym, iv), daemon=True).start()
        if new_topics and self._ws and self._running:
            self._send_sub(new_topics)

    def start(self):
        threading.Thread(target=self._run_forever, daemon=True).start()
        logger.info('WS manager started')

    # ── internal ─────────────────────────────────────────────────────────────

    def _run_forever(self):
        while True:
            try:
                self._connect()
            except Exception as e:
                logger.error(f'WS loop error: {e}')
            self._running = False
            self.ready.clear()
            logger.info('WS reconnecting in 5s...')
            time.sleep(5)

    def _connect(self):
        try:
            import websocket as ws_lib
        except ImportError:
            logger.error('websocket-client not installed — pip install websocket-client')
            time.sleep(60)
            return

        symbols = self._fetch_top_symbols()
        for s in symbols:
            self._pending_topics.add(f'tickers.{s}')

        ws = ws_lib.WebSocketApp(
            BYBIT_WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws
        ws.run_forever(ping_interval=20, ping_timeout=10)

    def _fetch_top_symbols(self):
        try:
            r = requests.get('https://api.bybit.com/v5/market/tickers',
                             params={'category': 'linear'}, timeout=10)
            data = r.json()
            if data.get('retCode') != 0:
                return []
            pairs = []
            for item in data['result']['list']:
                sym = item.get('symbol', '')
                if not sym.endswith('USDT'):
                    continue
                price = float(item.get('lastPrice', 0) or 0)
                vol   = float(item.get('volume24h', 0) or 0) * price
                pairs.append((sym, vol))
            pairs.sort(key=lambda x: x[1], reverse=True)
            syms = [s for s, _ in pairs[:TOP_N_TICKERS]]
            logger.info(f'WS: subscribing to {len(syms)} ticker streams')
            return syms
        except Exception as e:
            logger.error(f'WS: _fetch_top_symbols: {e}')
            return []

    def _seed_klines(self, symbol, interval, limit=300):
        """Pre-fill kline cache from REST so EMA can be computed immediately."""
        try:
            r = requests.get(
                'https://api.bybit.com/v5/market/kline',
                params={'category': 'linear', 'symbol': symbol,
                        'interval': interval, 'limit': limit},
                timeout=10)
            data = r.json()
            if data.get('retCode') != 0:
                return
            candles = []
            for k in reversed(data['result']['list']):
                candles.append({
                    'time':   int(k[0]) // 1000,
                    'open':   float(k[1]),
                    'high':   float(k[2]),
                    'low':    float(k[3]),
                    'close':  float(k[4]),
                    'volume': float(k[5]),
                })
            with self._lock:
                self._klines[(symbol, interval)] = candles
            logger.debug(f'WS: seeded {len(candles)} klines {symbol}/{interval}')
        except Exception as e:
            logger.warning(f'WS: seed_klines {symbol}/{interval}: {e}')

    def _send_sub(self, topics):
        if not self._ws or not self._running:
            return
        for i in range(0, len(topics), 10):
            try:
                self._ws.send(json.dumps({'op': 'subscribe', 'args': topics[i:i+10]}))
            except Exception as e:
                logger.warning(f'WS: send_sub error: {e}')

    def _on_open(self, ws):
        self._running = True
        topics = list(self._pending_topics)
        logger.info(f'WS connected — subscribing {len(topics)} topics')
        self._send_sub(topics)

    def _on_message(self, ws, message):
        try:
            data  = json.loads(message)
            topic = data.get('topic', '')

            if topic.startswith('tickers.'):
                self._handle_ticker(topic[8:], data.get('data', {}))
            elif topic.startswith('kline.'):
                parts = topic.split('.')
                if len(parts) == 3:
                    self._handle_kline(parts[2], parts[1], data.get('data', []))
        except Exception as e:
            logger.error(f'WS: on_message: {e}')

    def _handle_ticker(self, symbol, payload):
        if not payload:
            return
        update = {'ts': time.time()}
        if payload.get('lastPrice'):
            update['price'] = float(payload['lastPrice'])
        if payload.get('price24hPcnt') is not None:
            update['change_24h'] = round(float(payload['price24hPcnt'] or 0) * 100, 4)
        if payload.get('volume24h') and payload.get('lastPrice'):
            update['volume_24h'] = float(payload['volume24h']) * float(payload['lastPrice'])
        if payload.get('prevPrice24h'):
            update['prev_price24h'] = float(payload['prevPrice24h'])
        if payload.get('highPrice24h'):
            update['high_24h'] = float(payload['highPrice24h'])
        if payload.get('lowPrice24h'):
            update['low_24h'] = float(payload['lowPrice24h'])

        with self._lock:
            if symbol not in self._tickers:
                self._tickers[symbol] = {}
            self._tickers[symbol].update(update)
            current = dict(self._tickers[symbol])
            if not self.ready.is_set() and len(self._tickers) >= 10:
                self.ready.set()
                logger.info('WS: ticker cache ready')

        for cb in self._tick_callbacks:
            try:
                cb(symbol, current)
            except Exception as e:
                logger.error(f'WS: tick callback error: {e}')

    def _handle_kline(self, symbol, interval, data_list):
        for item in data_list:
            candle = {
                'time':   int(item['start']) // 1000,
                'open':   float(item['open']),
                'high':   float(item['high']),
                'low':    float(item['low']),
                'close':  float(item['close']),
                'volume': float(item['volume']),
            }
            is_closed = bool(item.get('confirm', False))
            key = (symbol, interval)

            with self._lock:
                buf = self._klines.get(key, [])
                if buf and buf[-1]['time'] == candle['time']:
                    buf[-1] = candle
                else:
                    buf.append(candle)
                    if len(buf) > 500:
                        buf = buf[-500:]
                self._klines[key] = buf

            for cb in self._kline_callbacks:
                try:
                    cb(symbol, interval, candle, is_closed)
                except Exception as e:
                    logger.error(f'WS: kline callback error: {e}')

    def _on_error(self, ws, error):
        logger.error(f'WS error: {error}')

    def _on_close(self, ws, code, msg):
        logger.warning(f'WS closed: {code} {msg}')
        self._running = False
        self.ready.clear()
