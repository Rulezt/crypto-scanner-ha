"""EMA Touch Scanner — 30m Timeframe, EMA 60 Focus (real-time via kline WebSocket)"""
import threading
import requests
from datetime import datetime, timedelta
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COOLDOWN_FILE = None  # set in __init__

# How many top coins to subscribe klines for
TOP_KLINE_SYMBOLS = 40
# Refresh kline subscription list every N seconds
KLINE_SUB_REFRESH = 4 * 3600


class EMAScanner:
    def __init__(self, telegram_config, enabled=True, ema_touch_threshold=2.0,
                 scan_interval_minutes=30, min_volume_24h=10000000,
                 max_coins_per_alert=10, screenshot_tf='30',
                 ws_manager=None, live_config=None,
                 schedule_start='', schedule_end='', utc_offset=2, **kwargs):

        self.telegram_token   = telegram_config['token']
        self.telegram_chat_id = telegram_config['chat_id']
        self.ha_url           = telegram_config.get('ha_url', '')
        self.enabled          = enabled
        self.ema_touch_threshold = ema_touch_threshold
        self.min_volume_24h   = min_volume_24h
        self.max_coins_per_alert = max_coins_per_alert
        self.screenshot_tf    = screenshot_tf
        self._live_config     = live_config

        self._setup_cooldown_path()
        self.last_alerts = self._load_cooldown()
        self._lock       = threading.Lock()

        self._ws_manager = ws_manager
        if ws_manager is not None:
            ws_manager.add_kline_callback(self._on_kline)
            threading.Thread(target=self._init_kline_subs, daemon=True).start()

        print(f'🎯 EMA Touch Scanner init — threshold={self.ema_touch_threshold}% ws={"on" if ws_manager else "off"}')

    # ── cooldown ─────────────────────────────────────────────────────────────

    def _setup_cooldown_path(self):
        global COOLDOWN_FILE
        for path in ['/config/ema_cooldown.json', '/share/ema_cooldown.json', '/data/ema_cooldown.json']:
            try:
                d = os.path.dirname(path)
                if os.path.exists(d):
                    tf = os.path.join(d, '.test_write')
                    with open(tf, 'w') as f: f.write('test')
                    os.remove(tf)
                    COOLDOWN_FILE = path
                    return
            except Exception:
                continue
        COOLDOWN_FILE = '/data/ema_cooldown.json'

    def _load_cooldown(self):
        try:
            if COOLDOWN_FILE and os.path.exists(COOLDOWN_FILE):
                with open(COOLDOWN_FILE, 'r') as f:
                    return {k: datetime.fromisoformat(v) for k, v in json.load(f).items()}
        except Exception as e:
            print(f'⚠️ Error loading EMA cooldown: {e}')
        return {}

    def _save_cooldown(self):
        try:
            if COOLDOWN_FILE:
                os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
                with open(COOLDOWN_FILE, 'w') as f:
                    json.dump({k: v.isoformat() for k, v in self.last_alerts.items()}, f)
        except Exception as e:
            print(f'⚠️ Error saving EMA cooldown: {e}')

    def is_in_cooldown(self, symbol):
        now = datetime.utcnow()
        current_day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if symbol not in self.last_alerts:
            return False
        last = self.last_alerts[symbol]
        return last.replace(hour=0, minute=0, second=0, microsecond=0) >= current_day_start

    def mark_alerted(self, symbol):
        self.last_alerts[symbol] = datetime.utcnow()
        self._save_cooldown()

    # ── schedule check ───────────────────────────────────────────────────────

    def _is_in_schedule(self):
        from alert_utils import is_in_schedule
        gen = (self._live_config or {}).get('general', {})
        return is_in_schedule(
            gen.get('schedule_start', ''),
            gen.get('schedule_end', ''),
            float(gen.get('utc_offset') or 2),
        )

    # ── kline subscription management ────────────────────────────────────────

    def _init_kline_subs(self):
        """Wait for WS ready, then subscribe 30m klines for top N coins."""
        if not self._ws_manager.ready.wait(timeout=120):
            print('⚠️ EMA: WS not ready after 120s, skipping kline subscription')
            return
        self._refresh_kline_subs()

    def _refresh_kline_subs(self):
        tickers = self._ws_manager.get_all_tickers()
        if not tickers:
            return
        ranked = sorted(tickers.items(), key=lambda x: x[1].get('volume_24h', 0), reverse=True)
        top_syms = [s for s, d in ranked
                    if d.get('volume_24h', 0) >= self.min_volume_24h][:TOP_KLINE_SYMBOLS]
        self._ws_manager.subscribe_klines(top_syms, intervals=['30'])
        print(f'🎯 EMA: subscribed 30m klines for {len(top_syms)} symbols')
        # Schedule next refresh
        threading.Timer(KLINE_SUB_REFRESH, self._refresh_kline_subs).start()

    # ── EMA calculation ───────────────────────────────────────────────────────

    @staticmethod
    def _calc_ema(prices, period):
        if len(prices) < period:
            return None
        mult = 2 / (period + 1)
        ema  = sum(prices[:period]) / period
        for p in prices[period:]:
            ema = (p - ema) * mult + ema
        return ema

    def _calc_ema_from_closes(self, closes):
        if len(closes) < 223:
            return None
        ema5   = self._calc_ema(closes, 5)
        ema10  = self._calc_ema(closes, 10)
        ema60  = self._calc_ema(closes, 60)
        ema223 = self._calc_ema(closes, 223)
        if ema60 is None:
            return None
        price = closes[-1]
        return {
            'current_price': price, 'ema60': ema60,
            'ema5': ema5, 'ema10': ema10, 'ema223': ema223,
            'distance_pct': abs((price - ema60) / ema60 * 100)
        }

    # ── real-time kline callback ──────────────────────────────────────────────

    def _on_kline(self, symbol, interval, candle, is_closed):
        """Called on every kline update from WebSocket (live + closed)."""
        if not self.enabled or interval != '30':
            return
        if not self._is_in_schedule():
            return

        klines = self._ws_manager.get_klines(symbol, '30')
        if len(klines) < 223:
            return  # Cache not seeded yet

        closes = [k['close'] for k in klines]
        ema_data = self._calc_ema_from_closes(closes)
        if not ema_data:
            return

        # Use live price from the current candle, not last closed close
        live_price   = candle['close']
        ema60        = ema_data['ema60']

        # Only alert when price approaches EMA from above (support bounce setup)
        # Skip if price is already below EMA (rebounding from below = not our setup)
        if live_price < ema60:
            return

        distance_pct = abs((live_price - ema60) / ema60 * 100)

        if distance_pct < self.ema_touch_threshold:
            with self._lock:
                if not self.is_in_cooldown(symbol):
                    self.mark_alerted(symbol)
                    coin = {
                        'symbol': symbol,
                        'price': live_price,
                        'ema60': ema60,
                        'distance_pct': distance_pct,
                        'approach': 'from above' if live_price > ema60 else 'from below',
                        'volume_24h': 0,
                    }
                    threading.Thread(
                        target=self.send_alert, args=([coin],), daemon=True).start()

    # ── REST kline + EMA (used by polling scan) ───────────────────────────────

    def fetch_klines_and_calculate_ema(self, symbol, interval='30', limit=250):
        # Try WS cache first
        if self._ws_manager:
            klines = self._ws_manager.get_klines(symbol, interval)
            if len(klines) >= 223:
                closes = [k['close'] for k in klines]
                return self._calc_ema_from_closes(closes)

        # Fall back to REST
        try:
            url    = 'https://api.bybit.com/v5/market/kline'
            params = {'category': 'linear', 'symbol': symbol,
                      'interval': interval, 'limit': limit}
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            if data['retCode'] != 0:
                return None
            klines = data['result']['list']
            if len(klines) < 223:
                return None
            klines.sort(key=lambda x: int(x[0]))
            closes = [float(k[4]) for k in klines]
            return self._calc_ema_from_closes(closes)
        except Exception as e:
            print(f'❌ EMA: fetch_klines {symbol}: {e}')
            return None

    # ── polling scan (fallback / manual) ─────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return []

        print(f'🎯 EMA Touch Scanner — polling scan ({self.ema_touch_threshold}% threshold)...')
        try:
            if self._ws_manager and self._ws_manager.ready.is_set():
                raw = self._ws_manager.get_all_tickers()
                all_pairs = [
                    {'item': {'symbol': s, 'volume24h': str(d.get('volume_24h', 0))},
                     'change_pct': d.get('change_24h', 0)}
                    for s, d in raw.items()
                    if d.get('price', 0) > 0 and d.get('volume_24h', 0) >= self.min_volume_24h
                ]
            else:
                url = 'https://api.bybit.com/v5/market/tickers?category=linear'
                response = requests.get(url, timeout=10)
                data = response.json()
                if data['retCode'] != 0:
                    return []
                all_pairs = []
                for item in data['result']['list']:
                    if not item['symbol'].endswith('USDT'):
                        continue
                    vol_usd = float(item.get('volume24h', 0)) * float(item.get('lastPrice', 0))
                    if vol_usd < self.min_volume_24h:
                        continue
                    all_pairs.append({'item': item,
                                      'change_pct': float(item.get('price24hPcnt', 0)) * 100})

            all_pairs.sort(key=lambda x: x['change_pct'], reverse=True)
            change_pct_map   = {p['item']['symbol']: p['change_pct'] for p in all_pairs}
            pairs_to_analyze = [p['item'] for p in all_pairs[:10]] + \
                               [p['item'] for p in all_pairs[-10:]]

            found = []
            for pair in pairs_to_analyze:
                symbol   = pair['symbol']
                ema_data = self.fetch_klines_and_calculate_ema(symbol, interval='30', limit=250)
                if not ema_data:
                    continue
                if ema_data['distance_pct'] < self.ema_touch_threshold:
                    with self._lock:
                        if not self.is_in_cooldown(symbol):
                            self.mark_alerted(symbol)
                            found.append({
                                'symbol': symbol,
                                'price': ema_data['current_price'],
                                'ema60': ema_data['ema60'],
                                'distance_pct': ema_data['distance_pct'],
                                'approach': 'from above' if ema_data['current_price'] > ema_data['ema60'] else 'from below',
                                'volume_24h': float(pair.get('volume24h', 0)),
                                'change_pct': change_pct_map.get(symbol, 0.0),
                            })

            found = found[:self.max_coins_per_alert]
            if found:
                self.send_alert(found)

            return found

        except Exception as e:
            print(f'❌ EMA scanner error: {e}')
            return []

    def send_alert(self, coins):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, build_caption
        except ImportError:
            return
        for coin in coins[:3]:
            sym     = coin['symbol']
            dir_str = 'da sotto' if 'below' in coin['approach'] else 'da sopra'
            note    = f"EMA60 30m {dir_str} {coin['distance_pct']:.2f}%"
            caption = build_caption(sym, coin.get('change_pct', 0.0), note, self.ha_url)
            img = get_chart(sym, interval=self.screenshot_tf, signal={'type': 'ema'})
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
            print(f'EMA alert: {sym}')
