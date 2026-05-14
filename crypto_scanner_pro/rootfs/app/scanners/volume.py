"""Volume Scanner — Volume Spikes, Gainers, Losers"""
import threading
import requests
from datetime import datetime, timedelta
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GAINERS_COOLDOWN_FILE = '/data/gainers_cooldown.json'
LOSERS_COOLDOWN_FILE  = '/data/losers_cooldown.json'


class VolumeScanner:
    def __init__(self, telegram_config, enabled=True, volume_spike_threshold=200,
                 gainers_enabled=True, gainers_threshold=10,
                 losers_enabled=True,  losers_threshold=10,
                 scan_interval_minutes=30, min_volume_24h=10000000,
                 max_coins_per_alert=10, cooldown_hours=2,
                 ws_manager=None, **kwargs):

        self.telegram_token  = telegram_config['token']
        self.telegram_chat_id = telegram_config['chat_id']
        self.ha_url          = telegram_config.get('ha_url', '')
        self.enabled         = enabled
        self.gainers_enabled = gainers_enabled
        self.gainers_threshold = gainers_threshold
        self.losers_enabled  = losers_enabled
        self.losers_threshold = losers_threshold
        self.max_coins       = max_coins_per_alert
        self.min_volume_24h  = min_volume_24h
        self.cooldown_hours  = cooldown_hours

        self.last_gainers = self._load_cooldown(GAINERS_COOLDOWN_FILE)
        self.last_losers  = self._load_cooldown(LOSERS_COOLDOWN_FILE)
        self._lock        = threading.Lock()

        self._ws_manager = ws_manager
        if ws_manager is not None:
            ws_manager.add_tick_callback(self._on_tick)

    # ── cooldown ─────────────────────────────────────────────────────────────

    def _load_cooldown(self, filepath):
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    return {k: datetime.fromisoformat(v) for k, v in data.items()}
        except Exception as e:
            print(f'⚠️ Error loading cooldown {filepath}: {e}')
        return {}

    def _save_cooldown(self, filepath, alerts_dict):
        try:
            with open(filepath, 'w') as f:
                json.dump({k: v.isoformat() for k, v in alerts_dict.items()}, f)
        except Exception as e:
            print(f'⚠️ Error saving cooldown {filepath}: {e}')

    def is_in_cooldown(self, symbol, alert_type='gainer'):
        alerts_dict = self.last_gainers if alert_type == 'gainer' else self.last_losers
        if symbol not in alerts_dict:
            return False
        return (datetime.now() - alerts_dict[symbol]) < timedelta(hours=self.cooldown_hours)

    def mark_alerted(self, symbol, alert_type='gainer'):
        if alert_type == 'gainer':
            self.last_gainers[symbol] = datetime.now()
            self._save_cooldown(GAINERS_COOLDOWN_FILE, self.last_gainers)
        else:
            self.last_losers[symbol] = datetime.now()
            self._save_cooldown(LOSERS_COOLDOWN_FILE, self.last_losers)

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

        if self.gainers_enabled and change > self.gainers_threshold:
            with self._lock:
                if not self.is_in_cooldown(symbol, 'gainer'):
                    self.mark_alerted(symbol, 'gainer')
                    coin = {'symbol': symbol, 'price': price,
                            'change_pct': change, 'volume_24h_usd': volume}
                    threading.Thread(
                        target=self._send_single_alert, args=(coin, 'gainer'),
                        daemon=True).start()

        elif self.losers_enabled and change < -self.losers_threshold:
            with self._lock:
                if not self.is_in_cooldown(symbol, 'loser'):
                    self.mark_alerted(symbol, 'loser')
                    coin = {'symbol': symbol, 'price': price,
                            'change_pct': change, 'volume_24h_usd': volume}
                    threading.Thread(
                        target=self._send_single_alert, args=(coin, 'loser'),
                        daemon=True).start()

    def _send_single_alert(self, coin, alert_type):
        try:
            from alert_utils import send_photo, send_text, get_chart, mtf_link
        except ImportError:
            return
        sym = coin['symbol']
        def _vol(v):
            if v >= 1e9: return f'${v/1e9:.1f}B'
            if v >= 1e6: return f'${v/1e6:.0f}M'
            return f'${v/1e3:.0f}K'
        if alert_type == 'gainer':
            caption = (f"{mtf_link(sym, self.ha_url)}  +{coin['change_pct']:.2f}% (24h)\n"
                       f"Vol: {_vol(coin['volume_24h_usd'])}")
            sig_type = 'gainer'
        else:
            caption = (f"{mtf_link(sym, self.ha_url)}  {coin['change_pct']:.2f}% (24h)\n"
                       f"Vol: {_vol(coin['volume_24h_usd'])}")
            sig_type = 'loser'
        img = get_chart(sym, interval='240', signal={'type': sig_type})
        if img:
            send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
        else:
            send_text(self.telegram_token, self.telegram_chat_id, caption)
        print(f'Volume alert: {sym} ({alert_type})')

    # ── polling scan (fallback / manual) ─────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return {}

        print('📊 Volume Scanner — polling scan...')
        try:
            # Use WS cache if ready
            if self._ws_manager and self._ws_manager.ready.is_set():
                raw = self._ws_manager.get_all_tickers()
                all_pairs = [
                    {'symbol': s,
                     'price': d['price'],
                     'change_pct': d.get('change_24h', 0),
                     'volume_24h_usd': d.get('volume_24h', 0)}
                    for s, d in raw.items()
                    if d.get('price', 0) > 0 and d.get('volume_24h', 0) >= self.min_volume_24h
                ]
            else:
                url = 'https://api.bybit.com/v5/market/tickers?category=linear'
                response = requests.get(url, timeout=10)
                data = response.json()
                if data['retCode'] != 0:
                    return {}
                all_pairs = []
                for item in data['result']['list']:
                    if not item['symbol'].endswith('USDT'):
                        continue
                    last_price = float(item['lastPrice'])
                    change_pct = float(item.get('price24hPcnt', 0)) * 100
                    vol_usd    = float(item.get('volume24h', 0)) * last_price
                    if vol_usd < self.min_volume_24h:
                        continue
                    all_pairs.append({'symbol': item['symbol'], 'price': last_price,
                                      'change_pct': change_pct, 'volume_24h_usd': vol_usd})

            all_pairs.sort(key=lambda x: x['change_pct'], reverse=True)
            top_gainers = all_pairs[:20]
            top_losers  = all_pairs[-20:] if len(all_pairs) >= 20 else []

            gainers, losers = [], []
            with self._lock:
                if self.gainers_enabled:
                    for p in top_gainers:
                        if p['change_pct'] > self.gainers_threshold and \
                                not self.is_in_cooldown(p['symbol'], 'gainer'):
                            self.mark_alerted(p['symbol'], 'gainer')
                            gainers.append(p)
                if self.losers_enabled:
                    for p in top_losers:
                        if p['change_pct'] < -self.losers_threshold and \
                                not self.is_in_cooldown(p['symbol'], 'loser'):
                            self.mark_alerted(p['symbol'], 'loser')
                            losers.append(p)

            gainers = gainers[:self.max_coins]
            losers  = losers[:self.max_coins]

            if gainers or losers:
                self.send_alert({'gainers': gainers, 'losers': losers, 'volume_spikes': []})

            return {'gainers': gainers, 'losers': losers, 'volume_spikes': []}

        except Exception as e:
            print(f'❌ Volume scanner error: {e}')
            return {}

    def send_alert(self, result):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, mtf_link
        except ImportError:
            return

        def _vol(v):
            if v >= 1e9: return f'${v/1e9:.1f}B'
            if v >= 1e6: return f'${v/1e6:.0f}M'
            return f'${v/1e3:.0f}K'

        for coin in result.get('gainers', [])[:2]:
            sym     = coin['symbol']
            caption = (f"{mtf_link(sym, self.ha_url)}  +{coin['change_pct']:.2f}% (24h)\n"
                       f"Vol: {_vol(coin['volume_24h_usd'])}")
            img = get_chart(sym, interval='240', signal={'type': 'gainer'})
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)

        for coin in result.get('losers', [])[:2]:
            sym     = coin['symbol']
            caption = (f"{mtf_link(sym, self.ha_url)}  {coin['change_pct']:.2f}% (24h)\n"
                       f"Vol: {_vol(coin['volume_24h_usd'])}")
            img = get_chart(sym, interval='240', signal={'type': 'loser'})
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
