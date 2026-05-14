"""Shared alert utilities used by all scanners."""
from datetime import datetime
import logging
import requests

logger = logging.getLogger(__name__)


def fmt_price(p):
    if p >= 10000: return f'{p:,.0f}'
    if p >= 1:     return f'{p:.3f}'
    if p >= 0.01:  return f'{p:.5f}'
    return f'{p:.7f}'


def utc_time():
    return datetime.utcnow().strftime('%H:%M UTC')


def send_photo(token, chat_id, image_bytes, caption):
    """Send a Telegram photo with caption (no Markdown)."""
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendPhoto',
            files={'photo': ('chart.png', image_bytes, 'image/png')},
            data={'chat_id': chat_id, 'caption': caption},
            timeout=30,
        )
    except Exception as e:
        logger.error(f'Telegram photo error: {e}')


def send_text(token, chat_id, text):
    """Send a plain-text Telegram message."""
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': text},
            timeout=10,
        )
    except Exception as e:
        logger.error(f'Telegram text error: {e}')


def get_chart(symbol, interval='30', signal=None):
    """
    Returns PNG bytes for the chart.
    Tries Selenium screenshot first (matches chart.html exactly).
    Falls back to matplotlib if Selenium fails.

    signal dict keys:
      type       : 'ema' | 'price' | 'gainer' | 'loser' | 'flip' | 'ath' | 'atl'
      price      : float   (for price alerts)
      condition  : 'above' | 'below'  (for price alerts)
    """
    sig_type  = signal.get('type')      if signal else None
    sig_price = signal.get('price')     if signal else None
    sig_cond  = signal.get('condition') if signal else None

    # Primary: Selenium screenshot of chart.html-style page
    try:
        from screenshot_generator import take_screenshot
        img = take_screenshot(
            symbol, interval=interval,
            signal_type=sig_type,
            signal_price=sig_price,
            signal_condition=sig_cond,
        )
        if img:
            return img
    except Exception as e:
        logger.warning(f'Selenium screenshot failed for {symbol}: {e}')

    # Fallback: matplotlib chart
    try:
        from chart_generator import generate_alert_chart
        return generate_alert_chart(symbol, interval=interval, signal=signal)
    except Exception as e:
        logger.warning(f'Matplotlib chart failed for {symbol}: {e}')

    return None
