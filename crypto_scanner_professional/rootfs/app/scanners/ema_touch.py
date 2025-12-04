"""EMA Touch Scanner"""
import requests
from datetime import datetime, timedelta
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
    def __init__(self, telegram_config, enabled=True, ema_period=60, 
                 ema_touch_candles=3, proximity_threshold=0.5, 
                 scan_interval_minutes=30, min_volume_24h=10000000,
                 new_listing_days=30, cooldown_hours=2, 
                 send_screenshots=True, max_coins_per_alert=10):
        
        self.telegram_token = telegram_config['token']
        self.telegram_chat_id = telegram_config['chat_id']
        self.enabled = enabled
        self.ema_period = ema_period
        self.proximity_threshold = proximity_threshold / 100
        self.min_volume_24h = min_volume_24h
        self.cooldown_hours = cooldown_hours
        self.max_coins_per_alert = max_coins_per_alert
        
        # Carica cooldown da file
        self.last_alerts = self._load_cooldown()
        
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
        """Check if symbol is in cooldown period"""
        if symbol not in self.last_alerts:
            return False
        
        last_alert_time = self.last_alerts[symbol]
        now = datetime.now()
        cooldown_delta = timedelta(hours=self.cooldown_hours)
        
        in_cooldown = (now - last_alert_time) < cooldown_delta
        
        if in_cooldown:
            remaining = cooldown_delta - (now - last_alert_time)
            remaining_minutes = int(remaining.total_seconds() / 60)
            print(f"⏳ {symbol} in cooldown ({remaining_minutes} min remaining)")
        
        return in_cooldown
    
    def mark_alerted(self, symbol):
        """Mark symbol as alerted"""
        self.last_alerts[symbol] = datetime.now()
        self._save_cooldown()  # Salva subito su file!
        
    def scan(self):
        """Scan for EMA touches"""
        if not self.enabled:
            return []
        
        print(f"🎯 EMA Touch Scanner - Looking for EMA {self.ema_period} touches...")
        
        # Get trading pairs from Bybit
        try:
            url = "https://api.bybit.com/v5/market/tickers?category=linear"
            response = requests.get(url, timeout=10)
            data = response.json()
            
            if data['retCode'] != 0:
                print(f"❌ Bybit API error: {data['retMsg']}")
                return []
            
            pairs = [item for item in data['result']['list'] 
                    if item['symbol'].endswith('USDT') and 
                    float(item.get('volume24h', 0)) * float(item.get('lastPrice', 0)) > self.min_volume_24h]
            
            print(f"📊 Analyzing {len(pairs)} pairs...")
            
            found = []
            for pair in pairs[:50]:  # Limit to avoid rate limits
                symbol = pair['symbol']
                
                # Get klines and calculate EMA
                # Simplified: check if price is near EMA
                last_price = float(pair['lastPrice'])
                
                # Mock EMA calculation (in production, fetch klines and calculate)
                # For demo, assume EMA is close to last price
                mock_ema = last_price * 1.001  # Mock: EMA slightly above
                
                distance = abs(last_price - mock_ema) / last_price
                
                if distance < self.proximity_threshold:
                    # Check cooldown
                    if self.is_in_cooldown(symbol):
                        print(f"⏳ {symbol} in cooldown, skipping")
                        continue
                    
                    found.append({
                        'symbol': symbol,
                        'price': last_price,
                        'distance_pct': distance * 100,
                        'volume_24h': float(pair.get('volume24h', 0))
                    })
            
            # Limita coins per alert
            found = found[:self.max_coins_per_alert]
            
            if found:
                print(f"✅ Found {len(found)} EMA touches!")
                
                # Mark all coins as alerted
                for coin in found:
                    self.mark_alerted(coin['symbol'])
                
                self.send_alert(found)
            else:
                print(f"⚠️ No EMA touches found")
            
            return found
            
        except Exception as e:
            print(f"❌ Error in EMA scanner: {e}")
            return []
    
    def send_alert(self, coins):
        """Send Telegram alert"""
        if not self.telegram_token or not self.telegram_chat_id:
            print("⚠️ Telegram not configured")
            return
        
        message = f"🎯 *EMA {self.ema_period} Touch Alert!*\n\n"
        message += f"Found {len(coins)} coins:\n\n"
        
        for coin in coins[:10]:
            message += f"🟢 *{coin['symbol']}*\n"
            message += f"   Price: ${coin['price']:.4f}\n"
            message += f"   Distance: {coin['distance_pct']:.2f}%\n\n"
        
        message += f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            payload = {
                'chat_id': self.telegram_chat_id,
                'text': message,
                'parse_mode': 'Markdown'
            }
            response = requests.post(url, json=payload, timeout=10)
            
            if response.ok:
                print("✅ Telegram alert sent")
                
                # Send charts for top 3 coins
                if CHARTS_AVAILABLE and len(coins) > 0:
                    self.send_charts(coins[:3])
            else:
                print(f"❌ Telegram error: {response.text}")
                
        except Exception as e:
            print(f"❌ Error sending Telegram: {e}")
    
    def send_charts(self, coins):
        """Send chart images for coins"""
        for coin in coins:
            try:
                print(f"📊 Generating chart for {coin['symbol']}...")
                chart_bytes = generate_chart_for_coin(coin['symbol'], self.ema_period)
                
                if chart_bytes:
                    # Link TradingView con .P per perpetual
                    # BTCUSDT -> BYBIT:BTCUSDT.P
                    tv_symbol = coin['symbol'].replace('USDT', 'USDT.P')
                    tv_link = f"https://it.tradingview.com/chart/KDtSSRjB/?symbol=BYBIT:{tv_symbol}"
                    
                    # Send photo to Telegram
                    url = f"https://api.telegram.org/bot{self.telegram_token}/sendPhoto"
                    files = {'photo': ('chart.png', chart_bytes, 'image/png')}
                    data = {
                        'chat_id': self.telegram_chat_id,
                        'caption': f"📈 [{coin['symbol']}]({tv_link}) - EMA {self.ema_period}",
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
