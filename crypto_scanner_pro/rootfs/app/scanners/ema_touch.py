"""EMA Touch Scanner - 30m Timeframe with EMA 60 Focus"""
import requests
from datetime import datetime
import sys
import os
import json

# Add parent directory to path for chart_generator
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from chart_generator import generate_chart_for_coin
    CHARTS_AVAILABLE = True
except ImportError:
    CHARTS_AVAILABLE = False
    print("⚠️ Chart generator not available")

# File per salvare cooldown persistente - Path fallback per massima compatibilità
COOLDOWN_FILE = None  # Will be set in __init__

class EMAScanner:
    def __init__(self, telegram_config, enabled=True, ema_touch_threshold=2.0,
                 scan_interval_minutes=30, min_volume_24h=10000000,
                 max_coins_per_alert=10, **kwargs):
        """
        EMA Touch Scanner - Monitors 30m timeframe for EMA 60 proximity

        Args:
            telegram_config: Dict with 'token' and 'chat_id'
            enabled: Enable/disable scanner
            ema_touch_threshold: Distance threshold in % (default 2.0%)
            min_volume_24h: Minimum 24h volume filter
            max_coins_per_alert: Max coins per alert batch
        """

        self.telegram_token = telegram_config['token']
        self.telegram_chat_id = telegram_config['chat_id']
        self.ha_url = telegram_config.get('ha_url', '')
        self.enabled = enabled
        self.ema_touch_threshold = ema_touch_threshold  # Configurable threshold
        self.min_volume_24h = min_volume_24h
        self.max_coins_per_alert = max_coins_per_alert

        # Find persistent path for cooldown file
        self._setup_cooldown_path()

        # Carica cooldown da file CON LOGGING
        print(f"🔄 Initializing EMA Scanner cooldown system...")
        self.last_alerts = self._load_cooldown()
        print(f"📊 Cooldown state loaded: {len(self.last_alerts)} active cooldowns")

        print(f"🎯 EMA Touch Scanner initialized - Threshold: {self.ema_touch_threshold}%, Timeframe: 30m")

    def _setup_cooldown_path(self):
        """Find first writable path for cooldown persistence"""
        global COOLDOWN_FILE

        # Prova path multipli in ordine di preferenza (HA persistent locations)
        candidate_paths = [
            '/config/ema_cooldown.json',
            '/share/ema_cooldown.json',
            '/data/ema_cooldown.json'
        ]

        print("🔍 Searching for persistent cooldown storage path...")
        for path in candidate_paths:
            try:
                test_dir = os.path.dirname(path)
                # Check if directory exists and is writable
                if os.path.exists(test_dir):
                    # Try to create test file
                    test_file = os.path.join(test_dir, '.test_write')
                    try:
                        with open(test_file, 'w') as f:
                            f.write('test')
                        os.remove(test_file)
                        COOLDOWN_FILE = path
                        print(f"✅ Using cooldown path: {COOLDOWN_FILE}")
                        return
                    except:
                        print(f"   ⚠️ Path {path} exists but not writable")
                        continue
                else:
                    print(f"   ℹ️ Path {test_dir} does not exist")
            except Exception as e:
                print(f"   ⚠️ Error checking {path}: {e}")
                continue

        # Fallback to /data/ if nothing else works
        COOLDOWN_FILE = '/data/ema_cooldown.json'
        print(f"⚠️ Using fallback path: {COOLDOWN_FILE}")

    def _load_cooldown(self):
        """Carica cooldown da file persistente con logging esteso"""
        try:
            if os.path.exists(COOLDOWN_FILE):
                print(f"📂 Loading cooldown from: {COOLDOWN_FILE}")
                with open(COOLDOWN_FILE, 'r') as f:
                    data = json.load(f)
                    # Converti stringhe ISO in datetime
                    cooldown_data = {k: datetime.fromisoformat(v) for k, v in data.items()}
                    print(f"✅ Loaded {len(cooldown_data)} cooldown entries")
                    if cooldown_data:
                        print(f"📋 Cooldown data preview: {dict(list(cooldown_data.items())[:3])}...")
                    return cooldown_data
            else:
                print(f"⚠️ Cooldown file not found at {COOLDOWN_FILE} (first run or file deleted)")
        except Exception as e:
            print(f"❌ Error loading cooldown: {e}")
            import traceback
            traceback.print_exc()
        return {}

    def _save_cooldown(self):
        """Salva cooldown su file persistente con verifica"""
        try:
            # Crea directory se non esiste
            cooldown_dir = os.path.dirname(COOLDOWN_FILE)
            if not os.path.exists(cooldown_dir):
                print(f"📁 Creating directory: {cooldown_dir}")
                os.makedirs(cooldown_dir, exist_ok=True)

            # Converti datetime in stringhe ISO
            data = {k: v.isoformat() for k, v in self.last_alerts.items()}

            print(f"💾 Saving cooldown to: {COOLDOWN_FILE}")
            print(f"📋 Data to save: {len(data)} entries")
            if data:
                print(f"   Sample: {dict(list(data.items())[:2])}...")

            with open(COOLDOWN_FILE, 'w') as f:
                json.dump(data, f, indent=2)

            print(f"✅ Cooldown saved successfully")

            # Verifica che il file sia stato salvato
            if os.path.exists(COOLDOWN_FILE):
                file_size = os.path.getsize(COOLDOWN_FILE)
                print(f"✅ File verified at {COOLDOWN_FILE} ({file_size} bytes)")
            else:
                print(f"❌ WARNING: File not found after save!")

        except Exception as e:
            print(f"❌ Error saving cooldown: {e}")
            import traceback
            traceback.print_exc()

    def is_in_cooldown(self, symbol):
        """Check if symbol already alerted on current daily candle (UTC 00:00 reset)"""
        now = datetime.utcnow()
        current_candle_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        print(f"🔍 Checking cooldown for {symbol}")
        print(f"   Current UTC time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"   Current daily candle start: {current_candle_start.strftime('%Y-%m-%d %H:%M:%S')}")

        if symbol not in self.last_alerts:
            print(f"   ✅ {symbol} - NO previous alert found")
            return False

        last_alert_time = self.last_alerts[symbol]
        last_alert_candle_start = last_alert_time.replace(hour=0, minute=0, second=0, microsecond=0)

        print(f"   Last alert time: {last_alert_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"   Last alert candle start: {last_alert_candle_start.strftime('%Y-%m-%d %H:%M:%S')}")

        # If last alert was on a different daily candle, allow new alert
        if last_alert_candle_start < current_candle_start:
            print(f"   ✅ {symbol} - New daily candle, allowing alert")
            return False

        # Same candle, in cooldown
        hours_since_alert = (now - last_alert_time).total_seconds() / 3600
        print(f"   ❌ {symbol} - Already alerted on current daily candle ({hours_since_alert:.1f}h ago)")
        return True

    def mark_alerted(self, symbol):
        """Mark symbol as alerted with verification"""
        now = datetime.utcnow()
        self.last_alerts[symbol] = now
        print(f"✅ Marking {symbol} as alerted at {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")

        # Save to file
        self._save_cooldown()

        # Ricarica per verificare che il salvataggio sia andato a buon fine
        print(f"🔄 Verifying cooldown persistence for {symbol}...")
        reloaded = self._load_cooldown()
        if symbol in reloaded:
            print(f"   ✅ Cooldown verified for {symbol}")
        else:
            print(f"   ❌ WARNING: {symbol} not found in reloaded cooldown!")

    def fetch_klines_and_calculate_ema(self, symbol, interval='30', limit=250):
        """
        Fetch klines from Bybit and calculate EMA 60

        Returns:
            dict: {
                'current_price': float,
                'ema60': float,
                'distance_pct': float,
                'ema5': float,
                'ema10': float,
                'ema223': float
            } or None if error
        """
        try:
            url = 'https://api.bybit.com/v5/market/kline'
            params = {
                'category': 'linear',
                'symbol': symbol,
                'interval': interval,  # 30m timeframe
                'limit': limit
            }

            response = requests.get(url, params=params, timeout=10)
            data = response.json()

            if data['retCode'] != 0:
                return None

            # Converti klines in lista di prezzi di chiusura
            klines = data['result']['list']
            if len(klines) < 223:  # Need at least 223 candles for EMA 223
                return None

            # Ordina per timestamp crescente
            klines.sort(key=lambda x: int(x[0]))

            # Estrai prezzi di chiusura
            closes = [float(k[4]) for k in klines]

            # Calcola EMA usando formula: EMA = (Close - EMA_prev) * multiplier + EMA_prev
            # Multiplier = 2 / (period + 1)

            def calculate_ema(prices, period):
                """Calculate EMA for given period"""
                if len(prices) < period:
                    return None

                multiplier = 2 / (period + 1)
                # SMA come primo valore
                ema = sum(prices[:period]) / period

                # Calcola EMA per tutti i valori successivi
                for price in prices[period:]:
                    ema = (price - ema) * multiplier + ema

                return ema

            # Calcola tutte le 4 EMA
            ema5 = calculate_ema(closes, 5)
            ema10 = calculate_ema(closes, 10)
            ema60 = calculate_ema(closes, 60)
            ema223 = calculate_ema(closes, 223)

            if ema60 is None:
                return None

            # Prezzo corrente (ultima chiusura)
            current_price = closes[-1]

            # Calcola distanza percentuale dall'EMA 60
            distance_pct = abs((current_price - ema60) / ema60 * 100)

            return {
                'current_price': current_price,
                'ema60': ema60,
                'ema5': ema5,
                'ema10': ema10,
                'ema223': ema223,
                'distance_pct': distance_pct
            }

        except Exception as e:
            print(f"❌ Error fetching klines for {symbol}: {e}")
            return None

    def scan(self):
        """Scan for EMA 60 touches on 30m timeframe - Top 10 Gainers + Top 10 Losers"""
        if not self.enabled:
            return []

        print(f"🎯 EMA Touch Scanner - Looking for EMA 60 proximity (Threshold: {self.ema_touch_threshold}%, Timeframe: 30m)...")

        # Get trading pairs from Bybit
        try:
            url = "https://api.bybit.com/v5/market/tickers?category=linear"
            response = requests.get(url, timeout=10)
            data = response.json()

            if data['retCode'] != 0:
                print(f"❌ Bybit API error: {data['retMsg']}")
                return []

            # Filter pairs by volume and calculate 24h change %
            all_pairs = []
            for item in data['result']['list']:
                if not item['symbol'].endswith('USDT'):
                    continue

                volume_24h_usd = float(item.get('volume24h', 0)) * float(item.get('lastPrice', 0))
                if volume_24h_usd < self.min_volume_24h:
                    continue

                # Get 24h price change %
                change_pct = float(item.get('price24hPcnt', 0)) * 100

                all_pairs.append({
                    'item': item,
                    'change_pct': change_pct
                })

            print(f"📊 Found {len(all_pairs)} pairs with sufficient volume")

            # Sort by change % to get gainers and losers
            all_pairs.sort(key=lambda x: x['change_pct'], reverse=True)

            # Get top 10 gainers (highest positive change)
            top_10_gainers = all_pairs[:10]

            # Get top 10 losers (lowest negative change)
            top_10_losers = all_pairs[-10:] if len(all_pairs) >= 10 else []

            # Combine: analyze top 10 gainers + top 10 losers (20 total)
            pairs_to_analyze = [p['item'] for p in top_10_gainers] + [p['item'] for p in top_10_losers]

            print(f"🎯 Analyzing top 10 Gainers + top 10 Losers (20 total)...")
            print(f"   Top Gainer: {top_10_gainers[0]['item']['symbol']} (+{top_10_gainers[0]['change_pct']:.2f}%)")
            if top_10_losers:
                print(f"   Top Loser: {top_10_losers[-1]['item']['symbol']} ({top_10_losers[-1]['change_pct']:.2f}%)")

            found = []
            analyzed = 0

            for pair in pairs_to_analyze:
                symbol = pair['symbol']
                analyzed += 1

                if analyzed % 5 == 0:
                    print(f"   Progress: {analyzed}/20 pairs analyzed...")

                # Fetch klines and calculate EMA
                ema_data = self.fetch_klines_and_calculate_ema(symbol, interval='30', limit=250)

                if not ema_data:
                    continue

                # Check if distance is within threshold
                if ema_data['distance_pct'] < self.ema_touch_threshold:
                    # Check if first touch of the day
                    if self.is_in_cooldown(symbol):
                        continue

                    # Determine approach direction
                    approach_dir = "from above" if ema_data['current_price'] > ema_data['ema60'] else "from below"

                    found.append({
                        'symbol': symbol,
                        'price': ema_data['current_price'],
                        'ema60': ema_data['ema60'],
                        'distance_pct': ema_data['distance_pct'],
                        'approach': approach_dir,
                        'volume_24h': float(pair.get('volume24h', 0))
                    })

                    print(f"   ✅ {symbol}: {ema_data['distance_pct']:.2f}% from EMA60 ({approach_dir})")

            # Limita coins per alert
            found = found[:self.max_coins_per_alert]

            if found:
                print(f"🎯 Found {len(found)} EMA 60 touches!")

                # Mark all coins as alerted
                for coin in found:
                    self.mark_alerted(coin['symbol'])

                self.send_alert(found)
            else:
                print(f"⚠️ No EMA 60 touches found within {self.ema_touch_threshold}% threshold")

            return found

        except Exception as e:
            print(f"❌ Error in EMA scanner: {e}")
            import traceback
            traceback.print_exc()
            return []

    def send_alert(self, coins):
        """Send Telegram alert: one photo per coin (max 3) with clean caption."""
        if not self.telegram_token or not self.telegram_chat_id:
            print("Telegram not configured")
            return

        try:
            from alert_utils import send_photo, send_text, get_chart, mtf_link
        except ImportError as e:
            print(f"Cannot import alert_utils: {e}")
            return

        for coin in coins[:3]:
            sym     = coin['symbol']
            dir_str = 'da sotto' if 'below' in coin['approach'] else 'da sopra'
            caption = (
                f"{mtf_link(sym, self.ha_url)}  EMA60 Touch · 30m\n"
                f"distanza: {coin['distance_pct']:.2f}% {dir_str}"
            )
            img = get_chart(sym, interval='30', signal={'type': 'ema'})
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
                print(f"Alert foto inviato: {sym}")
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
                print(f"Alert testo inviato: {sym}")
