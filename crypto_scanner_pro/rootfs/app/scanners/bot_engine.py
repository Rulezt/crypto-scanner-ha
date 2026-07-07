"""BOT — Breakout Pattern (port of "Breakout Pattern Setup [WillyAlgoTrader]", Pine v6)

Canali convergenti individuati sui pivot high/low (fit brute-force delle due rette
di canale entro tolleranza/deviazione in ATR), con breakout, scoring di forza
(penetrazione, body ratio, volume, momentum RSI), Entry/SL/TP1-3 calcolati dalla
geometria del canale e SL spostato a breakeven dopo TP1 (nessuna chiusura
parziale — il trade si chiude solo a SL o TP3). Stessa logica già portata in
JS per lo scanner visuale `breakout.html`; qui è la versione Python usata sia
dal backtest sia dal motore live.

`run_engine()` è l'unica funzione che replica lo stato bar-by-bar: il backtest
la chiama una volta su tutta la history (accumulando i trade chiusi), il motore
live la richiama da zero sull'intera finestra di kline in cache ad ogni nuova
candela chiusa e confronta lo stato risultante con quello della chiamata
precedente per rilevare le transizioni (entrata, TP1/TP2 toccati, chiusura).
"""
import json
import math
import os
import queue
import threading

STATE_FILE = '/data/bot_state.json'

TF_SECONDS = {
    '1': 60, '5': 300, '15': 900, '30': 1800, '60': 3600,
    '240': 14400, 'D': 86400, 'W': 604800, 'M': 2592000,  # M: approssimato a 30gg
}

DEFAULT_PARAMS = {
    'pivot_len': 5, 'min_touches': 2, 'max_channel_bars': 120,
    'convergence_min': 0.02, 'touch_tolerance': 0.15, 'deviation_max': 0.3,
    'min_channel_width_atr': 0.5, 'vol_confirm': 1.2,
    'vol_contraction': True, 'momentum_filter': True,
}
# SL Padding (ATR) dell'indicatore originale — default Pine 0, non esposto in UI.
SL_PADDING = 0.0

MAX_PIVOTS = 60
MAX_PIVOT_SCAN = 15
RESCAN_INTERVAL = 10
VOL_CONTR_THRESH = 0.85
MAX_WIDTH_MULT = 10.0

_EMPTY_ENGINE_STATE = {
    'channel_active': False,
    'hi_x1': 0.0, 'hi_y1': 0.0, 'hi_x2': 0.0, 'hi_y2': 0.0,
    'lo_x1': 0.0, 'lo_y1': 0.0, 'lo_x2': 0.0, 'lo_y2': 0.0,
    'ch_hi_touches': 0, 'ch_lo_touches': 0, 'ch_convergence': 0.0, 'ch_max_width': 0.0, 'ch_detect_bar': 0,
    'breakout_dir': 0, 'breakout_bar': 0, 'break_strength': '—', 'vol_contraction': 0.0,
    'entry_price': 0.0, 'sl_price': 0.0, 'sl_price_orig': 0.0,
    'tp1_price': 0.0, 'tp2_price': 0.0, 'tp3_price': 0.0,
    'tp1_hit': False, 'tp2_hit': False, 'tp3_hit': False, 'sl_hit': False, 'trade_open': False,
    'dir_score': 50.0,
    'closed_bar': None, 'closed_reason': None, 'closed_dir': 0, 'closed_price': None,
}


def normalize_params(cfg):
    cfg = cfg or {}
    p = dict(DEFAULT_PARAMS)
    for k in DEFAULT_PARAMS:
        if k in cfg:
            p[k] = cfg[k]
    p['pivot_len']             = int(p['pivot_len'])
    p['min_touches']           = int(p['min_touches'])
    p['max_channel_bars']      = int(p['max_channel_bars'])
    p['convergence_min']       = float(p['convergence_min'])
    p['touch_tolerance']       = float(p['touch_tolerance'])
    p['deviation_max']         = float(p['deviation_max'])
    p['min_channel_width_atr'] = float(p['min_channel_width_atr'])
    p['vol_confirm']           = float(p['vol_confirm'])
    p['vol_contraction']       = bool(p['vol_contraction'])
    p['momentum_filter']       = bool(p['momentum_filter'])
    return p


def warmup_bars_for(params):
    return max(params['pivot_len'] * 2 + params['max_channel_bars'], 50)


# ── Indicatori (Wilder ATR/RSI, SMA) ─────────────────────────────────────────

def _calc_tr(candles):
    n = len(candles)
    tr = [0.0] * n
    for i in range(n):
        if i == 0:
            tr[i] = candles[i]['high'] - candles[i]['low']
            continue
        pc = candles[i - 1]['close']
        tr[i] = max(candles[i]['high'] - candles[i]['low'],
                    abs(candles[i]['high'] - pc), abs(candles[i]['low'] - pc))
    return tr


def _calc_rma(values, period):
    n = len(values)
    out = [None] * n
    rma = None
    for i in range(n):
        if i < period - 1:
            continue
        if rma is None:
            rma = sum(values[i - period + 1:i + 1]) / period
        else:
            rma = (values[i] - rma) / period + rma
        out[i] = rma
    return out


