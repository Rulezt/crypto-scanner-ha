"""EMA60 Pre-Touch Proximity Scanner — Multi-TF Independent Regime
Architecture: one EMA per TF per symbol, each TF tracked and alerted independently.
Touch on a TF is a permanent kill signal for THAT TF only (for the rest of the day) —
it never blocks the other TFs. Any configured TF can fire its own proximity alert.
"""
import threading
import queue
import time
import json
import os
import sys
import requests
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STATE_FILE        = '/data/ema_state.json'
TOP_KLINE_SYMBOLS = 500
KLINE_SUB_REFRESH = 3600
EMA_PERIOD        = 60
MIN_SEED_BARS     = EMA_PERIOD + 5

DEFAULT_SCAN_TFS  = ['240', '60', '30', '5', '1']


class EMAScanner:
    def __init__(self, telegram_config, enabled=True, ema_touch_threshold=2.0,
                 touch_tolerance=0.05,
                 scan_interval_minutes=30, min_volume_24h=10_000_000,
                 max_coins_per_alert=10, screenshot_tf='30',
                 scan_tfs=None,
                 ws_manager=None, live_config=None, **kwargs):

        self.telegram_token      = telegram_config['token']
        self.telegram_chat_id    = telegram_config['chat_id']
        self.base_url            = telegram_config.get('base_url', '')
        self.enabled             = enabled
        self.ema_touch_threshold = ema_touch_threshold
        self.touch_tolerance     = touch_tolerance
        self.min_volume_24h      = min_volume_24h
        self.max_coins_per_alert = max_coins_per_alert
        self.screenshot_tf       = screenshot_tf
        self._live_config        = live_config

        raw_tfs         = scan_tfs or DEFAULT_SCAN_TFS
        self.scan_tfs   = raw_tfs if isinstance(raw_tfs, list) else [raw_tfs]

        self._lock        = threading.Lock()
        self._state       = self._load_state()
        self._alert_queue = queue.Queue(maxsize=200)
        threading.Thread(target=self._alert_worker, daemon=True).start()

        self._ws_manager = ws_manager
        if ws_manager is not None:
            ws_manager.add_kline_callback(self._on_kline)
            threading.Thread(target=self._init_kline_subs, daemon=True).start()

        print(f'📡 EMA Proximity init — tfs={",".join(self.scan_tfs)} (indipendenti) thr={self.ema_touch_threshold}%')

    # ── State persistence ─────────────────────────────────────────────────────

    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            print(f'⚠️ EMA: load state: {e}')
        return {}

    def _save_state(self):
        with self._lock:
            snapshot = json.loads(json.dumps(self._state))
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(snapshot, f)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            print(f'⚠️ EMA: save state: {e}')

    # ── Utils ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _today_utc():
        return datetime.now(timezone.utc).date().isoformat()

    def _get_state(self, symbol):
        """Return per-symbol state, auto-resetting on new day. Call inside lock.
        Per-symbol fields:
          day  — YYYY-MM-DD reset key
          tf   — {tf: {ema, touched, alerted, recalc}} fully independent per TF
                   ema      — float, persisted across days
                   touched  — bool: permanent kill signal for THIS tf, for today only
                   alerted  — per-approach debounce (resets when price exits zone)
                   recalc   — closed-candle counter for EMA recalibration
          alerted_today — True if ANY tf fired an alert today (never resets mid-day)
        """
        today = self._today_utc()
        st = self._state.get(symbol)
        if not st or st.get('day') != today or 'tf' not in st:
            legacy_ema = (st or {}).get('ema', {})   # pre-migration schema (top-level 'ema' dict)
            prev_tf    = (st or {}).get('tf', {})
            st = {
                'day': today,
                'tf': {
                    tf: {
                        'ema':     prev_tf.get(tf, {}).get('ema', legacy_ema.get(tf)),
                        'touched': False,
                        'alerted': False,
                        'recalc':  0,
                    }
                    for tf in self.scan_tfs
                },
                'alerted_today': False,
            }
            self._state[symbol] = st
        return st

    # ── EMA helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _ema_from_closes(closes):
        if len(closes) < EMA_PERIOD:
            return None
        k   = 2.0 / (EMA_PERIOD + 1)
        ema = sum(closes[:EMA_PERIOD]) / EMA_PERIOD
        for c in closes[EMA_PERIOD:]:
            ema = c * k + ema * (1 - k)
        return ema

    def _seed_ema_tf(self, symbol, tf):
        """Bootstrap EMA60 for one TF from WS kline cache."""
        if not self._ws_manager:
            return False
        klines = self._ws_manager.get_klines(symbol, tf)
        closed = klines[:-1]
        if len(closed) < MIN_SEED_BARS:
            return False
        ema = self._ema_from_closes([k['close'] for k in closed])
        if ema is None:
            return False
        cfg       = (self._live_config or {}).get('ema_touch', {})
        tolerance = float(cfg.get('touch_tolerance', self.touch_tolerance))
        touched_today = self._touched_today(closed, ema, tolerance)
        with self._lock:
            st = self._get_state(symbol)
            st['tf'][tf]['ema'] = ema
            if touched_today:
                st['tf'][tf]['touched'] = True
        return True

    def _touched_today(self, closed_klines, ema, tolerance):
        """Retroactive check: did any already-closed candle from today cross this EMA
        (within `tolerance`%)? Approximation — uses the current (just-seeded) EMA value
        against today's candles, since we don't have the historical EMA series. Prevents
        firing a fresh pre-touch alert on a symbol that's only NOW being seeded (bot
        restart, or newly promoted into the tracked top-volume list) but already crossed
        the level earlier today.
        """
        today = self._today_utc()
        tol   = ema * (tolerance / 100.0)
        for k in closed_klines:
            if datetime.fromtimestamp(k['time'], timezone.utc).date().isoformat() != today:
                continue
            if k['low'] - tol <= ema <= k['high'] + tol:
                return True
        return False

    def _seed_all_tfs(self, symbol):
        """Seed EMA for every configured TF. Returns True if at least one TF got seeded."""
        seeded = False
        for tf in self.scan_tfs:
            if self._seed_ema_tf(symbol, tf):
                seeded = True
        return seeded

    # ── Core logic ────────────────────────────────────────────────────────────

    def _evaluate_candle(self, symbol, tf, candle, is_closed, ticker_data, threshold, tolerance):
        """
        Per-candle logic for one TF. Must be called WITHOUT holding self._lock.

        Every TF is fully independent: touch on this TF permanently silences THIS
        TF for the rest of the day, but never affects the other TFs.
        """
        fire_coin   = None
        save_needed = False

        with self._lock:
            st  = self._get_state(symbol)
            tst = st['tf'][tf]

            # Snapshot BEFORE this call can overwrite it — represents the close of
            # the last COMPLETED bar, stable across every tick of the current bar.
            prev_close = tst.get('prev_close')

            # ── EMA update (closed only) + recalibration every 50 bars ────────
            if is_closed:
                ema = tst['ema']
                if ema is None:
                    return None
                k       = 2.0 / (EMA_PERIOD + 1)
                new_ema = candle['close'] * k + ema * (1 - k)
                tst['ema_slope_pct'] = (new_ema - ema) / ema * 100.0
                tst['prev_close']    = candle['close']
                tst['ema']    = new_ema
                tst['recalc'] += 1
                if tst['recalc'] % 50 == 0 and self._ws_manager:
                    kl  = self._ws_manager.get_klines(symbol, tf)
                    new = self._ema_from_closes([c['close'] for c in kl[:-1]])
                    if new:
                        tst['ema'] = new

            ema = tst['ema']
            if not ema or ema <= 0:
                return None

            # ── TOUCH DETECTION (full candle: body + wick, ± tolerance) → permanent kill signal ─
            tol = ema * (tolerance / 100.0)
            if candle['low'] - tol <= ema <= candle['high'] + tol:
                if not tst['touched']:
                    tst['touched'] = True
                    save_needed    = True
                return None

            if tst['touched']:
                return None

            # ── PROXIMITY ALERT: independent per tf ───────────────────────────
            effective_threshold = max(threshold, 0.2)   # min 0.2%
            distance_pct        = abs(candle['close'] - ema) / ema * 100.0
            in_zone             = distance_pct <= effective_threshold
            # Hysteresis: re-arm the debounce only once price is clearly away from
            # the zone (2x threshold), not at the first tiny wobble back out of it —
            # avoids alert spam when price chops right on the zone boundary.
            re_armed            = distance_pct > effective_threshold * 2

            if in_zone:
                # Anti-spam: skip near-flat / low-conviction candles hugging the EMA
                range_pct = abs(candle['high'] - candle['low']) / ema * 100.0
                body_pct  = abs(candle['close'] - candle['open']) / ema * 100.0
                if range_pct < 0.1 or body_pct < 0.03:
                    return None

                # Directional check: only alert if price actually moved toward the
                # EMA since the last closed bar (not just sitting/drifting nearby)
                if prev_close is not None:
                    moving_toward = abs(candle['close'] - ema) < abs(prev_close - ema)
                    if not moving_toward:
                        return None

                # EMA slope filter: skip a dead-flat EMA (no real trend context)
                slope = tst.get('ema_slope_pct')
                if slope is not None and abs(slope) < 0.01:
                    return None

            if in_zone and not tst['alerted']:
                tst['alerted']      = True
                st['alerted_today'] = True
                save_needed         = True
                fire_coin = {
                    'symbol':       symbol,
                    'tf':           tf,
                    'price':        candle['close'],
                    'ema60':        round(ema, 6),
                    'distance_pct': round(distance_pct, 4),
                    'approach':     'from_above' if candle['close'] > ema else 'from_below',
                    'volume_24h':   ticker_data.get('volume_24h', 0),
                    'change_pct':   ticker_data.get('change_24h',
                                     ticker_data.get('change_pct', 0.0)),
                }
            elif re_armed and tst['alerted']:
                tst['alerted'] = False

        if save_needed:
            self._save_state()
        return fire_coin

    # ── Schedule ──────────────────────────────────────────────────────────────

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
            print('⚠️ EMA: WS not ready after 120s')
            return
        time.sleep(30)
        self._kline_refresh_loop()

    def _kline_refresh_loop(self):
        while True:
            self._refresh_kline_subs()
            time.sleep(KLINE_SUB_REFRESH)

    def _refresh_kline_subs(self):
        tickers = self._ws_manager.get_all_tickers()
        if not tickers or len(tickers) < 30:
            time.sleep(30)
            self._refresh_kline_subs()
            return
        ranked = sorted(tickers.items(), key=lambda x: x[1].get('volume_24h', 0), reverse=True)
        top    = [s for s, d in ranked if d.get('volume_24h', 0) >= self.min_volume_24h][:TOP_KLINE_SYMBOLS]
        self._ws_manager.subscribe_klines(top, intervals=self.scan_tfs)
        print(f'📡 EMA: subscribed {len(top)} symbols × {len(self.scan_tfs)} TF')

    # ── WS callback ───────────────────────────────────────────────────────────

    def _on_kline(self, symbol, interval, candle, is_closed):
        if not self.enabled or interval not in self.scan_tfs:
            return
        if not self._is_in_schedule():
            return

        # Seed this TF if needed
        with self._lock:
            needs_seed = self._get_state(symbol)['tf'][interval]['ema'] is None
        if needs_seed:
            if not self._seed_ema_tf(symbol, interval):
                return

        cfg       = (self._live_config or {}).get('ema_touch', {})
        threshold = float(cfg.get('ema_touch_threshold', self.ema_touch_threshold))
        tolerance = float(cfg.get('touch_tolerance', self.touch_tolerance))
        ticker    = self._ws_manager.get_all_tickers().get(symbol, {}) if self._ws_manager else {}

        fire_coin = self._evaluate_candle(symbol, interval, candle, is_closed, ticker, threshold, tolerance)

        if fire_coin:
            try:
                self._alert_queue.put_nowait([fire_coin])
            except queue.Full:
                try:
                    old     = self._alert_queue.get_nowait()
                    evicted = old[0].get('symbol', '?') if old else '?'
                    self._alert_queue.put_nowait([fire_coin])
                    print(f'⚠️ EMA queue full: evicted {evicted}, queued {symbol}')
                except Exception:
                    print(f'⚠️ EMA queue full: dropped {symbol}')

    # ── Alert worker ──────────────────────────────────────────────────────────

    def _alert_worker(self):
        while True:
            try:
                coins = self._alert_queue.get(timeout=5)
                if coins:
                    self.send_alert(coins)
                self._alert_queue.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                print(f'❌ EMA alert worker: {e}')

    # ── Polling scan ──────────────────────────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return []
        print(f'📡 EMA Proximity scan (tfs={",".join(self.scan_tfs)} thr={self.ema_touch_threshold}%)...')
        try:
            if self._ws_manager and self._ws_manager.ready.is_set():
                raw        = self._ws_manager.get_all_tickers()
                ticker_map = raw
                all_pairs  = [
                    {'symbol': s, 'volume_24h': d.get('volume_24h', 0),
                     'change_pct': d.get('change_24h', 0.0)}
                    for s, d in raw.items()
                    if d.get('volume_24h', 0) >= self.min_volume_24h
                ]
            else:
                r = requests.get('https://api.bybit.com/v5/market/tickers',
                                 params={'category': 'linear'}, timeout=10)
                data = r.json()
                if data['retCode'] != 0:
                    return []
                all_pairs  = []
                ticker_map = {}
                for item in data['result']['list']:
                    if not item['symbol'].endswith('USDT'):
                        continue
                    price = float(item.get('lastPrice', 0))
                    vol   = float(item.get('volume24h', 0)) * price
                    if vol < self.min_volume_24h:
                        continue
                    td = {'volume_24h': vol,
                          'change_pct': float(item.get('price24hPcnt', 0)) * 100}
                    all_pairs.append({'symbol': item['symbol'], **td})
                    ticker_map[item['symbol']] = td

            cfg       = (self._live_config or {}).get('ema_touch', {})
            threshold = float(cfg.get('ema_touch_threshold', self.ema_touch_threshold))
            tolerance = float(cfg.get('touch_tolerance', self.touch_tolerance))
            found     = []

            for p in all_pairs:
                sym = p['symbol']
                # Every TF is independent: evaluate ALL of them, not just the first hit
                for tf in self.scan_tfs:
                    with self._lock:
                        ema = self._get_state(sym)['tf'][tf]['ema']
                    if ema is None:
                        if not self._seed_ema_tf(sym, tf):
                            continue
                    klines = self._ws_manager.get_klines(sym, tf) if self._ws_manager else []
                    if not klines:
                        continue
                    fc = self._evaluate_candle(sym, tf, klines[-1], False,
                                               ticker_map.get(sym, p), threshold, tolerance)
                    if fc:
                        found.append(fc)
                if len(found) >= self.max_coins_per_alert:
                    break

            if found:
                self.send_alert(found)
            return found

        except Exception as e:
            print(f'❌ EMA scan: {e}')
            return []

    # ── Status helpers ────────────────────────────────────────────────────────

    def get_today_alerts(self):
        today = self._today_utc()
        with self._lock:
            return [sym for sym, st in self._state.items()
                    if st.get('day') == today and st.get('alerted_today')]

    def get_monitored_count(self):
        today = self._today_utc()
        with self._lock:
            return sum(1 for st in self._state.values() if st.get('day') == today)

    # ── send_alert ────────────────────────────────────────────────────────────

    def send_alert(self, coins):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, log_alert, fmt_vol
        except ImportError:
            return
        tf_label = {'D': '1D', '240': '4h', '60': '1h', '30': '30m', '5': '5m', '1': '1m'}
        for coin in coins[:3]:
            sym      = coin['symbol']
            tf       = coin['tf']
            al       = tf_label.get(tf, tf)
            dist     = coin.get('distance_pct', 0.0)
            change   = coin.get('change_pct', 0.0)
            approach = coin.get('approach', '').replace('_', ' ')
            base     = (self.base_url or 'https://cryptoscannerpro.com').rstrip('/')
            lines = [
                f'📡 EMA60 Proximity {al}',
                '',
                '------------------------------------------------',
                f'- Coin: {sym}',
                f'- Var: {change:+.2f}%',
                f'- Distanza EMA60: {dist:.2f}%',
                f'- Direzione: {approach}',
                f'- Volume: {fmt_vol(coin.get("volume_24h", 0))}',
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
            log_alert(sym, 'EMA Proximity', emoji='📡',
                      note=f'dist={dist:.2f}% tf={al}', tf=al, screenshot=img)
            print(f'📡 EMA proximity: {sym} {al} dist={dist:.2f}% {approach}')
