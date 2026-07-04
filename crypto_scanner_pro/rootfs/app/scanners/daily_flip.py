"""Daily Flip Scanner — monitora il cambio di colore della candela giornaliera (vs apertura 00:00 UTC)."""
import threading
import requests
from datetime import datetime, timedelta
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COOLDOWN_FILE = '/data/flip_cooldown.json'


class DailyFlipScanner:
    def __init__(self, telegram_config, enabled=True, flip_threshold=2.0,
                 flip_type='both', scan_interval_minutes=30, max_coins=20,
                 min_volume_24h=10000000, cooldown_hours=2,
                 screenshot_tf='240', ws_manager=None, live_config=None,
                 schedule_start='', schedule_end='', utc_offset=2,
                 screenshot_no_indicators=False, show_flip_level=False,
                 screenshot_no_levels=False, **kwargs):

        self.telegram_token   = telegram_config['token']
        self.telegram_chat_id = telegram_config['chat_id']
        self.base_url         = telegram_config.get('base_url', '')
        self.enabled          = enabled
        self.flip_threshold   = flip_threshold        # soglia in % (es. 1.0)
        self.flip_type        = flip_type
        self.max_coins        = max_coins
        self.min_volume_24h   = min_volume_24h
        self.cooldown_hours   = cooldown_hours
        self.screenshot_no_indicators = screenshot_no_indicators
        self.show_flip_level  = show_flip_level
        self.screenshot_no_levels = screenshot_no_levels
        self._live_config     = live_config

        self.last_alerts      = self._load_cooldown()
        self._lock            = threading.Lock()
        self._daily_open_cache = {}   # {symbol: {'date': date, 'open': float}}
        self._cache_lock      = threading.Lock()

        self._ws_manager = ws_manager
        if ws_manager is not None:
            ws_manager.add_tick_callback(self._on_tick)

    # ── daily open cache ──────────────────────────────────────────────────────

    def _get_daily_open(self, symbol):
        """Restituisce il prezzo di apertura della candela daily odierna (UTC).
        Usa la cache ws_manager se disponibile, altrimenti REST Bybit."""
        today = datetime.utcnow().date()
        with self._cache_lock:
            cached = self._daily_open_cache.get(symbol)
            if cached and cached['date'] == today:
                return cached['open']

        # 1) Prova dalla cache klines del ws_manager
        if self._ws_manager:
            klines = self._ws_manager.get_klines(symbol, 'D')
            if klines:
                open_price = float(klines[-1]['open'])
                with self._cache_lock:
                    self._daily_open_cache[symbol] = {'date': today, 'open': open_price}
                return open_price

        # 2) Fallback REST Bybit
        try:
            resp = requests.get(
                'https://api.bybit.com/v5/market/kline',
                params={'category': 'linear', 'symbol': symbol, 'interval': 'D', 'limit': 1},
                timeout=5,
            )
            lst = resp.json().get('result', {}).get('list', [])
            if lst:
                open_price = float(lst[0][1])  # [time, open, high, low, close, ...]
                with self._cache_lock:
                    self._daily_open_cache[symbol] = {'date': today, 'open': open_price}
                return open_price
        except Exception as e:
            print(f'⚠️ Flip: daily open REST fallback failed for {symbol}: {e}')

        return None

    # ── cooldown ──────────────────────────────────────────────────────────────

    def _load_cooldown(self):
        try:
            if os.path.exists(COOLDOWN_FILE):
                with open(COOLDOWN_FILE, 'r') as f:
                    return {k: datetime.fromisoformat(v) for k, v in json.load(f).items()}
        except Exception as e:
            print(f'⚠️ Error loading flip cooldown: {e}')
        return {}

    def _save_cooldown(self):
        try:
            with open(COOLDOWN_FILE, 'w') as f:
                json.dump({k: v.isoformat() for k, v in self.last_alerts.items()}, f)
        except Exception as e:
            print(f'⚠️ Error saving flip cooldown: {e}')

    def is_in_cooldown(self, symbol):
        if symbol not in self.last_alerts:
            return False
        return (datetime.now() - self.last_alerts[symbol]) < timedelta(hours=self.cooldown_hours)

    def mark_alerted(self, symbol):
        self.last_alerts[symbol] = datetime.now()
        self._save_cooldown()

    # ── real-time callback ────────────────────────────────────────────────────

    def _on_tick(self, symbol, data):
        """Chiamato ad ogni aggiornamento ticker dal WebSocket."""
        if not self.enabled:
            return
        from alert_utils import is_in_schedule
        _gen = (self._live_config or {}).get('general', {})
        if not is_in_schedule(_gen.get('schedule_start', ''), _gen.get('schedule_end', ''), float(_gen.get('utc_offset') or 2)):
            return

        price  = data.get('price', 0)
        volume = data.get('volume_24h', 0)
        if price <= 0 or volume < self.min_volume_24h:
            return

        # Pre-filtro rapido con 24h rolling: evita REST inutili se la coin è lontana
        change_24h = data.get('change_24h')
        if change_24h is not None and abs(change_24h) > self.flip_threshold * 3:
            return

        daily_open = self._get_daily_open(symbol)
        if not daily_open or daily_open <= 0:
            return

        # Variazione reale rispetto all'apertura della candela giornaliera
        change = (price - daily_open) / daily_open * 100

        if abs(change) >= self.flip_threshold:
            return  # Non abbastanza vicino al flip

        direction = 'green_to_red' if change > 0 else 'red_to_green'
        if self.flip_type not in ('both', direction):
            return

        with self._lock:
            if not self.is_in_cooldown(symbol):
                self.mark_alerted(symbol)
                coin = {
                    'symbol': symbol, 'price': price, 'change_pct': change,
                    'daily_open': daily_open, 'volume': volume,
                    'flip_direction': '🟢➡️🔴' if change > 0 else '🔴➡️🟢',
                }
                threading.Thread(
                    target=self._send_single_alert, args=(coin,), daemon=True).start()

    def _build_signal(self, coin):
        signal = {'type': 'flip'}
        if self.screenshot_no_indicators:
            signal['no_indicators'] = True
        if self.screenshot_no_levels:
            signal['no_levels'] = True
        if self.show_flip_level and coin.get('daily_open'):
            signal['flip_level'] = coin['daily_open']
        return signal

    def _send_single_alert(self, coin):
        try:
            from alert_utils import send_photo, send_text, get_chart, log_alert
        except ImportError:
            return
        from alert_utils import fmt_vol
        sym  = coin['symbol']
        base = (self.base_url or 'https://cryptoscannerpro.com').rstrip('/')
        lines = [
            '🔔 Daily Flip',
            '',
            '------------------------------------------------',
            f'- Coin: {sym}',
            f'- Var: {coin["change_pct"]:+.2f}%',
            f'- Distanza flip: {abs(coin["change_pct"]):.2f}%',
            f'- Volume: {fmt_vol(coin.get("volume", 0))}',
            '------------------------------------------------',
            '',
            f'<a href="https://www.bybit.com/trade/usdt/{sym}">- View Bybit</a>',
            f'<a href="{base}/mtf?symbol={sym}">- View Desktop</a>',
            f'<a href="{base}/chart?symbol={sym}&layout=1x1">- View Mobile</a>',
        ]
        caption = '\n'.join(lines)
        img = get_chart(sym, interval='D', signal=self._build_signal(coin))
        if img:
            send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
        else:
            send_text(self.telegram_token, self.telegram_chat_id, caption)
        log_alert(sym, 'Daily Flip', emoji='🔄', note=f'{abs(coin["change_pct"]):.2f}%', screenshot=img)
        print(f'🔄 Flip alert: {sym} ({coin["change_pct"]:+.2f}% vs open {coin["daily_open"]})')

    # ── polling scan (fallback / manuale) ─────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return []

        print(f'🔄 Daily Flip Scanner — polling scan (soglia {self.flip_threshold:.1f}%)...')
        try:
            # Recupera tutti i ticker
            if self._ws_manager and self._ws_manager.ready.is_set():
                raw = self._ws_manager.get_all_tickers()
                candidates = [
                    {'symbol': s, 'price': d['price'], 'volume': d.get('volume_24h', 0),
                     'change_24h': d.get('change_24h', 0)}
                    for s, d in raw.items()
                    if d.get('price', 0) > 0 and d.get('volume_24h', 0) >= self.min_volume_24h
                ]
            else:
                resp = requests.get(
                    'https://api.bybit.com/v5/market/tickers?category=linear', timeout=10)
                data = resp.json()
                if data['retCode'] != 0:
                    return []
                candidates = []
                for item in data['result']['list']:
                    if not item['symbol'].endswith('USDT'):
                        continue
                    price  = float(item['lastPrice'])
                    vol    = float(item.get('volume24h', 0)) * price
                    if vol < self.min_volume_24h:
                        continue
                    candidates.append({
                        'symbol': item['symbol'], 'price': price, 'volume': vol,
                        'change_24h': float(item.get('price24hPcnt', 0)) * 100,
                    })

            # Pre-filtro rapido con 24h rolling
            candidates = [c for c in candidates if abs(c['change_24h']) <= self.flip_threshold * 3]

            found = []
            with self._lock:
                for c in candidates:
                    daily_open = self._get_daily_open(c['symbol'])
                    if not daily_open or daily_open <= 0:
                        continue
                    change = (c['price'] - daily_open) / daily_open * 100
                    if abs(change) >= self.flip_threshold:
                        continue
                    direction = 'green_to_red' if change > 0 else 'red_to_green'
                    if self.flip_type not in ('both', direction):
                        continue
                    if not self.is_in_cooldown(c['symbol']):
                        self.mark_alerted(c['symbol'])
                        found.append({
                            'symbol': c['symbol'], 'price': c['price'],
                            'change_pct': change, 'daily_open': daily_open,
                            'flip_direction': '🟢➡️🔴' if change > 0 else '🔴➡️🟢',
                        })

            if found:
                self.send_alert(found)
            return found

        except Exception as e:
            print(f'❌ Flip scanner error: {e}')
            return []

    def send_alert(self, coins):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, log_alert
        except ImportError:
            return
        from alert_utils import fmt_vol
        for coin in coins[:2]:
            sym  = coin['symbol']
            base = (self.base_url or 'https://cryptoscannerpro.com').rstrip('/')
            lines = [
                '🔔 Daily Flip',
                '',
                '------------------------------------------------',
                f'- Coin: {sym}',
                f'- Var: {coin["change_pct"]:+.2f}%',
                f'- Distanza flip: {abs(coin["change_pct"]):.2f}%',
                f'- Volume: {fmt_vol(coin.get("volume", 0))}',
                '------------------------------------------------',
                '',
                f'<a href="https://www.bybit.com/trade/usdt/{sym}">- View Bybit</a>',
                f'<a href="{base}/mtf?symbol={sym}">- View Desktop</a>',
                f'<a href="{base}/chart?symbol={sym}&layout=1x1">- View Mobile</a>',
            ]
            caption = '\n'.join(lines)
            img = get_chart(sym, interval='D', signal=self._build_signal(coin))
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
            log_alert(sym, 'Daily Flip', emoji='🔄', note=f'{abs(coin["change_pct"]):.2f}%', screenshot=img)
            print(f'🔄 Flip alert inviato: {sym} ({coin["change_pct"]:+.2f}%)')

    # ── status ────────────────────────────────────────────────────────────────

    def get_today_alerts(self):
        today = datetime.utcnow().date()
        with self._lock:
            return [k for k, dt in self.last_alerts.items() if dt.date() == today]

    def get_monitored_count(self):
        if self._ws_manager:
            tickers = self._ws_manager.get_all_tickers()
            return sum(1 for d in tickers.values() if d.get('volume_24h', 0) >= self.min_volume_24h)
        return 0
