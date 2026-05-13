"""
Crypto Scanner Professional - All-in-One
Flask API + Scanners integrati + Dashboard
"""
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import os
import json
import threading
import time
import logging
import uuid
from scanners.ema_touch import EMAScanner
from scanners.daily_flip import DailyFlipScanner
from scanners.volume import VolumeScanner
from scanners.ath_atl_scanner import ATHATLScanner

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
        'enabled': True
    },
    'ema_touch': {
        'enabled': True,
        'ema_period': 60,
        'ema_touch_candles': 3,
        'ema_touch_threshold': 2.0,
        'scan_interval_minutes': 30
    },
    'daily_flip': {
        'enabled': True,
        'flip_threshold': 2.0,
        'flip_type': 'both',
        'scan_interval_minutes': 30,
        'max_coins': 20
    },
    'volume_scanner': {
        'enabled': True,
        'volume_spike_threshold': 200,
        'gainers_enabled': True,
        'gainers_threshold': 10,
        'losers_enabled': True,
        'losers_threshold': 10,
        'scan_interval_minutes': 30
    },
    'ath_atl': {
        'enabled': True,
        'ath_enabled': True,
        'atl_enabled': True,
        'proximity_threshold': 1.0,
        'lookback_days': 365,
        'scan_interval_minutes': 60
    },
    'general': {
        'min_volume_24h': 10000000,
        'new_listing_days': 30,
        'cooldown_hours': 2,
        'send_screenshots': True,
        'max_coins_per_alert': 10
    }
}

# Global config
config = DEFAULT_CONFIG.copy()
scanners = {}
scanner_threads = {}

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

                    # Get EMA Touch threshold from add-on (if set)
                    ema_touch_threshold = addon_options.get('ema_touch_threshold')
                    if ema_touch_threshold is not None:
                        config['ema_touch']['ema_touch_threshold'] = float(ema_touch_threshold)
                        logger.info(f"✅ EMA touch threshold loaded from add-on config: {ema_touch_threshold}%")

                    # Get ATH/ATL threshold from add-on (if set)
                    ath_atl_threshold = addon_options.get('ath_atl_threshold')
                    if ath_atl_threshold is not None:
                        config['ath_atl']['proximity_threshold'] = float(ath_atl_threshold)
                        logger.info(f"✅ ATH/ATL threshold loaded from add-on config: {ath_atl_threshold}%")

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
    
    telegram_config = {
        'token': config['telegram']['token'],
        'chat_id': config['telegram']['chat_id']
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
            **config['ema_touch'],
            **config['general']
        )
        
        scanners['flip'] = DailyFlipScanner(
            telegram_config=telegram_config,
            **config['daily_flip'],
            **config['general']
        )
        
        scanners['volume'] = VolumeScanner(
            telegram_config=telegram_config,
            **config['volume_scanner'],
            **config['general']
        )

        scanners['ath_atl'] = ATHATLScanner(
            telegram_config=telegram_config,
            **config['ath_atl'],
            **config['general']
        )

        logger.info("✅ Scanners initialized")
    except Exception as e:
        logger.error(f"❌ Error initializing scanners: {e}")

def run_scanner(name, scanner, interval_minutes):
    """Run scanner in loop"""
    while True:
        try:
            if config.get(name, {}).get('enabled', True):
                logger.info(f"🔄 Running {name} scanner...")
                scanner.scan()
        except Exception as e:
            logger.error(f"❌ Error in {name} scanner: {e}")
        
        time.sleep(interval_minutes * 60)

def start_scanners():
    """Start all scanner threads"""
    global scanner_threads
    
    threads_config = [
        ('ema_touch', scanners.get('ema'), config['ema_touch']['scan_interval_minutes']),
        ('daily_flip', scanners.get('flip'), config['daily_flip']['scan_interval_minutes']),
        ('volume_scanner', scanners.get('volume'), config['volume_scanner']['scan_interval_minutes']),
        ('ath_atl', scanners.get('ath_atl'), config['ath_atl']['scan_interval_minutes'])
    ]
    
    for name, scanner, interval in threads_config:
        if scanner and config[name]['enabled']:
            thread = threading.Thread(
                target=run_scanner,
                args=(name, scanner, interval),
                daemon=True
            )
            thread.start()
            scanner_threads[name] = thread
            logger.info(f"✅ {name} thread started")

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
        'version': '2.9.4',
        'telegram_configured': telegram_configured,
        'telegram_token_set': bool(config['telegram']['token']),
        'telegram_chat_id_set': bool(config['telegram']['chat_id']),
        'scanners': {
            'ema_touch': config['ema_touch']['enabled'],
            'daily_flip': config['daily_flip']['enabled'],
            'volume_scanner': config['volume_scanner']['enabled'],
            'ath_atl': config['ath_atl']['enabled']
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
            'top_gainers': top_gainers[:10],
            'top_losers': top_losers[:10],
            'total_pairs': len(all_pairs)
        })

    except Exception as e:
        logger.error(f"❌ Error getting ATH/ATL status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/scanner-api/alerts/recent', methods=['GET'])
def get_recent_alerts():
    """Get recent alerts from all scanners"""
    try:
        from datetime import datetime

        recent_alerts = []

        return jsonify({
            'success': True,
            'alerts': recent_alerts,
            'count': len(recent_alerts)
        })

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
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
            timeout=10
        )
    except Exception as e:
        logger.error(f"Telegram send error: {e}")

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
                    coin = sym.replace('USDT', '')
                    direction = '📈' if alert['condition'] == 'above' else '📉'
                    msg = (f"{direction} *Alert Prezzo Raggiunto*\n"
                           f"*{coin}/USDT*\n"
                           f"Prezzo attuale: `{cur_price}`\n"
                           f"Target: `{alert['price']}` "
                           f"({'sopra' if alert['condition'] == 'above' else 'sotto'})")
                    if alert.get('notify', 'both') != 'browser':
                        send_telegram(msg)
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
        result = [{
            'time':   int(k[0]) // 1000,
            'open':   float(k[1]),
            'high':   float(k[2]),
            'low':    float(k[3]),
            'close':  float(k[4]),
            'volume': float(k[5]),
        } for k in klines]

        return jsonify({'success': True, 'data': result, 'symbol': symbol, 'interval': interval})

    except Exception as e:
        logger.error(f"Error fetching klines for {symbol}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/chart', methods=['GET'])
def chart_page():
    """Serve realtime chart page"""
    return send_file('/usr/share/nginx/html/chart.html')


@app.route('/', methods=['GET'])
def index():
    """Serve dashboard"""
    return send_file('/usr/share/nginx/html/index.html')

if __name__ == '__main__':
    logger.info("🚀 Crypto Scanner Professional Starting...")
    
    # Load config
    load_config()
    
    # Initialize scanners
    init_scanners()
    
    # Start scanner threads
    start_scanners()
    
    # Start Flask app
    logger.info("✅ Starting Flask on port 8080...")
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
