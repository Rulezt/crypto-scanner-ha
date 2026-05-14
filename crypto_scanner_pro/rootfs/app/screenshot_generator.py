"""Screenshot generator - uses headless Chromium + Selenium to capture chart.html-style charts."""
import logging
import time
import base64

logger = logging.getLogger(__name__)

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    _SELENIUM_OK = True
except ImportError:
    _SELENIUM_OK = False
    logger.warning("Selenium not available - screenshots disabled, fallback to matplotlib")

PAGE_W, PAGE_H = 1280, 760


def take_screenshot(symbol, interval='30', signal_type=None, signal_price=None,
                    signal_condition=None, port=8080):
    """
    Renders screenshot.html in headless Chromium for the given symbol/interval.
    Returns PNG bytes or None on failure.
    """
    if not _SELENIUM_OK:
        return None
    try:
        opts = Options()
        opts.add_argument('--headless')
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--disable-gpu')
        opts.add_argument(f'--window-size={PAGE_W},{PAGE_H}')
        opts.add_argument('--hide-scrollbars')
        opts.add_argument('--force-device-scale-factor=1')
        opts.binary_location = '/usr/bin/chromium-browser'

        driver = webdriver.Chrome(
            service=Service('/usr/bin/chromedriver'), options=opts
        )
        try:
            # Belt-and-suspenders: set window size after init too
            driver.set_window_size(PAGE_W, PAGE_H)

            url = (f'http://localhost:{port}/screenshot'
                   f'?symbol={symbol}&interval={interval}')
            if signal_type:
                url += f'&signal_type={signal_type}'
            if signal_price and signal_price > 0:
                url += f'&signal_price={signal_price}'
            if signal_condition:
                url += f'&signal_condition={signal_condition}'

            driver.get(url)

            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    ready = driver.execute_script(
                        "return document.body && document.body.dataset.ready === '1'"
                    )
                    if ready:
                        break
                except Exception:
                    pass
                time.sleep(0.25)

            # Extra sleep so canvas finishes painting
            time.sleep(1.0)

            # Use CDP to capture exactly PAGE_W x PAGE_H regardless of viewport quirks
            try:
                result = driver.execute_cdp_cmd('Page.captureScreenshot', {
                    'format': 'png',
                    'captureBeyondViewport': True,
                    'clip': {
                        'x': 0, 'y': 0,
                        'width': PAGE_W, 'height': PAGE_H,
                        'scale': 1,
                    },
                })
                png = base64.b64decode(result['data'])
            except Exception as cdp_err:
                logger.warning(f"CDP screenshot failed ({cdp_err}), fallback to viewport")
                png = driver.get_screenshot_as_png()

            logger.info(f"Screenshot OK: {symbol} {interval}")
            return png
        finally:
            try:
                driver.quit()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Screenshot error for {symbol}/{interval}: {e}")
        return None
