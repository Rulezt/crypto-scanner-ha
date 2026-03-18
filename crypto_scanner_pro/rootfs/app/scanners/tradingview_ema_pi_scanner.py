import requests
import time
import numpy as np
import pandas as pd
from datetime import datetime

class TradingViewEMAPiScanner:
    def __init__(self, telegram_token, chat_id):
        self.telegram_token = telegram_token
        self.chat_id = chat_id
        self.last_signal_time = None
        self.cooldown = 60  # cooldown period in seconds

    def calculate_ema(self, prices, window):
        return prices.ewm(span=window, adjust=False).mean()

    def check_signals(self, ema_values):
        if ema_values['ema_short'].iloc[-1] > ema_values['ema_long'].iloc[-1]:
            return 'LONG'
        elif ema_values['ema_short'].iloc[-1] < ema_values['ema_long'].iloc[-1]:
            return 'SHORT'
        return None

    def send_telegram_notification(self, message):
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {'chat_id': self.chat_id, 'text': message}
        requests.post(url, data=payload)

    def run(self, price_data):
        ema_values = pd.DataFrame({
            'price': price_data,
            'ema_short': self.calculate_ema(price_data, window=5),
            'ema_long': self.calculate_ema(price_data, window=20)
        })
        
        signal = self.check_signals(ema_values)

        curr_time = time.time()
        if signal and (self.last_signal_time is None or (curr_time - self.last_signal_time) > self.cooldown):
            self.last_signal_time = curr_time
            self.send_telegram_notification(f"Signal: {signal} at {datetime.utcfromtimestamp(curr_time).strftime('%Y-%m-%d %H:%M:%S')}")

# Example usage
# scanner = TradingViewEMAPiScanner('your_telegram_bot_token', 'your_chat_id')
# price_data = pd.Series(np.random.randn(100))  # Replace with your price data
# scanner.run(price_data)