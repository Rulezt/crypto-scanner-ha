"""
Crypto Scanner Professional - All-in-One
Flask API + Scanners integrati + Dashboard
"""
from flask import Flask, jsonify, request, send_file, session, redirect, url_for, Response
from flask_cors import CORS
from datetime import datetime, timedelta
from functools import wraps
import hashlib
import secrets
import os
import json
import threading
import time
import logging
import uuid
import sqlite3
import re
from scanners.ema_touch import EMAScanner
from scanners.ath_atl_scanner import ATHATLScanner
from scanners.ico_levels_scanner import ICOLevelsScanner
from scanners.double_touch import DoubleTouchScanner
from scanners.daily_flip import DailyFlipScanner
from scanners.shimano_scanner import ShimanoScanner
from scanners.pattern_scanner import PatternScanner
from scanners.bot_engine import BotEngine
from ws_manager import BybitWSManager

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Secret key per sessioni
_sk_file = '/data/.secret_key'
if os.path.exists(_sk_file):
    with open(_sk_file) as f:
        app.secret_key = f.read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    with open(_sk_file, 'w') as f:
        f.write(app.secret_key)

# Auth config
AUTH_FILE = '/data/auth.json'

def _get_auth():
    if os.path.exists(AUTH_FILE):
        with open(AUTH_FILE) as f:
            auth = json.load(f)
        dirty = False
        if 'email' not in auth:
            auth['email'] = ''; dirty = True
        if 'role' not in auth:
            auth['role'] = 'admin'; dirty = True
        if dirty:
            with open(AUTH_FILE, 'w') as f: json.dump(auth, f)
        return auth
    default = {'username': 'admin', 'password_hash': _hash_pw('admin'), 'email': '', 'role': 'admin'}
    with open(AUTH_FILE, 'w') as f:
        json.dump(default, f)
    return default

def _hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

# ── USERS DB ───────────────────────────────────────────────────────────────────
USERS_DB = '/data/users.db'

