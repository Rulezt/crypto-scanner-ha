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
        """Scan for volume spikes, gainers, losers - Top 20 Gainers + Top 20 Losers"""
        if not self.enabled:
            return {}

        print(f"📊 Volume Scanner - Looking for spikes and movers...")

        try:
            url = "https://api.bybit.com/v5/market/tickers?category=linear"
            response = requests.get(url, timeout=10)
            data = response.json()

            if data['retCode'] != 0:
                return {}

            # Filter pairs by volume and calculate 24h change %
            all_pairs = []
            for item in data['result']['list']:
                if not item['symbol'].endswith('USDT'):
                    continue

                last_price = float(item['lastPrice'])
                change_pct = float(item.get('price24hPcnt', 0)) * 100
                volume_24h_usd = float(item.get('volume24h', 0)) * last_price

                if volume_24h_usd < self.min_volume_24h:
                    continue

                all_pairs.append({
                    'symbol': item['symbol'],
                    'price': last_price,
                    'change_pct': change_pct,
                    'volume_24h_usd': volume_24h_usd
                })

            print(f"📊 Found {len(all_pairs)} pairs with sufficient volume")

            # Sort by change % to get gainers and losers
            all_pairs.sort(key=lambda x: x['change_pct'], reverse=True)

            # Get top 20 gainers and top 20 losers
            top_20_gainers = all_pairs[:20]
            top_20_losers = all_pairs[-20:] if len(all_pairs) >= 20 else []

            print(f"🎯 Analyzing top 20 Gainers + top 20 Losers ({len(top_20_gainers) + len(top_20_losers)} total)...")
            if top_20_gainers:
                print(f"   Top Gainer: {top_20_gainers[0]['symbol']} (+{top_20_gainers[0]['change_pct']:.2f}%)")
            if top_20_losers:
                print(f"   Top Loser: {top_20_losers[-1]['symbol']} ({top_20_losers[-1]['change_pct']:.2f}%)")

            gainers = []
            losers = []
            volume_spikes = []

            # Process top 20 gainers
            if self.gainers_enabled:
                for pair in top_20_gainers:
                    if pair['change_pct'] > self.gainers_threshold:
                        # Check cooldown
                        if not self.is_in_cooldown(pair['symbol'], 'gainer'):
                            gainers.append(pair)
                        else:
                            print(f"⏳ {pair['symbol']} (gainer) in cooldown, skipping")

            # Process top 20 losers
            if self.losers_enabled:
                for pair in top_20_losers:
                    if pair['change_pct'] < -self.losers_threshold:
                        # Check cooldown
                        if not self.is_in_cooldown(pair['symbol'], 'loser'):
                            losers.append(pair)
                        else:
                            print(f"⏳ {pair['symbol']} (loser) in cooldown, skipping")

            # Already sorted, just limit to max_coins
            gainers = gainers[:self.max_coins]
            losers = losers[:self.max_coins]
            
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
        """Send Telegram alert: one photo per coin (max 2 gainers + 2 losers) with clean caption."""
        if not self.telegram_token or not self.telegram_chat_id:
            return

        try:
            from alert_utils import fmt_price, utc_time, send_photo, send_text, get_chart
        except ImportError as e:
            print(f"Cannot import alert_utils: {e}")
            return

        def _vol_str(v):
            if v >= 1e9: return f'${v/1e9:.1f}B'
            if v >= 1e6: return f'${v/1e6:.0f}M'
            return f'${v/1e3:.0f}K'

        for coin in result.get('gainers', [])[:2]:
            sym     = coin['symbol']
            name    = sym.replace('USDT', '/USDT')
            caption = (
                f"{name}  +{coin['change_pct']:.2f}% (24h)\n"
                f"Vol: {_vol_str(coin['volume_24h_usd'])}  Prezzo: {fmt_price(coin['price'])}\n"
                f"{utc_time()}"
            )
            img = get_chart(sym, interval='240', signal={'type': 'gainer'})
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
            print(f"Gainer alert inviato: {sym}")

        for coin in result.get('losers', [])[:2]:
            sym     = coin['symbol']
            name    = sym.replace('USDT', '/USDT')
            caption = (
                f"{name}  {coin['change_pct']:.2f}% (24h)\n"
                f"Vol: {_vol_str(coin['volume_24h_usd'])}  Prezzo: {fmt_price(coin['price'])}\n"
                f"{utc_time()}"
            )
            img = get_chart(sym, interval='240', signal={'type': 'loser'})
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
            print(f"Loser alert inviato: {sym}")
