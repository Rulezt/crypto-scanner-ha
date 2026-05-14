"""ATH/ATL Scanner — All-Time High/Low Monitor"""
import threading
import requests
from datetime import datetime, timedelta
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ATH_COOLDOWN_FILE = '/data/ath_cooldown.json'
ATL_COOLDOWN_FILE = '/data/atl_cooldown.json'

# Refresh ATH/ATL cache every 6 hours
ATH_CACHE_TTL = 6 * 3600


class ATHATLScanner:
    def __init__(self, telegram_config, enabled=True,
                 ath_enabled=True, atl_enabled=True,
                 proximity_threshold=2.0, lookback_days=365,
                 scan_interval_minutes=30, min_volume_24h=10000000,
                 max_coins_per_alert=10, cooldown_hours=24,
                 ws_manager=None, **kwargs):

        self.telegram_token     = telegram_config['token']
        self.telegram_chat_id   = telegram_config['chat_id']
        self.ha_url             = telegram_config.get('ha_url', '')
        self.enabled            = enabled
        self.ath_enabled        = ath_enabled
        self.atl_enabled        = atl_enabled
        self.proximity_threshold = proximity_threshold
        self.lookback_days      = lookback_days
        self.min_volume_24h     = min_volume_24h
        self.max_coins_per_alert = max_coins_per_alert
        self.cooldown_hours     = cooldown_hours

        self.last_ath_alerts = self._load_cooldown(ATH_COOLDOWN_FILE)
        self.last_atl_alerts = self._load_cooldown(ATL_COOLDOWN_FILE)
        self._lock           = threading.Lock()

        # ATH/ATL pre-computed cache: {symbol: {ath, atl, computed_at}}
        self._ath_cache      = {}
        self._ath_cache_lock = threading.Lock()

        self._ws_manager = ws_manager
        if ws_manager is not None:
            ws_manager.add_tick_callback(self._on_tick)
            threading.Thread(target=self._precompute_loop, daemon=True).start()

        print(f'🏆 ATH/ATL Scanner init — threshold={proximity_threshold}% lookback={lookback_days}d ws={"on" if ws_manager else "off"}')

    # ── cooldown ─────────────────────────────────────────────────────────────

    def _load_cooldown(self, filepath):
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    return {k: datetime.fromisoformat(v) for k, v in json.load(f).items()}
        except Exception as e:
            print(f'⚠️ Error loading cooldown {filepath}: {e}')
        return {}

    def _save_cooldown(self, filepath, alerts_dict):
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, 'w') as f:
                json.dump({k: v.isoformat() for k, v in alerts_dict.items()}, f)
        except Exception as e:
            print(f'⚠️ Error saving cooldown {filepath}: {e}')

    def is_in_cooldown(self, symbol, alert_type='ath'):
        d = self.last_ath_alerts if alert_type == 'ath' else self.last_atl_alerts
        if symbol not in d:
            return False
        return (datetime.now() - d[symbol]) < timedelta(hours=self.cooldown_hours)

    def mark_alerted(self, symbol, alert_type='ath'):
        if alert_type == 'ath':
            self.last_ath_alerts[symbol] = datetime.now()
            self._save_cooldown(ATH_COOLDOWN_FILE, self.last_ath_alerts)
        else:
            self.last_atl_alerts[symbol] = datetime.now()
            self._save_cooldown(ATL_COOLDOWN_FILE, self.last_atl_alerts)

    # ── ATH/ATL cache pre-computation ────────────────────────────────────────

    def _precompute_loop(self):
        """Pre-compute ATH/ATL for top coins; refresh every ATH_CACHE_TTL seconds."""
        while True:
            try:
                self._precompute_ath_atl()
            except Exception as e:
                print(f'⚠️ ATH precompute error: {e}')
            time.sleep(ATH_CACHE_TTL)

    def _precompute_ath_atl(self):
        """Fetch top 40 coins by volume and compute their ATH/ATL from REST."""
        # Wait for WS to have some data, or use REST directly
        symbols = []
        if self._ws_manager and self._ws_manager.ready.is_set():
            raw = self._ws_manager.get_all_tickers()
            ranked = sorted(raw.items(), key=lambda x: x[1].get('volume_24h', 0), reverse=True)
            symbols = [s for s, d in ranked if d.get('volume_24h', 0) >= self.min_volume_24h][:40]
        if not symbols:
            # Fallback: fetch from REST
            try:
                r = requests.get('https://api.bybit.com/v5/market/tickers',
                                 params={'category': 'linear'}, timeout=10)
                data = r.json()
                if data.get('retCode') == 0:
                    pairs = []
                    for item in data['result']['list']:
                        if not item['symbol'].endswith('USDT'):
                            continue
                        price = float(item.get('lastPrice', 0) or 0)
                        vol   = float(item.get('volume24h', 0) or 0) * price
                        if vol >= self.min_volume_24h:
                            pairs.append((item['symbol'], vol))
                    pairs.sort(key=lambda x: x[1], reverse=True)
                    symbols = [s for s, _ in pairs[:40]]
            except Exception as e:
                print(f'⚠️ ATH precompute: REST fallback error: {e}')

        computed = 0
        for sym in symbols:
            try:
                klines = self.fetch_historical_data(sym, self.lookback_days)
                if not klines:
                    continue
                highs = [float(k[2]) for k in klines]
                lows  = [float(k[3]) for k in klines]
                with self._ath_cache_lock:
                    self._ath_cache[sym] = {
                        'ath': max(highs), 'atl': min(lows),
                        'computed_at': time.time()
                    }
                computed += 1
            except Exception:
                pass
        print(f'🏆 ATH/ATL cache refreshed: {computed}/{len(symbols)} symbols')

    # ── real-time callback ────────────────────────────────────────────────────

    def _on_tick(self, symbol, data):
        if not self.enabled:
            return
        price  = data.get('price', 0)
        volume = data.get('volume_24h', 0)
        if price <= 0 or volume < self.min_volume_24h:
            return

        with self._ath_cache_lock:
            aa = self._ath_cache.get(symbol)
        if not aa:
            return  # Not pre-computed yet

        ath_dist = (aa['ath'] - price) / aa['ath'] * 100
        atl_dist = (price - aa['atl']) / aa['atl'] * 100

        with self._lock:
            if self.ath_enabled and ath_dist <= self.proximity_threshold and not self.is_in_cooldown(symbol, 'ath'):
                self.mark_alerted(symbol, 'ath')
                coin = {'symbol': symbol, 'price': price,
                        'ath': aa['ath'], 'distance_pct': ath_dist,
                        'is_new_ath': price >= aa['ath'], 'change_pct': data.get('change_24h', 0)}
                threading.Thread(target=self._send_single_alert,
                                 args=(coin, 'ath'), daemon=True).start()

            if self.atl_enabled and 0 <= atl_dist <= self.proximity_threshold and not self.is_in_cooldown(symbol, 'atl'):
                self.mark_alerted(symbol, 'atl')
                coin = {'symbol': symbol, 'price': price,
                        'atl': aa['atl'], 'distance_pct': atl_dist,
                        'is_new_atl': price <= aa['atl'], 'change_pct': data.get('change_24h', 0)}
                threading.Thread(target=self._send_single_alert,
                                 args=(coin, 'atl'), daemon=True).start()

    def _send_single_alert(self, coin, alert_type):
        try:
            from alert_utils import send_photo, send_text, get_chart, mtf_link
        except ImportError:
            return
        sym = coin['symbol']
        if alert_type == 'ath':
            caption = (f"{mtf_link(sym, self.ha_url)}  Nuovo ATH!" if coin['is_new_ath']
                       else f"{mtf_link(sym, self.ha_url)}  Vicino ATH\ndistanza: {coin['distance_pct']:.2f}%")
            sig_type = 'ath'
        else:
            caption = (f"{mtf_link(sym, self.ha_url)}  Nuovo ATL!" if coin['is_new_atl']
                       else f"{mtf_link(sym, self.ha_url)}  Vicino ATL\ndistanza: {coin['distance_pct']:.2f}%")
            sig_type = 'atl'
        img = get_chart(sym, interval='D', signal={'type': sig_type})
        if img:
            send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
        else:
            send_text(self.telegram_token, self.telegram_chat_id, caption)
        print(f'ATH/ATL alert: {sym} ({alert_type})')

    # ── historical data helpers ───────────────────────────────────────────────

    def fetch_historical_data(self, symbol, days=365):
        try:
            r = requests.get('https://api.bybit.com/v5/market/kline',
                             params={'category': 'linear', 'symbol': symbol,
                                     'interval': 'D', 'limit': min(days, 1000)},
                             timeout=10)
            data = r.json()
            if data['retCode'] != 0:
                return None
            klines = data['result']['list']
            return klines if len(klines) >= 30 else None
        except Exception as e:
            print(f'❌ fetch_historical_data {symbol}: {e}')
            return None

    def calculate_ath_atl(self, klines, current_price):
        try:
            highs = [float(k[2]) for k in klines]
            lows  = [float(k[3]) for k in klines]
            ath   = max(highs)
            atl   = min(lows)
            return {
                'ath': ath, 'atl': atl,
                'ath_distance_pct': (ath - current_price) / ath * 100,
                'atl_distance_pct': (current_price - atl) / atl * 100,
            }
        except Exception as e:
            print(f'❌ calculate_ath_atl: {e}')
            return None

    # ── polling scan (fallback / manual) ─────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return {}

        print(f'🏆 ATH/ATL Scanner — polling scan ({self.proximity_threshold}% threshold)...')
        try:
            if self._ws_manager and self._ws_manager.ready.is_set():
                raw = self._ws_manager.get_all_tickers()
                all_pairs = [
                    {'symbol': s, 'price': d['price'],
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
            pairs_to_analyze = all_pairs[:20] + (all_pairs[-20:] if len(all_pairs) >= 20 else [])

            ath_coins, atl_coins = [], []
            for pair in pairs_to_analyze:
                symbol = pair['symbol']
                price  = pair['price']

                # Use pre-computed cache if available and fresh
                with self._ath_cache_lock:
                    cached = self._ath_cache.get(symbol)
                if cached and (time.time() - cached['computed_at']) < ATH_CACHE_TTL:
                    aa_data = {'ath': cached['ath'], 'atl': cached['atl'],
                               'ath_distance_pct': (cached['ath'] - price) / cached['ath'] * 100,
                               'atl_distance_pct': (price - cached['atl']) / cached['atl'] * 100}
                else:
                    klines = self.fetch_historical_data(symbol, self.lookback_days)
                    if not klines:
                        continue
                    aa_data = self.calculate_ath_atl(klines, price)
                    if aa_data:
                        with self._ath_cache_lock:
                            self._ath_cache[symbol] = {
                                'ath': aa_data['ath'], 'atl': aa_data['atl'],
                                'computed_at': time.time()}

                if not aa_data:
                    continue

                with self._lock:
                    if self.ath_enabled and aa_data['ath_distance_pct'] <= self.proximity_threshold \
                            and not self.is_in_cooldown(symbol, 'ath'):
                        self.mark_alerted(symbol, 'ath')
                        ath_coins.append({'symbol': symbol, 'price': price,
                                          'ath': aa_data['ath'],
                                          'distance_pct': aa_data['ath_distance_pct'],
                                          'is_new_ath': price >= aa_data['ath'],
                                          'change_pct': pair['change_pct']})

                    if self.atl_enabled and 0 <= aa_data['atl_distance_pct'] <= self.proximity_threshold \
                            and not self.is_in_cooldown(symbol, 'atl'):
                        self.mark_alerted(symbol, 'atl')
                        atl_coins.append({'symbol': symbol, 'price': price,
                                          'atl': aa_data['atl'],
                                          'distance_pct': aa_data['atl_distance_pct'],
                                          'is_new_atl': price <= aa_data['atl'],
                                          'change_pct': pair['change_pct']})

            ath_coins = ath_coins[:self.max_coins_per_alert]
            atl_coins = atl_coins[:self.max_coins_per_alert]
            result    = {'ath': ath_coins, 'atl': atl_coins}

            if ath_coins or atl_coins:
                self.send_alert(result)

            return result

        except Exception as e:
            print(f'❌ ATH/ATL scanner error: {e}')
            return {}

    def send_alert(self, result):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, mtf_link
        except ImportError:
            return

        for coin in result.get('ath', [])[:3]:
            sym     = coin['symbol']
            caption = (f"{mtf_link(sym, self.ha_url)}  Nuovo ATH!" if coin['is_new_ath']
                       else f"{mtf_link(sym, self.ha_url)}  Vicino ATH\ndistanza: {coin['distance_pct']:.2f}%")
            img = get_chart(sym, interval='D', signal={'type': 'ath'})
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)

        for coin in result.get('atl', [])[:3]:
            sym     = coin['symbol']
            caption = (f"{mtf_link(sym, self.ha_url)}  Nuovo ATL!" if coin['is_new_atl']
                       else f"{mtf_link(sym, self.ha_url)}  Vicino ATL\ndistanza: {coin['distance_pct']:.2f}%")
            img = get_chart(sym, interval='D', signal={'type': 'atl'})
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
