"""ATH/ATL Scanner - All-Time High/Low Monitor"""
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
    print("⚠️ Chart generator not available")

# File per cooldown persistenti
ATH_COOLDOWN_FILE = '/data/ath_cooldown.json'
ATL_COOLDOWN_FILE = '/data/atl_cooldown.json'

class ATHATLScanner:
    def __init__(self, telegram_config, enabled=True,
                 ath_enabled=True, atl_enabled=True,
                 proximity_threshold=2.0, lookback_days=365,
                 scan_interval_minutes=30, min_volume_24h=10000000,
                 max_coins_per_alert=10, cooldown_hours=24, **kwargs):
        """
        ATH/ATL Scanner - Monitors coins approaching or breaking All-Time High/Low

        Args:
            telegram_config: Dict with 'token' and 'chat_id'
            enabled: Enable/disable scanner
            ath_enabled: Monitor ATH (All-Time High)
            atl_enabled: Monitor ATL (All-Time Low)
            proximity_threshold: Alert when within X% of ATH/ATL (default 2.0%)
            lookback_days: Days of historical data to analyze (default 365)
            min_volume_24h: Minimum 24h volume filter
            max_coins_per_alert: Max coins per alert batch
            cooldown_hours: Hours before re-alerting same coin
        """

        self.telegram_token = telegram_config['token']
        self.telegram_chat_id = telegram_config['chat_id']
        self.enabled = enabled
        self.ath_enabled = ath_enabled
        self.atl_enabled = atl_enabled
        self.proximity_threshold = proximity_threshold
        self.lookback_days = lookback_days
        self.min_volume_24h = min_volume_24h
        self.max_coins_per_alert = max_coins_per_alert
        self.cooldown_hours = cooldown_hours

        # Carica cooldown da file
        self.last_ath_alerts = self._load_cooldown(ATH_COOLDOWN_FILE)
        self.last_atl_alerts = self._load_cooldown(ATL_COOLDOWN_FILE)

        print(f"🏆 ATH/ATL Scanner initialized - Lookback: {lookback_days} days, Threshold: {proximity_threshold}%")

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
            # Crea directory se non esiste
            cooldown_dir = os.path.dirname(filepath)
            if not os.path.exists(cooldown_dir):
                os.makedirs(cooldown_dir, exist_ok=True)

            data = {k: v.isoformat() for k, v in alerts_dict.items()}
            with open(filepath, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            print(f"⚠️ Error saving cooldown to {filepath}: {e}")

    def is_in_cooldown(self, symbol, alert_type='ath'):
        """Check if symbol is in cooldown period"""
        alerts_dict = self.last_ath_alerts if alert_type == 'ath' else self.last_atl_alerts

        if symbol not in alerts_dict:
            return False

        last_alert_time = alerts_dict[symbol]
        now = datetime.now()
        cooldown_delta = timedelta(hours=self.cooldown_hours)

        in_cooldown = (now - last_alert_time) < cooldown_delta

        if in_cooldown:
            remaining = cooldown_delta - (now - last_alert_time)
            remaining_hours = int(remaining.total_seconds() / 3600)
            print(f"⏳ {symbol} ({alert_type.upper()}) in cooldown ({remaining_hours}h remaining)")

        return in_cooldown

    def mark_alerted(self, symbol, alert_type='ath'):
        """Mark symbol as alerted"""
        if alert_type == 'ath':
            self.last_ath_alerts[symbol] = datetime.now()
            self._save_cooldown(ATH_COOLDOWN_FILE, self.last_ath_alerts)
        else:
            self.last_atl_alerts[symbol] = datetime.now()
            self._save_cooldown(ATL_COOLDOWN_FILE, self.last_atl_alerts)

    def fetch_historical_data(self, symbol, days=365):
        """
        Fetch historical kline data from Bybit

        Args:
            symbol: Trading pair (e.g., 'BTCUSDT')
            days: Number of days of historical data

        Returns:
            list: List of klines or None if error
        """
        try:
            # Use daily candles for historical data
            url = 'https://api.bybit.com/v5/market/kline'

            # Calculate how many candles we need (1 candle per day)
            limit = min(days, 1000)  # Bybit max limit is 1000

            params = {
                'category': 'linear',
                'symbol': symbol,
                'interval': 'D',  # Daily candles
                'limit': limit
            }

            response = requests.get(url, params=params, timeout=10)
            data = response.json()

            if data['retCode'] != 0:
                return None

            klines = data['result']['list']

            if len(klines) < 30:  # Need at least 30 days of data
                return None

            return klines

        except Exception as e:
            print(f"❌ Error fetching historical data for {symbol}: {e}")
            return None

    def calculate_ath_atl(self, klines, current_price):
        """
        Calculate ATH and ATL from historical klines

        Args:
            klines: List of klines from Bybit
            current_price: Current price

        Returns:
            dict: {'ath': float, 'atl': float, 'ath_distance_pct': float, 'atl_distance_pct': float}
        """
        try:
            # Extract high and low prices from klines
            # Kline format: [timestamp, open, high, low, close, volume, turnover]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]

            # Find ATH and ATL
            ath = max(highs)
            atl = min(lows)

            # Calculate distance from ATH and ATL in percentage
            ath_distance_pct = ((ath - current_price) / ath) * 100
            atl_distance_pct = ((current_price - atl) / atl) * 100

            return {
                'ath': ath,
                'atl': atl,
                'ath_distance_pct': ath_distance_pct,
                'atl_distance_pct': atl_distance_pct
            }

        except Exception as e:
            print(f"❌ Error calculating ATH/ATL: {e}")
            return None

    def scan(self):
        """Scan for ATH/ATL proximity - Top 20 Gainers + Top 20 Losers"""
        if not self.enabled:
            return {}

        print(f"🏆 ATH/ATL Scanner - Looking for coins near ATH/ATL (Threshold: {self.proximity_threshold}%)...")

        try:
            url = "https://api.bybit.com/v5/market/tickers?category=linear"
            response = requests.get(url, timeout=10)
            data = response.json()

            if data['retCode'] != 0:
                print(f"❌ Bybit API error: {data['retMsg']}")
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

            # Combine for analysis
            pairs_to_analyze = top_20_gainers + top_20_losers

            print(f"🎯 Analyzing top 20 Gainers + top 20 Losers ({len(pairs_to_analyze)} total)...")
            if top_20_gainers:
                print(f"   Top Gainer: {top_20_gainers[0]['symbol']} (+{top_20_gainers[0]['change_pct']:.2f}%)")
            if top_20_losers:
                print(f"   Top Loser: {top_20_losers[-1]['symbol']} ({top_20_losers[-1]['change_pct']:.2f}%)")

            ath_coins = []
            atl_coins = []
            analyzed = 0

            for pair in pairs_to_analyze:
                symbol = pair['symbol']
                current_price = pair['price']
                analyzed += 1

                if analyzed % 10 == 0:
                    print(f"   Progress: {analyzed}/{len(pairs_to_analyze)} pairs analyzed...")

                # Fetch historical data
                klines = self.fetch_historical_data(symbol, self.lookback_days)

                if not klines:
                    continue

                # Calculate ATH/ATL
                ath_atl_data = self.calculate_ath_atl(klines, current_price)

                if not ath_atl_data:
                    continue

                # Check ATH proximity
                if self.ath_enabled and ath_atl_data['ath_distance_pct'] <= self.proximity_threshold:
                    # Check if current price is at or above ATH (new ATH!)
                    is_new_ath = current_price >= ath_atl_data['ath']

                    if not self.is_in_cooldown(symbol, 'ath'):
                        ath_coins.append({
                            'symbol': symbol,
                            'price': current_price,
                            'ath': ath_atl_data['ath'],
                            'distance_pct': ath_atl_data['ath_distance_pct'],
                            'is_new_ath': is_new_ath,
                            'change_pct': pair['change_pct']
                        })

                        status = "🚀 NEW ATH!" if is_new_ath else f"📈 {ath_atl_data['ath_distance_pct']:.2f}% from ATH"
                        print(f"   ✅ {symbol}: {status}")

                # Check ATL proximity
                if self.atl_enabled and ath_atl_data['atl_distance_pct'] >= -self.proximity_threshold:
                    # Check if current price is at or below ATL (new ATL!)
                    is_new_atl = current_price <= ath_atl_data['atl']

                    if not self.is_in_cooldown(symbol, 'atl'):
                        atl_coins.append({
                            'symbol': symbol,
                            'price': current_price,
                            'atl': ath_atl_data['atl'],
                            'distance_pct': abs(ath_atl_data['atl_distance_pct']),
                            'is_new_atl': is_new_atl,
                            'change_pct': pair['change_pct']
                        })

                        status = "💥 NEW ATL!" if is_new_atl else f"📉 {abs(ath_atl_data['atl_distance_pct']):.2f}% from ATL"
                        print(f"   ✅ {symbol}: {status}")

            # Limit results
            ath_coins = ath_coins[:self.max_coins_per_alert]
            atl_coins = atl_coins[:self.max_coins_per_alert]

            result = {
                'ath': ath_coins,
                'atl': atl_coins
            }

            if ath_coins or atl_coins:
                print(f"🏆 Found {len(ath_coins)} ATH alerts, {len(atl_coins)} ATL alerts")

                # Mark all as alerted
                for coin in ath_coins:
                    self.mark_alerted(coin['symbol'], 'ath')
                for coin in atl_coins:
                    self.mark_alerted(coin['symbol'], 'atl')

                self.send_alert(result)
            else:
                print(f"⚠️ No ATH/ATL proximity found within {self.proximity_threshold}% threshold")

            return result

        except Exception as e:
            print(f"❌ Error in ATH/ATL scanner: {e}")
            import traceback
            traceback.print_exc()
            return {}

    def send_alert(self, result):
        """Send Telegram alert"""
        if not self.telegram_token or not self.telegram_chat_id:
            print("⚠️ Telegram not configured")
            return

        # Send charts only
        if CHARTS_AVAILABLE:
            print("📊 Sending ATH/ATL chart images")
            charts_to_send = []

            # Add ATH coins (max 3)
            for coin in result['ath'][:3]:
                charts_to_send.append({
                    'coin': coin,
                    'type': 'ath'
                })

            # Add ATL coins (max 3)
            for coin in result['atl'][:3]:
                charts_to_send.append({
                    'coin': coin,
                    'type': 'atl'
                })

            if charts_to_send:
                self.send_charts(charts_to_send)

    def send_charts(self, chart_data):
        """Send chart images"""
        for item in chart_data:
            try:
                coin = item['coin']
                alert_type = item['type']

                print(f"📊 Generating chart for {coin['symbol']}...")
                chart_bytes = generate_chart_for_coin(coin['symbol'], ema_period=20)

                if chart_bytes:
                    # Link TradingView con .P per perpetual
                    tv_symbol = coin['symbol'].replace('USDT', 'USDT.P')
                    tv_link = f"https://it.tradingview.com/chart/KDtSSRjB/?symbol=BYBIT:{tv_symbol}"

                    # Build caption
                    if alert_type == 'ath':
                        if coin['is_new_ath']:
                            caption = f"[{coin['symbol']}]({tv_link}) 🚀 NUOVO ATH!\n"
                            caption += f"Prezzo: ${coin['price']:.6f}"
                        else:
                            caption = f"[{coin['symbol']}]({tv_link}) 📈 Vicino ATH\n"
                            caption += f"Distanza: {coin['distance_pct']:.2f}%\n"
                            caption += f"ATH: ${coin['ath']:.6f}"
                    else:  # atl
                        if coin['is_new_atl']:
                            caption = f"[{coin['symbol']}]({tv_link}) 💥 NUOVO ATL!\n"
                            caption += f"Prezzo: ${coin['price']:.6f}"
                        else:
                            caption = f"[{coin['symbol']}]({tv_link}) 📉 Vicino ATL\n"
                            caption += f"Distanza: {coin['distance_pct']:.2f}%\n"
                            caption += f"ATL: ${coin['atl']:.6f}"

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
