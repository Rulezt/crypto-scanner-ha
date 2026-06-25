"""Double Touch / Terzo Tocco Scanner — real-time via kline WebSocket"""
import threading
import requests
import time
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COOLDOWN_FILE      = '/data/double_touch_cooldown.json'
MAX_COINS          = 1000
TOP_KLINE_SYMBOLS  = 500
KLINE_SUB_REFRESH  = 4 * 3600


class DoubleTouchScanner:
    def __init__(self, telegram_config, enabled=True,
                 tolerance=0.5, proximity=2.0,
                 scan_tf=None, min_volume_24h=10_000_000,
                 scan_interval_minutes=240, cooldown_hours=12,
                 max_coins_per_alert=5,
                 ws_manager=None, live_config=None, **kwargs):

        self.telegram_token      = telegram_config['token']
        self.telegram_chat_id    = telegram_config['chat_id']
        self.base_url            = telegram_config.get('base_url', '')
        self.enabled             = enabled
        self.tolerance           = float(tolerance)
        self.proximity           = float(proximity)
        raw_tf                   = scan_tf if scan_tf is not None else 'D'
        self.scan_tfs            = raw_tf if isinstance(raw_tf, list) else [raw_tf]
        self.min_volume_24h      = min_volume_24h
        self.max_coins_per_alert = max_coins_per_alert
        self.cooldown_hours      = cooldown_hours
        self._live_config        = live_config

        self.last_alerts      = self._load_cooldown()
        self._lock            = threading.Lock()
        self._last_scan_count = 0
        self._ws_manager      = ws_manager

        if ws_manager is not None:
            ws_manager.add_kline_callback(self._on_kline)
            threading.Thread(target=self._init_kline_subs, daemon=True).start()

        mode = 'WebSocket' if ws_manager else 'polling'
        print(f'🔁 Terzo Tocco Scanner init — tol={self.tolerance}% prox={self.proximity}% '
              f'tf={",".join(self.scan_tfs)} mode={mode}')

    # ── cooldown ──────────────────────────────────────────────────────────────

    def _load_cooldown(self):
        try:
            if os.path.exists(COOLDOWN_FILE):
                with open(COOLDOWN_FILE, 'r') as f:
                    return {k: datetime.fromisoformat(v) for k, v in json.load(f).items()}
        except Exception as e:
            print(f'⚠️ Terzo Tocco: load cooldown: {e}')
        return {}

    def _save_cooldown(self):
        try:
            os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
            with open(COOLDOWN_FILE, 'w') as f:
                json.dump({k: v.isoformat() for k, v in self.last_alerts.items()}, f)
        except Exception as e:
            print(f'⚠️ Terzo Tocco: save cooldown: {e}')

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

    # ── kline subscriptions ───────────────────────────────────────────────────

    def _init_kline_subs(self):
        if not self._ws_manager.ready.wait(timeout=120):
            print('⚠️ Terzo Tocco: WS not ready after 120s')
            return
        time.sleep(30)  # let ticker cache populate before first subscription
        self._refresh_kline_subs()

    def _refresh_kline_subs(self):
        tickers = self._ws_manager.get_all_tickers()
        if not tickers:
            return
        ranked = sorted(tickers.items(), key=lambda x: x[1].get('volume_24h', 0), reverse=True)
        top = [s for s, d in ranked
               if d.get('volume_24h', 0) >= self.min_volume_24h][:TOP_KLINE_SYMBOLS]
        self._ws_manager.subscribe_klines(top, intervals=self.scan_tfs)
        self._last_scan_count = len(top)
        print(f'🔁 Terzo Tocco: subscribed klines {len(top)} symbols × {len(self.scan_tfs)} TF')
        threading.Timer(KLINE_SUB_REFRESH, self._refresh_kline_subs).start()

    # ── WebSocket callback ────────────────────────────────────────────────────

    def _on_kline(self, symbol, interval, candle, is_closed):
        if not self.enabled or not is_closed:
            return
        if interval not in self.scan_tfs:
            return
        if not self._is_in_schedule():
            return

        klines = self._ws_manager.get_klines(symbol, interval)
        if len(klines) < 10:
            return

        ticker = self._ws_manager.get_all_tickers().get(symbol, {})
        if ticker.get('volume_24h', 0) < self.min_volume_24h:
            return

        current_price = ticker.get('price') or candle['close']
        patterns = self._find_double_touches(klines, current_price)

        for p in patterns:
            cooldown_key = f"{symbol}_{interval}_{p['type']}"
            coin = None
            with self._lock:
                if not self.is_in_cooldown(cooldown_key):
                    self.mark_alerted(cooldown_key)
                    coin = {
                        'symbol': symbol, 'tf': interval,
                        'price': current_price,
                        'volume': ticker.get('volume_24h', 0),
                        'change_pct': ticker.get('change_24h', 0.0),
                        **p,
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
            return result[:MAX_COINS]
        except Exception as e:
            print(f'❌ Terzo Tocco: fetch tickers: {e}')
            return []

    def _fetch_klines(self, symbol, tf):
        try:
            r = requests.get('https://api.bybit.com/v5/market/kline',
                             params={'category': 'linear', 'symbol': symbol,
                                     'interval': tf, 'limit': 100},
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
                post_violated = False
                for k in range(j + 1, n):
                    if candles[k]['close'] > level or candles[k]['high'] > level:
                        post_violated = True
                        break
                if post_violated:
                    continue
                # 3rd touch: price must have bounced away after touch 2 before returning
                bounced_away = any(
                    candles[k]['close'] <= level * (1 - tol_frac)
                    for k in range(j + 1, n)
                )
                if not bounced_away:
                    continue
                dist_pct = (current_price - level) / level * 100
                if abs(dist_pct) > prox_abs:
                    continue
                patterns.append({
                    'type': 'resistance', 'level': level,
                    'precision': diff * 100, 'gap': gap,
                    'freshness': max(1, n - j), 'dist_pct': dist_pct,
                    'touchATime': candles[i]['time'],
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
                # 3rd touch: price must have bounced away after touch 2 before returning
                bounced_away = any(
                    candles[k]['close'] >= level * (1 + tol_frac)
                    for k in range(j + 1, n)
                )
                if not bounced_away:
                    continue
                dist_pct = (current_price - level) / level * 100
                if abs(dist_pct) > prox_abs:
                    continue
                patterns.append({
                    'type': 'support', 'level': level,
                    'precision': diff * 100, 'gap': gap,
                    'freshness': max(1, n - j), 'dist_pct': dist_pct,
                    'touchATime': candles[i]['time'],
                })

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

    # ── polling scan (fallback / manual) ──────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return []
        # If WebSocket is active, polling is redundant but kept as manual fallback
        if self._ws_manager and self._ws_manager.ready.is_set():
            return []
        print(f'🔁 Terzo Tocco Scanner — polling scan (tol={self.tolerance}% prox={self.proximity}% tf={",".join(self.scan_tfs)})...')
        found = []
        try:
            tickers = self._fetch_tickers()
            self._last_scan_count = len(tickers)
            for i, ticker in enumerate(tickers):
                symbol = ticker['symbol']
                for tf in self.scan_tfs:
                    candles = self._fetch_klines(symbol, tf)
                    if len(candles) < 10:
                        continue
                    patterns = self._find_double_touches(candles, ticker['price'])
                    for p in patterns:
                        cooldown_key = f"{symbol}_{tf}_{p['type']}"
                        with self._lock:
                            if not self.is_in_cooldown(cooldown_key):
                                self.mark_alerted(cooldown_key)
                                found.append({'symbol': symbol, 'tf': tf,
                                              'price': ticker['price'],
                                              'volume': ticker['volume'],
                                              'change_pct': ticker.get('change_pct', 0.0), **p})
                if (i + 1) % 10 == 0:
                    time.sleep(0.5)

            found = found[:self.max_coins_per_alert]
            if found:
                self.send_alert(found)
            print(f'🔁 Terzo Tocco: {len(found)} pattern found')
            return found

        except Exception as e:
            print(f'❌ Terzo Tocco scanner error: {e}')
            return []

    # ── alert ─────────────────────────────────────────────────────────────────

    def send_alert(self, patterns):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, log_alert
        except ImportError:
            return
        TF_LABEL = {'D': '1D', '240': '4h', '60': '1h', '30': '30m', '15': '15m', '5': '5m', '1': '1m'}
        for p in patterns[:3]:
            sym      = p['symbol']
            tf       = p.get('tf', self.scan_tfs[0])
            tf_label = TF_LABEL.get(tf, tf)
            segnale  = 'Long' if p['type'] == 'resistance' else 'Short'
            change   = p.get('change_pct', 0.0)
            lines    = [
                f'🔔 Terzo Tocco {tf_label}',
                '',
                '------------------------------------------------',
                f'- Coin: {sym}',
                f'- Vol: {change:+.2f}%',
                '------------------------------------------------',
            ]
            base = (self.base_url or 'https://cryptoscannerpro.com').rstrip('/')
            lines.append('')
            lines.append(f'<a href="https://www.bybit.com/trade/usdt/{sym}">- View Bybit</a>')
            lines.append(f'<a href="{base}/mtf?symbol={sym}">- View Desktop</a>')
            lines.append(f'<a href="{base}/chart?symbol={sym}&layout=1x1">- View Mobile</a>')
            caption = '\n'.join(lines)
            img = get_chart(sym, interval=tf, signal={
                'type': 'price',
                'price': p['level'],
                'condition': 'above' if p['type'] == 'resistance' else 'below',
                'time': p.get('touchATime'),
            })
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
            log_alert(sym, 'Terzo Tocco', emoji='🔁', note=segnale, tf=tf_label)
            print(f'🔁 Terzo Tocco alert: {sym} {tf_label} ({p["type"]})')

    # ── status ────────────────────────────────────────────────────────────────

    def get_today_alerts(self):
        today = datetime.utcnow().date()
        symbols = set()
        with self._lock:
            for key, dt in self.last_alerts.items():
                if dt.date() == today:
                    symbols.add(key.rsplit('_', 2)[0])
        return list(symbols)

    def get_monitored_count(self):
        return self._last_scan_count
