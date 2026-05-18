"""Shared alert utilities used by all scanners."""
from datetime import datetime, timedelta
import logging
import requests

logger = logging.getLogger(__name__)


def is_in_schedule(schedule_start, schedule_end, utc_offset=0):
    """Return True if current local time (UTC+offset) is within the configured window."""
    if not schedule_start or not schedule_end:
        return True
    try:
        now = datetime.utcnow() + timedelta(hours=utc_offset)
        sh, sm = map(int, schedule_start.split(':'))
        eh, em = map(int, schedule_end.split(':'))
        now_m   = now.hour * 60 + now.minute
        start_m = sh * 60 + sm
        end_m   = eh * 60 + em
        if start_m <= end_m:
            return start_m <= now_m <= end_m
        return now_m >= start_m or now_m <= end_m
    except Exception:
        return True


def fmt_price(p):
    if p >= 10000: return f'{p:,.0f}'
    if p >= 1:     return f'{p:.3f}'
    if p >= 0.01:  return f'{p:.5f}'
    return f'{p:.7f}'


def mtf_link(symbol, ha_url=''):
    """Return HTML link to MTF page if ha_url is set, else plain name."""
    name = symbol.replace('USDT', '')
    if ha_url:
        url = ha_url.rstrip('/') + f'/mtf?symbol={symbol}'
        return f'<a href="{url}">{name}</a>'
    return name


def build_caption(symbol, price, note, ha_url=''):
    """Standard 🔔 Alert caption format used by all scanners."""
    lines = ['🔔 Alert', '', f'Coin: {symbol}', f'Price: {fmt_price(price)}', f'Note: {note}', '']
    if ha_url:
        base = ha_url.rstrip('/')
        lines.append(f'<a href="{base}/chart?symbol={symbol}">View Chart</a>')
        lines.append(f'<a href="{base}/mtf?symbol={symbol}">View MultiTimeframe</a>')
    else:
        lines.append('View Chart')
        lines.append('View MultiTimeframe')
    return '\n'.join(lines)


def send_photo(token, chat_id, image_bytes, caption):
    """Send a Telegram photo with HTML caption."""
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendPhoto',
            files={'photo': ('chart.png', image_bytes, 'image/png')},
            data={'chat_id': chat_id, 'caption': caption, 'parse_mode': 'HTML'},
            timeout=30,
        )
    except Exception as e:
        logger.error(f'Telegram photo error: {e}')


def send_text(token, chat_id, text):
    """Send an HTML Telegram message."""
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'},
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
