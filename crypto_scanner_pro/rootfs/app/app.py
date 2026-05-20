"""
Crypto Scanner Professional - All-in-One
Flask API + Scanners integrati + Dashboard
"""
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from datetime import datetime, timedelta
import os
import json
import threading
import time
import logging
import uuid
from scanners.ema_touch import EMAScanner
from scanners.ath_atl_scanner import ATHATLScanner
from scanners.ico_levels_scanner import ICOLevelsScanner
from scanners.double_touch import DoubleTouchScanner
from ws_manager import BybitWSManager

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Config storage file
CONFIG_FILE = '/data/scanner_config.json'

# Default config
DEFAULT_CONFIG = {
    'telegram': {
        'token': os.getenv('TELEGRAM_TOKEN', ''),
        'chat_id': os.getenv('TELEGRAM_CHAT_ID', ''),
        'enabled': True,
        'ha_url': '',
    },
    'ema_touch': {
        'enabled': True,
        'ema_period': 60,
        'ema_touch_candles': 3,
        'ema_touch_threshold': 2.0,
        'scan_interval_minutes': 30,
        'screenshot_tf': '30',
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
        'scan_tf': 'D',
        'scan_interval_minutes': 240,
        'cooldown_hours': 12,
        'screenshot_tf': 'D',
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
        
        # Override Telegram config from add-on options (if set)
        addon_options_file = '/data/options.json'
        if os.path.exists(addon_options_file):
            try:
                with open(addon_options_file, 'r') as f:
                    addon_options = json.load(f)
                    
                    # Get Telegram config from add-on
                    telegram_token = os.getenv('TELEGRAM_TOKEN') or addon_options.get('telegram_token', '')
                    telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID') or addon_options.get('telegram_chat_id', '')
                    
                    if telegram_token:
                        config['telegram']['token'] = telegram_token
                        logger.info("✅ Telegram token loaded from add-on config")

                    if telegram_chat_id:
                        config['telegram']['chat_id'] = telegram_chat_id
                        logger.info("✅ Telegram chat_id loaded from add-on config")

                    ha_url = addon_options.get('ha_url', '')
                    if ha_url:
                        config['telegram']['ha_url'] = ha_url

            except Exception as e:
                logger.warning(f"⚠️ Could not load add-on options: {e}")
                
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
        'ha_url': config['telegram'].get('ha_url', ''),
    }
    
    # Check if Telegram is configured
    if not telegram_config['token'] or not telegram_config['chat_id']:
        logger.warning("⚠️ Telegram NOT configured!")
        logger.warning("Configure in:")
        logger.warning("  1. Add-on Configuration tab (telegram_token, telegram_chat_id)")
        logger.warning("  2. OR Dashboard → Telegram tab → Save")
    else:
        logger.info(f"✅ Telegram configured: {telegram_config['chat_id'][:8]}...")
    
    try:
        scanners['ema'] = EMAScanner(
            telegram_config=telegram_config,
            ws_manager=ws_manager,
            live_config=config,
            **config['ema_touch'],
            **config['general']
        )

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
            **config['double_touch'],
            **{k: v for k, v in config['general'].items() if k in ('min_volume_24h', 'max_coins_per_alert')}
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
        ('ema_touch',      'ema',        config['ema_touch']['scan_interval_minutes']),
        ('ath_atl',        'ath_atl',    config['ath_atl']['scan_interval_minutes']),
        ('ico_levels',     'ico_levels',    config['ico_levels']['scan_interval_minutes']),
        ('double_touch',   'double_touch',  config['double_touch']['scan_interval_minutes']),
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
        'version': '3.8.63',
        'telegram_configured': telegram_configured,
        'telegram_token_set': bool(config['telegram']['token']),
        'telegram_chat_id_set': bool(config['telegram']['chat_id']),
        'ws_connected': ws_manager.ready.is_set(),
        'ws_tickers': len(ws_manager.get_all_tickers()),
        'scanners': {
            'ema_touch': config['ema_touch']['enabled'],
            'ath_atl': config['ath_atl']['enabled'],
            'ico_levels': config['ico_levels']['enabled'],
            'double_touch': config['double_touch']['enabled'],
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

        # Filter and sort pairs
        all_pairs = []
        min_volume = config['general']['min_volume_24h']

        for item in data['result']['list']:
            if not item['symbol'].endswith('USDT'):
                continue

            last_price = float(item['lastPrice'])
            change_pct = float(item.get('price24hPcnt', 0)) * 100
            volume_24h_usd = float(item.get('volume24h', 0)) * last_price

            if volume_24h_usd < min_volume:
                continue

            all_pairs.append({
                'symbol': item['symbol'],
                'price': last_price,
                'change_24h': change_pct,
                'volume_24h': volume_24h_usd
            })

        # Sort by change %
        all_pairs.sort(key=lambda x: x['change_24h'], reverse=True)

        # Get top 20 gainers and losers
        top_gainers = all_pairs[:20]
        top_losers = all_pairs[-20:] if len(all_pairs) >= 20 else []
        top_losers.reverse()  # Most negative first

        # Combine for monitoring (what scanner analyzes)
        monitored_coins = top_gainers + top_losers

        return jsonify({
            'success': True,
            'config': config.get('ath_atl', {}),
            'monitored_coins': monitored_coins,
            'top_gainers': top_gainers[:20],
            'top_losers': top_losers[:20],
            'total_pairs': len(all_pairs)
        })

    except Exception as e:
        logger.error(f"❌ Error getting ATH/ATL status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/scanner-api/alerts/recent', methods=['GET'])
def get_recent_alerts():
    """Get recent alerts by reading cooldown files from all scanners"""
    try:
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(hours=24)

        # EMA scanner may write to /config, /share, or /data
        ema_candidates = ['/config/ema_cooldown.json', '/share/ema_cooldown.json', '/data/ema_cooldown.json']
        ema_path = next((p for p in ema_candidates if os.path.exists(p)), None)

        # (filepath, label, emoji, key_transform)
        # key_transform: 'plain' = key is symbol, 'strip_suffix' = key is SYMBOL_TYPE
        sources = [
            (ema_path,                            'EMA Touch',    '🎯', 'plain'),
            ('/data/gainers_cooldown.json',       'Gainer',       '📈', 'plain'),
            ('/data/losers_cooldown.json',        'Loser',        '📉', 'plain'),
            ('/data/ath_cooldown.json',           'ATH',          '🏆', 'plain'),
            ('/data/atl_cooldown.json',           'ATL',          '⬇️', 'plain'),
            ('/data/double_touch_cooldown.json',  'Terzo Tocco',  '🔁', 'strip_suffix'),
        ]

        recent_alerts = []

        # Standard cooldown files: {symbol: isoformat_timestamp}
        for filepath, label, emoji, key_mode in sources:
            if not filepath or not os.path.exists(filepath):
                continue
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                for key, ts_str in data.items():
                    if not isinstance(ts_str, str):
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except Exception:
                        continue
                    if ts >= cutoff:
                        symbol = key.rsplit('_', 1)[0] if key_mode == 'strip_suffix' else key
                        recent_alerts.append({
                            'symbol':    symbol,
                            'type':      label,
                            'emoji':     emoji,
                            'time':      ts.strftime('%Y-%m-%dT%H:%M:%SZ'),
                            'timestamp': ts.timestamp(),
                        })
            except Exception:
                continue

        # ICO scanner: /data/ico_levels_state.json has {discarded:[...], alerted:{SYMBOL_TF: ts}}
        ico_state_path = '/data/ico_levels_state.json'
        if os.path.exists(ico_state_path):
            try:
                with open(ico_state_path, 'r') as f:
                    ico_state = json.load(f)
                for key, ts_str in ico_state.get('alerted', {}).items():
                    if not isinstance(ts_str, str):
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except Exception:
                        continue
                    if ts >= cutoff:
                        symbol = key.rsplit('_', 1)[0]  # strip _TF suffix
                        recent_alerts.append({
                            'symbol':    symbol,
                            'type':      'ICO Level',
                            'emoji':     '🚀',
                            'time':      ts.strftime('%Y-%m-%dT%H:%M:%SZ'),
                            'timestamp': ts.timestamp(),
                        })
            except Exception:
                pass

        recent_alerts.sort(key=lambda x: x['timestamp'], reverse=True)
        return jsonify({'success': True, 'alerts': recent_alerts[:50], 'count': len(recent_alerts)})

    except Exception as e:
        logger.error(f"❌ Error getting recent alerts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/top-coins', methods=['GET'])
def get_top_coins():
    """Return top N coins sorted by 24h change % (gainers or losers), filtered by min volume"""
    import requests as req
    try:
        limit = min(int(request.args.get('limit', 18)), 50)
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

        descending = (sort != 'losers')
        coins.sort(key=lambda x: x['change_24h'], reverse=descending)
        return jsonify({'success': True, 'data': coins[:limit]})
    except Exception as e:
        logger.error(f"Error fetching top coins: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/new-listings', methods=['GET'])
def get_new_listings():
    """Return recently listed USDT perpetuals on Bybit, sorted by listing date desc"""
    import requests as req
    try:
        days  = int(request.args.get('days', 90))
        limit = min(int(request.args.get('limit', 18)), 50)
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
                    ha_url    = config['telegram'].get('ha_url', '').rstrip('/')
                    link      = f'<a href="{ha_url}/mtf?symbol={sym}">{coin}</a>' if ha_url else coin
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

@app.route('/api/klines', methods=['GET'])
def get_klines():
    """Proxy Bybit klines for the chart page"""
    import requests as req
    import re

    symbol = request.args.get('symbol', 'BTCUSDT').upper()
    interval = request.args.get('interval', '15')

    if not re.match(r'^[A-Z0-9]{3,20}$', symbol) or not symbol.endswith('USDT'):
        return jsonify({'error': 'Invalid symbol'}), 400

    if interval not in {'1', '5', '15', '30', '60', '240', 'D'}:
        return jsonify({'error': 'Invalid interval'}), 400

    try:
        url = 'https://api.bybit.com/v5/market/kline'
        params = {'category': 'linear', 'symbol': symbol, 'interval': interval, 'limit': 500}
        response = req.get(url, params=params, timeout=10)
        data = response.json()

        if data.get('retCode') != 0:
            return jsonify({'error': 'Bybit API error', 'detail': data.get('retMsg')}), 502

        # Bybit returns newest first — reverse to chronological order
        klines = list(reversed(data['result']['list']))
        utc_off = config.get('general', {}).get('utc_offset', 2)
        try:
            utc_off = float(utc_off)
        except (TypeError, ValueError):
            utc_off = 2
        tz_s = int(utc_off * 3600)
        result = [{
            'time':   int(k[0]) // 1000 + tz_s,
            'open':   float(k[1]),
            'high':   float(k[2]),
            'low':    float(k[3]),
            'close':  float(k[4]),
            'volume': float(k[5]),
        } for k in klines]

        return jsonify({'success': True, 'data': result, 'symbol': symbol,
                        'interval': interval, 'utc_offset_s': tz_s})

    except Exception as e:
        logger.error(f"Error fetching klines for {symbol}: {e}")
        return jsonify({'error': str(e)}), 500


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


@app.route('/chart', methods=['GET'])
def chart_page():
    """Serve realtime chart page"""
    return send_file('/usr/share/nginx/html/chart.html')


@app.route('/mtf', methods=['GET'])
def mtf_page():
    """Serve multi-timeframe chart page."""
    return send_file('/usr/share/nginx/html/mtf.html')


@app.route('/screener', methods=['GET'])
def screener_page():
    return send_file('/usr/share/nginx/html/screener.html')


@app.route('/screenshot', methods=['GET'])
def screenshot_page():
    """Serve single-chart screenshot page (used by Selenium for alert images)."""
    return send_file('/usr/share/nginx/html/screenshot.html')


@app.route('/double-touch', methods=['GET'])
@app.route('/double-touch.html', methods=['GET'])
def double_touch_page():
    """Serve Terzo Tocco scanner page."""
    return send_file('/usr/share/nginx/html/double_touch.html')


@app.route('/orderbook', methods=['GET'])
@app.route('/orderbook.html', methods=['GET'])
def orderbook_page():
    """Serve order book page."""
    return send_file('/usr/share/nginx/html/orderbook.html')


@app.route('/orderbook.js', methods=['GET'])
def orderbook_js():
    return send_file('/usr/share/nginx/html/orderbook.js', mimetype='application/javascript')


@app.route('/orderbook-styles.css', methods=['GET'])
def orderbook_css():
    return send_file('/usr/share/nginx/html/orderbook-styles.css', mimetype='text/css')


@app.route('/favicon.svg', methods=['GET'])
def favicon():
    return send_file('/usr/share/nginx/html/favicon.svg', mimetype='image/svg+xml')


@app.route('/', methods=['GET'])
def index():
    """Serve dashboard"""
    return send_file('/usr/share/nginx/html/index.html')

if __name__ == '__main__':
    logger.info("🚀 Crypto Scanner Professional Starting...")

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
