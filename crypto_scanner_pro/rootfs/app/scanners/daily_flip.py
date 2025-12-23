"""Daily Flip Scanner"""
import requests
from datetime import datetime, timedelta
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from chart_generator import generate_chart_for_coin
    CHARTS_AVAILABLE = True
except ImportError:
    CHARTS_AVAILABLE = False

# File per cooldown persistente
COOLDOWN_FILE = '/data/flip_cooldown.json'

class DailyFlipScanner:
    def __init__(self, telegram_config, enabled=True, flip_threshold=2.0,
                 flip_type='both', scan_interval_minutes=30, max_coins=20,
                 min_volume_24h=10000000, cooldown_hours=2, **kwargs):
        
        self.telegram_token = telegram_config['token']
        self.telegram_chat_id = telegram_config['chat_id']
        self.enabled = enabled
        self.flip_threshold = flip_threshold / 100
        self.flip_type = flip_type
        self.max_coins = max_coins
        self.min_volume_24h = min_volume_24h
        self.cooldown_hours = cooldown_hours
        
        # Carica cooldown da file
        self.last_alerts = self._load_cooldown()
    
    def _load_cooldown(self):
        """Carica cooldown da file"""
        try:
            if os.path.exists(COOLDOWN_FILE):
                with open(COOLDOWN_FILE, 'r') as f:
                    data = json.load(f)
                    return {k: datetime.fromisoformat(v) for k, v in data.items()}
        except Exception as e:
            print(f"⚠️ Error loading flip cooldown: {e}")
        return {}
    
    def _save_cooldown(self):
        """Salva cooldown su file"""
        try:
            data = {k: v.isoformat() for k, v in self.last_alerts.items()}
            with open(COOLDOWN_FILE, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            print(f"⚠️ Error saving flip cooldown: {e}")
    
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
        self._save_cooldown()
        
    def scan(self):
        """Scan for daily flips - Top 20 Gainers + Top 20 Losers"""
        if not self.enabled:
            return []

        print(f"🔄 Daily Flip Scanner - Looking for flips within {self.flip_threshold*100}%...")

        try:
            url = "https://api.bybit.com/v5/market/tickers?category=linear"
            response = requests.get(url, timeout=10)
            data = response.json()

            if data['retCode'] != 0:
                return []

            # Filter pairs by volume and calculate 24h change %
            all_pairs = []
            for item in data['result']['list']:
                if not item['symbol'].endswith('USDT'):
                    continue

                volume_24h_usd = float(item.get('volume24h', 0)) * float(item.get('lastPrice', 0))
                if volume_24h_usd < self.min_volume_24h:
                    continue

                last_price = float(item['lastPrice'])
                open_price = float(item.get('prevPrice24h', last_price))
                change_pct = ((last_price - open_price) / open_price) * 100

                all_pairs.append({
                    'item': item,
                    'change_pct': change_pct,
                    'last_price': last_price
                })

            print(f"📊 Found {len(all_pairs)} pairs with sufficient volume")

            # Sort by change % to get gainers and losers
            all_pairs.sort(key=lambda x: x['change_pct'], reverse=True)

            # Get top 20 gainers and top 20 losers
            top_20_gainers = all_pairs[:20]
            top_20_losers = all_pairs[-20:] if len(all_pairs) >= 20 else []

            # Combine: analyze top 20 gainers + top 20 losers (40 total max)
            pairs_to_analyze = top_20_gainers + top_20_losers

            print(f"🎯 Analyzing top 20 Gainers + top 20 Losers ({len(pairs_to_analyze)} total)...")
            if top_20_gainers:
                print(f"   Top Gainer: {top_20_gainers[0]['item']['symbol']} (+{top_20_gainers[0]['change_pct']:.2f}%)")
            if top_20_losers:
                print(f"   Top Loser: {top_20_losers[-1]['item']['symbol']} ({top_20_losers[-1]['change_pct']:.2f}%)")

            found = []
            for pair_data in pairs_to_analyze:
                symbol = pair_data['item']['symbol']
                change_pct = pair_data['change_pct']
                last_price = pair_data['last_price']

                # Check if near flip (green to red or red to green)
                if abs(change_pct) < self.flip_threshold * 100:
                    flip_direction = "🟢➡️🔴" if change_pct > 0 else "🔴➡️🟢"

                    if self.flip_type == 'both' or \
                       (self.flip_type == 'green_to_red' and change_pct > 0) or \
                       (self.flip_type == 'red_to_green' and change_pct < 0):

                        # Check cooldown
                        if self.is_in_cooldown(symbol):
                            print(f"⏳ {symbol} in cooldown, skipping")
                            continue

                        found.append({
                            'symbol': symbol,
                            'price': last_price,
                            'change_pct': change_pct,
                            'flip_direction': flip_direction
                        })
            
            if found:
                print(f"✅ Found {len(found)} flip candidates!")
                
                # Mark all coins as alerted
                for coin in found:
                    self.mark_alerted(coin['symbol'])
                
                self.send_alert(found)
            
            return found
            
        except Exception as e:
            print(f"❌ Error in Flip scanner: {e}")
            return []
    
    def send_alert(self, coins):
        """Send Telegram alert"""
        if not self.telegram_token or not self.telegram_chat_id:
            return

        # Text alerts disabled - send only charts
        # message = f"🔄 *Daily Flip Alert!*\n\n"
        # message += f"Found {len(coins)} near flip:\n\n"
        #
        # for coin in coins:
        #     message += f"{coin['flip_direction']} *{coin['symbol']}*\n"
        #     message += f"   Change: {coin['change_pct']:.2f}%\n\n"
        #
        # message += f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        #
        # try:
        #     url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        #     payload = {
        #         'chat_id': self.telegram_chat_id,
        #         'text': message,
        #         'parse_mode': 'Markdown'
        #     }
        #     requests.post(url, json=payload, timeout=10)
        #     print("✅ Flip alert sent")
        #
        #     # Send charts
        #     if CHARTS_AVAILABLE and len(coins) > 0:
        #         self.send_charts(coins[:2])
        #
        # except Exception as e:
        #     print(f"❌ Error sending alert: {e}")

        # Send only charts for top 2 coins
        if CHARTS_AVAILABLE and len(coins) > 0:
            print("📊 Sending only chart images (text alerts disabled)")
            self.send_charts(coins[:2])
    
    def send_charts(self, coins):
        """Send chart images"""
        for coin in coins:
            try:
                chart_bytes = generate_chart_for_coin(coin['symbol'], ema_period=20)
                if chart_bytes:
                    # Link TradingView con .P per perpetual
                    tv_symbol = coin['symbol'].replace('USDT', 'USDT.P')
                    tv_link = f"https://it.tradingview.com/chart/KDtSSRjB/?symbol=BYBIT:{tv_symbol}"
                    
                    url = f"https://api.telegram.org/bot{self.telegram_token}/sendPhoto"
                    files = {'photo': ('chart.png', chart_bytes, 'image/png')}
                    data = {
                        'chat_id': self.telegram_chat_id,
                        'caption': f"[{coin['symbol']}]({tv_link}) Daily Flip",
                        'parse_mode': 'Markdown'
                    }
                    requests.post(url, files=files, data=data, timeout=30)
                    print(f"✅ Chart sent for {coin['symbol']}")
            except Exception as e:
                print(f"❌ Error sending chart: {e}")


