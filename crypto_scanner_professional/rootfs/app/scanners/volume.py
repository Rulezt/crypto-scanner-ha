"""Volume Scanner - Volume Spikes, Gainers, Losers"""
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

# File per cooldown persistenti
GAINERS_COOLDOWN_FILE = '/data/gainers_cooldown.json'
LOSERS_COOLDOWN_FILE = '/data/losers_cooldown.json'

class VolumeScanner:
    def __init__(self, telegram_config, enabled=True, volume_spike_threshold=200,
                 gainers_enabled=True, gainers_threshold=10,
                 losers_enabled=True, losers_threshold=10,
                 scan_interval_minutes=30, min_volume_24h=10000000,
                 max_coins_per_alert=10, cooldown_hours=2, **kwargs):
        
        self.telegram_token = telegram_config['token']
        self.telegram_chat_id = telegram_config['chat_id']
        self.enabled = enabled
        self.volume_spike_threshold = volume_spike_threshold
        self.gainers_enabled = gainers_enabled
        self.gainers_threshold = gainers_threshold
        self.losers_enabled = losers_enabled
        self.losers_threshold = losers_threshold
        self.max_coins = max_coins_per_alert
        self.min_volume_24h = min_volume_24h
        self.cooldown_hours = cooldown_hours
        
        # Carica cooldown da file
        self.last_gainers = self._load_cooldown(GAINERS_COOLDOWN_FILE)
        self.last_losers = self._load_cooldown(LOSERS_COOLDOWN_FILE)
    
    def _load_cooldown(self, filepath):
        """Carica cooldown da file"""
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    return {k: datetime.fromisoformat(v) for k, v in data.items()}
        except Exception as e:
            print(f"⚠️ Error loading cooldown from {filepath}: {e}")
        return {}
    
    def _save_cooldown(self, filepath, alerts_dict):
        """Salva cooldown su file"""
        try:
            data = {k: v.isoformat() for k, v in alerts_dict.items()}
            with open(filepath, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            print(f"⚠️ Error saving cooldown to {filepath}: {e}")
    
    def is_in_cooldown(self, symbol, alert_type='gainer'):
        """Check if symbol is in cooldown period"""
        alerts_dict = self.last_gainers if alert_type == 'gainer' else self.last_losers
        
        if symbol not in alerts_dict:
            return False
        
        last_alert_time = alerts_dict[symbol]
        now = datetime.now()
        cooldown_delta = timedelta(hours=self.cooldown_hours)
        
        in_cooldown = (now - last_alert_time) < cooldown_delta
        
        if in_cooldown:
            remaining = cooldown_delta - (now - last_alert_time)
            remaining_minutes = int(remaining.total_seconds() / 60)
            print(f"⏳ {symbol} ({alert_type}) in cooldown ({remaining_minutes} min remaining)")
        
        return in_cooldown
    
    def mark_alerted(self, symbol, alert_type='gainer'):
        """Mark symbol as alerted"""
        if alert_type == 'gainer':
            self.last_gainers[symbol] = datetime.now()
            self._save_cooldown(GAINERS_COOLDOWN_FILE, self.last_gainers)
        else:
            self.last_losers[symbol] = datetime.now()
            self._save_cooldown(LOSERS_COOLDOWN_FILE, self.last_losers)
        
    def scan(self):
        """Scan for volume spikes, gainers, losers"""
        if not self.enabled:
            return {}
        
        print(f"📊 Volume Scanner - Looking for spikes and movers...")
        
        try:
            url = "https://api.bybit.com/v5/market/tickers?category=linear"
            response = requests.get(url, timeout=10)
            data = response.json()
            
            if data['retCode'] != 0:
                return {}
            
            pairs = [item for item in data['result']['list'] 
                    if item['symbol'].endswith('USDT')]
            
            gainers = []
            losers = []
            volume_spikes = []
            
            for pair in pairs:
                symbol = pair['symbol']
                last_price = float(pair['lastPrice'])
                change_pct = float(pair.get('price24hPcnt', 0)) * 100
                volume_24h_usd = float(pair.get('volume24h', 0)) * last_price
                
                if volume_24h_usd < self.min_volume_24h:
                    continue
                
                # Gainers
                if self.gainers_enabled and change_pct > self.gainers_threshold:
                    # Check cooldown
                    if not self.is_in_cooldown(symbol, 'gainer'):
                        gainers.append({
                            'symbol': symbol,
                            'price': last_price,
                            'change_pct': change_pct,
                            'volume_24h_usd': volume_24h_usd
                        })
                    else:
                        print(f"⏳ {symbol} (gainer) in cooldown, skipping")
                
                # Losers
                if self.losers_enabled and change_pct < -self.losers_threshold:
                    # Check cooldown
                    if not self.is_in_cooldown(symbol, 'loser'):
                        losers.append({
                            'symbol': symbol,
                            'price': last_price,
                            'change_pct': change_pct,
                            'volume_24h_usd': volume_24h_usd
                        })
                    else:
                        print(f"⏳ {symbol} (loser) in cooldown, skipping")
            
            # Sort and limit
            gainers.sort(key=lambda x: x['change_pct'], reverse=True)
            losers.sort(key=lambda x: x['change_pct'])
            
            result = {
                'gainers': gainers[:self.max_coins],
                'losers': losers[:self.max_coins],
                'volume_spikes': volume_spikes[:self.max_coins]
            }
            
            if gainers or losers:
                print(f"✅ Found {len(gainers)} gainers, {len(losers)} losers")
                
                # Mark all as alerted
                for coin in gainers[:self.max_coins]:
                    self.mark_alerted(coin['symbol'], 'gainer')
                for coin in losers[:self.max_coins]:
                    self.mark_alerted(coin['symbol'], 'loser')
                
                self.send_alert(result)
            
            return result
            
        except Exception as e:
            print(f"❌ Error in Volume scanner: {e}")
            return {}
    
    def send_alert(self, result):
        """Send Telegram alert"""
        if not self.telegram_token or not self.telegram_chat_id:
            return
        
        message = f"📊 *Volume & Movers Alert!*\n\n"
        
        if result['gainers']:
            message += f"🚀 *Top Gainers:*\n"
            for coin in result['gainers'][:5]:
                message += f"   {coin['symbol']}: +{coin['change_pct']:.2f}%\n"
            message += "\n"
        
        if result['losers']:
            message += f"📉 *Top Losers:*\n"
            for coin in result['losers'][:5]:
                message += f"   {coin['symbol']}: {coin['change_pct']:.2f}%\n"
            message += "\n"
        
        message += f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            payload = {
                'chat_id': self.telegram_chat_id,
                'text': message,
                'parse_mode': 'Markdown'
            }
            requests.post(url, json=payload, timeout=10)
            print("✅ Volume alert sent")
            
            # Send charts for top gainer and top loser
            if CHARTS_AVAILABLE:
                charts_to_send = []
                if result['gainers']:
                    charts_to_send.append(result['gainers'][0])
                if result['losers']:
                    charts_to_send.append(result['losers'][0])
                if charts_to_send:
                    self.send_charts(charts_to_send)
                    
        except Exception as e:
            print(f"❌ Error sending alert: {e}")
    
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
                    
                    # Caption con nome coin come link
                    if coin['change_pct'] > 0:
                        caption = f"📊 [{coin['symbol']}]({tv_link}) 🚀 +{coin['change_pct']:.2f}%"
                    else:
                        caption = f"📊 [{coin['symbol']}]({tv_link}) 📉 {coin['change_pct']:.2f}%"
                    
                    data = {
                        'chat_id': self.telegram_chat_id, 
                        'caption': caption,
                        'parse_mode': 'Markdown'
                    }
                    requests.post(url, files=files, data=data, timeout=30)
                    print(f"✅ Chart sent for {coin['symbol']}")
            except Exception as e:
                print(f"❌ Error sending chart: {e}")
