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

# File per salvare cooldown persistente
COOLDOWN_FILE = '/data/ema_cooldown.json'

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
        self.enabled = enabled
        self.ema_touch_threshold = ema_touch_threshold  # Configurable threshold
        self.min_volume_24h = min_volume_24h
        self.max_coins_per_alert = max_coins_per_alert

        # Carica cooldown da file
        self.last_alerts = self._load_cooldown()

        print(f"🎯 EMA Touch Scanner initialized - Threshold: {self.ema_touch_threshold}%, Timeframe: 30m")

    def _load_cooldown(self):
        """Carica cooldown da file persistente"""
        try:
            if os.path.exists(COOLDOWN_FILE):
                with open(COOLDOWN_FILE, 'r') as f:
                    data = json.load(f)
                    # Converti stringhe ISO in datetime
                    return {k: datetime.fromisoformat(v) for k, v in data.items()}
        except Exception as e:
            print(f"⚠️ Error loading cooldown: {e}")
        return {}

    def _save_cooldown(self):
        """Salva cooldown su file persistente"""
        try:
            # Converti datetime in stringhe ISO
            data = {k: v.isoformat() for k, v in self.last_alerts.items()}
            with open(COOLDOWN_FILE, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            print(f"⚠️ Error saving cooldown: {e}")

    def is_in_cooldown(self, symbol):
        """Check if symbol already alerted on current daily candle (UTC 00:00 reset)"""
        if symbol not in self.last_alerts:
            return False

        last_alert_time = self.last_alerts[symbol]
        now = datetime.utcnow()

        # Get current daily candle start (UTC 00:00)
        current_candle_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        last_alert_candle_start = last_alert_time.replace(hour=0, minute=0, second=0, microsecond=0)

        # If last alert was on a different daily candle, allow new alert
        if last_alert_candle_start < current_candle_start:
            print(f"✅ {symbol} - new daily candle, allowing alert")
            return False

        # Same candle, in cooldown
        print(f"⏳ {symbol} already alerted on current daily candle (UTC)")
        return True

    def mark_alerted(self, symbol):
        """Mark symbol as alerted"""
        self.last_alerts[symbol] = datetime.utcnow()
        self._save_cooldown()

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
        """Scan for EMA 60 touches on 30m timeframe"""
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

            # Filter pairs by volume
            pairs = [item for item in data['result']['list']
                    if item['symbol'].endswith('USDT') and
                    float(item.get('volume24h', 0)) * float(item.get('lastPrice', 0)) > self.min_volume_24h]

            print(f"📊 Analyzing {len(pairs)} pairs with sufficient volume...")

            found = []
            analyzed = 0

            for pair in pairs[:50]:  # Limit to avoid rate limits
                symbol = pair['symbol']
                analyzed += 1

                if analyzed % 10 == 0:
                    print(f"   Progress: {analyzed}/50 pairs analyzed...")

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
        """Send Telegram alert - charts only"""
        if not self.telegram_token or not self.telegram_chat_id:
            print("⚠️ Telegram not configured")
            return

        # Send only charts for top 3 coins
        if CHARTS_AVAILABLE and len(coins) > 0:
            print(f"📊 Sending chart images for {len(coins[:3])} coins (text alerts disabled)")
            self.send_charts(coins[:3])

    def send_charts(self, coins):
        """Send chart images for coins"""
        for coin in coins:
            try:
                print(f"📊 Generating 30m chart for {coin['symbol']}...")
                chart_bytes = generate_chart_for_coin(coin['symbol'], ema_period=60)

                if chart_bytes:
                    # Link TradingView con .P per perpetual
                    tv_symbol = coin['symbol'].replace('USDT', 'USDT.P')
                    tv_link = f"https://it.tradingview.com/chart/KDtSSRjB/?symbol=BYBIT:{tv_symbol}"

                    # Caption con distanza e approccio
                    caption = f"📈 [{coin['symbol']}]({tv_link}) - EMA 60\n"
                    caption += f"Distance: {coin['distance_pct']:.2f}% ({coin['approach']})"

                    # Send photo to Telegram
                    url = f"https://api.telegram.org/bot{self.telegram_token}/sendPhoto"
                    files = {'photo': ('chart.png', chart_bytes, 'image/png')}
                    data = {
                        'chat_id': self.telegram_chat_id,
                        'caption': caption,
                        'parse_mode': 'Markdown'
                    }

                    response = requests.post(url, files=files, data=data, timeout=30)

                    if response.ok:
                        print(f"✅ Chart sent for {coin['symbol']}")
                    else:
                        print(f"❌ Failed to send chart: {response.text}")
                else:
                    print(f"⚠️ No chart generated for {coin['symbol']}")

            except Exception as e:
                print(f"❌ Error sending chart for {coin['symbol']}: {e}")