def _calc_sma_plain(values, period):
    n = len(values)
    out = [None] * n
    s = 0.0
    for i in range(n):
        s += values[i] or 0.0
        if i >= period:
            s -= values[i - period] or 0.0
        if i >= period - 1:
            out[i] = s / period
    return out


def _calc_rsi(candles, period):
    n = len(candles)
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = candles[i]['close'] - candles[i - 1]['close']
        gains[i]  = max(diff, 0.0)
        losses[i] = max(-diff, 0.0)
    avg_g = _calc_rma(gains, period)
    avg_l = _calc_rma(losses, period)
    out = [None] * n
    for i in range(n):
        if avg_g[i] is None or avg_l[i] is None:
            continue
        if avg_l[i] == 0:
            out[i] = 100.0
            continue
        out[i] = 100.0 - 100.0 / (1.0 + avg_g[i] / avg_l[i])
    return out


def _detect_pivots(candles, length):
    n = len(candles)
    piv_high = [None] * n
    piv_low  = [None] * n
    for p in range(length, n - length):
        hp, lp = candles[p]['high'], candles[p]['low']
        is_high, is_low = True, True
        for k in range(p - length, p + length + 1):
            if k == p:
                continue
            if candles[k]['high'] >= hp:
                is_high = False
            if candles[k]['low'] <= lp:
                is_low = False
            if not is_high and not is_low:
                break
        if is_high:
            piv_high[p] = hp
        if is_low:
            piv_low[p] = lp
    return piv_high, piv_low


def _line_at(x1, y1, x2, y2, x):
    dx = x2 - x1
    if abs(dx) > 1e-10:
        return y1 + (y2 - y1) * (x - x1) / dx
    return y1


def _safe_div(num, den, fallback=0.0):
    if den != 0 and math.isfinite(num) and math.isfinite(den):
        return num / den
    return fallback


# ── Motore breakout — replay bar-by-bar ──────────────────────────────────────

