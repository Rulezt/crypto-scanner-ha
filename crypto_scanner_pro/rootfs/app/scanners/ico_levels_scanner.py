"""
ICO Levels Scanner
Monitors first-candle High/Low of recent ICO listings.
Discards coins that have already broken out of the first daily candle range.
Alerts when price approaches the first-candle High or Low within threshold %.
"""
import os
import json
import time
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

STATE_FILE = '/data/ico_levels_state.json'


class ICOLevelsScanner:

    def __init__(self, telegram_config, enabled=True,
                 ico_levels_threshold=2.0, ico_levels_tf='D',
                 new_listing_days=30, cooldown_hours=2,
                 scan_interval_minutes=60, **kwargs):
        self.telegram_token   = telegram_config['token']
        self.telegram_chat_id = telegram_config['chat_id']
        self.ha_url           = telegram_config.get('ha_url', '')
        self.enabled          = enabled
        self.threshold        = ico_levels_threshold
        self.tf               = ico_levels_tf
        self.new_listing_days = new_listing_days
        self.cooldown_hours   = cooldown_hours
        self.scan_interval_minutes = scan_interval_minutes

        logger.info(f'ICO Levels Scanner init — threshold={self.threshold}% tf={self.tf} days={self.new_listing_days}')

    # ── state ─────────────────────────────────────────────────────────────────

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {'discarded': [], 'alerted': {}}

    def _save_state(self, state):
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)

    # ── Bybit helpers ──────────────────────────────────────────────────────────

    def _fetch_new_listings(self):
        """Return list of symbols listed in the last new_listing_days days."""
        cutoff_ms = int((datetime.utcnow() - timedelta(days=self.new_listing_days)).timestamp() * 1000)
        resp = requests.get(
            'https://api.bybit.com/v5/market/instruments-info',
            params={'category': 'linear', 'status': 'Trading', 'limit': 1000},
            timeout=15,
        )
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

    def _fetch_daily_klines(self, symbol):
        """Fetch daily klines sorted ascending (oldest first)."""
        resp = requests.get(
            'https://api.bybit.com/v5/market/kline',
            params={'category': 'linear', 'symbol': symbol, 'interval': 'D', 'limit': 200},
            timeout=10,
        )
        data = resp.json()
        if data.get('retCode') != 0:
            return []
        bars = []
        for item in data['result']['list']:
            bars.append({
                'time':  int(item[0]) // 1000,
                'open':  float(item[1]),
                'high':  float(item[2]),
                'low':   float(item[3]),
                'close': float(item[4]),
            })
        bars.sort(key=lambda x: x['time'])
        return bars

    # ── core scan ──────────────────────────────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return

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

            # Need at least 2 daily candles (skip coins still on day 1)
            if len(klines) < 2:
                continue

            first_high    = klines[0]['high']
            first_low     = klines[0]['low']
            current_price = klines[-1]['close']

            # Discard if price has already broken out of first-candle range
            if current_price > first_high or current_price < first_low:
                discarded.add(sym)
                modified = True
                logger.info(f'ICO levels: {sym} discarded (price broke first candle range)')
                continue

            dist_high = (first_high - current_price) / first_high * 100
            dist_low  = (current_price - first_low)  / first_low  * 100

            for side, dist, level in [('high', dist_high, first_high), ('low', dist_low, first_low)]:
                if dist > self.threshold:
                    continue
                key        = f'{sym}_{side}'
                last_str   = alerted.get(key)
                if last_str:
                    elapsed = (now - datetime.fromisoformat(last_str)).total_seconds()
                    if elapsed < self.cooldown_hours * 3600:
                        continue
                self._send_alert(sym, side, dist, level)
                alerted[key] = now.isoformat()
                modified = True

        if modified:
            state['discarded'] = list(discarded)
            state['alerted']   = alerted
            self._save_state(state)

    # ── alert ──────────────────────────────────────────────────────────────────

    def _send_alert(self, sym, side, dist, level):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, mtf_link
        except ImportError as e:
            logger.error(f'Cannot import alert_utils: {e}')
            return

        side_str = 'massimo' if side == 'high' else 'minimo'
        sig_type = 'ath' if side == 'high' else 'atl'
        caption  = (
            f"{mtf_link(sym, self.ha_url)}  ICO Level\n"
            f"vicino al {side_str} prima candela: {dist:.2f}%"
        )
        img = get_chart(sym, interval=self.tf, signal={'type': sig_type})
        if img:
            send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
        else:
            send_text(self.telegram_token, self.telegram_chat_id, caption)
        logger.info(f'ICO levels alert sent: {sym} {side} dist={dist:.2f}%')
