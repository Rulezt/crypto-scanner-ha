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

# ========== API ENDPOINTS ==========

@app.route('/scanner-api/health', methods=['GET'])
def health():
    """Health check"""
    telegram_configured = bool(config['telegram']['token'] and config['telegram']['chat_id'])
    
    return jsonify({
        'status': 'ok',
        'version': '2.2.4',
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
    """Get recent alerts from all scanners (simulated for now - in future can use persistent storage)"""
    try:
        from datetime import datetime

        # For now, return mock data - in production this would read from a database/log file
        # This is a placeholder that can be extended to read actual alert history
        recent_alerts = []

        # You can extend this to read from actual scanner logs or database
        # For now, just return empty or sample data structure

        return jsonify({
            'success': True,
            'alerts': recent_alerts,
            'count': len(recent_alerts)
        })

    except Exception as e:
        logger.error(f"❌ Error getting recent alerts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

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