def run_engine(candles, params):
    """Ritorna (stato_finale, trades). `trades` è la lista di tutti i trade
    chiusi durante il replay (per il backtest); `stato_finale` è lo stato "adesso"
    (per il motore live, che lo confronta con la chiamata precedente)."""
    n = len(candles)
    piv_high, piv_low = _detect_pivots(candles, params['pivot_len'])
    atr_arr     = _calc_rma(_calc_tr(candles), 20)
    rsi_arr     = _calc_rsi(candles, 14)
    vol_arr     = [c.get('volume', 0.0) or 0.0 for c in candles]
    vol_sma_arr = _calc_sma_plain(vol_arr, 20)
    cum_vol = [0.0] * n
    running = 0.0
    for i in range(n):
        running += vol_arr[i]
        cum_vol[i] = running

    def cum_vol_at(i):
        return 0.0 if i < 0 else cum_vol[i]

    hi_prices, hi_bars, lo_prices, lo_bars = [], [], [], []
    warmup = warmup_bars_for(params)

    st = dict(_EMPTY_ENGINE_STATE)
    trades = []
    pending = None  # dettagli del trade attualmente aperto — costruisce il record alla chiusura

    for i in range(n):
        is_warmed_up = i >= warmup
        p_idx = i - params['pivot_len']
        new_pivot = False
        if p_idx >= 0:
            if piv_high[p_idx] is not None:
                hi_prices.insert(0, piv_high[p_idx]); hi_bars.insert(0, p_idx); new_pivot = True
                if len(hi_prices) > MAX_PIVOTS:
                    hi_prices.pop(); hi_bars.pop()
            if piv_low[p_idx] is not None:
                lo_prices.insert(0, piv_low[p_idx]); lo_bars.insert(0, p_idx); new_pivot = True
                if len(lo_prices) > MAX_PIVOTS:
                    lo_prices.pop(); lo_bars.pop()

        atr     = atr_arr[i] if atr_arr[i] is not None else candles[i]['close'] * 0.0001
        rsi     = rsi_arr[i] if rsi_arr[i] is not None else 50.0
        vol_sma = vol_sma_arr[i] if vol_sma_arr[i] is not None else 0.0
        has_volume = vol_arr[i] > 0

        rescan_due  = (i % RESCAN_INTERVAL) == 0
        should_scan = ((new_pivot or rescan_due) and not st['channel_active'] and not st['trade_open']
                       and is_warmed_up and len(hi_prices) >= params['min_touches']
                       and len(lo_prices) >= params['min_touches'])

        if should_scan:
            hx, hy, lx, ly = [], [], [], []
            for k in range(min(len(hi_prices), MAX_PIVOT_SCAN + 1)):
                if i - hi_bars[k] <= params['max_channel_bars']:
                    hx.append(hi_bars[k]); hy.append(hi_prices[k])
            for k in range(min(len(lo_prices), MAX_PIVOT_SCAN + 1)):
                if i - lo_bars[k] <= params['max_channel_bars']:
                    lx.append(lo_bars[k]); ly.append(lo_prices[k])
            h_cnt, l_cnt = len(hx), len(lx)
            dev_tol, touch_tol = atr * params['deviation_max'], atr * params['touch_tolerance']

            best_hi_touches, found_hi = 0, False
            b_hi_x1 = b_hi_y1 = b_hi_x2 = b_hi_y2 = 0.0
            if h_cnt >= params['min_touches']:
                for a in range(h_cnt - 1):
                    for b in range(a + 1, h_cnt):
                        ax, ay, bx, bpy = hx[a], hy[a], hx[b], hy[b]
                        if abs(bx - ax) < 1.0:
                            continue
                        all_below, touches = True, 0
                        for k in range(h_cnt):
                            diff = hy[k] - _line_at(ax, ay, bx, bpy, hx[k])
                            if diff > dev_tol:
                                all_below = False; break
                            if abs(diff) <= touch_tol:
                                touches += 1
                        if all_below and touches >= params['min_touches'] and touches > best_hi_touches:
                            best_hi_touches = touches
                            b_hi_x1, b_hi_y1, b_hi_x2, b_hi_y2 = ax, ay, bx, bpy
                            found_hi = True

            best_lo_touches, found_lo = 0, False
            b_lo_x1 = b_lo_y1 = b_lo_x2 = b_lo_y2 = 0.0
            if l_cnt >= params['min_touches']:
                for a in range(l_cnt - 1):
                    for b in range(a + 1, l_cnt):
                        ax, ay, bx, bpy = lx[a], ly[a], lx[b], ly[b]
                        if abs(bx - ax) < 1.0:
                            continue
                        all_above, touches = True, 0
                        for k in range(l_cnt):
                            diff = ly[k] - _line_at(ax, ay, bx, bpy, lx[k])
                            if diff < -dev_tol:
                                all_above = False; break
                            if abs(diff) <= touch_tol:
                                touches += 1
                        if all_above and touches >= params['min_touches'] and touches > best_lo_touches:
                            best_lo_touches = touches
                            b_lo_x1, b_lo_y1, b_lo_x2, b_lo_y2 = ax, ay, bx, bpy
                            found_lo = True

            if found_hi and found_lo:
                hi_min_x, hi_max_x = min(b_hi_x1, b_hi_x2), max(b_hi_x1, b_hi_x2)
                lo_min_x, lo_max_x = min(b_lo_x1, b_lo_x2), max(b_lo_x1, b_lo_x2)
                overlap_start, overlap_end = max(hi_min_x, lo_min_x), min(hi_max_x, lo_max_x)
                max_h = 0.0
                if overlap_end > overlap_start:
                    samples = max(int((overlap_end - overlap_start) / 5.0), 2)
                    for s in range(samples + 1):
                        sx = overlap_start + (overlap_end - overlap_start) * s / samples
                        hu = _line_at(b_hi_x1, b_hi_y1, b_hi_x2, b_hi_y2, sx)
                        lo = _line_at(b_lo_x1, b_lo_y1, b_lo_x2, b_lo_y2, sx)
                        if hu - lo > max_h:
                            max_h = hu - lo
                else:
                    mid_x = (hi_min_x + hi_max_x + lo_min_x + lo_max_x) / 4.0
                    max_h = abs(_line_at(b_hi_x1, b_hi_y1, b_hi_x2, b_hi_y2, mid_x)
                                - _line_at(b_lo_x1, b_lo_y1, b_lo_x2, b_lo_y2, mid_x))

                start_x = min(hi_min_x, lo_min_x)
                end_x = i
                upper_now = _line_at(b_hi_x1, b_hi_y1, b_hi_x2, b_hi_y2, end_x)
                lower_now = _line_at(b_lo_x1, b_lo_y1, b_lo_x2, b_lo_y2, end_x)
                width_now = upper_now - lower_now
                upper_start = _line_at(b_hi_x1, b_hi_y1, b_hi_x2, b_hi_y2, start_x)
                lower_start = _line_at(b_lo_x1, b_lo_y1, b_lo_x2, b_lo_y2, start_x)
                width_start = upper_start - lower_start
                conv_rate = (1.0 - _safe_div(width_now, width_start, 1.0)) if width_start > 0 else 0.0
                not_inv  = width_now > 0
                is_conv  = conv_rate >= params['convergence_min']
                width_ok = width_now >= atr * params['min_channel_width_atr'] and width_now < atr * MAX_WIDTH_MULT
                prev_close = candles[i - 1]['close'] if i > 0 else candles[i]['close']
                is_inside = not_inv and lower_now <= prev_close <= upper_now

                pat_start_idx = int(start_x)
                pat_bars    = max(i - pat_start_idx, 1)
                saf_pat_bars = min(pat_bars, i)
                pre_bars    = min(saf_pat_bars, pat_start_idx)
                vol_in_pat  = (_safe_div(cum_vol_at(i) - cum_vol_at(i - saf_pat_bars), saf_pat_bars, vol_sma)
                               if (has_volume and saf_pat_bars > 0) else vol_sma)
                vol_pre_pat = (_safe_div(cum_vol_at(i - saf_pat_bars) - cum_vol_at(i - saf_pat_bars - pre_bars), pre_bars, vol_sma)
                               if (has_volume and pre_bars > 0) else vol_sma)
                vol_cont_ratio = _safe_div(vol_in_pat, vol_pre_pat, 1.0)
                vol_cont_ok = ((not has_volume or vol_cont_ratio < VOL_CONTR_THRESH)
                               if params['vol_contraction'] else True)

                if not_inv and is_conv and width_ok and is_inside and vol_cont_ok:
                    st['channel_active'] = True
                    st['hi_x1'], st['hi_y1'], st['hi_x2'], st['hi_y2'] = b_hi_x1, b_hi_y1, b_hi_x2, b_hi_y2
                    st['lo_x1'], st['lo_y1'], st['lo_x2'], st['lo_y2'] = b_lo_x1, b_lo_y1, b_lo_x2, b_lo_y2
                    st['ch_hi_touches'], st['ch_lo_touches'] = best_hi_touches, best_lo_touches
                    st['ch_convergence'], st['ch_max_width'], st['ch_detect_bar'] = conv_rate, max_h, i
                    st['breakout_dir'] = 0
                    st['break_strength'] = '—'
                    st['vol_contraction'] = vol_cont_ratio

        # breakout detection
        raw_bull = raw_bear = False
        strength_score, break_boundary, opp_boundary = 0.0, 0.0, 0.0
        channel_mature = st['channel_active'] and st['breakout_dir'] == 0
        if channel_mature and is_warmed_up:
            upper = _line_at(st['hi_x1'], st['hi_y1'], st['hi_x2'], st['hi_y2'], i)
            lower = _line_at(st['lo_x1'], st['lo_y1'], st['lo_x2'], st['lo_y2'], i)
            c = candles[i]
            body_len, candle_len = abs(c['close'] - c['open']), c['high'] - c['low']
            body_ratio = body_len / candle_len if candle_len > 0 else 0.0
            body_mid = (c['open'] + c['close']) / 2.0
            vol_ok = (vol_arr[i] > vol_sma * params['vol_confirm']) if has_volume else True
            mom_ok = (((rsi > 50) if c['close'] > upper else (rsi < 50)) if params['momentum_filter'] else True)

            if c['close'] > upper:
                raw_bull, break_boundary, opp_boundary = True, upper, lower
                penetration = min((c['close'] - upper) / atr, 2.0) / 2.0 if atr > 0 else 0.5
                body_commit = 1.0 if body_mid > upper else 0.3
                vol_bonus, mom_bonus = (1.0 if vol_ok else 0.4), (1.0 if mom_ok else 0.4)
                strength_score = (penetration*0.25 + body_ratio*0.15 + body_commit*0.15
                                  + vol_bonus*0.25 + mom_bonus*0.20) * 100
            if c['close'] < lower:
                raw_bear, break_boundary, opp_boundary = True, lower, upper
                penetration = min((lower - c['close']) / atr, 2.0) / 2.0 if atr > 0 else 0.5
                body_commit = 1.0 if body_mid < lower else 0.3
                vol_bonus, mom_bonus = (1.0 if vol_ok else 0.4), (1.0 if mom_ok else 0.4)
                strength_score = (penetration*0.25 + body_ratio*0.15 + body_commit*0.15
                                  + vol_bonus*0.25 + mom_bonus*0.20) * 100

        # directional scoring
        if st['channel_active'] and st['breakout_dir'] == 0:
            upper_now = _line_at(st['hi_x1'], st['hi_y1'], st['hi_x2'], st['hi_y2'], i)
            lower_now = _line_at(st['lo_x1'], st['lo_y1'], st['lo_x2'], st['lo_y2'], i)
            p_ix = max(i - 1, 0)
            upper_prev = _line_at(st['hi_x1'], st['hi_y1'], st['hi_x2'], st['hi_y2'], p_ix)
            lower_prev = _line_at(st['lo_x1'], st['lo_y1'], st['lo_x2'], st['lo_y2'], p_ix)
            mid_slope = ((upper_now + lower_now) - (upper_prev + lower_prev)) / 2.0
            slope_bias = (min(mid_slope/(atr*0.01), 1.0) if mid_slope > 0 else max(mid_slope/(atr*0.01), -1.0))
            rsi_bias = (rsi - 50.0) / 50.0
            ch_range = upper_now - lower_now
            pos_in_ch = (candles[i]['close'] - lower_now) / ch_range if ch_range > 0 else 0.5
            pos_bias = (pos_in_ch - 0.5) * 2.0
            combined = slope_bias*0.35 + rsi_bias*0.35 + pos_bias*0.30
            st['dir_score'] = max(0.0, min(100.0, 50.0 + combined*50.0))
        if not st['channel_active']:
            st['dir_score'] = 50.0

        # signal processing
        if raw_bull:
            st['breakout_dir'], st['breakout_bar'] = 1, i
            st['entry_price'] = candles[i]['close']
            st['sl_price'] = opp_boundary - atr * SL_PADDING
            st['sl_price_orig'] = st['sl_price']
            target_price = break_boundary + st['ch_max_width']
            full_move = abs(target_price - st['entry_price'])
            st['tp1_price'] = st['entry_price'] + full_move / 3.0
            st['tp2_price'] = st['entry_price'] + full_move * 2.0 / 3.0
            st['tp3_price'] = st['entry_price'] + full_move
            st['tp1_hit'] = st['tp2_hit'] = st['tp3_hit'] = st['sl_hit'] = False
            st['trade_open'] = True
            st['break_strength'] = 'Strong' if strength_score >= 65 else 'Medium' if strength_score >= 35 else 'Weak'
            pending = {
                'side': 'long', 'entry_time': candles[i]['time'], 'entry_price': st['entry_price'],
                'sl_orig': st['sl_price_orig'], 'tp1_price': st['tp1_price'], 'tp2_price': st['tp2_price'],
                'tp3_price': st['tp3_price'], 'strength': st['break_strength'],
                'touches_h': st['ch_hi_touches'], 'touches_l': st['ch_lo_touches'], 'conv': st['ch_convergence'] * 100,
            }
        if raw_bear:
            st['breakout_dir'], st['breakout_bar'] = -1, i
            st['entry_price'] = candles[i]['close']
            st['sl_price'] = opp_boundary + atr * SL_PADDING
            st['sl_price_orig'] = st['sl_price']
            target_price = break_boundary - st['ch_max_width']
            full_move = abs(st['entry_price'] - target_price)
            st['tp1_price'] = st['entry_price'] - full_move / 3.0
            st['tp2_price'] = st['entry_price'] - full_move * 2.0 / 3.0
            st['tp3_price'] = st['entry_price'] - full_move
            st['tp1_hit'] = st['tp2_hit'] = st['tp3_hit'] = st['sl_hit'] = False
            st['trade_open'] = True
            st['break_strength'] = 'Strong' if strength_score >= 65 else 'Medium' if strength_score >= 35 else 'Weak'
            pending = {
                'side': 'short', 'entry_time': candles[i]['time'], 'entry_price': st['entry_price'],
                'sl_orig': st['sl_price_orig'], 'tp1_price': st['tp1_price'], 'tp2_price': st['tp2_price'],
                'tp3_price': st['tp3_price'], 'strength': st['break_strength'],
                'touches_h': st['ch_hi_touches'], 'touches_l': st['ch_lo_touches'], 'conv': st['ch_convergence'] * 100,
            }

        # channel timeout (solo se non c'è ancora stato un breakout)
        if st['channel_active'] and st['breakout_dir'] == 0 and (i - st['ch_detect_bar']) > params['max_channel_bars']:
            st['channel_active'] = False

        # TP/SL hit detection
        if st['trade_open'] and st['breakout_dir'] != 0 and not st['sl_hit'] and i > st['breakout_bar']:
            hi, lo = candles[i]['high'], candles[i]['low']
            tp_side  = (hi >= st['tp1_price']) if st['breakout_dir'] == 1 else (lo <= st['tp1_price'])
            tp2_side = (hi >= st['tp2_price']) if st['breakout_dir'] == 1 else (lo <= st['tp2_price'])
            tp3_side = (hi >= st['tp3_price']) if st['breakout_dir'] == 1 else (lo <= st['tp3_price'])
            sl_side  = (lo <= st['sl_price'])  if st['breakout_dir'] == 1 else (hi >= st['sl_price'])
            if sl_side:
                st['sl_hit'] = True
            else:
                if tp_side and not st['tp1_hit']:
                    st['tp1_hit'] = True
                    st['sl_price'] = st['entry_price']  # breakeven
                if tp2_side and not st['tp2_hit']:
                    st['tp2_hit'] = True
                if tp3_side and not st['tp3_hit']:
                    st['tp3_hit'] = True

        # close trade (solo a SL o TP3 — nessuna chiusura parziale a TP1/TP2)
        if st['trade_open'] and (st['tp3_hit'] or st['sl_hit']):
            reason = ('be' if st['tp1_hit'] else 'sl') if st['sl_hit'] else 'tp3'
            exit_price = st['tp3_price'] if reason == 'tp3' else st['sl_price']
            st['closed_dir'] = st['breakout_dir']
            st['closed_reason'] = reason
            st['closed_bar'] = i
            st['closed_price'] = exit_price
            if pending is not None:
                trades.append({
                    **pending,
                    'exit_time': candles[i]['time'], 'exit_price': exit_price, 'exit_reason': reason,
                    'tp1_hit': st['tp1_hit'], 'tp2_hit': st['tp2_hit'],
                })
                pending = None
            st['trade_open'] = False
            st['channel_active'] = False
            st['breakout_dir'] = 0

    return st, trades


