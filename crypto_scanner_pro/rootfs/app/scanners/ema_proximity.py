"""EMA Proximity Scanner — avvisa quando il prezzo si avvicina all'EMA60 su 30m con 0 tocchi oggi."""
import threading
import json
import os
import calendar
from datetime import datetime
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COOLDOWN_FILE = '/data/ema_proximity_cooldown.json'
TOP_KLINE_SYMBOLS = 500
KLINE_SUB_REFRESH = 4 * 3600


class EMAProximityScanner:
    def __init__(self, telegram_config, enabled=True,
                 proximity_threshold=1.5, touch_threshold=2.0,
                 min_volume_24h=10_000_000,
                 scan_tf=None,
                 ws_manager=None, live_config=None, **kwargs):
        self.telegram_token      = telegram_config['token']
        self.telegram_chat_id    = telegram_config['chat_id']
        self.base_url            = telegram_config.get('base_url', '')
        self.enabled             = enabled
        self.proximity_threshold = proximity_threshold
        self.touch_threshold     = touch_threshold
        self.min_volume_24h      = min_volume_24h
        raw_tf                   = scan_tf if scan_tf is not None else ['30']
        self.scan_tfs            = raw_tf if isinstance(raw_tf, list) else [raw_tf]
        self._live_config        = live_config
        self._ws_manager         = ws_manager
        self._lock               = threading.Lock()
        self._alerted: dict      = self._load_cooldown()  # {symbol_tf: "YYYY-MM-DD"}

        if ws_manager is not None:
            ws_manager.add_kline_callback(self._on_kline)
            threading.Thread(target=self._init_kline_subs, daemon=True).start()

        print(f'🔎 EMA60 Scanner init — proximity={self.proximity_threshold}% touch_thr={self.touch_threshold}% tf={",".join(self.scan_tfs)}')

    # ── cooldown ──────────────────────────────────────────────────────────────

    def _load_cooldown(self):
        try:
            if os.path.exists(COOLDOWN_FILE):
                with open(COOLDOWN_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            print(f'⚠️ EMAProx: load cooldown: {e}')
        return {}

    def _save_cooldown(self):
        try:
            os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
            with open(COOLDOWN_FILE, 'w') as f:
                json.dump(self._alerted, f)
        except Exception as e:
            print(f'⚠️ EMAProx: save cooldown: {e}')

    @staticmethod
    def _today_utc():
        return datetime.utcnow().strftime('%Y-%m-%d')

    def _alerted_today(self, symbol):
        return self._alerted.get(symbol) == self._today_utc()

    # ── schedule ──────────────────────────────────────────────────────────────

    def _is_in_schedule(self):
        from alert_utils import is_in_schedule
        gen = (self._live_config or {}).get('general', {})
        return is_in_schedule(
            gen.get('schedule_start', ''),
            gen.get('schedule_end', ''),
            float(gen.get('utc_offset') or 2),
        )

    # ── kline subscriptions ──────────────────────────────────────────────────

    def _init_kline_subs(self):
        if not self._ws_manager.ready.wait(timeout=120):
            print('⚠️ EMAProx: WS not ready after 120s')
            return
        self._refresh_kline_subs()

    def _refresh_kline_subs(self):
        tickers = self._ws_manager.get_all_tickers()
        if not tickers:
            return
        ranked = sorted(tickers.items(), key=lambda x: x[1].get('volume_24h', 0), reverse=True)
        top = [s for s, d in ranked if d.get('volume_24h', 0) >= self.min_volume_24h][:TOP_KLINE_SYMBOLS]
        self._ws_manager.subscribe_klines(top, intervals=self.scan_tfs)
        print(f'🔎 EMAProx: subscribed klines {len(top)} symbols × {len(self.scan_tfs)} TF')
        threading.Timer(KLINE_SUB_REFRESH, self._refresh_kline_subs).start()

    # ── EMA helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _calc_ema(prices, period):
        if len(prices) < period:
            return None
        k = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        for p in prices[period:]:
            ema = p * k + ema * (1 - k)
        return ema

    @staticmethod
    def _calc_ema_series(prices, period):
        if len(prices) < period:
            return [None] * len(prices)
        k = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        series = [None] * (period - 1)
        series.append(ema)
        for p in prices[period:]:
            ema = p * k + ema * (1 - k)
            series.append(ema)
        return series

    def _count_touches_today(self, klines):
        """Candele 30m chiuse da mezzanotte UTC dove close >= EMA60 e dist < touch_threshold%."""
        past = klines[:-1]  # escludi candela live
        if len(past) < 60:
            return None
        now = datetime.utcnow()
        midnight_ts = calendar.timegm((now.year, now.month, now.day, 0, 0, 0, 0, 0, 0))
        closes = [k['close'] for k in past]
        ema_series = self._calc_ema_series(closes, 60)
        count = 0
        for i, k in enumerate(past):
            if k['time'] < midnight_ts:
                continue
            ev = ema_series[i]
            if ev is None:
                continue
            if k['close'] >= ev and abs((k['close'] - ev) / ev * 100) < self.touch_threshold:
                count += 1
        return count

    # ── kline callback ────────────────────────────────────────────────────────

    def _on_kline(self, symbol, interval, candle, is_closed):
        if not self.enabled or interval not in self.scan_tfs:
            return
        if not self._is_in_schedule():
            return

        klines = self._ws_manager.get_klines(symbol, interval)
        if len(klines) < 60:
            return

        closes = [k['close'] for k in klines]
        ema60 = self._calc_ema(closes, 60)
        if ema60 is None:
            return

        live_price = candle['close']
        dist = abs((live_price - ema60) / ema60 * 100)

        # Fast path: prezzo troppo lontano
        if dist > self.proximity_threshold:
            return

        # Controlla tocchi solo quando siamo vicini all'EMA (più costoso)
        touch_count = self._count_touches_today(klines)
        if touch_count is None or touch_count > 0:
            return

        cooldown_key = f'{symbol}_{interval}'
        coin = None
        with self._lock:
            if self._alerted_today(cooldown_key):
                return
            ticker = self._ws_manager.get_all_tickers().get(symbol, {})
            coin = {
                'symbol': symbol,
                'interval': interval,
                'price': live_price,
                'ema60': ema60,
                'distance_pct': dist,
                'side': 'sopra' if live_price > ema60 else 'sotto',
                'volume_24h': ticker.get('volume_24h', 0),
                'change_pct': ticker.get('change_24h', 0.0),
            }
            self._alerted[cooldown_key] = self._today_utc()
            self._save_cooldown()

        if coin:
            threading.Thread(target=self._send_alert, args=(coin,), daemon=True).start()

    # ── alert ─────────────────────────────────────────────────────────────────

    def _send_alert(self, coin):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, log_alert
        except ImportError:
            return
        TF_LABEL = {'D': '1D', '240': '4h', '60': '1h', '30': '30m', '15': '15m', '5': '5m', '1': '1m'}
        sym      = coin['symbol']
        interval = coin.get('interval', '30')
        tf_label = TF_LABEL.get(interval, interval)
        dist     = coin['distance_pct']
        change   = coin.get('change_pct', 0.0)
        lines = [
            f'🔔 EMA60 {tf_label}',
            '',
            '------------------------------------------------',
            f'- Coin: {sym}',
            f'- Distanza: {dist:.2f}%',
            f'- Var: {change:+.2f}%',
            '------------------------------------------------',
        ]
        base = (self.base_url or 'https://cryptoscannerpro.com').rstrip('/')
        lines.append('')
        lines.append(f'<a href="https://www.bybit.com/trade/usdt/{sym}">- View Bybit</a>')
        lines.append(f'<a href="{base}/mtf?symbol={sym}">- View Desktop</a>')
        lines.append(f'<a href="{base}/chart?symbol={sym}&layout=1x1">- View Mobile</a>')
        caption = '\n'.join(lines)
        img = get_chart(sym, interval=interval, signal={'type': 'ema'})
        if img:
            send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
        else:
            send_text(self.telegram_token, self.telegram_chat_id, caption)
        log_alert(sym, 'EMA60', emoji='🔎', note=f'dist={dist:.2f}%', tf=tf_label)
        print(f'🔎 EMAProx alert: {sym} {tf_label} dist={dist:.2f}%')

    # ── status ────────────────────────────────────────────────────────────────

    def get_today_alerts(self):
        today = self._today_utc()
        with self._lock:
            return [k.split('_')[0] for k, d in self._alerted.items() if d == today]
