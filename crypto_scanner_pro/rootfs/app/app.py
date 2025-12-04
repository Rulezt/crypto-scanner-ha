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
        'proximity_threshold': 0.5,
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
        ('volume_scanner', scanners.get('volume'), config['volume_scanner']['scan_interval_minutes'])
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
        'version': '2.1.5',
        'telegram_configured': telegram_configured,
        'telegram_token_set': bool(config['telegram']['token']),
        'telegram_chat_id_set': bool(config['telegram']['chat_id']),
        'scanners': {
            'ema_touch': config['ema_touch']['enabled'],
            'daily_flip': config['daily_flip']['enabled'],
            'volume_scanner': config['volume_scanner']['enabled']
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