def _init_users_db():
    with sqlite3.connect(USERS_DB) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT '',
            verified INTEGER NOT NULL DEFAULT 0,
            verify_token TEXT,
            created_at TEXT NOT NULL
        )''')
        for _col, _ddl in [
            ('verified',          'INTEGER NOT NULL DEFAULT 0'),
            ('verify_token',      'TEXT'),
            ('bybit_api_key',     'TEXT NOT NULL DEFAULT ""'),
            ('bybit_api_secret',  'TEXT NOT NULL DEFAULT ""'),
        ]:
            try:
                conn.execute(f'ALTER TABLE users ADD COLUMN {_col} {_ddl}')
            except Exception:
                pass
        conn.commit()

def _db_get_user(username):
    try:
        with sqlite3.connect(USERS_DB) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute('SELECT * FROM users WHERE lower(username)=lower(?)', (username,)).fetchone()
            return dict(row) if row else None
    except Exception:
        return None

def _db_create_user(username, email, password_hash):
    import secrets
    token = secrets.token_urlsafe(32)
    try:
        with sqlite3.connect(USERS_DB) as conn:
            conn.execute(
                'INSERT INTO users (username, email, password_hash, role, created_at, verified, verify_token) VALUES (?,?,?,?,?,?,?)',
                (username, email.lower(), password_hash, '', datetime.utcnow().isoformat(), 0, token))
            conn.commit()
        return token
    except sqlite3.IntegrityError:
        return None



def _send_verification_email(to_email, username, token):
    import smtplib
    from email.mime.text import MIMEText
    link = 'https://cryptoscannerpro.com/verify/' + token
    body = (
        'Ciao ' + username + ',\n\n'
        'Grazie per esserti iscritto a Crypto Scanner Pro.\n'
        'Clicca il link qui sotto per attivare il tuo account:\n\n'
        + link + '\n\n'
        'Crypto Scanner Pro'
    )
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = 'Attiva il tuo account - Crypto Scanner Pro'
    msg['From'] = 'Crypto Scanner Pro <info@cryptoscannerpro.com>'
    msg['To'] = to_email
    try:
        with smtplib.SMTP('172.17.0.1', 25, timeout=10) as s:
            s.sendmail('info@cryptoscannerpro.com', [to_email], msg.as_string())
        return True
    except Exception as e:
        logger.error('Email send error: ' + str(e))
        return False


def _send_recovery_email(to_email, username, new_pw):
    import smtplib
    from email.mime.text import MIMEText
    body = (
        'Ciao ' + username + ',\n\n'
        'Hai richiesto il recupero delle credenziali di accesso a Crypto Scanner Pro.\n\n'
        'Username: ' + username + '\n'
        'Password temporanea: ' + new_pw + '\n\n'
        'Accedi con queste credenziali e cambia la password appena possibile '
        'dalla sezione impostazioni del tuo profilo.\n\n'
        'Crypto Scanner Pro'
    )
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = 'Recupero accesso - Crypto Scanner Pro'
    msg['From'] = 'Crypto Scanner Pro <info@cryptoscannerpro.com>'
    msg['To'] = to_email
    try:
        with smtplib.SMTP('172.17.0.1', 25, timeout=10) as s:
            s.sendmail('info@cryptoscannerpro.com', [to_email], msg.as_string())
        return True
    except Exception as e:
        logger.error('Recovery email error: ' + str(e))
        return False
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'unauthorized'}), 401
            return redirect(url_for('login_page', next=request.url))
        return f(*args, **kwargs)
    return decorated

# Config storage file
CONFIG_FILE = '/data/scanner_config.json'

# Default config
DEFAULT_CONFIG = {
    'telegram': {
        'token': os.getenv('TELEGRAM_TOKEN', ''),
        'chat_id': os.getenv('TELEGRAM_CHAT_ID', ''),
        'enabled': True,
        'base_url': '',
    },
    'ath_atl': {
        'enabled': True,
        'ath_enabled': True,
        'atl_enabled': True,
        'proximity_threshold': 1.0,
        'lookback_days': 365,
        'scan_interval_minutes': 60,
        'screenshot_tf': 'D',
    },
    'ico_levels': {
        'enabled': True,
        'ico_levels_threshold': 2.0,
        'ico_levels_tf': 'D',
        'scan_interval_minutes': 60,
        'screenshot_tf': 'D',
    },
    'double_touch': {
        'enabled': True,
        'tolerance': 0.5,
        'proximity': 2.0,
        'scan_tf': ['D', '240'],
        'scan_interval_minutes': 240,
        'cooldown_hours': 12,
    },
    'ema_touch': {
        'enabled': True,
        'ema_touch_threshold': 2.0,
        'touch_tolerance': 0.05,
        'scan_tfs': ['240', '60', '30', '5', '1'],
    },
    'daily_flip': {
        'enabled': True,
        'flip_threshold': 2.0,
        'flip_type': 'both',
        'cooldown_hours': 2,
    },
    'pattern': {
        'enabled': True,
        'scan_tf': ['D', '240', '60', '30', '5', '1'],
        'cooldown_hours': 24,
        'scan_interval_minutes': 60,
        'doji_threshold': 0.1,
    },
    'shimano': {
        'enabled': True,
        'scan_tf': ['D'],
        'cooldown_hours': 24,
        'scan_interval_minutes': 240,
        'fuori_enabled': True,
    },
    'bot': {
        'symbol': '',
        'tf': '60',
        'mode': 'signal',
        'sizing': {'type': 'fixed', 'value': 50.0},
        'leverage': 1,
        'sma_lenta_period': 10, 'sma_lenta_source': 'close',
        'sma_veloce_period': 60, 'sma_veloce_source': 'close',
        'filter_enabled': True, 'filter_period': 200, 'filter_source': 'close',
        'candle_filter_enabled': False,
        'sl_pct': 1.0, 'tp_pct': 1.7,
    },
    'general': {
        'min_volume_24h': 10000000,
        'new_listing_days': 30,
        'cooldown_hours': 2,
        'send_screenshots': True,
        'max_coins_per_alert': 10,
        'utc_offset': 2,
        'schedule_start': '',
        'schedule_end': '',
    }
}

# Global config
config = DEFAULT_CONFIG.copy()
scanners = {}
scanner_threads = {}
ws_manager = BybitWSManager()

_triggered_lock = threading.Lock()
_recently_triggered = []

_hv_klines_subscribed = set()
_mtf_klines_subscribed = set()  # (symbol, interval) tuples for MTF live polling

def _compute_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 8)

def _compute_ema_series(prices, period):
    if len(prices) < period:
        return [None] * len(prices)
    k = 2 / (period + 1)
    series = [None] * (period - 1)
    ema = sum(prices[:period]) / period
    series.append(ema)
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
        series.append(ema)
    return series

def _count_ema_touches_daily(klines_30m, period=60, threshold=2.0):
    """Count 30m candles since midnight UTC where wick crosses EMA (low<=EMA<=high).
    Each candle counts as 1 touch. Resets at midnight UTC."""
    import calendar, datetime
    past = klines_30m
    if len(past) < period:
        return None
    now = datetime.datetime.utcnow()
    midnight_ts = calendar.timegm((now.year, now.month, now.day, 0, 0, 0, 0, 0, 0))
    closes = [k['close'] for k in past]
    ema_series = _compute_ema_series(closes, period)
    count = 0
    for i, k in enumerate(past):
        if k['time'] < midnight_ts:
            continue
        ev = ema_series[i]
        if ev is None:
            continue
        if k['low'] <= ev <= k['high']:
            count += 1
    return count

def load_config():
    """Load config from file"""
    global config
    try:
        # Load saved config
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                saved = json.load(f)
                config.update(saved)
                logger.info(f"✅ Config loaded from {CONFIG_FILE}")
        else:
            save_config()
        
        # Override Telegram config from environment variables (if set)
        telegram_token = os.getenv('TELEGRAM_TOKEN', '')
        telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID', '')

        if telegram_token:
            config['telegram']['token'] = telegram_token
            logger.info("✅ Telegram token loaded from environment")

        if telegram_chat_id:
            config['telegram']['chat_id'] = telegram_chat_id
            logger.info("✅ Telegram chat_id loaded from environment")

    except Exception as e:
        logger.error(f"❌ Error loading config: {e}")

def save_config():
    """Save config to file"""
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        logger.info(f"✅ Config saved to {CONFIG_FILE}")
        return True
    except Exception as e:
        logger.error(f"❌ Error saving config: {e}")
        return False

def init_scanners():
    """Initialize all scanners"""
    global scanners

    # Remove all old WS callbacks before registering new ones.
    # Without this, every config save accumulates additional callbacks and
    # causes duplicate alerts even when a scanner is toggled off.
    ws_manager.clear_callbacks()

    telegram_config = {
        'token': config['telegram']['token'],
        'chat_id': config['telegram']['chat_id'],
        'base_url': config['telegram'].get('base_url', ''),
    }
    
    # Check if Telegram is configured
    if not telegram_config['token'] or not telegram_config['chat_id']:
        logger.warning("⚠️ Telegram NOT configured!")
        logger.warning("Configure via: Dashboard → Telegram tab → Save, or env vars TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
    else:
        logger.info(f"✅ Telegram configured: {telegram_config['chat_id'][:8]}...")
    
    try:
        scanners['ath_atl'] = ATHATLScanner(
            telegram_config=telegram_config,
            ws_manager=ws_manager,
            live_config=config,
            **config['ath_atl'],
            **config['general']
        )

        scanners['ico_levels'] = ICOLevelsScanner(
            telegram_config=telegram_config,
            ws_manager=ws_manager,
            live_config=config,
            **config['ico_levels'],
            **config['general']
        )

        scanners['double_touch'] = DoubleTouchScanner(
            telegram_config=telegram_config,
            ws_manager=ws_manager,
            live_config=config,
            **config['double_touch'],
            **{k: v for k, v in config['general'].items() if k in ('min_volume_24h', 'max_coins_per_alert')}
        )

        scanners['ema_touch'] = EMAScanner(
            telegram_config=telegram_config,
            ws_manager=ws_manager,
            live_config=config,
            **config['ema_touch'],
            **{k: v for k, v in config['general'].items()
               if k in ('min_volume_24h', 'max_coins_per_alert') and k not in config['ema_touch']}
        )

        scanners['daily_flip'] = DailyFlipScanner(
            telegram_config=telegram_config,
            ws_manager=ws_manager,
            live_config=config,
            **config['daily_flip'],
            **{k: v for k, v in config['general'].items()
               if k in ('min_volume_24h', 'cooldown_hours') and k not in config['daily_flip']}
        )

        scanners['shimano'] = ShimanoScanner(
            telegram_config=telegram_config,
            ws_manager=ws_manager,
            live_config=config,
            **config['shimano'],
            **{k: v for k, v in config['general'].items()
               if k in ('min_volume_24h', 'max_coins_per_alert') and k not in config['shimano']}
        )

        scanners['pattern'] = PatternScanner(
            telegram_config=telegram_config,
            ws_manager=ws_manager,
            live_config=config,
            **config['pattern'],
            **{k: v for k, v in config['general'].items()
               if k in ('min_volume_24h', 'max_coins_per_alert') and k not in config['pattern']}
        )

        scanners['bot'] = BotEngine(
            telegram_config=telegram_config,
            ws_manager=ws_manager,
            live_config=config,
            trade_client=_bot_trade_client,
            **config['bot']
        )

        logger.info("✅ Scanners initialized")
    except Exception as e:
        logger.error(f"❌ Error initializing scanners: {e}")

def _is_in_schedule():
    """Return True if current local time (UTC+offset) is within the configured window."""
    start_str = config['general'].get('schedule_start', '')
    end_str   = config['general'].get('schedule_end', '')
    if not start_str or not end_str:
        return True
    try:
        utc_offset = float(config['general'].get('utc_offset') or 2)
        now = datetime.utcnow() + timedelta(hours=utc_offset)
        sh, sm = map(int, start_str.split(':'))
        eh, em = map(int, end_str.split(':'))
        now_m   = now.hour * 60 + now.minute
        start_m = sh * 60 + sm
        end_m   = eh * 60 + em
        if start_m <= end_m:
            return start_m <= now_m <= end_m
        # overnight window e.g. 22:00 → 06:00
        return now_m >= start_m or now_m <= end_m
    except Exception:
        return True


def run_scanner(config_name, scanner_key, interval_minutes):
    """Run scanner in loop — looks up scanner dynamically so reinit is picked up."""
    while True:
        try:
            scanner = scanners.get(scanner_key)
            if scanner and config.get(config_name, {}).get('enabled', True):
                if _is_in_schedule():
                    logger.info(f"🔄 Running {config_name} scanner...")
                    scanner.scan()
                else:
                    logger.info(f"⏸ {config_name} fuori orario, skip")
        except Exception as e:
            logger.error(f"❌ Error in {config_name} scanner: {e}")

        time.sleep(interval_minutes * 60)

def start_scanners():
    """Start all scanner threads"""
    global scanner_threads

    # (config_name, scanner_key, interval)
    threads_config = [
        ('ath_atl',        'ath_atl',    config['ath_atl']['scan_interval_minutes']),
        ('ico_levels',     'ico_levels',    config['ico_levels']['scan_interval_minutes']),
        ('double_touch',   'double_touch',  config['double_touch']['scan_interval_minutes']),
        ('shimano',        'shimano',        config['shimano']['scan_interval_minutes']),
        ('pattern',        'pattern',        config['pattern']['scan_interval_minutes']),
    ]

    for config_name, scanner_key, interval in threads_config:
        thread = threading.Thread(
            target=run_scanner,
            args=(config_name, scanner_key, interval),
            daemon=True
        )
        thread.start()
        scanner_threads[config_name] = thread
        logger.info(f"✅ {config_name} thread started")

    alert_thread = threading.Thread(target=check_price_alerts, daemon=True)
    alert_thread.start()
    logger.info("✅ price alert checker thread started")

# ========== API ENDPOINTS ==========

@app.route('/scanner-api/health', methods=['GET'])
def health():
    """Health check"""
    telegram_configured = bool(config['telegram']['token'] and config['telegram']['chat_id'])
    
    return jsonify({
        'status': 'ok',
        'version': '4.6.91',
        'telegram_configured': telegram_configured,
        'telegram_token_set': bool(config['telegram']['token']),
        'telegram_chat_id_set': bool(config['telegram']['chat_id']),
        'ws_connected': ws_manager.ready.is_set(),
        'ws_tickers': len(ws_manager.get_all_tickers()),
        'scanners': {
            'ath_atl': config['ath_atl']['enabled'],
            'ico_levels': config['ico_levels']['enabled'],
            'double_touch': config['double_touch']['enabled'],
            'ema_touch': config['ema_touch']['enabled'],
            'daily_flip': config['daily_flip']['enabled'],
            'shimano': config['shimano']['enabled'],
            'pattern': config['pattern']['enabled'],
        }
    })

@app.route('/scanner-api/config', methods=['GET'])
def get_config():
    """Get current config"""
    logger.info("📥 GET /scanner-api/config")
    return jsonify(config)

@app.route('/scanner-api/config', methods=['POST'])
def update_config():
    """Update config"""
    global config
    try:
        logger.info("📤 POST /scanner-api/config")
        new_config = request.get_json()
        
        if not new_config:
            return jsonify({'success': False, 'error': 'No JSON data'}), 400
        
        config.update(new_config)
        
        if save_config():
            # Reinitialize scanners with new config
            init_scanners()
            return jsonify({'success': True, 'message': 'Config updated'})
        else:
            return jsonify({'success': False, 'error': 'Failed to save'}), 500
    except Exception as e:
        logger.error(f"❌ Error updating config: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/scanner-api/scan/<scanner_name>', methods=['POST'])
def manual_scan(scanner_name):
    """Trigger manual scan"""
    try:
        logger.info(f"🔄 Manual scan: {scanner_name}")
        if scanner_name in scanners:
            result = scanners[scanner_name].scan()
            return jsonify({'success': True, 'result': result})
        else:
            return jsonify({'success': False, 'error': 'Scanner not found'}), 404
    except Exception as e:
        logger.error(f"❌ Error in manual scan: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/scanner-api/ath-atl/status', methods=['GET'])
def get_ath_atl_status():
    """Get ATH/ATL scanner status and top monitored coins"""
    try:
        import requests

        # Get top 20 gainers + top 20 losers from Bybit
        url = "https://api.bybit.com/v5/market/tickers?category=linear"
        response = requests.get(url, timeout=10)
        data = response.json()

        if data['retCode'] != 0:
            return jsonify({'success': False, 'error': 'Bybit API error'}), 500

        # Filter and sort pairs (no volume filter — show all coins)
        all_pairs = []

        for item in data['result']['list']:
            if not item['symbol'].endswith('USDT'):
                continue

            last_price = float(item['lastPrice'])
            change_pct = float(item.get('price24hPcnt', 0)) * 100
            volume_24h_usd = float(item.get('volume24h', 0)) * last_price

            all_pairs.append({
                'symbol': item['symbol'],
                'price': last_price,
                'change_24h': change_pct,
                'volume_24h': volume_24h_usd
            })

        # Split into gainers (positive) and losers (negative)
        top_gainers = [c for c in all_pairs if c['change_24h'] > 0]
        top_gainers.sort(key=lambda x: x['change_24h'], reverse=True)
        top_losers  = [c for c in all_pairs if c['change_24h'] < 0]
        top_losers.sort(key=lambda x: x['change_24h'])  # most negative first

        return jsonify({
            'success': True,
            'config': config.get('ath_atl', {}),
            'top_gainers': top_gainers,
            'top_losers': top_losers,
            'total_pairs': len(all_pairs)
        })

    except Exception as e:
        logger.error(f"❌ Error getting ATH/ATL status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/scanner-api/ath-atl/visual', methods=['GET'])
def ath_atl_visual():
    scanner = scanners.get('ath_atl')
    if not scanner:
        return jsonify({'symbols': []})
    return jsonify({'symbols': scanner.get_visual_data()})

@app.route('/scanner-api/ema-touch/status', methods=['GET'])
def ema_touch_status():
    scanner = scanners.get('ema_touch')
    if not scanner:
        return jsonify({'count': 0, 'alerts': [], 'monitored': 0})
    alerts = scanner.get_today_alerts()
    return jsonify({'count': len(alerts), 'alerts': alerts,
                    'monitored': scanner.get_monitored_count()})

@app.route('/scanner-api/ath-atl/alerts', methods=['GET'])
def ath_atl_alerts():
    scanner = scanners.get('ath_atl')
    if not scanner:
        return jsonify({'count': 0, 'alerts': [], 'monitored': 0, 'nearby_symbols': []})
    alerts = scanner.get_today_alerts()
    return jsonify({'count': len(alerts), 'alerts': alerts, 'monitored': scanner.get_monitored_count(), 'nearby_symbols': scanner.get_nearby_symbols()})

@app.route('/scanner-api/ico-levels/status', methods=['GET'])
def ico_levels_status():
    scanner = scanners.get('ico_levels')
    if not scanner:
        return jsonify({'count': 0, 'alerts': [], 'monitored': 0, 'monitored_symbols': []})
    alerts = scanner.get_today_alerts()
    return jsonify({'count': len(alerts), 'alerts': alerts, 'monitored': scanner.get_monitored_count(), 'monitored_symbols': scanner.get_monitored_symbols()})

@app.route('/scanner-api/ico-levels/visual', methods=['GET'])
def ico_levels_visual():
    scanner = scanners.get('ico_levels')
    if not scanner:
        return jsonify({'symbols': []})
    return jsonify({'symbols': scanner.get_visual_data()})

@app.route('/scanner-api/double-touch/status', methods=['GET'])
def double_touch_status():
    scanner = scanners.get('double_touch')
    if not scanner:
        return jsonify({'count': 0, 'alerts': [], 'monitored': 0})
    alerts = scanner.get_today_alerts()
    return jsonify({'count': len(alerts), 'alerts': alerts, 'monitored': scanner.get_monitored_count()})

@app.route('/scanner-api/daily-flip/status', methods=['GET'])
def daily_flip_status():
    scanner = scanners.get('daily_flip')
    if not scanner:
        return jsonify({'count': 0, 'alerts': [], 'monitored': 0})
    alerts = scanner.get_today_alerts()
    return jsonify({'count': len(alerts), 'alerts': alerts, 'monitored': scanner.get_monitored_count()})

@app.route('/scanner-api/shimano/status', methods=['GET'])
def shimano_status():
    scanner = scanners.get('shimano')
    if not scanner:
        return jsonify({'count': 0, 'alerts': [], 'monitored': 0})
    alerts = scanner.get_today_alerts()
    return jsonify({'count': len(alerts), 'alerts': alerts, 'monitored': scanner.get_monitored_count()})

@app.route('/scanner-api/alerts/recent', methods=['GET'])
def get_recent_alerts():
    """Return all alerts fired today (UTC). Resets automatically at midnight."""
    try:
        from daily_log import get_today_alerts
        alerts = get_today_alerts()
        return jsonify({'success': True, 'alerts': alerts, 'count': len(alerts)})
    except Exception as e:
        logger.error(f"❌ Error getting recent alerts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/scanner-api/screenshots/<filename>', methods=['GET'])
def get_screenshot_file(filename):
    if not re.match(r'^[\w\-\.]+\.png$', filename):
        return '', 404
    path = f'/data/screenshots/{filename}'
    if not os.path.exists(path):
        return '', 404
    with open(path, 'rb') as f:
        return Response(f.read(), mimetype='image/png',
                        headers={'Cache-Control': 'public, max-age=86400'})

_news_cache = {'data': None, 'ts': 0}
_news_lock = threading.Lock()

@app.route('/api/news', methods=['GET'])
def get_bybit_news():
    import requests as req
    CATEGORIES = [
        ('new_crypto',          'Listing'),
        ('delistings',          'Delisting'),
        ('latest_bybit_news',   'News'),
    ]
    global _news_cache
    with _news_lock:
        if _news_cache['data'] and time.time() - _news_cache['ts'] < 300:
            return jsonify(_news_cache['data'])
    try:
        result = {}
        for key, label in CATEGORIES:
            resp = req.get(
                'https://api.bybit.com/v5/announcements/index',
                params={'locale': 'en-US', 'type': key, 'limit': 50},
                timeout=8
            )
            data = resp.json()
            items = []
            if data.get('retCode') == 0:
                for item in data['result']['list']:
                    items.append({
                        'title': item['title'],
                        'desc': item.get('description', ''),
                        'url': item['url'],
                        'ts': item['dateTimestamp'],
                    })
            result[key] = {'label': label, 'items': items}
        payload = {'success': True, 'categories': result, 'fetched_at': int(time.time() * 1000)}
        with _news_lock:
            _news_cache = {'data': payload, 'ts': time.time()}
        return jsonify(payload)
    except Exception as e:
        logger.error(f"❌ News API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/top-coins', methods=['GET'])
def get_top_coins():
    """Return top N coins sorted by 24h change % (gainers or losers), filtered by min volume"""
    import requests as req
    try:
        limit = min(int(request.args.get('limit', 500)), 500)
        min_vol = float(request.args.get('min_volume', 10_000_000))
        sort = request.args.get('sort', 'gainers')  # 'gainers' | 'losers'

        url = 'https://api.bybit.com/v5/market/tickers'
        response = req.get(url, params={'category': 'linear'}, timeout=10)
        data = response.json()

        if data.get('retCode') != 0:
            return jsonify({'error': 'Bybit API error'}), 502

        coins = []
        for item in data['result']['list']:
            if not item['symbol'].endswith('USDT'):
                continue
            last_price = float(item['lastPrice'])
            vol_24h = float(item.get('volume24h', 0)) * last_price
            if vol_24h < min_vol:
                continue
            coins.append({
                'symbol': item['symbol'],
                'price': last_price,
                'change_24h': round(float(item.get('price24hPcnt', 0)) * 100, 2),
                'volume_24h': vol_24h,
            })

        if sort == 'gainers':
            coins = [c for c in coins if c['change_24h'] > 0]
            coins.sort(key=lambda x: x['change_24h'], reverse=True)
        else:
            coins = [c for c in coins if c['change_24h'] < 0]
            coins.sort(key=lambda x: x['change_24h'], reverse=False)
        return jsonify({'success': True, 'data': coins[:limit]})
    except Exception as e:
        logger.error(f"Error fetching top coins: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/high-volume', methods=['GET'])
def get_high_volume():
    """Return all USDT perpetuals with 24h volume >= min_volume, sorted by volume desc.
    Uses ws_manager in-memory cache when ready; falls back to Bybit REST otherwise."""
    import requests as req
    try:
        min_vol = float(request.args.get('min_volume', config['general'].get('min_volume_24h', 10_000_000)))
        extra_symbols = set(s.strip() for s in request.args.get('extra_symbols', '').split(',') if s.strip())
        coins = []
        seen = set()

        if ws_manager.ready.is_set():
            tickers = ws_manager.get_all_tickers()
            for symbol, t in tickers.items():
                if not symbol.endswith('USDT'):
                    continue
                price   = t.get('price', 0)
                vol_24h = t.get('volume_24h', 0)
                if not price or (vol_24h < min_vol and symbol not in extra_symbols):
                    continue
                coins.append({
                    'symbol':     symbol,
                    'price':      price,
                    'change_24h': round(t.get('change_24h', 0), 2),
                    'volume_24h': vol_24h,
                })
                seen.add(symbol)
        else:
            response = req.get('https://api.bybit.com/v5/market/tickers',
                               params={'category': 'linear'}, timeout=10)
            data = response.json()
            if data.get('retCode') != 0:
                return jsonify({'error': 'Bybit API error'}), 502
            for item in data['result']['list']:
                if not item['symbol'].endswith('USDT'):
                    continue
                last_price = float(item['lastPrice'])
                vol_24h    = float(item.get('volume24h', 0)) * last_price
                if vol_24h < min_vol and item['symbol'] not in extra_symbols:
                    continue
                coins.append({
                    'symbol':     item['symbol'],
                    'price':      last_price,
                    'change_24h': round(float(item.get('price24hPcnt', 0)) * 100, 2),
                    'volume_24h': vol_24h,
                })
                seen.add(item['symbol'])

        # Extra symbols not found in ws_manager: fetch via REST
        missing = extra_symbols - seen
        if missing:
            try:
                r = req.get('https://api.bybit.com/v5/market/tickers',
                            params={'category': 'linear'}, timeout=8)
                d = r.json()
                if d.get('retCode') == 0:
                    for item in d['result']['list']:
                        if item['symbol'] not in missing:
                            continue
                        lp = float(item['lastPrice'])
                        coins.append({
                            'symbol':     item['symbol'],
                            'price':      lp,
                            'change_24h': round(float(item.get('price24hPcnt', 0)) * 100, 2),
                            'volume_24h': float(item.get('volume24h', 0)) * lp,
                        })
            except Exception:
                pass

        coins.sort(key=lambda x: x['volume_24h'], reverse=True)

        # Subscribe to 30m klines for coins not yet tracked
        new_syms = [c['symbol'] for c in coins if c['symbol'] not in _hv_klines_subscribed]
        if new_syms:
            ws_manager.subscribe_klines(new_syms, intervals=['30'])
            _hv_klines_subscribed.update(new_syms)

        # Attach EMA60(30m), daily touch count, ATH/ATL distances
        ath_scanner = scanners.get('ath_atl')
        for coin in coins:
            klines_30m = ws_manager.get_klines(coin['symbol'], '30')
            if len(klines_30m) < 60:
                try:
                    import requests as _req
                    r = _req.get('https://api.bybit.com/v5/market/kline',
                        params={'category':'linear','symbol':coin['symbol'],'interval':'30','limit':200},
                        timeout=5)
                    d = r.json()
                    if d.get('retCode') == 0:
                        klines_30m = [{'time':int(k[0])//1000,'open':float(k[1]),'high':float(k[2]),
                            'low':float(k[3]),'close':float(k[4]),'volume':float(k[5])}
                            for k in reversed(d['result']['list'])]
                        with ws_manager._lock:
                            ws_manager._klines[(coin['symbol'],'30')] = klines_30m
                except Exception as _fe:
                    logger.error(f'kline fallback {coin["symbol"]}: {_fe}')
            closes = [k['close'] for k in klines_30m]
            ema = _compute_ema(closes, 60)
            coin['ema60_30m'] = ema
            coin['ema60_dist'] = round((coin['price'] - ema) / ema * 100, 2) if ema else None
            coin['ema_touch_count'] = _count_ema_touches_daily(klines_30m, period=60, threshold=2.0)
            aa = ath_scanner.get_ath_atl(coin['symbol']) if ath_scanner else None
            if aa:
                p = coin['price']
                coin['ath_dist']  = round((aa['ath'] - p) / aa['ath'] * 100, 2)
                coin['atl_dist']  = round((p - aa['atl']) / aa['atl'] * 100, 2)
                coin['ath_price'] = aa['ath']
                coin['atl_price'] = aa['atl']
            else:
                coin['ath_dist']  = None
                coin['atl_dist']  = None
                coin['ath_price'] = None
                coin['atl_price'] = None

        return jsonify({'success': True, 'data': coins, 'count': len(coins)})
    except Exception as e:
        logger.error(f"Error fetching high-volume coins: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/ath-atl')
def get_ath_atl_symbol():
    symbol = request.args.get('symbol', '').upper()
    if not symbol:
        return jsonify({'error': 'symbol required'}), 400
    scanner = scanners.get('ath_atl')
    if not scanner:
        return jsonify({'error': 'scanner unavailable'}), 503
    aa = scanner.get_ath_atl(symbol)
    if aa is None:
        return jsonify({'status': 'pending'})
    return jsonify({'status': 'ok', 'ath': aa['ath'], 'atl': aa['atl']})


@app.route('/api/new-listings', methods=['GET'])
def get_new_listings():
    """Return recently listed USDT perpetuals on Bybit, sorted by listing date desc"""
    import requests as req
    try:
        days  = int(request.args.get('days', 90))
        limit = min(int(request.args.get('limit', 500)), 500)
        cutoff_ms = (time.time() - days * 86400) * 1000

        resp_i = req.get('https://api.bybit.com/v5/market/instruments-info',
                         params={'category': 'linear', 'limit': 1000}, timeout=15)
        instr_data = resp_i.json()
        if instr_data.get('retCode') != 0:
            return jsonify({'error': 'Bybit instruments API error'}), 502

        new_symbols = {}
        for item in instr_data['result']['list']:
            sym = item.get('symbol', '')
            if not sym.endswith('USDT') or item.get('status') != 'Trading':
                continue
            if item.get('symbolType') in ('stock', 'commodity'):
                continue
            launch = int(item.get('launchTime', 0))
            if launch >= cutoff_ms:
                new_symbols[sym] = launch

        if not new_symbols:
            return jsonify({'success': True, 'data': []})

        resp_t = req.get('https://api.bybit.com/v5/market/tickers',
                         params={'category': 'linear'}, timeout=10)
        ticker_data = resp_t.json()
        if ticker_data.get('retCode') != 0:
            return jsonify({'error': 'Bybit tickers API error'}), 502

        result = []
        for item in ticker_data['result']['list']:
            sym = item['symbol']
            if sym not in new_symbols:
                continue
            last_price = float(item['lastPrice'])
            vol_24h = float(item.get('volume24h', 0)) * last_price
            result.append({
                'symbol': sym,
                'price': last_price,
                'change_24h': round(float(item.get('price24hPcnt', 0)) * 100, 2),
                'volume_24h': vol_24h,
                'launch_time': new_symbols[sym],
            })

        result.sort(key=lambda x: x['launch_time'], reverse=True)
        return jsonify({'success': True, 'data': result[:limit]})
    except Exception as e:
        logger.error(f"Error fetching new listings: {e}")
        return jsonify({'error': str(e)}), 500


FAVORITES_FILE = '/data/favorites.json'

def _load_favorites():
    try:
        if os.path.exists(FAVORITES_FILE):
            with open(FAVORITES_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return []

def _save_favorites(symbols):
    try:
        os.makedirs(os.path.dirname(FAVORITES_FILE), exist_ok=True)
        with open(FAVORITES_FILE, 'w') as f:
            json.dump(symbols, f)
        return True
    except Exception:
        return False

@app.route('/api/favorites', methods=['GET'])
def get_favorites():
    """Return favorites list with live ticker data from Bybit"""
    import requests as req
    symbols = _load_favorites()
    if not symbols:
        return jsonify({'success': True, 'symbols': [], 'data': []})
    try:
        response = req.get('https://api.bybit.com/v5/market/tickers',
                           params={'category': 'linear'}, timeout=10)
        data = response.json()
        if data.get('retCode') != 0:
            return jsonify({'error': 'Bybit API error'}), 502
        ticker_map = {item['symbol']: item for item in data['result']['list']}
        result = []
        for sym in symbols:
            item = ticker_map.get(sym)
            if not item:
                continue
            last_price = float(item['lastPrice'])
            vol_24h = float(item.get('volume24h', 0)) * last_price
            result.append({
                'symbol': sym,
                'price': last_price,
                'change_24h': round(float(item.get('price24hPcnt', 0)) * 100, 2),
                'volume_24h': vol_24h,
            })
        return jsonify({'success': True, 'symbols': symbols, 'data': result})
    except Exception as e:
        logger.error(f"Error fetching favorites data: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/favorites', methods=['POST'])
def update_favorites():
    """Save favorites list"""
    try:
        body = request.get_json() or {}
        symbols = [
            s for s in body.get('symbols', [])
            if isinstance(s, str) and s.endswith('USDT') and len(s) <= 20
        ]
        if _save_favorites(symbols):
            return jsonify({'success': True, 'count': len(symbols)})
        return jsonify({'success': False, 'error': 'Save failed'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


ALERTS_FILE = '/data/price_alerts.json'

def _load_alerts():
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return []

def _save_alerts(alerts):
    try:
        os.makedirs('/data', exist_ok=True)
        with open(ALERTS_FILE, 'w') as f:
            json.dump(alerts, f)
        return True
    except Exception:
        return False

def _fmt_price(p):
    if p >= 10000: return f'{p:,.0f}'
    if p >= 1:     return f'{p:.3f}'
    if p >= 0.01:  return f'{p:.5f}'
    return f'{p:.7f}'


def send_telegram(text):
    import requests as req
    token = config['telegram']['token']
    chat_id = config['telegram']['chat_id']
    if not token or not chat_id:
        logger.warning("Telegram not configured, skipping alert")
        return
    try:
        req.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': text},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Telegram send error: {e}")


def send_telegram_photo(image_bytes, caption):
    import requests as req
    token = config['telegram']['token']
    chat_id = config['telegram']['chat_id']
    if not token or not chat_id:
        return
    try:
        req.post(
            f'https://api.telegram.org/bot{token}/sendPhoto',
            files={'photo': ('chart.png', image_bytes, 'image/png')},
            data={'chat_id': chat_id, 'caption': caption, 'parse_mode': 'HTML'},
            timeout=30,
        )
    except Exception as e:
        logger.error(f"Telegram photo error: {e}")


def check_price_alerts():
    import requests as req
    while True:
        time.sleep(60)
        try:
            alerts = _load_alerts()
            active = [a for a in alerts if not a.get('triggered')]
            if not active:
                continue
            response = req.get('https://api.bybit.com/v5/market/tickers',
                               params={'category': 'linear'}, timeout=10)
            data = response.json()
            if data.get('retCode') != 0:
                continue
            price_map = {item['symbol']: float(item['lastPrice'])
                         for item in data['result']['list']}
            modified = False
            for alert in alerts:
                if alert.get('triggered'):
                    continue
                sym = alert['symbol']
                if sym not in price_map:
                    continue
                cur_price = price_map[sym]
                hit = (alert['condition'] == 'above' and cur_price >= alert['price']) or \
                      (alert['condition'] == 'below' and cur_price <= alert['price'])
                if hit:
                    alert['triggered'] = True
                    modified = True
                    with _triggered_lock:
                        _recently_triggered.append(dict(alert))

                    coin      = sym.replace('USDT', '')
                    dir_word  = 'Sopra' if alert['condition'] == 'above' else 'Sotto'
                    base_url    = config['telegram'].get('base_url', '').rstrip('/')
                    link      = f'<a href="{base_url}/mtf?symbol={sym}">{coin}</a>' if base_url else coin
                    caption   = f"{dir_word} {_fmt_price(alert['price'])}  {link}"

                    if alert.get('notify', 'both') != 'browser':
                        img = None
                        try:
                            from alert_utils import get_chart
                            img = get_chart(sym, interval='60', signal={
                                'type':      'price',
                                'price':     alert['price'],
                                'condition': alert['condition'],
                            })
                        except Exception as ce:
                            logger.error(f"Chart error for price alert {sym}: {ce}")
                        if img:
                            send_telegram_photo(img, caption)
                        else:
                            send_telegram(caption)

                    logger.info(f"Alert triggered: {sym} {alert['condition']} {alert['price']}")
            if modified:
                _save_alerts(alerts)
        except Exception as e:
            logger.error(f"Error in check_price_alerts: {e}")

@app.route('/api/price-alerts', methods=['GET'])
def get_price_alerts():
    alerts = [a for a in _load_alerts() if not a.get('triggered')]
    return jsonify({'success': True, 'data': alerts})

@app.route('/api/price-alerts', methods=['POST'])
def create_price_alert():
    try:
        body = request.get_json() or {}
        symbol = str(body.get('symbol', ''))
        price  = float(body.get('price', 0))
        condition = str(body.get('condition', ''))
        if not symbol or price <= 0 or condition not in ('above', 'below'):
            return jsonify({'success': False, 'error': 'Invalid params'}), 400
        notify = str(body.get('notify', 'both'))
        if notify not in ('both', 'browser'):
            notify = 'both'
        alerts = _load_alerts()
        alert = {
            'id':         str(uuid.uuid4()),
            'symbol':     symbol,
            'price':      price,
            'condition':  condition,
            'notify':     notify,
            'created_at': time.time(),
            'triggered':  False,
        }
        alerts.append(alert)
        _save_alerts(alerts)
        return jsonify({'success': True, 'alert': alert})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/price-alerts/<alert_id>', methods=['DELETE'])
def delete_price_alert(alert_id):
    alerts = [a for a in _load_alerts() if a.get('id') != alert_id]
    _save_alerts(alerts)
    return jsonify({'success': True})

@app.route('/api/price-alerts/recent-triggered', methods=['GET'])
def get_recent_triggered():
    with _triggered_lock:
        data = list(_recently_triggered)
        _recently_triggered.clear()
    return jsonify({'success': True, 'data': data})

def _fetch_klines_bybit(symbol, interval, max_pages):
    """Paginated Bybit kline fetch, shared by /api/klines and the bot backtest."""
    import requests as req

    utc_off = config.get('general', {}).get('utc_offset', 2)
    try:
        utc_off = float(utc_off)
    except (TypeError, ValueError):
        utc_off = 2
    tz_s = int(utc_off * 3600)

    url      = 'https://api.bybit.com/v5/market/kline'
    all_raw  = []
    end_time = None

    for _ in range(max_pages):
        params = {'category': 'linear', 'symbol': symbol, 'interval': interval, 'limit': 1000}
        if end_time:
            params['end'] = end_time
        data = req.get(url, params=params, timeout=8).json()
        if data.get('retCode') != 0:
            raise RuntimeError(data.get('retMsg', 'Bybit API error'))
        batch = data['result']['list']  # newest first
        if not batch:
            break
        all_raw.extend(batch)
        if len(batch) < 1000:
            break  # reached the beginning of available data
        end_time = int(batch[-1][0]) - 1  # move window further back

    # Reverse to chronological order, deduplicate by timestamp
    all_raw.reverse()
    seen   = set()
    result = []
    for k in all_raw:
        ts = int(k[0]) // 1000 + tz_s
        if ts in seen:
            continue
        seen.add(ts)
        result.append({'time': ts, 'open': float(k[1]), 'high': float(k[2]),
                        'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5])})
    return result, tz_s


@app.route('/api/klines', methods=['GET'])
def get_klines():
    """Proxy Bybit klines — paginated to fetch all available data"""
    import re

    symbol   = request.args.get('symbol', 'BTCUSDT').upper()
    interval = request.args.get('interval', '15')

    if not re.match(r'^[A-Z0-9]{3,20}$', symbol) or not symbol.endswith('USDT'):
        return jsonify({'error': 'Invalid symbol'}), 400

    if interval not in {'1', '5', '15', '30', '60', '240', 'D', 'W', 'M'}:
        return jsonify({'error': 'Invalid interval'}), 400

    # Max pages per TF to keep response times reasonable
    max_pages = {'1': 2, '5': 3, '15': 3, '30': 4, '60': 4, '240': 5}.get(interval, 5)

    try:
        result, tz_s = _fetch_klines_bybit(symbol, interval, max_pages)
        resp = jsonify({'success': True, 'data': result, 'symbol': symbol,
                        'interval': interval, 'utc_offset_s': tz_s})
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    except Exception as e:
        logger.error(f"Error fetching klines for {symbol}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/klines/live', methods=['GET'])
def get_klines_live():
    """Return last 10 live klines for fast-TF polling.
    Uses ws_manager cache when seeded; falls back to Bybit REST immediately otherwise."""
    import requests as req, re
    symbol   = request.args.get('symbol', 'BTCUSDT').upper()
    interval = request.args.get('interval', '1')

    if not re.match(r'^[A-Z0-9]{3,20}$', symbol) or not symbol.endswith('USDT'):
        return jsonify({'error': 'Invalid symbol'}), 400
    if interval not in {'1', '5', '15', '30', '60', '240', 'D', 'W', 'M'}:
        return jsonify({'error': 'Invalid interval'}), 400

    utc_off = config.get('general', {}).get('utc_offset', 2)
    try:
        utc_off = float(utc_off)
    except (TypeError, ValueError):
        utc_off = 2
    tz_s = int(utc_off * 3600)

    key = (symbol, interval)
    if key not in _mtf_klines_subscribed:
        ws_manager.subscribe_klines([symbol], intervals=[interval])
        _mtf_klines_subscribed.add(key)

    klines = ws_manager.get_klines(symbol, interval)

    if not klines:
        # ws_manager seed not ready yet — call Bybit REST directly
        try:
            r = req.get('https://api.bybit.com/v5/market/kline',
                        params={'category': 'linear', 'symbol': symbol,
                                'interval': interval, 'limit': 10},
                        timeout=6)
            data = r.json()
            if data.get('retCode') == 0:
                klines = [{'time': int(k[0]) // 1000,
                           'open': float(k[1]), 'high': float(k[2]),
                           'low':  float(k[3]), 'close': float(k[4]),
                           'volume': float(k[5])}
                          for k in reversed(data['result']['list'])]
        except Exception as e:
            logger.warning(f'klines/live REST fallback error: {e}')

    tail = klines[-10:] if klines else []
    result = [{
        'time':   k['time'] + tz_s,
        'open':   k['open'],
        'high':   k['high'],
        'low':    k['low'],
        'close':  k['close'],
        'volume': k['volume'],
    } for k in tail]

    resp = jsonify({'success': bool(result), 'data': result, 'utc_offset_s': tz_s})
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/ticker', methods=['GET'])
def get_ticker():
    """Get ticker data for a single USDT symbol."""
    import requests as req
    symbol = request.args.get('symbol', '').upper()
    if not symbol.endswith('USDT') or len(symbol) > 20:
        return jsonify({'error': 'Invalid symbol'}), 400
    try:
        r = req.get('https://api.bybit.com/v5/market/tickers',
                    params={'category': 'linear', 'symbol': symbol},
                    timeout=6)
        d = r.json()
        if d.get('retCode') != 0 or not d['result']['list']:
            return jsonify({'error': 'Symbol not found'}), 404
        t = d['result']['list'][0]
        last_price = float(t['lastPrice'])
        change_pct = round(float(t.get('price24hPcnt', 0)) * 100, 2)
        vol_24h    = float(t.get('volume24h', 0)) * last_price
        return jsonify({'success': True, 'symbol': symbol,
                        'price': last_price, 'change_24h': change_pct,
                        'volume_24h': vol_24h})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── SETTINGS ──────────────────────────────────────────────────────────────────
OPTIONS_FILE = '/data/options.json'

def _read_opts():
    try:
        with open(OPTIONS_FILE) as f:
            opts = json.load(f)
        opts['bybit_api_key']    = _dec(opts.get('bybit_api_key', ''))
        opts['bybit_api_secret'] = _dec(opts.get('bybit_api_secret', ''))
        return opts
    except:
        return {}

def _write_opts(opts):
    to_save = dict(opts)
    to_save['bybit_api_key']    = _enc(to_save.get('bybit_api_key', ''))
    to_save['bybit_api_secret'] = _enc(to_save.get('bybit_api_secret', ''))
    with open(OPTIONS_FILE, 'w') as f:
        json.dump(to_save, f, indent=2)

@app.route('/settings')
def settings_page():
    return send_file('/usr/share/nginx/html/settings.html')

@app.route('/profile')
@login_required
def profile_page():
    return send_file('/usr/share/nginx/html/profile.html')

@app.route('/api/settings', methods=['GET'])
@login_required
def get_settings():
    role = session.get('role', '')
    username = session.get('username', '')
    if role != 'admin':
        user = _db_get_user(username)
        k = _dec(user.get('bybit_api_key', '') or '') if user else ''
        s = _dec(user.get('bybit_api_secret', '') or '') if user else ''
        return jsonify({
            'bybit_api_key':    k,
            'bybit_api_secret': ('●' * 8) if s else '',
            'bybit_secret_set': bool(s),
            'trading_enabled':  True,
        })
    opts = _read_opts()
    key = opts.get('bybit_api_key', '')
    sec = opts.get('bybit_api_secret', '')
    return jsonify({
        'telegram_token':   opts.get('telegram_token', ''),
        'telegram_chat_id': opts.get('telegram_chat_id', ''),
        'bybit_api_key':    key,
        'bybit_api_secret': ('●' * 8) if sec else '',
        'bybit_secret_set': bool(sec),
        'trading_enabled':  opts.get('trading_enabled', False),
    })

@app.route('/api/settings', methods=['POST'])
@login_required
def save_settings():
    data = request.get_json() or {}
    role = session.get('role', '')
    username = session.get('username', '')
    if role != 'admin':
        bk = data.get('bybit_api_key', '').strip()
        bs = data.get('bybit_api_secret', '').strip()
        with sqlite3.connect(USERS_DB) as conn:
            if bk:
                conn.execute('UPDATE users SET bybit_api_key=? WHERE lower(username)=lower(?)', (_enc(bk), username))
            if bs and '●' not in bs:
                conn.execute('UPDATE users SET bybit_api_secret=? WHERE lower(username)=lower(?)', (_enc(bs), username))
            conn.commit()
        return jsonify({'success': True})
    opts = _read_opts()
    if 'telegram_token'   in data: opts['telegram_token']   = data['telegram_token'].strip()
    if 'telegram_chat_id' in data: opts['telegram_chat_id'] = data['telegram_chat_id'].strip()
    if 'bybit_api_key'    in data: opts['bybit_api_key']    = data['bybit_api_key'].strip()
    if 'bybit_api_secret' in data and data['bybit_api_secret'] and '●' not in data['bybit_api_secret']:
        opts['bybit_api_secret'] = data['bybit_api_secret'].strip()
    if 'trading_enabled'  in data: opts['trading_enabled']  = bool(data['trading_enabled'])
    _write_opts(opts)
    return jsonify({'success': True})

# ── TRADING ───────────────────────────────────────────────────────────────────
import hmac as _hmac, hashlib as _hashlib
from cryptography.fernet import Fernet
_BYB = 'https://api.bybit.com'

# ── FERNET ENCRYPTION ─────────────────────────────────────────────────────────
_fernet_key_file = '/data/.fernet_key'

def _get_fernet():
    if os.path.exists(_fernet_key_file):
        with open(_fernet_key_file, 'rb') as f:
            key = f.read().strip()
    else:
        key = Fernet.generate_key()
        with open(_fernet_key_file, 'wb') as f:
            f.write(key)
        try:
            os.chmod(_fernet_key_file, 0o600)
        except Exception:
            pass
        logger.info('✅ Fernet key generated')
    return Fernet(key)

def _enc(value):
    if not value:
        return value
    return 'enc:' + _get_fernet().encrypt(value.encode()).decode()

def _dec(value):
    if not value or not str(value).startswith('enc:'):
        return value  # plain text legacy — will be encrypted on next save
    try:
        return _get_fernet().decrypt(value[4:].encode()).decode()
    except Exception:
        logger.warning('⚠️ Failed to decrypt field — key mismatch?')
        return ''

def _tcfg():
    try:
        opts = _read_opts()
        k = opts.get('bybit_api_key', '').strip()
        s = opts.get('bybit_api_secret', '').strip()
        return k, s, bool(opts.get('trading_enabled')) and bool(k) and bool(s)
    except: return '', '', False

def _tcfg_user(username):
    try:
        user = _db_get_user(username)
        if user:
            k = _dec(user.get('bybit_api_key', '') or '')
            s = _dec(user.get('bybit_api_secret', '') or '')
            if k and s:
                opts = _read_opts()
                return k, s, bool(opts.get('trading_enabled', True))
    except Exception:
        pass
    return _tcfg()

def _bsign(key, secret, payload):
    ts = str(int(time.time() * 1000))
    sig = _hmac.new(secret.encode(), (ts + key + '5000' + payload).encode(), _hashlib.sha256).hexdigest()
    return {'X-BAPI-API-KEY': key, 'X-BAPI-TIMESTAMP': ts, 'X-BAPI-SIGN': sig,
            'X-BAPI-RECV-WINDOW': '5000', 'Content-Type': 'application/json'}

@app.route('/api/trade/config')
@login_required
def trade_config():
    _k, _s, en = _tcfg_user(session.get("username", ""))
    return jsonify({'enabled': en})

@app.route('/api/trade/balance')
@login_required
def trade_balance():
    import requests as rq
    k, s, en = _tcfg_user(session.get("username", ""))
    if not en: return jsonify({'error': 'not configured'}), 403
    qs = 'accountType=UNIFIED'
    d = rq.get(f'{_BYB}/v5/account/wallet-balance?{qs}', headers=_bsign(k, s, qs), timeout=6).json()
    if d.get('retCode') != 0: return jsonify({'error': d.get('retMsg'), 'code': d.get('retCode')}), 400
    for acc in d['result']['list']:
        for c in acc.get('coin', []):
            if c['coin'] == 'USDT':
                def _f(v): return float(v) if v not in ('', None) else 0.0
                avail = _f(c.get('availableToWithdraw')) or _f(c.get('walletBalance', 0))
                return jsonify({'available': avail, 'equity': _f(c.get('equity', 0))})
    return jsonify({'available': 0, 'equity': 0})

@app.route('/api/trade/position')
@login_required
def trade_position():
    import requests as rq
    k, s, en = _tcfg_user(session.get("username", ""))
    if not en: return jsonify({'error': 'not configured'}), 403
    sym = request.args.get('symbol', '').upper()
    qs = f'category=linear&symbol={sym}'
    d = rq.get(f'{_BYB}/v5/position/list?{qs}', headers=_bsign(k, s, qs), timeout=6).json()
    if d.get('retCode') != 0: return jsonify({'error': d.get('retMsg'), 'code': d.get('retCode')}), 400
    lst = d['result']['list']
    if not lst or float(lst[0].get('size', 0)) == 0: return jsonify({'position': None})
    p = lst[0]
    def _fv(v): return float(v) if v and v != '0' else None
    return jsonify({'position': {
        'side': p['side'], 'size': float(p['size']), 'entryPrice': float(p['avgPrice']),
        'leverage': float(p['leverage']), 'unrealizedPnl': float(p.get('unrealisedPnl', 0)),
        'stopLoss': _fv(p.get('stopLoss')), 'takeProfit': _fv(p.get('takeProfit')),
        'positionIdx': int(p.get('positionIdx', 0)),
        'markPrice': float(p.get('markPrice', 0)), 'liqPrice': _fv(p.get('liqPrice')),
    }})

@app.route('/api/trade/instrument')
@login_required
def trade_instrument():
    import requests as rq
    sym = request.args.get('symbol', '').upper()
    d = rq.get(f'{_BYB}/v5/market/instruments-info',
               params={'category': 'linear', 'symbol': sym}, timeout=6).json()
    if d.get('retCode') != 0 or not d['result']['list']: return jsonify({'error': 'not found'}), 404
    info = d['result']['list'][0]
    lot = info.get('lotSizeFilter', {}); lev = info.get('leverageFilter', {})
    return jsonify({'qtyStep': lot.get('qtyStep', '0.001'),
                    'minOrderQty': lot.get('minOrderQty', '0.001'),
                    'maxLeverage': float(lev.get('maxLeverage', 100))})

@app.route('/api/trade/orders')
@login_required
def trade_orders():
    import requests as rq
    k, s, en = _tcfg_user(session.get("username", ""))
    if not en: return jsonify({'error': 'not configured'}), 403
    sym = request.args.get('symbol', '').upper()
    orders = []
    for flt in ['Order', 'StopOrder']:
        qs = f'category=linear&symbol={sym}&openOnly=0&orderFilter={flt}&limit=50'
        try:
            d = rq.get(f'{_BYB}/v5/order/realtime?{qs}', headers=_bsign(k, s, qs), timeout=6).json()
        except Exception:
            continue
        if d.get('retCode') != 0:
            continue
        for o in d['result']['list']:
            if o.get('orderStatus') in ('Filled', 'Cancelled', 'Rejected', 'Deactivated'):
                continue
            orders.append({
                'orderId':      o['orderId'],
                'side':         o['side'],
                'orderType':    o['orderType'],
                'qty':          o['qty'],
                'price':        o.get('price', ''),
                'triggerPrice': o.get('triggerPrice', ''),
                'stopLoss':     o.get('stopLoss', ''),
                'takeProfit':   o.get('takeProfit', ''),
                'orderFilter':  flt,
                'status':       o['orderStatus'],
            })
    resp = jsonify({'orders': orders})
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.route('/api/trade/cancel', methods=['POST'])
@login_required
def trade_cancel():
    import requests as rq
    k, s, en = _tcfg_user(session.get("username", ""))
    if not en: return jsonify({'error': 'not configured'}), 403
    data = request.get_json() or {}
    sym = data.get('symbol', '').upper()
    order_id = data.get('orderId', '')
    order_filter = data.get('orderFilter', 'Order')
    if not sym or not order_id: return jsonify({'error': 'missing params'}), 400
    body = json.dumps({'category': 'linear', 'symbol': sym, 'orderId': order_id, 'orderFilter': order_filter})
    d = rq.post(f'{_BYB}/v5/order/cancel', headers=_bsign(k, s, body), data=body, timeout=10).json()
    if d.get('retCode') != 0: return jsonify({'error': d.get('retMsg'), 'code': d.get('retCode')}), 400
    return jsonify({'success': True})

@app.route('/api/trade/amend', methods=['POST'])
@login_required
def trade_amend():
    import requests as rq
    k, s, en = _tcfg_user(session.get("username", ""))
    if not en: return jsonify({'error': 'not configured'}), 403
    data = request.get_json() or {}
    sym = data.get('symbol', '').upper()
    order_id = data.get('orderId', '')
    if not sym or not order_id: return jsonify({'error': 'missing params'}), 400
    order_filter = data.get('orderFilter', 'Order')
    body = {'category': 'linear', 'symbol': sym, 'orderId': order_id}
    if order_filter == 'StopOrder': body['orderFilter'] = 'StopOrder'
    if data.get('triggerPrice'): body['triggerPrice'] = str(data['triggerPrice'])
    if data.get('price'): body['price'] = str(data['price'])
    if data.get('qty'): body['qty'] = str(data['qty'])
    if data.get('stopLoss') is not None and data.get('stopLoss') not in ('', None):
        sl = data['stopLoss']
        body['stopLoss'] = str(sl)
        if float(sl) != 0: body['slTriggerBy'] = 'MarkPrice'
    if data.get('takeProfit') is not None and data.get('takeProfit') not in ('', None):
        tp = data['takeProfit']
        body['takeProfit'] = str(tp)
        if float(tp) != 0: body['tpTriggerBy'] = 'MarkPrice'
    b = json.dumps(body)
    d = rq.post(f'{_BYB}/v5/order/amend', headers=_bsign(k, s, b), data=b, timeout=10).json()
    if d.get('retCode') != 0: return jsonify({'error': d.get('retMsg'), 'code': d.get('retCode')}), 400
    return jsonify({'success': True})

@app.route('/api/trade/order', methods=['POST'])
@login_required
def trade_order():
    import requests as rq
    k, s, en = _tcfg_user(session.get("username", ""))
    if not en: return jsonify({'error': 'not configured'}), 403
    data = request.get_json() or {}
    sym = data.get('symbol', '').upper(); side = data.get('side')
    otype = data.get('orderType', 'Market'); qty = str(data.get('qty', ''))
    lev = str(int(data.get('leverage', 10)))
    order_filter = data.get('orderFilter', 'Order')
    trigger_price = data.get('triggerPrice')
    if not all([sym, side, qty]): return jsonify({'error': 'missing params'}), 400
    lb = json.dumps({'category': 'linear', 'symbol': sym, 'buyLeverage': lev, 'sellLeverage': lev})
    rq.post(f'{_BYB}/v5/position/set-leverage', headers=_bsign(k, s, lb), data=lb, timeout=6)
    order = {'category': 'linear', 'symbol': sym, 'side': side, 'orderType': otype, 'qty': qty,
             'timeInForce': 'GTC' if otype == 'Limit' else 'IOC'}
    if order_filter == 'StopOrder':
        order['orderFilter'] = 'StopOrder'
        order['timeInForce'] = 'GTC'
        order['triggerBy'] = 'LastPrice'
        if trigger_price: order['triggerPrice'] = str(trigger_price)
        if data.get('triggerDirection'): order['triggerDirection'] = int(data['triggerDirection'])
    if otype == 'Limit' and data.get('price'): order['price'] = str(data['price'])
    if data.get('stopLoss'): order['stopLoss'] = str(data['stopLoss'])
    if data.get('takeProfit'): order['takeProfit'] = str(data['takeProfit'])
    body = json.dumps(order)
    d = rq.post(f'{_BYB}/v5/order/create', headers=_bsign(k, s, body), data=body, timeout=10).json()
    if d.get('retCode') != 0: return jsonify({'error': d.get('retMsg', 'Order failed')}), 400
    return jsonify({'success': True, 'orderId': d['result'].get('orderId')})

@app.route('/api/trade/close', methods=['POST'])
@login_required
def trade_close_pos():
    import requests as rq
    k, s, en = _tcfg_user(session.get("username", ""))
    if not en: return jsonify({'error': 'not configured'}), 403
    data = request.get_json() or {}
    body = json.dumps({'category': 'linear', 'symbol': data.get('symbol', '').upper(),
                       'side': data.get('side'), 'orderType': 'Market',
                       'qty': str(data.get('qty', '')), 'reduceOnly': True, 'timeInForce': 'IOC'})
    d = rq.post(f'{_BYB}/v5/order/create', headers=_bsign(k, s, body), data=body, timeout=10).json()
    if d.get('retCode') != 0: return jsonify({'error': d.get('retMsg'), 'code': d.get('retCode')}), 400
    return jsonify({'success': True})

@app.route('/api/trade/set-sltp', methods=['POST'])
@login_required
def trade_set_sltp():
    import requests as rq
    k, s, en = _tcfg_user(session.get("username", ""))
    if not en: return jsonify({'error': 'not configured'}), 403
    data = request.get_json() or {}
    pos_idx = int(data.get('positionIdx', 0))
    body = {'category': 'linear', 'symbol': data.get('symbol', '').upper(), 'positionIdx': pos_idx}
    if data.get('stopLoss') is not None:
        body['stopLoss'] = str(data['stopLoss'])
        if float(data['stopLoss']) != 0: body['slTriggerBy'] = 'MarkPrice'
    if data.get('takeProfit') is not None:
        body['takeProfit'] = str(data['takeProfit'])
        if float(data['takeProfit']) != 0: body['tpTriggerBy'] = 'MarkPrice'
    b = json.dumps(body)
    d = rq.post(f'{_BYB}/v5/position/trading-stop', headers=_bsign(k, s, b), data=b, timeout=6).json()
    if d.get('retCode') != 0: return jsonify({'error': d.get('retMsg'), 'code': d.get('retCode')}), 400
    return jsonify({'success': True})

# ── END TRADING ────────────────────────────────────────────────────────────────

# ── BOT ────────────────────────────────────────────────────────────────────────

class _BotTradeClient:
    """Adapter sottile che riusa _tcfg()/_bsign()/_BYB per il motore BOT —
    nessuna duplicazione della logica di firma/decrypt già usata da /api/trade/*."""

    def get_position(self, symbol):
        import requests as rq
        k, s, en = _tcfg()
        if not en:
            return None
        qs = f'category=linear&symbol={symbol}'
        try:
            d = rq.get(f'{_BYB}/v5/position/list?{qs}', headers=_bsign(k, s, qs), timeout=6).json()
        except Exception:
            return None
        if d.get('retCode') != 0:
            return None
        lst = d['result']['list']
        if not lst or float(lst[0].get('size', 0)) == 0:
            return None
        p = lst[0]
        return {'side': p['side'], 'size': float(p['size']), 'entryPrice': float(p['avgPrice']),
                'positionIdx': int(p.get('positionIdx', 0))}

    def get_balance(self):
        import requests as rq
        k, s, en = _tcfg()
        if not en:
            return {'available': 0.0, 'equity': 0.0}
        qs = 'accountType=UNIFIED'
        try:
            d = rq.get(f'{_BYB}/v5/account/wallet-balance?{qs}', headers=_bsign(k, s, qs), timeout=6).json()
        except Exception:
            return {'available': 0.0, 'equity': 0.0}
        if d.get('retCode') != 0:
            return {'available': 0.0, 'equity': 0.0}
        for acc in d['result']['list']:
            for c in acc.get('coin', []):
                if c['coin'] == 'USDT':
                    def _f(v): return float(v) if v not in ('', None) else 0.0
                    avail = _f(c.get('availableToWithdraw')) or _f(c.get('walletBalance', 0))
                    return {'available': avail, 'equity': _f(c.get('equity', 0))}
        return {'available': 0.0, 'equity': 0.0}

    def get_instrument(self, symbol):
        import requests as rq
        try:
            d = rq.get(f'{_BYB}/v5/market/instruments-info',
                       params={'category': 'linear', 'symbol': symbol}, timeout=6).json()
        except Exception:
            return {'qtyStep': 0.001, 'minOrderQty': 0.001, 'maxLeverage': 100.0}
        if d.get('retCode') != 0 or not d['result']['list']:
            return {'qtyStep': 0.001, 'minOrderQty': 0.001, 'maxLeverage': 100.0}
        info = d['result']['list'][0]
        lot = info.get('lotSizeFilter', {}); lev = info.get('leverageFilter', {})
        return {'qtyStep': float(lot.get('qtyStep', 0.001)),
                'minOrderQty': float(lot.get('minOrderQty', 0.001)),
                'maxLeverage': float(lev.get('maxLeverage', 100))}

    def place_order(self, symbol, side, qty, leverage=1, stop_loss=None, take_profit=None):
        import requests as rq
        k, s, en = _tcfg()
        if not en:
            return False, None, 'trading non configurato'
        lev = str(int(leverage or 1))
        lb = json.dumps({'category': 'linear', 'symbol': symbol, 'buyLeverage': lev, 'sellLeverage': lev})
        try:
            rq.post(f'{_BYB}/v5/position/set-leverage', headers=_bsign(k, s, lb), data=lb, timeout=6)
        except Exception:
            pass
        order = {'category': 'linear', 'symbol': symbol, 'side': side, 'orderType': 'Market',
                 'qty': str(qty), 'timeInForce': 'IOC'}
        if stop_loss:
            order['stopLoss'] = str(round(stop_loss, 8))
        if take_profit:
            order['takeProfit'] = str(round(take_profit, 8))
        body = json.dumps(order)
        try:
            d = rq.post(f'{_BYB}/v5/order/create', headers=_bsign(k, s, body), data=body, timeout=10).json()
        except Exception as e:
            return False, None, str(e)
        if d.get('retCode') != 0:
            return False, None, d.get('retMsg', 'order failed')
        return True, d['result'].get('orderId'), None

    def close_position(self, symbol, position_side, qty):
        import requests as rq
        k, s, en = _tcfg()
        if not en:
            return False, 'trading non configurato'
        close_side = 'Sell' if position_side == 'Buy' else 'Buy'
        body = json.dumps({'category': 'linear', 'symbol': symbol, 'side': close_side,
                           'orderType': 'Market', 'qty': str(qty), 'reduceOnly': True,
                           'timeInForce': 'IOC'})
        try:
            d = rq.post(f'{_BYB}/v5/order/create', headers=_bsign(k, s, body), data=body, timeout=10).json()
        except Exception as e:
            return False, str(e)
        if d.get('retCode') != 0:
            return False, d.get('retMsg')
        return True, None


_bot_trade_client = _BotTradeClient()


def _bot_admin_gate():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Accesso negato'}), 403
    return None


@app.route('/api/bot/config', methods=['GET'])
@login_required
def bot_get_config():
    err = _bot_admin_gate()
    if err: return err
    return jsonify(config.get('bot', {}))


@app.route('/api/bot/config', methods=['POST'])
@login_required
def bot_save_config():
    err = _bot_admin_gate()
    if err: return err
    bot = scanners.get('bot')
    if bot and bot.running:
        return jsonify({'error': 'Ferma il bot prima di modificare la configurazione'}), 400
    data = request.get_json() or {}
    config.setdefault('bot', {}).update(data)
    if save_config():
        init_scanners()
        return jsonify({'success': True})
    return jsonify({'error': 'Errore salvataggio configurazione'}), 500


@app.route('/api/bot/start', methods=['POST'])
@login_required
def bot_start():
    err = _bot_admin_gate()
    if err: return err
    bot = scanners.get('bot')
    if not bot:
        return jsonify({'error': 'Bot non inizializzato'}), 500
    ok, msg = bot.start()
    if not ok:
        return jsonify({'error': msg}), 400
    return jsonify({'success': True})


@app.route('/api/bot/stop', methods=['POST'])
@login_required
def bot_stop():
    err = _bot_admin_gate()
    if err: return err
    bot = scanners.get('bot')
    if not bot:
        return jsonify({'error': 'Bot non inizializzato'}), 500
    data = request.get_json(silent=True) or {}
    return jsonify(bot.stop(close_position=bool(data.get('close_position'))))


@app.route('/api/bot/status', methods=['GET'])
@login_required
def bot_status():
    err = _bot_admin_gate()
    if err: return err
    bot = scanners.get('bot')
    if not bot:
        return jsonify({'running': False})
    return jsonify(bot.status())


@app.route('/api/bot/signals', methods=['GET'])
@login_required
def bot_signals():
    err = _bot_admin_gate()
    if err: return err
    bot = scanners.get('bot')
    if not bot:
        return jsonify({'signals': []})
    return jsonify({'signals': bot.get_signals()})


@app.route('/api/bot/backtest', methods=['POST'])
@login_required
def bot_backtest():
    err = _bot_admin_gate()
    if err: return err
    import re
    from scanners.bot_engine import normalize_params, run_backtest, TF_SECONDS

    data     = request.get_json() or {}
    symbol   = (data.get('symbol') or '').upper()
    interval = str(data.get('tf', '60'))

    if not re.match(r'^[A-Z0-9]{3,20}$', symbol) or not symbol.endswith('USDT'):
        return jsonify({'error': 'Simbolo non valido'}), 400
    if interval not in {'1', '5', '15', '30', '60', '240', 'D', 'W', 'M'}:
        return jsonify({'error': 'TF non valido'}), 400

    # Il backtest, a differenza del popup grafico interattivo di /api/klines, ha
    # bisogno di storico lungo (anche 1+ anno) — calcoliamo le pagine necessarie
    # in base ai giorni richiesti invece di usare il cap ridotto per TF pensato
    # per i popup. Bybit limita 1000 candele a richiesta: più il TF è fine, più
    # pagine (richieste sequenziali) servono per coprire lo stesso periodo.
    try:
        lookback_days = float(data.get('lookback_days', 365))
    except (TypeError, ValueError):
        lookback_days = 365.0
    lookback_days = max(1.0, min(lookback_days, 1095.0))  # cap 3 anni

    iv_s = TF_SECONDS.get(interval, 3600)
    candles_needed = int(lookback_days * 86400 / iv_s) + 5
    max_pages = max(1, min(100, -(-candles_needed // 1000)))  # ceil, cap 100 pagine (100k candele)

    try:
        candles, _ = _fetch_klines_bybit(symbol, interval, max_pages)
    except Exception as e:
        return jsonify({'error': str(e)}), 502

    params = normalize_params(data)
    params['_interval_seconds'] = TF_SECONDS.get(interval, 3600)
    initial_capital = float(data.get('initial_capital', 1000.0))
    sizing = data.get('sizing') or {'type': 'fixed', 'value': 50.0}

    try:
        result = run_backtest(candles, params, initial_capital, sizing)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # Le candele usate per il backtest vengono restituite (senza volume, non
    # serve per il grafico) così il frontend può disegnare i marker di
    # entrata/uscita senza dover rifare un secondo fetch identico a Bybit.
    result['candles'] = [{'time': c['time'], 'open': c['open'], 'high': c['high'],
                          'low': c['low'], 'close': c['close']} for c in candles]

    return jsonify({'success': True, **result})

# ── END BOT ────────────────────────────────────────────────────────────────────

# ── AUTH ───────────────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')
    next_url = data.get('next', '').strip() or '/'
    if not next_url.startswith('/'):
        next_url = '/'
    auth = _get_auth()
    if username == auth['username'] and _hash_pw(password) == auth['password_hash']:
        session['logged_in'] = True
        session['username'] = auth['username']
        session['role'] = auth.get('role', 'admin')
        session.permanent = True
        return jsonify({'success': True, 'redirect': next_url})
    user = _db_get_user(username)
    if user and _hash_pw(password) == user['password_hash']:
        if not user.get('verified', 0):
            return jsonify({'success': False, 'error': 'Account non verificato. Controlla la tua email.'})
        session['logged_in'] = True
        session['username'] = user['username']
        session['role'] = user.get('role', '')
        session.permanent = True
        return jsonify({'success': True, 'redirect': next_url})
    return jsonify({'success': False, 'error': 'Credenziali non valide'})

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        auth = _get_auth()
        if username == auth['username'] and _hash_pw(password) == auth['password_hash']:
            session['logged_in'] = True
            session['username'] = auth['username']
            session['role'] = auth.get('role', 'admin')
            session.permanent = True
            return redirect(request.args.get('next') or url_for('index'))
        user = _db_get_user(username)
        if user and _hash_pw(password) == user['password_hash']:
            if not user.get('verified', 0):
                return redirect(url_for('login_page', error='Account+non+verificato.+Controlla+la+tua+email'))
            session['logged_in'] = True
            session['username'] = user['username']
            session['role'] = user.get('role', '')
            session.permanent = True
            return redirect(request.args.get('next') or url_for('index'))
        return redirect(url_for('login_page', error='Credenziali+non+valide'))
    if session.get('logged_in'):
        return redirect(url_for('index'))
    return send_file('/usr/share/nginx/html/login.html')


@app.route('/verify/<token>')
def verify_email(token):
    try:
        with sqlite3.connect(USERS_DB) as conn:
            row = conn.execute('SELECT username FROM users WHERE verify_token=?', (token,)).fetchone()
            if not row:
                return '<h2 style=font-family:sans-serif;color:#f87171;text-align:center;margin-top:100px>Link non valido o gia usato.</h2>', 400
            conn.execute('UPDATE users SET verified=1, verify_token=NULL WHERE verify_token=?', (token,))
        username = row[0]
        user = _db_get_user(username)
        session['logged_in'] = True
        session['username'] = username
        session['role'] = user.get('role', '') if user else ''
        session.permanent = True
        return redirect(url_for('index'))
    except Exception as e:
        return 'Errore: ' + str(e), 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/api/recover-password', methods=['POST'])
def recover_password():
    import secrets as _secrets, string, random
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    if not email:
        return jsonify({'success': False, 'error': 'Email mancante'}), 400
    username = None
    new_pw = None
    try:
        with sqlite3.connect(USERS_DB) as conn:
            row = conn.execute('SELECT username FROM users WHERE email=?', (email,)).fetchone()
            if row:
                username = row[0]
                chars = string.ascii_lowercase
                pw_list = [_secrets.choice(chars) for _ in range(3)] + [_secrets.choice(string.digits) for _ in range(3)]
                random.shuffle(pw_list)
                new_pw = ''.join(pw_list)
                conn.execute('UPDATE users SET password_hash=? WHERE email=?', (_hash_pw(new_pw), email))
    except Exception as e:
        logger.error('recover_password db error: ' + str(e))
    if username and new_pw:
        _send_recovery_email(email, username, new_pw)
    return jsonify({'success': True})

@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    email = data.get('email', '').strip()
    password = data.get('password', '')
    confirm = data.get('confirm', '')
    if not username or len(username) < 3:
        return jsonify({'success': False, 'error': 'Username troppo corto (min 3 caratteri)'}), 400
    if not re.match(r'^[a-zA-Z0-9_.\-]+$', username):
        return jsonify({'success': False, 'error': 'Username non valido (solo lettere, numeri, _, -, .)'}), 400
    if not email or '@' not in email or '.' not in email.split('@')[-1]:
        return jsonify({'success': False, 'error': 'Email non valida'}), 400
    if len(password) < 6:
        return jsonify({'success': False, 'error': 'Password min 6 caratteri'}), 400
    if password != confirm:
        return jsonify({'success': False, 'error': 'Le password non coincidono'}), 400
    auth = _get_auth()
    if username.lower() == auth['username'].lower():
        return jsonify({'success': False, 'error': 'Username già in uso'}), 400
    token = _db_create_user(username, email, _hash_pw(password))
    if not token:
        return jsonify({'success': False, 'error': 'Username o email già registrati'}), 400
    _send_verification_email(email, username, token)
    return jsonify({'success': True, 'message': 'Controlla la tua email per attivare il tuo account'})

@app.route('/api/auth/status')
def auth_status():
    logged_in = bool(session.get('logged_in'))
    if logged_in:
        username = session.get('username')
        role = session.get('role')
        if username is None:
            auth = _get_auth()
            username = auth.get('username', 'admin')
            role = auth.get('role', 'admin')
    else:
        username = None; role = None
    return jsonify({'logged_in': logged_in, 'username': username, 'role': role})

@app.route('/api/auth/profile', methods=['GET'])
@login_required
def get_profile():
    if session.get('role') == 'admin':
        auth = _get_auth()
        return jsonify({'username': auth.get('username', 'admin'), 'email': auth.get('email', ''), 'role': 'admin'})
    user = _db_get_user(session.get('username', ''))
    if not user:
        return jsonify({'error': 'Utente non trovato'}), 404
    return jsonify({'username': user['username'], 'email': user['email'], 'role': user['role'] or ''})

@app.route('/api/auth/profile', methods=['POST'])
@login_required
def save_profile():
    data = request.get_json() or {}
    if session.get('role') == 'admin':
        auth = _get_auth()
        if 'email' in data: auth['email'] = data['email'].strip()
        with open(AUTH_FILE, 'w') as f: json.dump(auth, f)
    else:
        username = session.get('username', '')
        if 'email' in data:
            try:
                with sqlite3.connect(USERS_DB) as conn:
                    conn.execute('UPDATE users SET email=? WHERE lower(username)=lower(?)',
                                 (data['email'].strip().lower(), username))
                    conn.commit()
            except Exception:
                return jsonify({'success': False, 'error': 'Errore aggiornamento email'}), 500
    return jsonify({'success': True})

@app.route('/api/auth/change-password', methods=['POST'])
@login_required
def change_password():
    data = request.get_json() or {}
    if session.get('role') == 'admin':
        auth = _get_auth()
        if _hash_pw(data.get('current', '')) != auth['password_hash']:
            return jsonify({'success': False, 'error': 'Password attuale errata'}), 400
        new_pw = data.get('new', '').strip()
        if len(new_pw) < 6:
            return jsonify({'success': False, 'error': 'Password troppo corta (min 6 caratteri)'}), 400
        auth['password_hash'] = _hash_pw(new_pw)
        if data.get('username'):
            auth['username'] = data['username'].strip()
        with open(AUTH_FILE, 'w') as f:
            json.dump(auth, f)
    else:
        username = session.get('username', '')
        user = _db_get_user(username)
        if not user:
            return jsonify({'success': False, 'error': 'Utente non trovato'}), 404
        if _hash_pw(data.get('current', '')) != user['password_hash']:
            return jsonify({'success': False, 'error': 'Password attuale errata'}), 400
        new_pw = data.get('new', '').strip()
        if len(new_pw) < 6:
            return jsonify({'success': False, 'error': 'Password troppo corta (min 6 caratteri)'}), 400
        try:
            with sqlite3.connect(USERS_DB) as conn:
                conn.execute('UPDATE users SET password_hash=? WHERE lower(username)=lower(?)',
                             (_hash_pw(new_pw), username))
                conn.commit()
        except Exception:
            return jsonify({'success': False, 'error': 'Errore aggiornamento password'}), 500
    return jsonify({'success': True})

# ── PAGES ──────────────────────────────────────────────────────────────────────

@app.route('/chart', methods=['GET'])
def chart_page():
    return send_file('/usr/share/nginx/html/chart.html')


@app.route('/mtf', methods=['GET'])
def mtf_page():
    return send_file('/usr/share/nginx/html/mtf.html')


@app.route('/screener', methods=['GET'])
@login_required
def screener_page():
    return send_file('/usr/share/nginx/html/screener.html')


@app.route('/i18n.js')
def serve_i18n():
    return send_file('/usr/share/nginx/html/i18n.js', mimetype='application/javascript')


@app.route('/screenshot', methods=['GET'])
def screenshot_page():
    return send_file('/usr/share/nginx/html/screenshot.html')


@app.route('/dt_chart', methods=['GET'])
def dt_chart_page():
    return send_file('/usr/share/nginx/html/dt_chart.html')


@app.route('/third-touch', methods=['GET'])
@app.route('/third-touch.html', methods=['GET'])
def double_touch_page():
    return send_file('/usr/share/nginx/html/third_touch.html')

@app.route('/double-touch', methods=['GET'])
@app.route('/double-touch.html', methods=['GET'])
def double_touch_redirect():
    return redirect('/third-touch', code=301)

@app.route('/ema60', methods=['GET'])
@app.route('/ema60.html', methods=['GET'])
def ema60_page():
    return send_file('/usr/share/nginx/html/ema60.html')


@app.route('/flip', methods=['GET'])
@app.route('/flip.html', methods=['GET'])
def flip_page():
    return send_file('/usr/share/nginx/html/flip.html')

@app.route('/ico', methods=['GET'])
@app.route('/ico.html', methods=['GET'])
def ico_page():
    return send_file('/usr/share/nginx/html/ico.html')

@app.route('/ath-atl', methods=['GET'])
@app.route('/ath-atl.html', methods=['GET'])
def ath_atl_page():
    return send_file('/usr/share/nginx/html/ath_atl.html')


@app.route('/ema223-60', methods=['GET'])
@app.route('/ema223-60.html', methods=['GET'])
@app.route('/shimano', methods=['GET'])
@app.route('/shimano.html', methods=['GET'])
def shimano_page():
    return send_file('/usr/share/nginx/html/shimano.html')


@app.route('/pattern', methods=['GET'])
@app.route('/pattern.html', methods=['GET'])
def pattern_page():
    return send_file('/usr/share/nginx/html/pattern.html')


@app.route('/bot', methods=['GET'])
@app.route('/bot.html', methods=['GET'])
@login_required
def bot_page():
    if session.get('role') != 'admin':
        return redirect(url_for('index'))
    return send_file('/usr/share/nginx/html/bot.html')


@app.route('/scanner-api/pattern/status', methods=['GET'])
def pattern_status():
    scanner = scanners.get('pattern')
    if not scanner:
        return jsonify({'count': 0, 'alerts': [], 'monitored': 0})
    alerts = scanner.get_today_alerts()
    return jsonify({'count': len(alerts), 'alerts': alerts, 'monitored': scanner.get_monitored_count()})


@app.route('/trade', methods=['GET'])
@app.route('/trade.html', methods=['GET'])
@app.route('/orderbook', methods=['GET'])
@app.route('/orderbook.html', methods=['GET'])
def trade_page():
    return send_file('/usr/share/nginx/html/trade.html')


@app.route('/trade-panel', methods=['GET'])
@app.route('/trade-panel.html', methods=['GET'])
def trade_panel_page():
    return send_file('/usr/share/nginx/html/trade-panel.html')


@app.route('/trade.js', methods=['GET'])
@app.route('/orderbook.js', methods=['GET'])
def trade_js():
    return send_file('/usr/share/nginx/html/trade.js', mimetype='application/javascript')


@app.route('/trade-styles.css', methods=['GET'])
@app.route('/orderbook-styles.css', methods=['GET'])
def trade_css():
    return send_file('/usr/share/nginx/html/trade-styles.css', mimetype='text/css')


@app.route('/favicon.svg', methods=['GET'])
def favicon():
    return send_file('/usr/share/nginx/html/favicon.svg', mimetype='image/svg+xml')


@app.route('/', methods=['GET'])
def index():
    return send_file('/usr/share/nginx/html/index.html')


def _prefetch_klines_30m():
    """Pre-fetch 30m klines for top 200 coins at startup, 5 at a time."""
    import requests as _req, time as _time
    try:
        r = _req.get('https://api.bybit.com/v5/market/tickers',
                     params={'category': 'linear'}, timeout=10)
        symbols = [item['symbol'] for item in r.json().get('result', {}).get('list', [])
                   if item.get('symbol', '').endswith('USDT')][:200]
    except Exception as e:
        logger.error(f'prefetch: failed to get symbols: {e}')
        return
    logger.info(f'Prefetching 30m klines for {len(symbols)} symbols...')
    def fetch_one(sym):
        try:
            r = _req.get('https://api.bybit.com/v5/market/kline',
                params={'category':'linear','symbol':sym,'interval':'30','limit':300},
                timeout=8)
            d = r.json()
            if d.get('retCode') == 0:
                candles = [{'time':int(k[0])//1000,'open':float(k[1]),'high':float(k[2]),
                    'low':float(k[3]),'close':float(k[4]),'volume':float(k[5])}
                    for k in reversed(d['result']['list'])]
                with ws_manager._lock:
                    ws_manager._klines[(sym,'30')] = candles
        except Exception as e:
            logger.warning(f'prefetch kline {sym}: {e}')
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(fetch_one, symbols))
    logger.info('Prefetch 30m klines complete.')


@app.route('/api/debug-klines')
def debug_klines():
    syms = ['TONUSDT','BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT']
    result = {}
    for s in syms:
        klines = ws_manager.get_klines(s, '30')
        result[s] = len(klines)
    result['tickers_count'] = len(ws_manager.get_all_tickers())
    return jsonify(result)


@app.route('/api/admin/unread-mail', methods=['GET'])
@login_required
def admin_unread_mail():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Accesso negato'}), 403
    import imaplib
    try:
        m = imaplib.IMAP4("172.17.0.1", 143)
        m.login("info", "CryptoScanner2026!")
        m.select("INBOX", readonly=True)
        _, msgs = m.search(None, "UNSEEN")
        count = len(msgs[0].split()) if msgs[0] else 0
        m.logout()
    except Exception:
        count = 0
        count = 0
    return jsonify({'unread': count})


@app.route('/api/admin/users/<username>', methods=['DELETE'])
@login_required
def admin_delete_user(username):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Accesso negato'}), 403
    if username == session.get('username'):
        return jsonify({'error': 'Non puoi eliminare il tuo account'}), 400
    try:
        with sqlite3.connect(USERS_DB) as conn:
            row = conn.execute('SELECT role FROM users WHERE username=?', (username,)).fetchone()
            if not row:
                return jsonify({'error': 'Utente non trovato'}), 404
            if row[0] == 'admin':
                return jsonify({'error': 'Non puoi eliminare un admin'}), 400
            conn.execute('DELETE FROM users WHERE username=?', (username,))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/users', methods=['GET'])
@login_required
def admin_get_users():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Accesso negato'}), 403
    try:
        with sqlite3.connect(USERS_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT username, email, role, created_at FROM users ORDER BY created_at DESC'
            ).fetchall()
            return jsonify({'users': [dict(r) for r in rows]})
    except Exception:
        return jsonify({'users': []})

if __name__ == '__main__':
    threading.Thread(target=_prefetch_klines_30m, daemon=True).start()
    logger.info("🚀 Crypto Scanner Professional Starting...")

    # Init users database
    _init_users_db()

    # Load config
    load_config()

    # Start WebSocket manager (real-time ticker feed)
    ws_manager.start()
    logger.info("✅ WebSocket manager started")

    # Initialize scanners (pass ws_manager)
    init_scanners()

    # Start polling threads (fallback / manual scan)
    start_scanners()

    # Start Flask app
    logger.info("✅ Starting Flask on port 8080...")
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
