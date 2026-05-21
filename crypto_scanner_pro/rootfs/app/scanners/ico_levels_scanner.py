"""
ICO Levels Scanner
Monitors first-candle High/Low of recent ICO listings.
Discards coins that have already broken out of the first daily candle range.
Alerts when price approaches the first-candle High or Low within threshold %.
"""
import threading
import os
import json
import time
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

STATE_FILE    = '/data/ico_levels_state.json'
REFRESH_SECS  = 3600  # Re-scan listings every hour


class ICOLevelsScanner:

    def __init__(self, telegram_config, enabled=True,
                 ico_levels_threshold=2.0, ico_levels_tf='D',
                 new_listing_days=30, cooldown_hours=2,
                 scan_interval_minutes=60, screenshot_tf=None,
                 ws_manager=None, live_config=None,
                 schedule_start='', schedule_end='', utc_offset=2, **kwargs):
        self.telegram_token   = telegram_config['token']
        self.telegram_chat_id = telegram_config['chat_id']
        self.ha_url           = telegram_config.get('ha_url', '')
        self.enabled          = enabled
        self.threshold        = ico_levels_threshold
        self.tf               = ico_levels_tf
        self.screenshot_tf    = screenshot_tf if screenshot_tf else ico_levels_tf
        self.new_listing_days = new_listing_days
        self.cooldown_hours   = cooldown_hours
        self._live_config     = live_config

        # In-memory ICO levels cache: {symbol: {first_high, first_low}}
        self._levels      = {}
        self._levels_lock = threading.Lock()
        # In-memory alerted cache (mirrors STATE_FILE alerted section)
        self._alerted     = {}
        self._alerted_lock = threading.Lock()
        self._discarded   = set()

        self._load_state_into_memory()

        self._ws_manager = ws_manager
        if ws_manager is not None:
            ws_manager.add_tick_callback(self._on_tick)
            threading.Thread(target=self._precompute_loop, daemon=True).start()

        logger.info(f'ICO Levels Scanner init — threshold={self.threshold}% tf={self.tf} days={self.new_listing_days} ws={"on" if ws_manager else "off"}')

    # ── state helpers ─────────────────────────────────────────────────────────

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {'discarded': [], 'alerted': {}}

    def _save_state(self, state):
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            logger.warning(f'ICO: save_state error: {e}')

    def _load_state_into_memory(self):
        state = self._load_state()
        self._discarded = set(state.get('discarded', []))
        self._alerted   = state.get('alerted', {})

    # ── Bybit helpers ─────────────────────────────────────────────────────────

    def _fetch_new_listings(self):
        cutoff_ms = int((datetime.utcnow() - timedelta(days=self.new_listing_days)).timestamp() * 1000)
        try:
            resp = requests.get(
                'https://api.bybit.com/v5/market/instruments-info',
                params={'category': 'linear', 'status': 'Trading', 'limit': 1000},
                timeout=15)
            data = resp.json()
            if data.get('retCode') != 0:
                return []
            symbols = []
            for item in data['result']['list']:
                sym = item.get('symbol', '')
                if not sym.endswith('USDT'):
                    continue
                if int(item.get('launchTime', 0)) >= cutoff_ms:
                    symbols.append(sym)
            return symbols
        except Exception as e:
            logger.error(f'ICO: fetch_new_listings: {e}')
            return []

    def _fetch_daily_klines(self, symbol):
        try:
            resp = requests.get(
                'https://api.bybit.com/v5/market/kline',
                params={'category': 'linear', 'symbol': symbol, 'interval': 'D', 'limit': 200},
                timeout=10)
            data = resp.json()
            if data.get('retCode') != 0:
                return []
            bars = [{'time': int(i[0])//1000, 'open': float(i[1]),
                     'high': float(i[2]), 'low': float(i[3]), 'close': float(i[4])}
                    for i in data['result']['list']]
            bars.sort(key=lambda x: x['time'])
            return bars
        except Exception as e:
            logger.warning(f'ICO: fetch_daily_klines {symbol}: {e}')
            return []

    # ── pre-computation ───────────────────────────────────────────────────────

    def _precompute_loop(self):
        """Compute/refresh ICO levels; run once at startup then every REFRESH_SECS."""
        while True:
            try:
                self._precompute_levels()
            except Exception as e:
                logger.error(f'ICO: precompute_loop error: {e}')
            time.sleep(REFRESH_SECS)

    def _precompute_levels(self):
        if not self.enabled:
            return
        symbols = self._fetch_new_listings()
        new_levels = {}
        new_discarded = set(self._discarded)

        for sym in symbols:
            if sym in new_discarded:
                continue
            klines = self._fetch_daily_klines(sym)
            if len(klines) < 2:
                continue
            first_high = klines[0]['high']
            first_low  = klines[0]['low']
            ever_broke = any(c['high'] > first_high or c['low'] < first_low for c in klines[1:])
            if ever_broke:
                new_discarded.add(sym)
                continue
            new_levels[sym] = {'first_high': first_high, 'first_low': first_low}

        with self._levels_lock:
            self._levels = new_levels
        self._discarded = new_discarded

        state = self._load_state()
        state['discarded'] = list(new_discarded)
        self._save_state(state)
        logger.info(f'ICO: precomputed {len(new_levels)} active levels, {len(new_discarded)} discarded')

    # ── real-time callback ────────────────────────────────────────────────────

    def _on_tick(self, symbol, data):
        if not self.enabled:
            return
        from alert_utils import is_in_schedule
        _gen = (self._live_config or {}).get('general', {})
        if not is_in_schedule(_gen.get('schedule_start',''), _gen.get('schedule_end',''), float(_gen.get('utc_offset') or 2)):
            return
        with self._levels_lock:
            levels = self._levels.get(symbol)
        if not levels:
            return

        price      = data.get('price', 0)
        change_pct = data.get('change_24h', 0.0)
        if price <= 0:
            return
        first_high = levels['first_high']
        first_low  = levels['first_low']
        dist_high  = (first_high - price) / first_high * 100
        dist_low   = (price - first_low)  / first_low  * 100
        now        = datetime.utcnow()

        for side, dist, level in [('high', dist_high, first_high), ('low', dist_low, first_low)]:
            if dist > self.threshold or dist < 0:
                continue
            key = f'{symbol}_{side}'
            with self._alerted_lock:
                last_str = self._alerted.get(key)
                if last_str:
                    elapsed = (now - datetime.fromisoformat(last_str)).total_seconds()
                    if elapsed < self.cooldown_hours * 3600:
                        continue
                self._alerted[key] = now.isoformat()

            # Persist and send asynchronously
            state = self._load_state()
            state['alerted'] = dict(self._alerted)
            self._save_state(state)
            threading.Thread(
                target=self._send_alert, args=(symbol, side, dist, level, price, change_pct),
                daemon=True).start()

    # ── polling scan (fallback / manual) ─────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return

        # If WS is active, levels are kept up-to-date by _precompute_loop
        # Just check the current prices against cached levels
        if self._ws_manager and self._ws_manager.ready.is_set():
            tickers = self._ws_manager.get_all_tickers()
            with self._levels_lock:
                symbols_to_check = list(self._levels.keys())
            for sym in symbols_to_check:
                td = tickers.get(sym)
                if td:
                    self._on_tick(sym, td)
            return

        # Full REST scan (original logic)
        state     = self._load_state()
        discarded = set(state.get('discarded', []))
        alerted   = state.get('alerted', {})
        modified  = False
        now       = datetime.utcnow()

        try:
            symbols = self._fetch_new_listings()
        except Exception as e:
            logger.error(f'ICO levels: fetch listings error: {e}')
            return

        logger.info(f'ICO levels: checking {len(symbols)} listing(s)')

        for sym in symbols:
            if sym in discarded:
                continue
            try:
                klines = self._fetch_daily_klines(sym)
            except Exception as e:
                logger.warning(f'ICO levels: klines error {sym}: {e}')
                continue

            if len(klines) < 2:
                continue

            first_high    = klines[0]['high']
            first_low     = klines[0]['low']
            current_price = klines[-1]['close']

            ever_broke = any(c['high'] > first_high or c['low'] < first_low for c in klines[1:])
            if ever_broke:
                discarded.add(sym)
                modified = True
                continue

            dist_high = (first_high - current_price) / first_high * 100
            dist_low  = (current_price - first_low)  / first_low  * 100

            for side, dist, level in [('high', dist_high, first_high), ('low', dist_low, first_low)]:
                if dist > self.threshold:
                    continue
                key      = f'{sym}_{side}'
                last_str = alerted.get(key)
                if last_str:
                    elapsed = (now - datetime.fromisoformat(last_str)).total_seconds()
                    if elapsed < self.cooldown_hours * 3600:
                        continue
                self._send_alert(sym, side, dist, level, current_price)
                alerted[key] = now.isoformat()
                modified = True

        if modified:
            state['discarded'] = list(discarded)
            state['alerted']   = alerted
            self._save_state(state)

    # ── alert ─────────────────────────────────────────────────────────────────

    def _send_alert(self, sym, side, dist, level, price=0, change_pct=0.0):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, build_caption
        except ImportError as e:
            logger.error(f'Cannot import alert_utils: {e}')
            return

        side_str = 'massimo' if side == 'high' else 'minimo'
        sig_type = 'ath' if side == 'high' else 'atl'
        note     = f"ICO {side_str} {dist:.2f}%"
        caption  = build_caption(sym, change_pct, note, self.ha_url)
        img = get_chart(sym, interval=self.screenshot_tf, signal={'type': sig_type})
        if img:
            send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
        else:
            send_text(self.telegram_token, self.telegram_chat_id, caption)
        logger.info(f'ICO levels alert: {sym} {side} dist={dist:.2f}%')
