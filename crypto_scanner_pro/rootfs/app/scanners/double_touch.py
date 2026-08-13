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
                 min_var_pct_24h=5.0,
                 scan_interval_minutes=240, cooldown_hours=12,
                 max_coins_per_alert=5,
                 max_freshness=30, min_gap=3, max_gap=60,
                 strict_mode=True,
                 ch_pivot_bars=3, ch_min_cup_bars=15, ch_max_cup_bars=80,
                 ch_min_handle_bars=2, ch_max_handle_bars=25,
                 ch_min_cup_depth_pct=3.0, ch_max_handle_retrace=0.5,
                 chn_short_period=30, chn_long_period=80,
                 chn_tolerance_pct=1.5, chn_parallel_ratio=0.5,
                 chn_max_violation_ratio=0.15,
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
        self.min_var_pct_24h     = min_var_pct_24h
        self.max_coins_per_alert = max_coins_per_alert
        self.cooldown_hours      = cooldown_hours
        self.max_freshness       = int(max_freshness)
        self.min_gap             = int(min_gap)
        self.max_gap             = int(max_gap)
        self.strict_mode         = bool(strict_mode)
        self.ch_pivot_bars         = int(ch_pivot_bars)
        self.ch_min_cup_bars       = int(ch_min_cup_bars)
        self.ch_max_cup_bars       = int(ch_max_cup_bars)
        self.ch_min_handle_bars    = int(ch_min_handle_bars)
        self.ch_max_handle_bars    = int(ch_max_handle_bars)
        self.ch_min_cup_depth_pct  = float(ch_min_cup_depth_pct)
        self.ch_max_handle_retrace = float(ch_max_handle_retrace)
        self.chn_short_period     = int(chn_short_period)
        self.chn_long_period      = int(chn_long_period)
        self.chn_tolerance_pct    = float(chn_tolerance_pct)
        self.chn_parallel_ratio   = float(chn_parallel_ratio)
        self.chn_max_violation_ratio = float(chn_max_violation_ratio)
        self._live_config        = live_config

        self.last_alerts      = self._load_cooldown()
        self._lock            = threading.Lock()
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
        # Must be called with self._lock held. Salvataggio sempre immediato (non
        # throttlato): un riavvio del container entro la finestra di throttle perdeva
        # il segno su disco dell'alert appena inviato — nessun handler di shutdown
        # flusha lo stato in-memory, quindi al riavvio successivo quell'alert veniva
        # rivalutato come "mai inviato" e reinviato su Telegram.
        self.last_alerts[key] = datetime.now(timezone.utc)
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
               if d.get('volume_24h', 0) >= self.min_volume_24h
               and (not self.min_var_pct_24h or d.get('change_24h', 0) >= self.min_var_pct_24h)][:TOP_KLINE_SYMBOLS]
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
        if self.min_var_pct_24h and ticker.get('change_24h', 0.0) < self.min_var_pct_24h:
            return

        current_price = ticker.get('price') or candle['close']
        patterns = self._find_double_touches(klines, current_price)

        for p in patterns:
            cooldown_key = f"{symbol}_{interval}"
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
                change_pct = float(item.get('price24hPcnt', 0) or 0) * 100
                if price <= 0 or vol < self.min_volume_24h:
                    continue
                if self.min_var_pct_24h and change_pct < self.min_var_pct_24h:
                    continue
                result.append({'symbol': item['symbol'], 'price': price, 'volume': vol,
                               'change_pct': change_pct})
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
            self._scan_side(highs, lows, closes, times, n, current_price, 'support',    sp_l) +
            self._find_cup_handle(highs, lows, closes, times, n, current_price) +
            self._find_inverse_cup_handle(highs, lows, closes, times, n, current_price) +
            self._find_channel_patterns(highs, lows, closes, times, n, current_price)
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

                # Price must be approaching the level, not receding from it
                prev_close = closes[-2]
                if abs(current_price - level) > abs(prev_close - level):
                    continue

                if res     and current_price >= level: continue
                if not res and current_price <= level: continue

                # All remaining checks are O(1) ─────────────────────────────
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

    # ── Cup and Handle ───────────────────────────────────────────────────────
    # Sempre bullish (Long): forma a "U" (left lip → cup bottom → right lip)
    # seguita da un ritracciamento breve (handle), poi avvicinamento al breakout
    # sopra il right lip. Stesso schema concettuale del terzo tocco (touchA/touchB
    # + avvicinamento corrente), con vincoli di forma aggiuntivi (profondità coppa,
    # simmetria dei due lip, handle poco profondo e breve).

    @staticmethod
    def _find_pivots(highs, lows, n, bars):
        """Fractal pivot: massimo/minimo locale con `bars` barre più basse/alte su entrambi i lati."""
        piv_hi, piv_lo = [], []
        for i in range(bars, n - bars):
            hi = highs[i]
            if all(hi > highs[i - k] for k in range(1, bars + 1)) and \
               all(hi > highs[i + k] for k in range(1, bars + 1)):
                piv_hi.append(i)
            lo = lows[i]
            if all(lo < lows[i - k] for k in range(1, bars + 1)) and \
               all(lo < lows[i + k] for k in range(1, bars + 1)):
                piv_lo.append(i)
        return piv_hi, piv_lo

    def _find_cup_handle(self, highs, lows, closes, times, n, current_price):
        min_needed = self.ch_min_cup_bars + self.ch_min_handle_bars + 2 * self.ch_pivot_bars + 2
        if n < min_needed:
            return []

        piv_hi, piv_lo = self._find_pivots(highs, lows, n, self.ch_pivot_bars)
        if len(piv_hi) < 2 or not piv_lo:
            return []

        tol_frac = self.tolerance / 100
        patterns = []

        for a in range(len(piv_hi)):
            Lidx = piv_hi[a]
            L = highs[Lidx]
            for b in range(a + 1, len(piv_hi)):
                Ridx = piv_hi[b]
                gap = Ridx - Lidx
                if gap < self.ch_min_cup_bars:
                    continue
                if gap > self.ch_max_cup_bars:
                    break  # piv_hi crescente: b successivi hanno gap solo maggiore

                R = highs[Ridx]
                ref = max(L, R)
                if abs(L - R) / ref > tol_frac:
                    continue

                between = lows[Lidx + 1:Ridx]
                if not between:
                    continue
                B = min(between)
                if (ref - B) / ref < self.ch_min_cup_depth_pct / 100:
                    continue

                upper = ref * (1 + tol_frac)
                if any(h > upper for h in highs[Lidx + 1:Ridx]):
                    continue  # nulla fra i due lip deve aver già superato la banda

                for Hidx in piv_lo:
                    if Hidx <= Ridx:
                        continue
                    hgap = Hidx - Ridx
                    if hgap < self.ch_min_handle_bars:
                        continue
                    if hgap > self.ch_max_handle_bars:
                        break  # piv_lo crescente

                    H = lows[Hidx]
                    if H <= B or H >= R:
                        continue
                    if (R - H) / (R - B) > self.ch_max_handle_retrace:
                        continue

                    if any(c > upper for c in closes[Ridx + 1:Hidx]):
                        continue  # handle pulito: non richiude sopra il lip prima del tempo
                    if any(c > R for c in closes[Hidx + 1:n]):
                        continue  # ancora nessun breakout confermato

                    dist_pct = (current_price - R) / R * 100
                    if abs(dist_pct) > self.proximity:
                        continue
                    if current_price >= R:
                        continue
                    prev_close = closes[-2]
                    if abs(current_price - R) > abs(prev_close - R):
                        continue

                    highest_high = max(highs[Lidx:Ridx + 1])
                    patterns.append({
                        'type': 'cup_handle', 'level': R,
                        'precision': abs(L - R) / ref * 100, 'gap': gap,
                        'freshness': max(1, n - Hidx), 'dist_pct': dist_pct,
                        'touchATime': times[Lidx],
                        'target': R + (highest_high - B), 'stop': R,
                        'cupBottom': B, 'leftLip': L, 'handleLow': H,
                    })

        return patterns

    # ── Inverse Cup and Handle ───────────────────────────────────────────────
    # Sempre bearish (Short): specchio verticale del Cup and Handle — "U" rovesciata
    # (left lip → cup top → right lip) seguita da un breve rimbalzo (handle) e
    # avvicinamento al breakout sotto il right lip.

    def _find_inverse_cup_handle(self, highs, lows, closes, times, n, current_price):
        min_needed = self.ch_min_cup_bars + self.ch_min_handle_bars + 2 * self.ch_pivot_bars + 2
        if n < min_needed:
            return []

        piv_hi, piv_lo = self._find_pivots(highs, lows, n, self.ch_pivot_bars)
        if len(piv_lo) < 2 or not piv_hi:
            return []

        tol_frac = self.tolerance / 100
        patterns = []

        for a in range(len(piv_lo)):
            Lidx = piv_lo[a]
            L = lows[Lidx]
            for b in range(a + 1, len(piv_lo)):
                Ridx = piv_lo[b]
                gap = Ridx - Lidx
                if gap < self.ch_min_cup_bars:
                    continue
                if gap > self.ch_max_cup_bars:
                    break  # piv_lo crescente: b successivi hanno gap solo maggiore

                R = lows[Ridx]
                ref = min(L, R)
                if abs(L - R) / ref > tol_frac:
                    continue

                between = highs[Lidx + 1:Ridx]
                if not between:
                    continue
                T = max(between)
                if (T - ref) / ref < self.ch_min_cup_depth_pct / 100:
                    continue

                lower = ref * (1 - tol_frac)
                if any(l < lower for l in lows[Lidx + 1:Ridx]):
                    continue  # nulla fra i due lip deve aver già rotto sotto la banda

                for Hidx in piv_hi:
                    if Hidx <= Ridx:
                        continue
                    hgap = Hidx - Ridx
                    if hgap < self.ch_min_handle_bars:
                        continue
                    if hgap > self.ch_max_handle_bars:
                        break  # piv_hi crescente

                    H = highs[Hidx]
                    if H >= T or H <= R:
                        continue
                    if (H - R) / (T - R) > self.ch_max_handle_retrace:
                        continue

                    if any(c < lower for c in closes[Ridx + 1:Hidx]):
                        continue  # handle pulito: non richiude sotto il lip prima del tempo
                    if any(c < R for c in closes[Hidx + 1:n]):
                        continue  # ancora nessun breakdown confermato

                    dist_pct = (current_price - R) / R * 100
                    if abs(dist_pct) > self.proximity:
                        continue
                    if current_price <= R:
                        continue
                    prev_close = closes[-2]
                    if abs(current_price - R) > abs(prev_close - R):
                        continue

                    lowest_low = min(lows[Lidx:Ridx + 1])
                    patterns.append({
                        'type': 'inv_cup_handle', 'level': R,
                        'precision': abs(L - R) / ref * 100, 'gap': gap,
                        'freshness': max(1, n - Hidx), 'dist_pct': dist_pct,
                        'touchATime': times[Lidx],
                        'target': R - (T - lowest_low), 'stop': R,
                        'cupTop': T, 'leftLip': L, 'handleHigh': H,
                    })

        return patterns

    # ── Channel Up / Channel Down ────────────────────────────────────────────
    # Porting dell'engine a 2 pivot già collaudato in trendline.html (pivotExtreme/
    # trendlineAt/isLineValid): pivot "recente" (offset 1..short_period da iEnd) e
    # pivot "vecchio" (offset short_period+1..long_period), separati per massimi
    # (resistenza) e minimi (supporto). Canale = le due rette hanno stessa direzione
    # e pendenza simile (parallele entro chn_parallel_ratio), mai invalidate da una
    # chiusura oltre tolleranza da quando esistono. Trigger sempre in continuazione
    # (avvicinamento al lato che si prevede rotto), mai sul rimbalzo interno al canale.

    @staticmethod
    def _pivot_extreme(highs, lows, i_end, off_from, off_to):
        low_val, low_off, high_val, high_off = float('inf'), None, float('-inf'), None
        for off in range(off_from, off_to + 1):
            idx = i_end - off
            if idx < 0:
                return None
            if lows[idx] < low_val:
                low_val, low_off = lows[idx], off
            if highs[idx] > high_val:
                high_val, high_off = highs[idx], off
        return low_val, low_off, high_val, high_off

    def _trendline_at(self, highs, lows, i_end, short_period, long_period):
        if i_end - long_period < 0:
            return None
        rec = self._pivot_extreme(highs, lows, i_end, 1, short_period)
        old = self._pivot_extreme(highs, lows, i_end, short_period + 1, long_period)
        if not rec or not old:
            return None
        rec_low_val, rec_low_off, rec_high_val, rec_high_off = rec
        old_low_val, old_low_off, old_high_val, old_high_off = old

        sup_x1, sup_y1 = i_end - old_low_off,  old_low_val
        sup_x2, sup_y2 = i_end - rec_low_off,  rec_low_val
        res_x1, res_y1 = i_end - old_high_off, old_high_val
        res_x2, res_y2 = i_end - rec_high_off, rec_high_val
        sup_slope = (sup_y2 - sup_y1) / (sup_x2 - sup_x1) if sup_x2 != sup_x1 else 0.0
        res_slope = (res_y2 - res_y1) / (res_x2 - res_x1) if res_x2 != res_x1 else 0.0
        return {
            'sup_x1': sup_x1, 'sup_y1': sup_y1, 'sup_x2': sup_x2, 'sup_y2': sup_y2,
            'sup_slope': sup_slope, 'sup_val': sup_y2 + sup_slope * (i_end - sup_x2),
            'res_x1': res_x1, 'res_y1': res_y1, 'res_x2': res_x2, 'res_y2': res_y2,
            'res_slope': res_slope, 'res_val': res_y2 + res_slope * (i_end - res_x2),
        }

    @staticmethod
    def _channel_containment_ok(highs, lows, tl, n, tolerance_pct, max_violation_ratio):
        """Un vero canale deve CONTENERE il prezzo (high sotto la resistenza, low
        sopra il supporto), non solo avere chiusure vicine — controllare solo il
        close (come in trendline.html per una singola retta) lascia passare rette
        che tagliano dritto in mezzo alle candele. Qui si ammette una piccola quota
        di violazioni (wick isolati) ma non un canale che il prezzo attraversa spesso."""
        start = min(tl['sup_x1'], tl['res_x1'])
        tol_frac = tolerance_pct / 100
        total = violations = 0
        for idx in range(start, n):
            sup_line = tl['sup_y1'] + tl['sup_slope'] * (idx - tl['sup_x1'])
            res_line = tl['res_y1'] + tl['res_slope'] * (idx - tl['res_x1'])
            if res_line <= sup_line:
                return False  # rette incrociate in questo punto, canale non valido
            total += 1
            sup_tol = abs(sup_line) * tol_frac
            res_tol = abs(res_line) * tol_frac
            if highs[idx] > res_line + res_tol or lows[idx] < sup_line - sup_tol:
                violations += 1
        return total > 0 and (violations / total) <= max_violation_ratio

    def _find_channel_patterns(self, highs, lows, closes, times, n, current_price):
        if n < self.chn_long_period + 5:
            return []
        i_end = n - 1
        tl = self._trendline_at(highs, lows, i_end, self.chn_short_period, self.chn_long_period)
        if not tl:
            return []

        sup_slope, res_slope = tl['sup_slope'], tl['res_slope']
        lo, hi = sorted((abs(sup_slope), abs(res_slope)))
        if hi == 0 or lo / hi < self.chn_parallel_ratio:
            return []  # rette non abbastanza parallele (pendenze troppo diverse)
        if tl['res_val'] <= tl['sup_val']:
            return []  # canale collassato/incrociato

        # Un canale vero mantiene una larghezza pressoché costante nel tempo. Il solo
        # rapporto fra le pendenze non basta: due rette possono avere pendenze "simili
        # in rapporto" (es. entrambe piccole) mentre in realtà divergono a ventaglio
        # (cuneo) partendo quasi dallo stesso punto — visto in un caso reale con
        # screenshot allegato dall'utente. Si confronta la larghezza all'inizio delle
        # rette con quella attuale: se cambia troppo, non è un canale parallelo.
        start_idx = min(tl['sup_x1'], tl['res_x1'])
        sup_at_start = tl['sup_y1'] + sup_slope * (start_idx - tl['sup_x1'])
        res_at_start = tl['res_y1'] + res_slope * (start_idx - tl['res_x1'])
        width_start = res_at_start - sup_at_start
        width_now   = tl['res_val'] - tl['sup_val']
        if width_start <= 0:
            return []
        width_ratio = width_now / width_start
        if width_ratio < 0.5 or width_ratio > 2.0:
            return []  # larghezza raddoppiata/dimezzata: cuneo, non canale

        if not self._channel_containment_ok(highs, lows, tl, n, self.chn_tolerance_pct, self.chn_max_violation_ratio):
            return []

        width     = tl['res_val'] - tl['sup_val']
        precision = (1 - lo / hi) * 100
        patterns  = []

        # Entrambe le rette (non solo quella del breakout) così chi disegna il
        # grafico può mostrare il canale intero, come nel pattern di riferimento.
        line_pts = {
            'supP1Time': times[tl['sup_x1']], 'supP1Price': tl['sup_y1'],
            'supP2Time': times[tl['sup_x2']], 'supP2Price': tl['sup_y2'],
            'resP1Time': times[tl['res_x1']], 'resP1Price': tl['res_y1'],
            'resP2Time': times[tl['res_x2']], 'resP2Price': tl['res_y2'],
        }

        if sup_slope > 0 and res_slope > 0:
            res_val = tl['res_val']
            dist_pct = (current_price - res_val) / res_val * 100
            if (abs(dist_pct) <= self.proximity and current_price < res_val and
                    abs(current_price - res_val) <= abs(closes[-2] - res_val)):
                patterns.append({
                    'type': 'channel_up', 'level': res_val,
                    'precision': precision, 'gap': i_end - tl['res_x1'],
                    'freshness': max(1, n - tl['res_x2']), 'dist_pct': dist_pct,
                    'touchATime': times[tl['res_x1']],
                    'target': res_val + width, 'stop': res_val,
                    **line_pts,
                })

        if sup_slope < 0 and res_slope < 0:
            sup_val = tl['sup_val']
            dist_pct = (current_price - sup_val) / sup_val * 100
            if (abs(dist_pct) <= self.proximity and current_price > sup_val and
                    abs(current_price - sup_val) <= abs(closes[-2] - sup_val)):
                patterns.append({
                    'type': 'channel_down', 'level': sup_val,
                    'precision': precision, 'gap': i_end - tl['sup_x1'],
                    'freshness': max(1, n - tl['sup_x2']), 'dist_pct': dist_pct,
                    'touchATime': times[tl['sup_x1']],
                    'target': sup_val - width, 'stop': sup_val,
                    **line_pts,
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
                        cooldown_key = f"{symbol}_{tf}"
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
        # (etichetta pattern, segnale Long/Short, condizione linea sul grafico 'above'/'below')
        TYPE_INFO = {
            'resistance':     ('Terzo Tocco',            'Short', 'below'),
            'support':        ('Terzo Tocco',            'Long',  'above'),
            'cup_handle':     ('Cup & Handle',            'Long',  'above'),
            'inv_cup_handle': ('Inverse Cup & Handle',    'Short', 'below'),
            'channel_up':     ('Channel Up',              'Long',  'above'),
            'channel_down':   ('Channel Down',            'Short', 'below'),
        }
        for p in patterns[:3]:
            sym      = p['symbol']
            tf       = p.get('tf', self.scan_tfs[0])
            tf_label = TF_LABEL.get(tf, tf)
            pattern_label, segnale, condition = TYPE_INFO.get(p['type'], ('Terzo Tocco', 'Long', 'above'))
            change   = p.get('change_pct', 0.0)
            lines    = [
                f'🔔 {pattern_label} {tf_label}',
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
            lines.append(f'<a href="{base}/trade?symbol={sym}">- View Mobile</a>')
            caption = '\n'.join(lines)
            img = get_chart(sym, interval=tf, signal={
                'type': 'price',
                'price': p['level'],
                'condition': condition,
                'time': p.get('touchATime'),
                'target': p.get('target'),
                'stop': p.get('stop'),
                'sup_p1_time': p.get('supP1Time'), 'sup_p1_price': p.get('supP1Price'),
                'sup_p2_time': p.get('supP2Time'), 'sup_p2_price': p.get('supP2Price'),
                'res_p1_time': p.get('resP1Time'), 'res_p1_price': p.get('resP1Price'),
                'res_p2_time': p.get('resP2Time'), 'res_p2_price': p.get('resP2Price'),
            })
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
            log_alert(sym, pattern_label, emoji='🔁', note=segnale, tf=tf_label, screenshot=img)
            logger.info('🔁 %s alert: %s %s (%s)', pattern_label, sym, tf_label, p['type'])

    # ── status ────────────────────────────────────────────────────────────────

    def get_today_alerts(self):
        today = datetime.now(timezone.utc).date()
        symbols = set()
        with self._lock:
            for key, dt in self.last_alerts.items():
                if dt.date() == today:
                    symbols.add(key.rsplit('_', 1)[0])
        return list(symbols)

    def get_monitored_count(self):
        return self._last_scan_count