def _max_drawdown_pct(curve):
    if not curve:
        return 0.0
    peak = curve[0]['equity']
    max_dd = 0.0
    for pt in curve:
        peak = max(peak, pt['equity'])
        if peak > 0:
            max_dd = max(max_dd, (peak - pt['equity']) / peak * 100.0)
    return round(max_dd, 2)


def run_backtest(candles, params, initial_capital=1000.0, sizing=None):
    sizing = sizing or {'type': 'fixed', 'value': 50.0}
    _, raw_trades = run_engine(candles, params)

    trades = []
    equity = float(initial_capital)
    equity_curve = [{'time': candles[0]['time'] if candles else 0, 'equity': round(equity, 4)}]

    for rt in raw_trades:
        side, entry_price, exit_price = rt['side'], rt['entry_price'], rt['exit_price']
        direction = 1 if side == 'long' else -1
        pnl_pct = direction * (exit_price - entry_price) / entry_price * 100.0

        if sizing.get('type') == 'pct_balance':
            notional = equity * (float(sizing.get('value', 0)) / 100.0)
        else:
            notional = float(sizing.get('value', 50.0))
        pnl_usdt = notional * (pnl_pct / 100.0)
        equity += pnl_usdt

        trades.append({
            'side': side, 'entry_time': rt['entry_time'], 'entry_price': entry_price,
            'exit_time': rt['exit_time'], 'exit_price': exit_price, 'exit_reason': rt['exit_reason'],
            'pnl_pct': round(pnl_pct, 4), 'pnl_usdt': round(pnl_usdt, 4), 'notional': round(notional, 2),
            'sl_orig': rt['sl_orig'], 'tp1_price': rt['tp1_price'], 'tp2_price': rt['tp2_price'], 'tp3_price': rt['tp3_price'],
            'strength': rt['strength'], 'touches_h': rt['touches_h'], 'touches_l': rt['touches_l'],
            'conv': round(rt['conv'], 2), 'tp1_hit': rt['tp1_hit'], 'tp2_hit': rt['tp2_hit'],
        })
        equity_curve.append({'time': rt['exit_time'], 'equity': round(equity, 4)})

    total  = len(trades)
    wins   = [tr for tr in trades if tr['pnl_usdt'] > 0]
    losses = [tr for tr in trades if tr['pnl_usdt'] <= 0]
    gross_profit = sum(tr['pnl_usdt'] for tr in wins)
    gross_loss   = -sum(tr['pnl_usdt'] for tr in losses)
    net_profit   = equity - initial_capital

    stats = {
        'net_profit': round(net_profit, 2),
        'net_profit_pct': round(net_profit / initial_capital * 100.0, 2) if initial_capital else 0.0,
        'total_trades': total,
        'win_rate': round(len(wins) / total * 100.0, 2) if total else 0.0,
        'profit_factor': (round(gross_profit / gross_loss, 4) if gross_loss > 0
                           else (None if gross_profit == 0 else float('inf'))),
        'avg_trade_pct': round(sum(tr['pnl_pct'] for tr in trades) / total, 4) if total else 0.0,
        'max_drawdown_pct': _max_drawdown_pct(equity_curve),
    }
    if stats['profit_factor'] == float('inf'):
        stats['profit_factor'] = None  # nessuna perdita — non rappresentabile in JSON

    return {'trades': trades, 'equity_curve': equity_curve, 'stats': stats}


