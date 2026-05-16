"""Daily Flip Scanner"""
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
                 screenshot_tf='240', ws_manager=None, **kwargs):

        self.telegram_token   = telegram_config['token']
        self.telegram_chat_id = telegram_config['chat_id']
        self.ha_url           = telegram_config.get('ha_url', '')
        self.enabled          = enabled
        self.flip_threshold   = flip_threshold / 100  # store as fraction
        self.flip_type        = flip_type
        self.max_coins        = max_coins
        self.min_volume_24h   = min_volume_24h
        self.cooldown_hours   = cooldown_hours
        self.screenshot_tf    = screenshot_tf

        self.last_alerts = self._load_cooldown()
        self._lock       = threading.Lock()

        self._ws_manager = ws_manager
        if ws_manager is not None:
            ws_manager.add_tick_callback(self._on_tick)

    # ── cooldown ─────────────────────────────────────────────────────────────

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
        """Called on every ticker update from WebSocket."""
        if not self.enabled:
            return
        price  = data.get('price', 0)
        change = data.get('change_24h')
        volume = data.get('volume_24h', 0)
        if price <= 0 or change is None or volume < self.min_volume_24h:
            return

        if abs(change) >= self.flip_threshold * 100:
            return  # Not near flip

        direction = 'green_to_red' if change > 0 else 'red_to_green'
        if self.flip_type not in ('both', direction):
            return

        with self._lock:
            if not self.is_in_cooldown(symbol):
                self.mark_alerted(symbol)
                coin = {
                    'symbol': symbol, 'price': price, 'change_pct': change,
                    'flip_direction': '🟢➡️🔴' if change > 0 else '🔴➡️🟢',
                }
                threading.Thread(
                    target=self._send_single_alert, args=(coin,), daemon=True).start()

    def _send_single_alert(self, coin):
        try:
            from alert_utils import send_photo, send_text, get_chart, mtf_link
        except ImportError:
            return
        sym     = coin['symbol']
        sign    = '+' if coin['change_pct'] >= 0 else ''
        caption = (f"{mtf_link(sym, self.ha_url)}  Daily Flip\n"
                   f"var 24h: {sign}{coin['change_pct']:.2f}%")
        img = get_chart(sym, interval=self.screenshot_tf, signal={'type': 'flip'})
        if img:
            send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
        else:
            send_text(self.telegram_token, self.telegram_chat_id, caption)
        print(f'Flip alert: {sym}')

    # ── polling scan (fallback / manual) ─────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return []

        print(f'🔄 Daily Flip Scanner — polling scan ({self.flip_threshold*100:.1f}% threshold)...')
        try:
            if self._ws_manager and self._ws_manager.ready.is_set():
                raw = self._ws_manager.get_all_tickers()
                all_pairs = [
                    {'symbol': s, 'last_price': d['price'],
                     'change_pct': d.get('change_24h', 0),
                     'volume': d.get('volume_24h', 0)}
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
                    last_price = float(item['lastPrice'])
                    open_price = float(item.get('prevPrice24h', last_price))
                    vol_usd    = float(item.get('volume24h', 0)) * last_price
                    if vol_usd < self.min_volume_24h:
                        continue
                    change_pct = ((last_price - open_price) / open_price) * 100
                    all_pairs.append({'symbol': item['symbol'], 'last_price': last_price,
                                      'change_pct': change_pct, 'volume': vol_usd})

            all_pairs.sort(key=lambda x: x['change_pct'], reverse=True)
            top_20_gainers = all_pairs[:20]
            top_20_losers  = all_pairs[-20:] if len(all_pairs) >= 20 else []
            pairs_to_check = top_20_gainers + top_20_losers

            found = []
            with self._lock:
                for p in pairs_to_check:
                    change = p['change_pct']
                    if abs(change) >= self.flip_threshold * 100:
                        continue
                    direction = 'green_to_red' if change > 0 else 'red_to_green'
                    if self.flip_type not in ('both', direction):
                        continue
                    if not self.is_in_cooldown(p['symbol']):
                        self.mark_alerted(p['symbol'])
                        found.append({'symbol': p['symbol'], 'price': p['last_price'],
                                      'change_pct': change,
                                      'flip_direction': '🟢➡️🔴' if change > 0 else '🔴➡️🟢'})

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
            from alert_utils import send_photo, send_text, get_chart, mtf_link
        except ImportError:
            return
        for coin in coins[:2]:
            sym     = coin['symbol']
            sign    = '+' if coin['change_pct'] >= 0 else ''
            caption = (f"Daily Flip  {mtf_link(sym, self.ha_url)}\n"
                       f"var 24h: {sign}{coin['change_pct']:.2f}%")
            img = get_chart(sym, interval=self.screenshot_tf, signal={'type': 'flip'})
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
            print(f'Flip alert inviato: {sym}')
