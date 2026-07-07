"""Shimano Scanner — distanza % tra EMA60 ed EMA223 sotto una soglia configurabile"""
import threading
import requests
import time
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COOLDOWN_FILE     = '/data/shimano_cooldown.json'
TOP_KLINE_SYMBOLS = 500
KLINE_SUB_REFRESH = 3600
MIN_KLINES        = 226  # margine oltre 223 per una EMA223 stabile


class ShimanoScanner:
    def __init__(self, telegram_config, enabled=True,
                 scan_tf=None, min_volume_24h=10_000_000,
                 scan_interval_minutes=240, cooldown_hours=24,
                 max_coins_per_alert=5, distance_threshold_pct=1.0,
                 ws_manager=None, live_config=None, **kwargs):

        self.telegram_token        = telegram_config['token']
        self.telegram_chat_id      = telegram_config['chat_id']
        self.base_url              = telegram_config.get('base_url', '')
        self.enabled               = enabled
        self.distance_threshold_pct = distance_threshold_pct
        raw_tf                     = scan_tf if scan_tf is not None else 'D'
        self.scan_tfs              = raw_tf if isinstance(raw_tf, list) else [raw_tf]
        self.min_volume_24h        = min_volume_24h
        self.max_coins_per_alert   = max_coins_per_alert
        self.cooldown_hours        = cooldown_hours
        self._live_config          = live_config

        self.last_alerts = self._load_cooldown()
        self._lock       = threading.Lock()
        self._last_count = 0
        self._ws_manager = ws_manager

        if ws_manager is not None:
            ws_manager.add_kline_callback(self._on_kline)
            threading.Thread(target=self._init_kline_subs, daemon=True).start()

        mode = 'WebSocket' if ws_manager else 'polling'
        print(f'🎣 Shimano Scanner init — tf={",".join(self.scan_tfs)} soglia={distance_threshold_pct}% mode={mode}')

    # ── cooldown ──────────────────────────────────────────────────────────────

    def _load_cooldown(self):
        try:
            if os.path.exists(COOLDOWN_FILE):
                with open(COOLDOWN_FILE, 'r') as f:
                    return {k: datetime.fromisoformat(v) for k, v in json.load(f).items()}
        except Exception as e:
            print(f'⚠️ Shimano: load cooldown: {e}')
        return {}

    def _save_cooldown(self):
        try:
            os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
            with open(COOLDOWN_FILE, 'w') as f:
                json.dump({k: v.isoformat() for k, v in self.last_alerts.items()}, f)
        except Exception as e:
            print(f'⚠️ Shimano: save cooldown: {e}')

    def is_in_cooldown(self, key):
        if key not in self.last_alerts:
            return False
        return (datetime.now() - self.last_alerts[key]) < timedelta(hours=self.cooldown_hours)

    def mark_alerted(self, key):
        self.last_alerts[key] = datetime.now()
        self._save_cooldown()

    # ── schedule ──────────────────────────────────────────────────────────────

    def _is_in_schedule(self):
        from alert_utils import is_in_schedule
        gen = (self._live_config or {}).get('general', {})
        return is_in_schedule(
            gen.get('schedule_start', ''),
            gen.get('schedule_end', ''),
            float(gen.get('utc_offset') or 2),
        )

    def _threshold(self):
        return float((self._live_config or {}).get('shimano', {}).get('distance_threshold_pct', self.distance_threshold_pct))

    # ── kline subscriptions ───────────────────────────────────────────────────

    def _init_kline_subs(self):
        if not self._ws_manager.ready.wait(timeout=120):
            print('⚠️ Shimano: WS not ready after 120s')
            return
        time.sleep(30)
        self._refresh_kline_subs()

    def _refresh_kline_subs(self):
        tickers = self._ws_manager.get_all_tickers()
        if not tickers:
            return
        ranked = sorted(tickers.items(), key=lambda x: x[1].get('volume_24h', 0), reverse=True)
        top = [s for s, d in ranked
               if d.get('volume_24h', 0) >= self.min_volume_24h][:TOP_KLINE_SYMBOLS]
        self._ws_manager.subscribe_klines(top, intervals=self.scan_tfs)
        self._last_count = len(top)
        print(f'🎣 Shimano: subscribed klines {len(top)} symbols × {len(self.scan_tfs)} TF')
        threading.Timer(KLINE_SUB_REFRESH, self._refresh_kline_subs).start()

    # ── EMA + condition ───────────────────────────────────────────────────────

    @staticmethod
    def _calc_ema(prices, period):
        if len(prices) < period:
            return None
        mult = 2 / (period + 1)
        ema  = sum(prices[:period]) / period
        for p in prices[period:]:
            ema = p * mult + ema * (1 - mult)
        return ema

    def _check_distance(self, klines, threshold_pct):
        """Ritorna (ok, ema60, ema223, distance_pct): ok se la distanza tra
        EMA60 ed EMA223, in % del prezzo corrente, è <= threshold_pct."""
        if len(klines) < MIN_KLINES:
            return False, None, None, None
        closes = [k['close'] for k in klines]
        ema60  = self._calc_ema(closes, 60)
        ema223 = self._calc_ema(closes, 223)
        if ema60 is None or ema223 is None:
            return False, None, None, None
        price = closes[-1]
        if price <= 0:
            return False, ema60, ema223, None
        distance_pct = abs(ema60 - ema223) / price * 100
        return distance_pct <= threshold_pct, ema60, ema223, distance_pct

    # ── WebSocket callback ────────────────────────────────────────────────────

    def _on_kline(self, symbol, interval, candle, is_closed):
        if not self.enabled or not is_closed:
            return
        if interval not in self.scan_tfs:
            return
        if not self._is_in_schedule():
            return

        klines = self._ws_manager.get_klines(symbol, interval)
        ticker = self._ws_manager.get_all_tickers().get(symbol, {})
        if ticker.get('volume_24h', 0) < self.min_volume_24h:
            return

        price  = ticker.get('price') or candle['close']
        volume = ticker.get('volume_24h', 0)
        change = ticker.get('change_24h', 0.0)

        ok, ema60, ema223, distance_pct = self._check_distance(klines, self._threshold())
        if not ok:
            return

        cooldown_key = f"{symbol}_{interval}"
        coin = None
        with self._lock:
            if not self.is_in_cooldown(cooldown_key):
                self.mark_alerted(cooldown_key)
                coin = {
                    'symbol': symbol, 'tf': interval,
                    'price': price, 'ema60': ema60, 'ema223': ema223,
                    'distance_pct': distance_pct,
                    'volume': volume, 'change_pct': change,
                }
        if coin:
            threading.Thread(target=self.send_alert, args=([coin],), daemon=True).start()

    # ── REST data fetching (polling fallback) ─────────────────────────────────

    def _fetch_tickers(self):
        try:
            r = requests.get('https://api.bybit.com/v5/market/tickers',
                             params={'category': 'linear'}, timeout=10)
            data = r.json()
            if data.get('retCode') != 0:
                return []
            result = []
            for item in data['result']['list']:
                if not item['symbol'].endswith('USDT'):
                    continue
                price = float(item.get('lastPrice', 0) or 0)
                vol   = float(item.get('turnover24h', 0) or 0)
                if price <= 0 or vol < self.min_volume_24h:
                    continue
                result.append({'symbol': item['symbol'], 'price': price, 'volume': vol,
                               'change_pct': float(item.get('price24hPcnt', 0) or 0) * 100})
            result.sort(key=lambda x: x['volume'], reverse=True)
            return result[:500]
        except Exception as e:
            print(f'❌ Shimano: fetch tickers: {e}')
            return []

    def _fetch_klines(self, symbol, tf):
        try:
            r = requests.get('https://api.bybit.com/v5/market/kline',
                             params={'category': 'linear', 'symbol': symbol,
                                     'interval': tf, 'limit': 250},
                             timeout=10)
            data = r.json()
            if data.get('retCode') != 0:
                return []
            raw = list(reversed(data['result']['list']))[:-1]
            return [{'time':  int(c[0]) // 1000,
                     'open':  float(c[1]), 'high': float(c[2]),
                     'low':   float(c[3]), 'close': float(c[4])}
                    for c in raw]
        except Exception:
            return []

    # ── polling scan (fallback / manual) ──────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return []
        if self._ws_manager and self._ws_manager.ready.is_set():
            return []
        threshold = self._threshold()
        print(f'🎣 Shimano Scanner — polling scan (tf={",".join(self.scan_tfs)} soglia={threshold}%)...')
        found = []
        try:
            tickers = self._fetch_tickers()
            self._last_count = len(tickers)
            for i, ticker in enumerate(tickers):
                symbol = ticker['symbol']
                for tf in self.scan_tfs:
                    klines = self._fetch_klines(symbol, tf)
                    ok, ema60, ema223, distance_pct = self._check_distance(klines, threshold)
                    if ok:
                        cooldown_key = f"{symbol}_{tf}"
                        with self._lock:
                            if not self.is_in_cooldown(cooldown_key):
                                self.mark_alerted(cooldown_key)
                                found.append({
                                    'symbol': symbol, 'tf': tf,
                                    'price': ticker['price'],
                                    'ema60': ema60, 'ema223': ema223,
                                    'distance_pct': distance_pct,
                                    'volume': ticker['volume'],
                                    'change_pct': ticker.get('change_pct', 0.0),
                                })
                if (i + 1) % 10 == 0:
                    time.sleep(0.5)

            found = found[:self.max_coins_per_alert]
            if found:
                self.send_alert(found)
            print(f'🎣 Shimano: {len(found)} coin trovate')
            return found
        except Exception as e:
            print(f'❌ Shimano scanner error: {e}')
            return []

    # ── alert ─────────────────────────────────────────────────────────────────

    def send_alert(self, coins):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, log_alert, fmt_vol
        except ImportError:
            return
        TF_LABEL = {'D': '1D', '240': '4h', '60': '1h', '30': '30m', '15': '15m', '5': '5m', '1': '1m'}
        for coin in coins[:3]:
            sym      = coin['symbol']
            tf       = coin.get('tf', self.scan_tfs[0])
            tf_label = TF_LABEL.get(tf, tf)
            change   = coin.get('change_pct', 0.0)
            distance = coin.get('distance_pct', 0.0)
            base = (self.base_url or 'https://cryptoscannerpro.com').rstrip('/')

            lines = [
                f'🔔 EMA223/60 {tf_label}',
                '',
                '------------------------------------------------',
                f'- Coin: {sym}',
                f'- Distanza EMA: {distance:.2f}%',
                f'- Var: {change:+.2f}%',
                f'- Volume: {fmt_vol(coin.get("volume", 0))}',
                '------------------------------------------------',
                '',
                f'<a href="https://www.bybit.com/trade/usdt/{sym}">- View Bybit</a>',
                f'<a href="{base}/mtf?symbol={sym}">- View Desktop</a>',
                f'<a href="{base}/chart?symbol={sym}&layout=1x1">- View Mobile</a>',
            ]
            caption = '\n'.join(lines)
            img = get_chart(sym, interval=tf, signal={'type': 'ema'})
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
            log_alert(sym, 'EMA223/60', emoji='🎣', note=f'distanza {distance:.2f}%', tf=tf_label, screenshot=img)
            print(f'🎣 EMA223/60 alert: {sym} {tf_label} dist={distance:.2f}%')

    # ── status ────────────────────────────────────────────────────────────────

    def get_today_alerts(self):
        today = datetime.utcnow().date()
        symbols = set()
        with self._lock:
            for key, dt in self.last_alerts.items():
                if dt.date() == today:
                    symbols.add(key.rsplit('_', 1)[0])
        return list(symbols)

    def get_monitored_count(self):
        return self._last_count
