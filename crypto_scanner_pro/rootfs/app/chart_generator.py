"""
Chart Generator - fallback matplotlib renderer that matches chart.html visual style.
Used when Selenium/Chromium is unavailable.
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from datetime import datetime
import io
import requests

# === Colors matching chart.html ===
_BG   = '#0B0E11'
_CARD = '#161A1E'
_GRID = '#1E2329'
_TEXT = '#9CA3AF'
_CUP  = '#26a69a'
_CDN  = '#ef5350'
_EMA  = {5: '#ef4444', 10: '#fbbf24', 60: '#3b82f6', 223: '#a855f7'}
_H    = '#22c55e'
_L    = '#ef4444'


def _calc_ema(closes, period):
    k = 2 / (period + 1)
    v = closes[0]
    out = []
    for c in closes:
        v = c * k + v * (1 - k)
        out.append(v)
    return out


def _fetch_klines(symbol, interval='30', limit=350):
    try:
        r = requests.get(
            'https://api.bybit.com/v5/market/kline',
            params={'category': 'linear', 'symbol': symbol,
                    'interval': interval, 'limit': limit},
            timeout=10,
        )
        d = r.json()
        if d.get('retCode') != 0:
            return []
        klines = [
            {'t': int(k[0]) // 1000, 'o': float(k[1]),
             'h': float(k[2]), 'l': float(k[3]), 'c': float(k[4])}
            for k in d['result']['list']
        ]
        klines.sort(key=lambda x: x['t'])
        return klines
    except Exception as e:
        print(f'kline fetch error {symbol}: {e}')
        return []


def _fmt_p(p):
    if p >= 10000: return f'{p:,.0f}'
    if p >= 1:     return f'{p:.3f}'
    if p >= 0.01:  return f'{p:.5f}'
    return f'{p:.7f}'


def generate_alert_chart(symbol, interval='30', signal=None):
    """
    Generate a chart PNG matching chart.html visual style.

    signal: dict with keys
      type      : 'ema' | 'price' | 'gainer' | 'loser' | 'flip' | 'ath' | 'atl'
      price     : float   (price alert target)
      condition : 'above' | 'below'

    Returns PNG bytes or None.
    """
    klines = _fetch_klines(symbol, interval, limit=300)
    if len(klines) < 60:
        return None
    daily = _fetch_klines(symbol, 'D', limit=3)

    N  = min(80, len(klines))
    dk = klines[-N:]

    closes_all = [k['c'] for k in klines]
    ev = {p: _calc_ema(closes_all, p)[-N:] for p in [5, 10, 60, 223]}

    x = np.arange(N)
    o = np.array([k['o'] for k in dk])
    h = np.array([k['h'] for k in dk])
    l = np.array([k['l'] for k in dk])
    c = np.array([k['c'] for k in dk])

    fig, ax = plt.subplots(figsize=(12.8, 6.4), dpi=100)
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.grid(True, color=_GRID, linewidth=0.4, zorder=0)
    ax.set_axisbelow(True)

    # Candles
    up = c >= o
    dn = ~up
    bw, ww = 0.55, 0.07

    ax.bar(x[up],  c[up] - o[up],  bw, bottom=o[up],  color=_CUP, zorder=3, linewidth=0)
    ax.bar(x[dn],  o[dn] - c[dn],  bw, bottom=c[dn],  color=_CDN, zorder=3, linewidth=0)
    ax.bar(x[up],  h[up] - c[up],  ww, bottom=c[up],  color=_CUP, zorder=3, linewidth=0)
    ax.bar(x[up],  o[up] - l[up],  ww, bottom=l[up],  color=_CUP, zorder=3, linewidth=0)
    ax.bar(x[dn],  h[dn] - o[dn],  ww, bottom=o[dn],  color=_CDN, zorder=3, linewidth=0)
    ax.bar(x[dn],  c[dn] - l[dn],  ww, bottom=l[dn],  color=_CDN, zorder=3, linewidth=0)

    # EMAs
    for p, lw in [(5, 2), (10, 2), (60, 2.5), (223, 2)]:
        ax.plot(x, ev[p], color=_EMA[p], linewidth=lw, zorder=4, alpha=0.9)

    # H / L / pH / pL lines
    if daily and len(daily) >= 2:
        td, pv = daily[-1], daily[-2]
        ax.axhline(td['h'], color=_H, lw=1.2, ls='-',  alpha=0.70, zorder=5)
        ax.axhline(td['l'], color=_L, lw=1.2, ls='-',  alpha=0.70, zorder=5)
        ax.axhline(pv['h'], color=_H, lw=1.2, ls='--', alpha=0.45, zorder=5)
        ax.axhline(pv['l'], color=_L, lw=1.2, ls='--', alpha=0.45, zorder=5)
        for val, col, lbl in [
            (td['h'], _H, 'H'), (td['l'], _L, 'L'),
            (pv['h'], _H, 'pH'), (pv['l'], _L, 'pL'),
        ]:
            ax.text(N - 0.3, val, f' {lbl}', color=col,
                    fontsize=7, va='center', zorder=6)

    # Signal highlight
    if signal:
        st  = signal.get('type', '')
        clr_map = {
            'ema':    '#3b82f6',
            'gainer': '#34d399',
            'loser':  '#f87171',
            'flip':   '#fbbf24',
            'ath':    '#f7a600',
            'atl':    '#a855f7',
        }
        if st == 'price' and signal.get('price', 0) > 0:
            clr = '#34d399' if signal.get('condition') == 'above' else '#f87171'
            ax.axhline(signal['price'], color=clr, lw=1.8, ls='--',
                       alpha=0.9, zorder=6)
            ax.axvspan(N - 2.5, N - 0.5, alpha=0.07, color=clr, zorder=2)
        elif st in clr_map:
            ax.axvspan(N - 2.5, N - 0.5, alpha=0.08,
                       color=clr_map[st], zorder=2)

    # X axis ticks
    step = max(1, N // 8)
    tpos = list(range(0, N, step))
    tf_v = str(interval)

    def _tick(k):
        dt = datetime.utcfromtimestamp(k['t'])
        if tf_v == 'D':
            return dt.strftime('%m/%d')
        if int(tf_v) >= 240:
            return dt.strftime('%m/%d\n%Hh')
        return dt.strftime('%m/%d\n%H:%M')

    ax.set_xticks(tpos)
    ax.set_xticklabels([_tick(dk[i]) for i in tpos], color=_TEXT, fontsize=7.5)
    ax.tick_params(axis='y', colors=_TEXT, labelsize=8, length=0)
    ax.tick_params(axis='x', colors=_TEXT, length=0)
    ax.yaxis.set_label_position('right')
    ax.yaxis.tick_right()
    ax.set_xlim(-0.8, N - 0.2)

    # EMA legend
    handles = [mpatches.Patch(color=_EMA[p], label=f'EMA {p}')
               for p in [5, 10, 60, 223]]
    ax.legend(handles=handles, fontsize=7.5, loc='upper left',
              framealpha=0.75, facecolor=_CARD, edgecolor=_GRID,
              labelcolor=_TEXT, ncol=4, columnspacing=0.8, handlelength=1.0)

    # Header bar (simulates chart.html slot header)
    coin    = symbol.replace('USDT', '')
    tf_lbl  = {'1':'1m','5':'5m','15':'15m','30':'30m',
               '60':'1h','240':'4h','D':'1D'}.get(tf_v, tf_v)
    fig.text(0.01, 0.977, f'{coin}/USDT', color='#E5E7EB',
             fontsize=11, fontweight='bold', va='top')
    fig.text(0.10, 0.977, tf_lbl, color='#60a5fa', fontsize=9, va='top')
    fig.text(0.145, 0.977, _fmt_p(c[-1]), color='#E5E7EB', fontsize=9, va='top')
    fig.text(0.99, 0.977, datetime.utcnow().strftime('%H:%M UTC'),
             color=_TEXT, fontsize=8, va='top', ha='right')

    plt.tight_layout(rect=[0, 0, 1, 0.965])
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, facecolor=_BG, bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()


def generate_chart_for_coin(symbol, ema_period=60):
    """Backward-compatible wrapper."""
    return generate_alert_chart(symbol, interval='30')
