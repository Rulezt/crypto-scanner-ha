"""Double Touch / Terzo Tocco Scanner — real-time via kline WebSocket"""
import threading
import requests
import time
import json
import os
import queue
import sys
import logging
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

COOLDOWN_FILE      = '/data/double_touch_cooldown.json'
MAX_COINS          = 1000
TOP_KLINE_SYMBOLS  = 500
KLINE_SUB_REFRESH  = 3600


class DoubleTouchScanner:
    _MAX_KLINES = 100   # cap passed to _find_double_touches; 100 bars is ample

    def __init__(self, telegram_config, enabled=True,
                 tolerance=0.5, proximity=2.0,
                 scan_tf=None, min_volume_24h=10_000_000,
                 scan_interval_minutes=240, cooldown_hours=12,
                 max_coins_per_alert=5,
                 max_freshness=30, min_gap=3, max_gap=60,
                 strict_mode=True,
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
        self.max_freshness       = int(max_freshness)
        self.min_gap             = int(min_gap)
        self.max_gap             = int(max_gap)
        self.strict_mode         = bool(strict_mode)
        self._live_config        = live_config

        self.last_alerts      = self._load_cooldown()
        self._lock            = threading.Lock()
        self._last_save       = 0.0
        self._last_scan_count = 0
        self._ws_manager      = ws_manager

        # LOG table pre-computed once; avoids O(n) rebuild inside every _scan_side call
        _sz = self._MAX_KLINES + 2
        self._log_table = [0] * _sz
        for _x in range(2, _sz):
            self._log_table[_x] = self._log_table[_x >> 1] + 1

        # maxsize=1000 prevents unbounded backlog if Telegram is slow
        self._alert_queue = queue.Queue(maxsize=1000)
        threading.Thread(target=self._alert_worker, daemon=True).start()

        if ws_manager is not None:
            ws_manager.add_kline_callback(self._on_kline)
            threading.Thread(target=self._init_kline_subs, daemon=True).start()

        mode = 'WebSocket' if ws_manager else 'polling'
        logger.info('🔁 Terzo Tocco Scanner init — tol=%.1f%% prox=%.1f%% tf=%s mode=%s',
                    self.tolerance, self.proximity, ','.join(self.scan_tfs), mode)

    # ── alert worker ──────────────────────────────────────────────────────────

    def _alert_worker(self):
        while True:
            patterns = self._alert_queue.get()
            try:
                self.send_alert(patterns)
            except Exception as e:
                logger.warning('⚠️ Terzo Tocco: alert error: %s', e)

    # ── cooldown ──────────────────────────────────────────────────────────────

    def _load_cooldown(self):
        try:
            if os.path.exists(COOLDOWN_FILE):
                with open(COOLDOWN_FILE, 'r') as f:
                    result = {}
                    for k, v in json.load(f).items():
                        dt = datetime.fromisoformat(v)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        result[k] = dt
                    return result
        except Exception as e:
            logger.warning('⚠️ Terzo Tocco: load cooldown: %s', e)
        return {}

    def _save_cooldown(self):
        # Must be called with self._lock held. Atomic write via tmp → replace.
        try:
            os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
            tmp = COOLDOWN_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump({k: v.astimezone(timezone.utc).isoformat()
                           for k, v in self.last_alerts.items()}, f)
            os.replace(tmp, COOLDOWN_FILE)
        except Exception as e:
            logger.warning('⚠️ Terzo Tocco: save cooldown: %s', e)

    def is_in_cooldown(self, key):
        if key not in self.last_alerts:
            return False
        return (datetime.now(timezone.utc) - self.last_alerts[key]) < timedelta(hours=self.cooldown_hours)

    def mark_alerted(self, key):
        # Must be called with self._lock held
        self.last_alerts[key] = datetime.now(timezone.utc)
        now = time.time()
        if now - self._last_save > 60:
            self._last_save = now
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
            logger.warning('⚠️ Terzo Tocco: WS not ready after 120s')
            return
        time.sleep(30)  # let ticker cache populate before first subscription
        self._kline_refresh_loop()

    def _kline_refresh_loop(self):
        while True:
            self._refresh_kline_subs()
            time.sleep(KLINE_SUB_REFRESH)

    def _refresh_kline_subs(self):
        tickers = self._ws_manager.get_all_tickers()
        if not tickers:
            return
        ranked = sorted(tickers.items(), key=lambda x: x[1].get('volume_24h', 0), reverse=True)
        top = [s for s, d in ranked
               if d.get('volume_24h', 0) >= self.min_volume_24h][:TOP_KLINE_SYMBOLS]
        self._ws_manager.subscribe_klines(top, intervals=self.scan_tfs)
        self._last_scan_count = len(top)
        logger.info('🔁 Terzo Tocco: subscribed klines %d symbols × %d TF', len(top), len(self.scan_tfs))

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
                try:
                    self._alert_queue.put_nowait([coin])
                except queue.Full:
                    logger.warning('⚠️ Terzo Tocco: alert queue full, dropping %s %s', symbol, interval)

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
            logger.error('❌ Terzo Tocco: fetch tickers: %s', e)
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
        candles = candles[-self._MAX_KLINES:]           # cap: 100 bars is sufficient
        n      = len(candles)
        highs  = [c['high']  for c in candles]
        lows   = [c['low']   for c in candles]
        closes = [c['close'] for c in candles]
        times  = [c['time']  for c in candles]

        # Build sparse tables ONCE per call, pass to both scan sides
        sp_h = self._build_sp(highs, max)
        sp_l = self._build_sp(lows,  min)

        patterns = (
            self._scan_side(highs, lows, closes, times, n, current_price, 'resistance', sp_h) +
            self._scan_side(highs, lows, closes, times, n, current_price, 'support',    sp_l)
        )

        # Normalized score: each dimension contributes equally regardless of units
        best = {}
        for p in patterns:
            t     = p['type']
            score = (p['freshness']     / self.max_freshness +
                     p['precision']     / max(self.tolerance, 1e-9) +
                     abs(p['dist_pct']) / max(self.proximity, 1e-9))
            if t not in best or score < best[t][0]:
                best[t] = (score, p)
        return [v[1] for v in best.values()]

    def _build_sp(self, arr, op):
        """Sparse table for range max/min: O(n log n) build, O(1) per query."""
        n  = len(arr)
        K  = self._log_table[n] + 1
        sp = [arr[:]]
        for k in range(1, K):
            prev = sp[k - 1]
            half = 1 << (k - 1)
            sp.append([op(prev[x], prev[x + half]) for x in range(n - (1 << k) + 1)])
        return sp

    def _scan_side(self, highs, lows, closes, times, n, current_price, mode, sp):
        tol_frac = self.tolerance / 100
        res      = mode == 'resistance'
        extreme  = highs if res else lows
        op       = max if res else min
        sentinel = float('-inf') if res else float('inf')
        LOG      = self._log_table               # pre-computed in __init__, never rebuilt
        patterns = []

        def rmq(l, r):                           # range max (res) or min (!res) inclusive
            if l > r: return sentinel
            k = LOG[r - l + 1]
            return op(sp[k][l], sp[k][r - (1 << k) + 1])

        # Prefix: O(1) violation check [0, i)
        pfx_ext = [sentinel] * (n + 1)
        for k in range(n):
            pfx_ext[k + 1] = op(pfx_ext[k], extreme[k])

        # Suffix: O(1) post-violation [j+1, n) and bounce check
        sfx_ext = [sentinel]                               * (n + 1)
        sfx_cls = [float('inf') if res else float('-inf')] * (n + 1)
        for k in range(n - 1, -1, -1):
            sfx_ext[k] = op(extreme[k], sfx_ext[k + 1])
            sfx_cls[k] = (min if res else max)(closes[k], sfx_cls[k + 1])

        for j in range(max(1, n - self.max_freshness), n):
            eJ = extreme[j]
            cJ = closes[j]
            # strict_mode: touch bar must show rejection (close not at the extreme)
            if self.strict_mode:
                if res     and cJ >= eJ: continue
                if not res and cJ <= eJ: continue

            j1 = j + 1

            for i in range(5, j - 2):
                eI = extreme[i]
                cI = closes[i]
                if self.strict_mode:
                    if res     and cI >= eI: continue
                    if not res and cI <= eI: continue

                ref  = max(eI, eJ) if res else min(eI, eJ)
                diff = abs(eI - eJ) / ref
                if diff > tol_frac:
                    continue

                level = (eI + eJ) / 2
                lower = level * (1 - tol_frac)
                upper = level * (1 + tol_frac)

                if res     and (cI >= level or cJ >= level): continue
                if not res and (cI <= level or cJ <= level): continue

                gap = j - i
                if gap < self.min_gap or gap > self.max_gap:
                    continue

                # Proximity filter — cheapest check first
                dist_pct = (current_price - level) / level * 100
                if abs(dist_pct) > self.proximity:
                    continue

                if res     and current_price >= level: continue
                if not res and current_price <= level: continue

                # All remaining checks are O(1) ─────────────────────────────
                if res     and pfx_ext[i] >= level: continue    # [0, i)
                if not res and pfx_ext[i] <= level: continue

                if res     and sfx_ext[j1] > level: continue    # [j+1, n)
                if not res and sfx_ext[j1] < level: continue

                if res     and rmq(i + 1, j - 1) >= level: continue  # (i, j)
                if not res and rmq(i + 1, j - 1) <= level: continue

                if res     and sfx_cls[j1] > lower: continue    # bounce
                if not res and sfx_cls[j1] < upper: continue

                patterns.append({
                    'type': mode, 'level': level,
                    'precision': diff * 100, 'gap': gap,
                    'freshness': max(1, n - j), 'dist_pct': dist_pct,
                    'touchATime': times[i],
                })

        return patterns

    # ── polling scan (fallback / manual) ──────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return []
        if self._ws_manager and self._ws_manager.ready.is_set():
            return []
        logger.info('🔁 Terzo Tocco Scanner — polling scan (tol=%.1f%% prox=%.1f%% tf=%s)...',
                    self.tolerance, self.proximity, ','.join(self.scan_tfs))
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
            logger.info('🔁 Terzo Tocco: %d pattern found', len(found))
            return found

        except Exception as e:
            logger.error('❌ Terzo Tocco scanner error: %s', e)
            return []

    # ── alert ─────────────────────────────────────────────────────────────────

    def send_alert(self, patterns):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, log_alert, fmt_vol
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
                f'- Var: {change:+.2f}%',
                f'- Volume: {fmt_vol(p.get("volume", 0))}',
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
            log_alert(sym, 'Terzo Tocco', emoji='🔁', note=segnale, tf=tf_label, screenshot=img)
            logger.info('🔁 Terzo Tocco alert: %s %s (%s)', sym, tf_label, p['type'])

    # ── status ────────────────────────────────────────────────────────────────

    def get_today_alerts(self):
        today = datetime.now(timezone.utc).date()
        symbols = set()
        with self._lock:
            for key, dt in self.last_alerts.items():
                if dt.date() == today:
                    symbols.add(key.rsplit('_', 2)[0])
        return list(symbols)

    def get_monitored_count(self):
        return self._last_scan_count