# ── Live engine ──────────────────────────────────────────────────────────────

class BotEngine:
    def __init__(self, telegram_config=None, ws_manager=None, live_config=None,
                 trade_client=None, symbol='', tf='60', mode='signal', sizing=None,
                 leverage=1, **params_cfg):
        telegram_config = telegram_config or {}
        self.telegram_token   = telegram_config.get('token', '')
        self.telegram_chat_id = telegram_config.get('chat_id', '')
        self._ws_manager  = ws_manager
        self._live_config = live_config
        self._trade_client = trade_client

        self.symbol = (symbol or '').upper()
        self.tf     = str(tf or '60')
        self.mode   = mode if mode in ('signal', 'execution') else 'signal'
        self.sizing = sizing or {'type': 'fixed', 'value': 50.0}
        self.leverage = int(leverage or 1)
        self.params = normalize_params(params_cfg)

        self._lock = threading.Lock()
        self._alert_queue = queue.Queue(maxsize=50)
        threading.Thread(target=self._alert_worker, daemon=True).start()

        st = self._load_state()
        self.running = bool(st.get('running', False))
        loaded_state = st.get('position_state') or {}
        self.state = dict(_EMPTY_ENGINE_STATE)
        if isinstance(loaded_state, dict) and 'trade_open' in loaded_state:
            self.state.update(loaded_state)
            self.signals = st.get('signals', [])
        else:
            # Stato persistito dalla vecchia strategia (Cross EMA) — forma incompatibile,
            # riparte da zero invece di crashare o mescolare segnali di due strategie diverse.
            self.signals = []

        # Sicurezza: la modalità esecuzione reale non riprende mai da sola dopo
        # un riavvio del container — richiede sempre uno Start manuale.
        if self.running and self.mode == 'execution':
            self.running = False
            self._save_state()

        if ws_manager is not None:
            ws_manager.add_kline_callback(self._on_kline)
            if self.running and self.symbol:
                ws_manager.subscribe_klines([self.symbol], intervals=[self.tf])

        print(f'🤖 BOT init — symbol={self.symbol or "-"} tf={self.tf} mode={self.mode} '
              f'running={self.running}')

    # ── State persistence ────────────────────────────────────────────────────

    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    return json.load(f)
        except Exception as e:
            print(f'⚠️ BOT: load state: {e}')
        return {}

    def _save_state(self):
        snapshot = {
            'running': self.running, 'mode': self.mode, 'symbol': self.symbol, 'tf': self.tf,
            'position_state': self.state, 'signals': self.signals[-200:],
        }
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(snapshot, f)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            print(f'⚠️ BOT: save state: {e}')

    # ── Start/Stop/Status ────────────────────────────────────────────────────

    def start(self):
        if not self.symbol:
            return False, 'Nessun simbolo configurato'
        if self.mode == 'execution' and self._trade_client:
            pos = self._trade_client.get_position(self.symbol)
            if pos:
                return False, (f'Posizione già aperta su {self.symbol} — chiudila '
                                f'manualmente prima di avviare il bot')
        with self._lock:
            self.running = True
            self.state = dict(_EMPTY_ENGINE_STATE)
            self._save_state()
        if self._ws_manager:
            self._ws_manager.subscribe_klines([self.symbol], intervals=[self.tf])
        return True, None

    def stop(self, close_position=False):
        result = {'success': True}
        with self._lock:
            self.running = False
            if close_position and self.mode == 'execution' and self.state.get('trade_open') and self._trade_client:
                pos = self._trade_client.get_position(self.symbol)
                if pos:
                    ok, err = self._trade_client.close_position(self.symbol, pos['side'], pos['size'])
                    result['closed'] = ok
                    if err:
                        result['error'] = err
                self.state = dict(_EMPTY_ENGINE_STATE)
            self._save_state()
        return result

    def status(self):
        live_pos = None
        if self.mode == 'execution' and self._trade_client and self.symbol:
            live_pos = self._trade_client.get_position(self.symbol)

        position = None
        if self.state.get('trade_open'):
            position = {
                'side': 'long' if self.state['breakout_dir'] == 1 else 'short',
                'entry_price': self.state['entry_price'], 'stop': self.state['sl_price'],
                'tp1': self.state['tp1_price'], 'tp2': self.state['tp2_price'], 'tp3': self.state['tp3_price'],
                'tp1_hit': self.state['tp1_hit'], 'tp2_hit': self.state['tp2_hit'],
                'strength': self.state['break_strength'],
            }
        channel = None
        if self.state.get('channel_active') and self.state.get('breakout_dir') == 0:
            channel = {
                'dir_score': round(self.state['dir_score'], 1),
                'touches_h': self.state['ch_hi_touches'], 'touches_l': self.state['ch_lo_touches'],
                'conv': round(self.state['ch_convergence'] * 100, 1),
            }

        return {
            'running': self.running, 'mode': self.mode, 'symbol': self.symbol, 'tf': self.tf,
            'position': position, 'channel': channel, 'exchange_position': live_pos,
            'last_signal_time': self.signals[-1]['time'] if self.signals else None,
        }

    def get_signals(self, limit=100):
        return self.signals[-limit:]

    # ── WS callback ──────────────────────────────────────────────────────────

    def _diff_events(self, prev, new, klines, i):
        """Confronta lo stato del motore tra due chiamate consecutive (una candela
        chiusa in più) e ne deriva gli eventi — replica l'ordine Pine: sullo stesso
        bar di un'entrata non si valuta nient'altro."""
        events = []
        t = klines[i]['time']

        if new['trade_open'] and (not prev.get('trade_open') or new['breakout_bar'] != prev.get('breakout_bar')):
            side = 'long' if new['breakout_dir'] == 1 else 'short'
            events.append({'type': 'entry', 'side': side, 'time': t, 'price': new['entry_price'],
                            'stop': new['sl_price_orig'], 'tp1': new['tp1_price'], 'tp2': new['tp2_price'],
                            'tp3': new['tp3_price'], 'strength': new['break_strength']})
            return events

        if prev.get('trade_open') and new['trade_open']:
            side = 'long' if new['breakout_dir'] == 1 else 'short'
            if new['tp1_hit'] and not prev.get('tp1_hit'):
                events.append({'type': 'tp1_hit', 'side': side, 'time': t, 'price': new['tp1_price'],
                                'new_stop': new['sl_price']})
            if new['tp2_hit'] and not prev.get('tp2_hit'):
                events.append({'type': 'tp2_hit', 'side': side, 'time': t, 'price': new['tp2_price']})

        if prev.get('trade_open') and not new['trade_open'] and new['closed_bar'] == i:
            side = 'long' if new['closed_dir'] == 1 else 'short'
            events.append({'type': 'exit', 'reason': new['closed_reason'], 'side': side,
                            'time': t, 'price': new['closed_price']})

        return events

    def _on_kline(self, symbol, interval, candle, is_closed):
        if not self.running or not is_closed:
            return
        if symbol != self.symbol or interval != self.tf:
            return
        if not self._ws_manager:
            return

        klines = self._ws_manager.get_klines(symbol, interval)
        needed = warmup_bars_for(self.params) + 5
        if len(klines) < needed:
            return

        with self._lock:
            new_state, _ = run_engine(klines, self.params)
            i = len(klines) - 1
            events = self._diff_events(self.state, new_state, klines, i)
            self.state = new_state
            if events:
                for ev in events:
                    rec = dict(ev)
                    rec['mode'] = self.mode
                    self.signals.append(rec)
                self.signals = self.signals[-200:]
            self._save_state()

        for ev in events:
            self._handle_event(ev)

    def _handle_event(self, ev):
        self._queue_alert(dict(ev, mode=self.mode))
        if self.mode != 'execution' or not self._trade_client:
            return
        try:
            if ev['type'] == 'entry':
                self._execute_entry(ev)
            elif ev['type'] == 'tp1_hit':
                self._execute_breakeven(ev)
            elif ev['type'] == 'exit':
                self._execute_exit(ev)
        except Exception as e:
            print(f'❌ BOT execution error: {e}')

    # ── Esecuzione reale ─────────────────────────────────────────────────────

    def _execute_entry(self, ev):
        symbol = self.symbol
        pos = self._trade_client.get_position(symbol)
        if pos:
            print(f'⚠️ BOT: posizione già aperta su {symbol}, entrata ignorata (anti doppia-entrata)')
            return

        price = ev['price']
        if self.sizing.get('type') == 'pct_balance':
            bal = self._trade_client.get_balance()
            notional = (bal.get('available', 0) or 0) * (float(self.sizing.get('value', 0)) / 100.0)
        else:
            notional = float(self.sizing.get('value', 50.0))

        instr = self._trade_client.get_instrument(symbol)
        qty = self._round_qty(notional / price if price else 0, instr)
        if qty <= 0:
            print(f'⚠️ BOT: qty calcolata 0 per {symbol}, entrata saltata')
            return

        side = 'Buy' if ev['side'] == 'long' else 'Sell'
        # SL originale + TP3 come take-profit dell'exchange: TP1/TP2 sono solo
        # milestone informative che spostano lo SL a breakeven (vedi _execute_breakeven).
        ok, order_id, err = self._trade_client.place_order(
            symbol=symbol, side=side, qty=qty, leverage=self.leverage,
            stop_loss=ev['stop'], take_profit=ev['tp3'])
        if not ok:
            print(f'❌ BOT order failed: {err}')

    def _execute_breakeven(self, ev):
        pos = self._trade_client.get_position(self.symbol)
        if not pos:
            return
        ok, err = self._trade_client.modify_stop_loss(self.symbol, ev['new_stop'], pos.get('positionIdx', 0))
        if not ok:
            print(f'❌ BOT breakeven SL move failed: {err}')

    def _execute_exit(self, ev):
        pos = self._trade_client.get_position(self.symbol)
        if not pos:
            return
        ok, err = self._trade_client.close_position(self.symbol, pos['side'], pos['size'])
        if not ok:
            print(f'❌ BOT close failed: {err}')

    @staticmethod
    def _round_qty(raw_qty, instr):
        step_v = float((instr or {}).get('qtyStep', 0.001)) or 0.001
        min_q  = float((instr or {}).get('minOrderQty', 0.001)) or 0.001
        qty = math.floor(raw_qty / step_v) * step_v
        if qty < min_q:
            return 0.0
        decimals = len(str(step_v).split('.')[1].rstrip('0')) if '.' in str(step_v) else 0
        return round(qty, decimals)

    # ── Alert ────────────────────────────────────────────────────────────────

    def _queue_alert(self, rec):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            self._alert_queue.put_nowait(rec)
        except queue.Full:
            pass

    def _alert_worker(self):
        while True:
            try:
                rec = self._alert_queue.get(timeout=5)
                self._send_alert(rec)
                self._alert_queue.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                print(f'❌ BOT alert worker: {e}')

    def _send_alert(self, rec):
        try:
            from alert_utils import send_text, log_alert
        except ImportError:
            return
        tf_label = {'D': '1D', '240': '4h', '60': '1h', '30': '30m', '15': '15m',
                    '5': '5m', '1': '1m', 'W': '1W', 'M': '1M'}.get(self.tf, self.tf)
        mode_label = 'ESECUZIONE REALE' if rec.get('mode') == 'execution' else 'Solo Segnale'

        if rec['type'] == 'entry':
            side_label = 'LONG' if rec['side'] == 'long' else 'SHORT'
            lines = [
                f'🤖 BOT Breakout Pattern {tf_label} — {side_label} [{rec.get("strength", "—")}]',
                '',
                '------------------------------------------------',
                f'- Coin: {self.symbol}',
                f'- Modalità: {mode_label}',
                f'- Prezzo entrata: {rec["price"]}',
                f'- Stop Loss: {rec["stop"]:.6f}',
                f'- TP1: {rec["tp1"]:.6f}  TP2: {rec["tp2"]:.6f}  TP3: {rec["tp3"]:.6f}',
                '------------------------------------------------',
                '',
                f'<a href="https://www.bybit.com/trade/usdt/{self.symbol}">- View Bybit</a>',
            ]
        elif rec['type'] in ('tp1_hit', 'tp2_hit'):
            label = 'TP1 raggiunto — SL spostato a breakeven' if rec['type'] == 'tp1_hit' else 'TP2 raggiunto'
            lines = [
                f'🤖 BOT Breakout Pattern {tf_label} — {label}',
                '',
                '------------------------------------------------',
                f'- Coin: {self.symbol}',
                f'- Modalità: {mode_label}',
                f'- Prezzo: {rec["price"]}',
                '------------------------------------------------',
            ]
        else:
            reason_label = {'sl': 'Stop Loss', 'be': 'Breakeven (dopo TP1)',
                             'tp3': 'Take Profit 3 (target)'}.get(rec['reason'], rec['reason'])
            lines = [
                f'🤖 BOT Breakout Pattern {tf_label} — CHIUSURA',
                '',
                '------------------------------------------------',
                f'- Coin: {self.symbol}',
                f'- Modalità: {mode_label}',
                f'- Motivo: {reason_label}',
                f'- Prezzo uscita: {rec["price"]}',
                '------------------------------------------------',
            ]
        caption = '\n'.join(lines)
        send_text(self.telegram_token, self.telegram_chat_id, caption)
        log_alert(self.symbol, 'BOT', emoji='🤖',
                  note=f'{rec["type"]} {rec.get("reason", "")}'.strip(), tf=tf_label)
