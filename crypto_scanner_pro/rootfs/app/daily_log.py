"""Daily alert log — accumulates all alerts for the current UTC day, resets at midnight."""
import json
import os
import threading
from datetime import datetime, timezone

DAILY_LOG_FILE = '/data/daily_alerts.json'
_lock = threading.Lock()


def _today():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def append_alert(symbol, alert_type, emoji='🔔', note='', tf=None):
    """Append an alert to today's log. Thread-safe. Resets automatically at day change."""
    now = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    entry = {
        'symbol':    symbol,
        'type':      alert_type,
        'emoji':     emoji,
        'time':      now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'timestamp': now.timestamp(),
    }
    if note:
        entry['note'] = note
    if tf:
        entry['tf'] = tf

    with _lock:
        try:
            if os.path.exists(DAILY_LOG_FILE):
                with open(DAILY_LOG_FILE, 'r') as f:
                    log = json.load(f)
            else:
                log = {'date': today, 'alerts': []}

            # New day → reset
            if log.get('date') != today:
                log = {'date': today, 'alerts': []}

            log['alerts'].append(entry)

            os.makedirs(os.path.dirname(DAILY_LOG_FILE), exist_ok=True)
            with open(DAILY_LOG_FILE, 'w') as f:
                json.dump(log, f)
        except Exception as e:
            print(f'⚠️ daily_log write error: {e}')


def get_today_alerts():
    """Return list of all alerts for today (UTC), most recent first."""
    today = _today()
    try:
        if os.path.exists(DAILY_LOG_FILE):
            with open(DAILY_LOG_FILE, 'r') as f:
                log = json.load(f)
            if log.get('date') == today:
                alerts = log.get('alerts', [])
                return sorted(alerts, key=lambda x: x.get('timestamp', 0), reverse=True)
    except Exception as e:
        print(f'⚠️ daily_log read error: {e}')
    return []
