"""Double Touch / Terzo Tocco Scanner"""
import threading
import requests
import time
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COOLDOWN_FILE = '/data/double_touch_cooldown.json'
MAX_COINS = 200


class DoubleTouchScanner:
    def __init__(self, telegram_config, enabled=True,
                 tolerance=0.5, proximity=2.0,
                 scan_tf='D', min_volume_24h=10_000_000,
                 scan_interval_minutes=240, cooldown_hours=12,
                 max_coins_per_alert=5, ws_manager=None, **kwargs):

        self.telegram_token   = telegram_config['token']
        self.telegram_chat_id = telegram_config['chat_id']
        self.ha_url           = telegram_config.get('ha_url', '')
        self.enabled          = enabled
        self.tolerance        = float(tolerance)
        self.proximity        = float(proximity)
        self.scan_tf          = scan_tf
        self.min_volume_24h   = min_volume_24h
        self.max_coins_per_alert = max_coins_per_alert
        self.cooldown_hours   = cooldown_hours

        self.last_alerts = self._load_cooldown()
        self._lock       = threading.Lock()

        print(f'🔁 Terzo Tocco Scanner init — tol={self.tolerance}% prox={self.proximity}% tf={self.scan_tf}')

    # ── cooldown ─────────────────────────────────────────────────────────────

    def _load_cooldown(self):
        try:
            if os.path.exists(COOLDOWN_FILE):
                with open(COOLDOWN_FILE, 'r') as f:
                    return {k: datetime.fromisoformat(v) for k, v in json.load(f).items()}
        except Exception as e:
            print(f'⚠️ Error loading double touch cooldown: {e}')
        return {}

    def _save_cooldown(self):
        try:
            os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
            with open(COOLDOWN_FILE, 'w') as f:
                json.dump({k: v.isoformat() for k, v in self.last_alerts.items()}, f)
        except Exception as e:
            print(f'⚠️ Error saving double touch cooldown: {e}')

    def is_in_cooldown(self, key):
        if key not in self.last_alerts:
            return False
        return (datetime.now() - self.last_alerts[key]) < timedelta(hours=self.cooldown_hours)

    def mark_alerted(self, key):
        self.last_alerts[key] = datetime.now()
        self._save_cooldown()

    # ── data fetching ─────────────────────────────────────────────────────────

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
                result.append({'symbol': item['symbol'], 'price': price, 'volume': vol})
            result.sort(key=lambda x: x['volume'], reverse=True)
            return result[:MAX_COINS]
        except Exception as e:
            print(f'❌ Double touch: fetch tickers: {e}')
            return []

    def _fetch_klines(self, symbol):
        try:
            r = requests.get('https://api.bybit.com/v5/market/kline',
                             params={'category': 'linear', 'symbol': symbol,
                                     'interval': self.scan_tf, 'limit': 100},
                             timeout=10)
            data = r.json()
            if data.get('retCode') != 0:
                return []
            # Bybit newest-first → reverse, drop last (incomplete) candle
            raw = list(reversed(data['result']['list']))[:-1]
            return [{'time':  int(c[0]) // 1000,
                     'open':  float(c[1]), 'high': float(c[2]),
                     'low':   float(c[3]), 'close': float(c[4])}
                    for c in raw]
        except Exception:
            return []

    # ── algorithm ─────────────────────────────────────────────────────────────

    def _find_double_touches(self, candles, current_price):
        tol_frac      = self.tolerance / 100
        prox_abs      = self.proximity
        max_freshness = 30
        n             = len(candles)
        patterns      = []

        # ── RESISTANCE: two High touches ──────────────────────────────────────
        for j in range(max(1, n - max_freshness), n):
            hJ = candles[j]['high']
            cJ = candles[j]['close']
            if cJ >= hJ:
                continue
            for i in range(5, j - 2):
                hI = candles[i]['high']
                cI = candles[i]['close']
                if cI >= hI:
                    continue
                diff = abs(hI - hJ) / max(hI, hJ)
                if diff > tol_frac:
                    continue
                level = (hI + hJ) / 2
                if cI >= level or cJ >= level:
                    continue
                gap = j - i
                if gap < 3 or gap > 60:
                    continue
                # Level never violated in full history before j (except T1)
                violated = False
                for k in range(j):
                    if k == i:
                        continue
                    c = candles[k]
                    if c['high'] >= level or c['close'] > level:
                        violated = True
                        break
                if violated:
                    continue
                if current_price >= level:
                    continue
                # No post-T2 violation
                post_violated = False
                for k in range(j + 1, n):
                    if candles[k]['close'] > level or candles[k]['high'] > level:
                        post_violated = True
                        break
                if post_violated:
                    continue
                dist_pct = (current_price - level) / level * 100
                if abs(dist_pct) > prox_abs:
                    continue
                patterns.append({
                    'type': 'resistance', 'level': level,
                    'precision': diff * 100, 'gap': gap,
                    'freshness': max(1, n - j), 'dist_pct': dist_pct,
                })

        # ── SUPPORT: two Low touches ──────────────────────────────────────────
        for j in range(max(1, n - max_freshness), n):
            lJ = candles[j]['low']
            cJ = candles[j]['close']
            if cJ <= lJ:
                continue
            for i in range(5, j - 2):
                lI = candles[i]['low']
                cI = candles[i]['close']
                if cI <= lI:
                    continue
                diff = abs(lI - lJ) / min(lI, lJ)
                if diff > tol_frac:
                    continue
                level = (lI + lJ) / 2
                if cI <= level or cJ <= level:
                    continue
                gap = j - i
                if gap < 3 or gap > 60:
                    continue
                violated = False
                for k in range(j):
                    if k == i:
                        continue
                    c = candles[k]
                    if c['low'] <= level or c['close'] < level:
                        violated = True
                        break
                if violated:
                    continue
                if current_price <= level:
                    continue
                post_violated = False
                for k in range(j + 1, n):
                    if candles[k]['close'] < level or candles[k]['low'] < level:
                        post_violated = True
                        break
                if post_violated:
                    continue
                dist_pct = (current_price - level) / level * 100
                if abs(dist_pct) > prox_abs:
                    continue
                patterns.append({
                    'type': 'support', 'level': level,
                    'precision': diff * 100, 'gap': gap,
                    'freshness': max(1, n - j), 'dist_pct': dist_pct,
                })

        # Keep best pattern per type (freshest, then most precise)
        best = {}
        for p in patterns:
            t = p['type']
            if t not in best:
                best[t] = p
            else:
                b = best[t]
                if p['freshness'] < b['freshness']:
                    best[t] = p
                elif p['freshness'] == b['freshness'] and p['precision'] < b['precision']:
                    best[t] = p
        return list(best.values())

    # ── polling scan ──────────────────────────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return []
        print(f'🔁 Terzo Tocco Scanner — scanning top {MAX_COINS} coin (tol={self.tolerance}% prox={self.proximity}% tf={self.scan_tf})...')
        found = []
        try:
            tickers = self._fetch_tickers()
            for i, ticker in enumerate(tickers):
                symbol  = ticker['symbol']
                candles = self._fetch_klines(symbol)
                if len(candles) < 10:
                    continue
                patterns = self._find_double_touches(candles, ticker['price'])
                for p in patterns:
                    cooldown_key = f"{symbol}_{p['type']}"
                    with self._lock:
                        if not self.is_in_cooldown(cooldown_key):
                            self.mark_alerted(cooldown_key)
                            found.append({'symbol': symbol,
                                          'price': ticker['price'],
                                          'volume': ticker['volume'], **p})
                # Gentle rate limit
                if (i + 1) % 10 == 0:
                    time.sleep(0.5)

            found = found[:self.max_coins_per_alert]
            if found:
                self.send_alert(found)
            print(f'🔁 Terzo Tocco: {len(found)} pattern found')
            return found

        except Exception as e:
            print(f'❌ Double touch scanner error: {e}')
            return []

    def send_alert(self, patterns):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, mtf_link
        except ImportError:
            return
        for p in patterns[:3]:
            sym      = p['symbol']
            sign_str = '🟢 Long (resistenza)' if p['type'] == 'resistance' else '🔴 Short (supporto)'
            sign_dist = f"{'+' if p['dist_pct'] >= 0 else ''}{p['dist_pct']:.2f}%"
            def _fmt(v):
                if v >= 1e9: return f'${v/1e9:.1f}B'
                if v >= 1e6: return f'${v/1e6:.0f}M'
                return f'${v/1e3:.0f}K'
            caption = (f"{mtf_link(sym, self.ha_url)}  Terzo Tocco · {self.scan_tf}\n"
                       f"{sign_str}\n"
                       f"dist: {sign_dist} · gap: {p['gap']} · fresh: {p['freshness']}\n"
                       f"vol: {_fmt(p['volume'])}")
            img = get_chart(sym, interval=self.scan_tf, signal={'type': 'ema'})
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
            print(f'Terzo Tocco alert: {sym} ({p["type"]})')
