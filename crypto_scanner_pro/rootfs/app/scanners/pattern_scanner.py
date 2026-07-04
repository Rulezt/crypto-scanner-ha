"""Pattern Scanner
Rileva pattern di inversione nelle candele: Doji e Hammer.
Doji: body < soglia% del range totale (default 10%)
Hammer: body piccolo (> soglia, ≤ 35%), ombra inferiore > 55%, ombra superiore < 20%
"""
import threading
import queue
import statistics
import requests
import time
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COOLDOWN_FILE     = '/data/pattern_cooldown.json'
TOP_KLINE_SYMBOLS = 300
KLINE_SUB_REFRESH = 3600
MIN_KLINES        = 5
CANDLE_MATURITY   = 0.9  # candela in corso valida solo se trascorso >= 90% del TF

TF_SECONDS = {'1': 60, '5': 300, '15': 900, '30': 1800, '60': 3600, '240': 14400, 'D': 86400}

TYPE_EMOJI = {'dragonfly': '🐲', 'gravestone': '🪦', 'standard': '➕', 'hammer': '🔨'}
TF_LABEL   = {'D': '1D', '240': '4h', '60': '1h', '30': '30m', '15': '15m', '5': '5m', '1': '1m'}


class PatternScanner:
    def __init__(self, telegram_config, enabled=True,
                 scan_tf=None, min_volume_24h=10_000_000,
                 scan_interval_minutes=60, cooldown_hours=24,
                 max_coins_per_alert=5, doji_threshold=0.1,
                 ws_manager=None, live_config=None, **kwargs):

        self.telegram_token      = telegram_config['token']
        self.telegram_chat_id    = telegram_config['chat_id']
        self.base_url            = telegram_config.get('base_url', '')
        self.enabled             = enabled
        self.doji_threshold      = doji_threshold
        raw_tf                   = scan_tf if scan_tf is not None else ['D', '240', '60']
        self.scan_tfs            = raw_tf if isinstance(raw_tf, list) else [raw_tf]
        self.min_volume_24h      = min_volume_24h
        self.max_coins_per_alert = max_coins_per_alert
        self.cooldown_hours      = cooldown_hours
        self._live_config        = live_config
        self.last_alerts         = self._load_cooldown()
        self._lock               = threading.Lock()
        self._file_lock          = threading.Lock()
        self._last_count         = 0
        self._last_save          = 0.0
        self._ws_manager         = ws_manager
        self._alert_queue        = queue.Queue(maxsize=500)
        self._recent_seen        = {}             # key → timestamp; TTL 5 min dedup WS+polling
        threading.Thread(target=self._alert_worker, daemon=True).start()

        if ws_manager is not None:
            ws_manager.add_kline_callback(self._on_kline)
            threading.Thread(target=self._init_kline_subs, daemon=True).start()

        print(f'🕯 Pattern Scanner init — tf={",".join(self.scan_tfs)} thr={doji_threshold*100:.0f}%')

    # ── cooldown ──────────────────────────────────────────────────────────────

    def _load_cooldown(self):
        try:
            if os.path.exists(COOLDOWN_FILE):
                with open(COOLDOWN_FILE, 'r') as f:
                    return {k: datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
                            for k, v in json.load(f).items()}
        except Exception as e:
            print(f'⚠️ Pattern: load cooldown: {e}')
        return {}

    def _save_cooldown(self):
        now = time.time()
        if now - self._last_save < 60:          # throttle: max 1 write/min
            return
        self._last_save = now
        with self._lock:                         # snapshot consistente senza dirty read
            snapshot = {k: v.isoformat() for k, v in self.last_alerts.items()}
        with self._file_lock:
            try:
                tmp = COOLDOWN_FILE + '.tmp'
                os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
                with open(tmp, 'w') as f:
                    json.dump(snapshot, f)
                os.replace(tmp, COOLDOWN_FILE)
            except Exception as e:
                print(f'⚠️ Pattern: save cooldown: {e}')

    def is_in_cooldown(self, key):
        if key not in self.last_alerts:
            return False
        return (datetime.now(timezone.utc) - self.last_alerts[key]) < timedelta(hours=self.cooldown_hours)

    def mark_alerted(self, key):
        self.last_alerts[key] = datetime.now(timezone.utc)
        self._save_cooldown()

    def _alert_worker(self):
        failures = 0
        while True:
            coins = self._alert_queue.get()
            try:
                self.send_alert(coins)
                failures = 0
            except Exception as e:
                failures = min(failures + 1, 5)
                delay = 2 ** failures           # 2s → 4s → 8s → 16s → 32s
                print(f'⚠️ Pattern: alert worker: {e} — retry in {delay}s')
                time.sleep(delay)
            finally:
                self._alert_queue.task_done()

    # ── schedule ──────────────────────────────────────────────────────────────

    def _is_in_schedule(self):
        from alert_utils import is_in_schedule
        gen = (self._live_config or {}).get('general', {})
        return is_in_schedule(
            gen.get('schedule_start', ''),
            gen.get('schedule_end', ''),
            float(gen.get('utc_offset') or 2),
        )

    # ── kline subscriptions ───────────────────────────────────────────────────

    def _init_kline_subs(self):
        if not self._ws_manager.ready.wait(timeout=120):
            print('⚠️ Pattern: WS not ready after 120s')
            return
        time.sleep(30)
        self._kline_refresh_loop()

    def _kline_refresh_loop(self):
        while True:
            self._refresh_kline_subs()
            time.sleep(KLINE_SUB_REFRESH)

    def _refresh_kline_subs(self):
        tickers = self._ws_manager.get_all_tickers()
        if not tickers:
            return
        ranked = sorted(tickers.items(), key=lambda x: x[1].get('volume_24h', 0), reverse=True)
        top = [s for s, d in ranked
               if d.get('volume_24h', 0) >= self.min_volume_24h][:TOP_KLINE_SYMBOLS]
        self._ws_manager.subscribe_klines(top, intervals=self.scan_tfs)
        self._last_count = len(top)
        print(f'🕯 Pattern: subscribed klines {len(top)} symbols × {len(self.scan_tfs)} TF')

    # ── Doji detection ────────────────────────────────────────────────────────

    @staticmethod
    def _check_doji(candle, threshold=0.1, prev_candles=None):
        """Indecision Candle Detector con contesto.
        Ritorna (doji_type, signal_score 0–100) oppure (None, 0.0).
        Senza contesto max ~50/100; con trend exhaustion max 100/100.
        """
        o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
        total_range = h - l
        if total_range <= 0:
            return None, 0.0
        body       = abs(c - o)
        body_ratio = body / total_range
        if body_ratio >= threshold:
            return None, 0.0

        # ── Hard filter: rumore ────────────────────────────────────────────────
        # Candela troppo piccola rispetto all'ATR proxy = spike di rumore, non Doji
        if prev_candles:
            rng = [abs(p.get('high', 0) - p.get('low', 0))
                   for p in prev_candles if p.get('high') and p.get('low')]
            if rng:
                avg_rng = sum(rng) / len(rng)
                if avg_rng > 0 and total_range < avg_rng * 0.30:
                    return None, 0.0

        upper = h - max(o, c)
        lower = min(o, c) - l

        if lower / total_range > 0.6:
            ptype = 'dragonfly'
        elif upper / total_range > 0.6:
            ptype = 'gravestone'
        else:
            ptype = 'standard'

        # ── Scoring (raw max = 95) ─────────────────────────────────────────────
        # 1. Body compattezza: 0 → 30 pts
        score = (1.0 - body_ratio / threshold) * 30.0

        # 2. Ultra-doji bonus: open ≈ close in senso stretto (body ≤ 2% del range)
        if body_ratio <= 0.02:
            score += 10.0

        # 3. Tipo: dragonfly/gravestone hanno bias direzionale
        if ptype in ('dragonfly', 'gravestone'):
            score += 20.0                                     # bias direzionale
            score += max(lower, upper) / total_range * 10.0  # dominanza shadow (0–10)
        else:
            score += 8.0                                      # standard: indecisione neutra

        # 4. Contesto: trend exhaustion (newest first)
        if prev_candles and len(prev_candles) >= 3:
            cl = [p.get('close', 0) for p in prev_candles[:3]]
            trend_down = cl[0] < cl[1] < cl[2]
            trend_up   = cl[0] > cl[1] > cl[2]
            if ptype == 'dragonfly' and trend_down:    # liquidità presa in downtrend
                score += 25.0
            elif ptype == 'gravestone' and trend_up:   # liquidità presa in uptrend
                score += 25.0
            elif ptype == 'standard' and (trend_down or trend_up):
                score += 15.0                          # esaurimento direzionale generico
            elif cl[0] != cl[2]:
                score += 8.0                           # trend parziale

        # Normalizza su 0–100 (raw max = 95)
        return ptype, round(min(score / 95.0 * 100.0, 100.0), 1)

    @staticmethod
    def _check_hammer(candle, prev_candles=None):
        """
        Liquidity Hammer Detector — pro grade.
        Hard geometric requirements → score ≥ 70 su 100 per passare.
        prev_candles: newest first; se assente il market structure filter è bypassato.
        """
        o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
        total_range = h - l
        if total_range <= 0:
            return None, 0.0
        body = abs(c - o)
        if body <= 0:
            return None, 0.0

        lower      = min(o, c) - l
        upper      = h - max(o, c)
        body_ratio = body / total_range

        # ── Hard requirements ──────────────────────────────────────────────────
        # Corpo ultra-compatto (≤ 20% del range — classico hammer pulito)
        if body_ratio > 0.20:
            return None, 0.0
        # Lower shadow dominante (≥ 3× corpo — spike di liquidità reale)
        if lower / body < 3.0:
            return None, 0.0
        # Upper wick assoluto: se > corpo, non è hammer (struttura rotta)
        upper_to_body = upper / body
        if upper_to_body > 1.0:
            return None, 0.0
        # Close: se oltre 35% dal massimo del range, nessun rimbalzo reale
        dist_from_top = (h - c) / total_range
        if dist_from_top > 0.35:
            return None, 0.0
        # Volatilità: candela significativa rispetto all'ATR proxy
        if prev_candles:
            ranges = [abs(p.get('high', 0) - p.get('low', 0))
                      for p in prev_candles if p.get('high') and p.get('low')]
            if ranges:
                avg_rng = statistics.median(ranges)
                if avg_rng > 0 and total_range < avg_rng * 0.6:
                    return None, 0.0

        # ── Soft penalties (ex hard filters — crypto può avere wick impuri) ───
        # Penalità graduale se upper_to_body ∈ (0.30, 1.0)
        upper_penalty = max(0.0, (upper_to_body - 0.30) / 0.70) * 15.0
        # Penalità graduale se dist_from_top ∈ (0.15, 0.35)
        close_penalty = max(0.0, (dist_from_top - 0.15) / 0.20) * 15.0

        # ── Quality score (raw max ≈ 130) ─────────────────────────────────────

        # Shadow strength: 15 pts baseline a 3× (min), scala a 30 pts a 6×
        shadow_x = min(lower / body, 6.0)
        score  = 15.0 + (shadow_x - 3.0) / (6.0 - 3.0) * 15.0

        # Body compactness (vs hard limit 0.20): 0 → 20 pts
        score += (1.0 - body_ratio / 0.20) * 20.0

        # Close quality: 20 pts per dist ∈ [0%, 15%], 0 oltre (penalità gestita sotto)
        score += max(0.0, 1.0 - dist_from_top / 0.15) * 20.0

        # Struttura bullish: close > open (+5 pts)
        if c > o:
            score += 5.0

        # Market structure (0 / 15 / 30 pts — scoring, non hard filter)
        if prev_candles and len(prev_candles) >= 8:
            p_highs  = [p.get('high',  0) for p in prev_candles[:8]]
            p_closes = [p.get('close', 0) for p in prev_candles[:8]]
            sma_now       = sum(p_closes[:4]) / 4
            sma_prev      = sum(p_closes[4:8]) / 4
            sma_declining = sma_now < sma_prev
            lh_count      = sum(1 for i in range(min(4, len(p_highs) - 1))
                                if p_highs[i] < p_highs[i + 1])
            lh_structure  = lh_count >= 2
            score += (sma_declining * 15) + (lh_structure * 15)

        # Trend ribassista a breve (3-bar, newest first)
        if prev_candles and len(prev_candles) >= 3:
            cl = [p.get('close', 0) for p in prev_candles[:3]]
            if cl[0] < cl[1] < cl[2]:            # 3 barre strettamente in discesa
                score += 25.0
            elif cl[0] < cl[1] or cl[1] < cl[2]: # trend parziale
                score += 12.0
            elif len(prev_candles) >= 4:          # fallback solo se 3-bar non dà segnale
                if prev_candles[0].get('close', 0) < prev_candles[3].get('close', 0):
                    score += 15.0

        # Soft penalties (ex hard filters — ammorbiditi per crypto)
        score -= upper_penalty + close_penalty

        if score < 70.0:
            return None, 0.0

        # Normalizza su 0–100 (raw max ≈ 130)
        return 'hammer', round(min(score / 1.30, 100.0), 1)

    @staticmethod
    def _score_to_level(score):
        """Converte score continuo in tier operativo discreto.
        Soglie calibrate per il modello a moltiplicatore (final = L1 × confluenza):
        0 = invalid, 1 = WEAK (≥28), 2 = VALID (≥50), 3 = HIGH_PROB (≥72)
        """
        if score >= 72: return 3
        if score >= 50: return 2
        if score >= 28: return 1
        return 0

    def _detect_pattern(self, candle, thr, prev_candles=None):
        """3-Level Hierarchical Pattern Engine.
        L1 = detection (geometria), L2 = validazione mercato, L3 = probabilità esecuzione.
        Ogni livello è gate per il successivo.
        Ritorna (pattern_type, final_score, level_0_3) oppure (None, 0.0, 0).
        """
        # Normalizza prev_candles newest-first una volta sola — evita dipendenza da upstream
        if prev_candles and len(prev_candles) >= 2:
            if prev_candles[0].get('time', 0) < prev_candles[-1].get('time', 0):
                prev_candles = sorted(prev_candles, key=lambda x: x.get('time', 0), reverse=True)

        # ── LEVEL 1: Geometry (hard filter) ──────────────────────────────────
        l1_type, l1_score = self._check_hammer(candle, prev_candles)
        if not l1_type:
            l1_type, l1_score = self._check_doji(candle, thr, prev_candles)
        if not l1_type:
            return None, 0.0, 0                    # niente pattern geometrico
        if self._score_to_level(l1_score) == 0:
            return None, 0.0, 0                    # geometria troppo debole

        # ── LEVEL 2: Market Event (solo se L1 valido) ─────────────────────────
        l2_event, l2_score = self._detect_level2(candle, prev_candles, l1_type)
        if l2_score < 30:
            return None, 0.0, 0                    # nessun evento di mercato reale

        # ── LEVEL 3: Trade Setup (solo se L2 rilevante) ───────────────────────
        l3_setup, l3_prob = self._build_level3(l1_type, l1_score,
                                               l2_event, l2_score, prev_candles)
        if l3_setup == 'no_trade' and l3_prob < 40:
            return None, 0.0, 0                    # setup non operativo

        # ── Score finale: L1 detection × confluenza L2/L3 ─────────────────────
        # Modello a moltiplicatore: L1 è il segnale base, L2+L3 scalano la confidenza.
        # multiplier ∈ [0.50, 1.00]: 0.50 quando L2/L3 appena sopra i gate,
        #                             1.00 quando entrambi perfetti.
        confluence  = (l2_score * 0.55 + l3_prob * 0.45) / 100.0  # 0.0 → 1.0
        multiplier  = 0.50 + confluence * 0.50                      # 0.50 → 1.00
        final_score = round(min(l1_score * multiplier, 100.0), 1)
        return l1_type, final_score, self._score_to_level(final_score)

    @staticmethod
    def _pattern_context(pattern_type, prev_candles):
        """Ritorna un label leggibile che spiega cosa ha guidato il segnale.
        Separato dallo scoring: geometria vs contesto vs struttura.
        """
        if not prev_candles or len(prev_candles) < 3:
            return 'geometry_only'
        cl = [p.get('close', 0) for p in prev_candles[:3]]  # newest first
        trend_down = cl[0] < cl[1] < cl[2]
        trend_up   = cl[0] > cl[1] > cl[2]
        if pattern_type == 'dragonfly':
            if trend_down: return 'exhaustion_down'
            if cl[0] < cl[2]: return 'partial_down'
            return 'neutral'
        if pattern_type == 'gravestone':
            if trend_up: return 'exhaustion_up'
            if cl[0] > cl[2]: return 'partial_up'
            return 'neutral'
        if pattern_type == 'hammer':
            if trend_down: return 'reversal_down'
            return 'structure'
        # standard doji
        if trend_down: return 'indecision_down'
        if trend_up:   return 'indecision_up'
        return 'neutral'

    @staticmethod
    def _detect_level2(candle, prev_candles, l1_type=None):
        """Level 2 — Liquidity Event: cosa è successo sotto/sopra la candela.
        Ritorna (event_type, score_0_100).
        event_type: 'sweep_bull' | 'sweep_bear' | 'exhaustion' | 'indecision'
        """
        o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
        total_range = h - l
        if total_range <= 0:
            return 'indecision', 0.0

        lower     = min(o, c) - l
        upper     = h - max(o, c)
        close_pos = (c - l) / total_range  # 0 = at low, 1 = at high

        avg_range = None
        if prev_candles:
            rng = [abs(p.get('high', 0) - p.get('low', 0))
                   for p in prev_candles if p.get('high') and p.get('low')]
            if rng:
                avg_range = statistics.median(rng)  # robusta agli spike

        if avg_range is None or avg_range <= 0:
            return 'indecision', 20.0   # ATR non disponibile: nessun contesto storico

        thr_wick = avg_range * 0.35  # wick significativo se > 35% dell'ATR proxy

        # SWEEP BULLISH: spike aggressivo sotto + recupero forte
        if lower > thr_wick and close_pos > 0.70:
            wick_x   = min(lower / thr_wick, 2.0) / 2.0  # 0–1
            recovery = (close_pos - 0.70) / 0.30                     # 0–1
            score    = wick_x * 60 + recovery * 40
            return 'sweep_bull', round(min(score, 100.0), 1)

        # SWEEP BEARISH: spike aggressivo sopra + rejection forte
        if upper > thr_wick and close_pos < 0.30:
            wick_x    = min(upper / thr_wick, 2.0) / 2.0
            rejection = (0.30 - close_pos) / 0.30
            score     = wick_x * 60 + rejection * 40
            return 'sweep_bear', round(min(score, 100.0), 1)

        # EXHAUSTION: compressione volatilità + contesto trend
        if total_range < avg_range * 0.80:
            trend_ctx = False
            if prev_candles and len(prev_candles) >= 3:
                cl = [p.get('close', 0) for p in prev_candles[:3]]
                trend_ctx = (cl[0] < cl[1] < cl[2]) or (cl[0] > cl[1] > cl[2])
            compression = max(0.0, (avg_range * 0.80 - total_range) / (avg_range * 0.80))
            score = compression * 70 + (30 if trend_ctx else 10)
            return 'exhaustion', round(min(score, 100.0), 1)

        # INDECISION: default
        ratio = total_range / max(avg_range, 1e-9)
        score = max(10.0, (1.0 - min(ratio, 1.5) / 1.5) * 50 + 20)
        return 'indecision', round(min(score, 100.0), 1)

    @staticmethod
    def _build_level3(l1_type, l1_score, l2_event, l2_score, prev_candles):
        """Level 3 — Trade Setup: ha senso entrare?
        Ritorna (setup_type, probability_0_100).
        setup_type: 'long_reversal' | 'short_reversal' | 'no_trade'
        """
        trend_down = trend_up = False
        if prev_candles and len(prev_candles) >= 3:
            cl = [p.get('close', 0) for p in prev_candles[:3]]
            trend_down = cl[0] < cl[1] < cl[2]
            trend_up   = cl[0] > cl[1] > cl[2]
        elif prev_candles and len(prev_candles) >= 2:
            cl = [p.get('close', 0) for p in prev_candles[:2]]
            trend_down = cl[0] < cl[1]
            trend_up   = cl[0] > cl[1]

        # L3 misura la qualità del market event + allineamento trend
        # L1 NON entra qui — viene pesato separatamente nel final_score di _detect_pattern
        base        = l2_score * 0.60   # market event quality domina l3_prob
        trend_bonus = 40.0 if (trend_down or trend_up) else 0.0

        # Sweep senza trend confermato = trap potenziale — filtrato prima delle long/short
        if l2_event == 'sweep_bull' and not trend_down:
            return 'no_trade', round(base * 0.30, 1)
        if l2_event == 'sweep_bear' and not trend_up:
            return 'no_trade', round(base * 0.30, 1)

        # Long reversal: sweep bullish in downtrend O esaurimento in downtrend
        long_signal = (
            (l2_event == 'sweep_bull' and l1_type in ('hammer', 'dragonfly') and trend_down) or
            (l2_event == 'exhaustion'  and trend_down)
        )
        if long_signal:
            return 'long_reversal', round(min(base + trend_bonus, 100.0), 1)

        # Short reversal: sweep bearish in uptrend O esaurimento in uptrend
        short_signal = (
            (l2_event == 'sweep_bear' and l1_type == 'gravestone' and trend_up) or
            (l2_event == 'exhaustion'  and trend_up)
        )
        if short_signal:
            return 'short_reversal', round(min(base + trend_bonus, 100.0), 1)

        # Nessun setup chiaro
        return 'no_trade', round(base * 0.35, 1)

    def _process_candle(self, symbol, tf, c, bars_ago, prev_candles, price, volume, change):
        """Engine di detection unificato (WS + polling).
        Ritorna coin dict se pattern valido + non in cooldown, altrimenti None."""
        if c is None:
            return None
        if bars_ago == 0 and not self._is_candle_mature(c, tf):
            return None

        pcfg         = (self._live_config or {}).get('pattern', {})
        thr          = pcfg.get('doji_threshold', self.doji_threshold)
        min_level    = int(pcfg.get('min_pattern_level', 1))   # configurable: 1/2/3
        doji_type, signal_score, pattern_level = self._detect_pattern(
            c, thr, prev_candles if bars_ago == 1 else None)
        if not doji_type or pattern_level < min_level:
            return None
        if doji_type == 'hammer' and bars_ago != 1:
            return None
        enabled = pcfg.get('enabled_patterns')
        if enabled and doji_type not in enabled:
            return None

        # L2 + L3 (calcolati fuori dal lock — pura logica, nessuno stato condiviso)
        ctx_candles = prev_candles if bars_ago == 1 else None
        l2_event, l2_score = self._detect_level2(c, ctx_candles, doji_type)
        l3_setup,  l3_prob  = self._build_level3(doji_type, signal_score,
                                                  l2_event, l2_score, ctx_candles)

        full_key     = f"{symbol}_{tf}_{c.get('time', 0)}"  # dedup: stessa barra esatta
        cooldown_key = f"{symbol}_{tf}"                       # throttle: symbol+tf indip. da barra
        now_ts = time.time()

        coin = None
        with self._lock:
            seen_at = self._recent_seen.get(full_key)
            if seen_at and now_ts - seen_at < 300:
                return None
            if not self.is_in_cooldown(cooldown_key):
                self.mark_alerted(cooldown_key)
                # TTL sempre attivo: elimina entry scadute ad ogni alert (non solo a 500)
                self._recent_seen = {k: v for k, v in self._recent_seen.items()
                                     if now_ts - v < 300}
                self._recent_seen[full_key] = now_ts
                coin = {
                    'symbol':       symbol,
                    'tf':           tf,
                    'price':        price,
                    'volume':       volume,
                    'change_pct':   change,
                    'bars_ago':     bars_ago,
                    # 3-level structure
                    'level_1': {'type': doji_type,  'score': signal_score},
                    'level_2': {'event': l2_event,  'score': l2_score},
                    'level_3': {'setup': l3_setup,  'probability': l3_prob},
                    'pattern_level': pattern_level,   # 1=WEAK 2=VALID 3=HIGH_PROB
                    # backward compat
                    'doji_type':    doji_type,
                    'signal_score': signal_score,
                    'context':      self._pattern_context(doji_type, ctx_candles),
                }
        return coin

    @staticmethod
    def _is_candle_mature(candle, interval):
        """True se almeno 90% del TF è trascorso dall'apertura della candela."""
        tf_secs = TF_SECONDS.get(interval, 3600)
        elapsed = time.time() - candle.get('time', 0)
        return elapsed >= tf_secs * CANDLE_MATURITY

    # ── WebSocket callback ────────────────────────────────────────────────────

    def _on_kline(self, symbol, interval, candle, is_closed):
        if not self.enabled:
            return
        if interval not in self.scan_tfs:
            return
        if not self._is_in_schedule():
            return

        klines = self._ws_manager.get_klines(symbol, interval)
        ticker = self._ws_manager.get_all_tickers().get(symbol, {})
        if ticker.get('volume_24h', 0) < self.min_volume_24h:
            return

        price  = ticker.get('price') or candle['close']
        volume = ticker.get('volume_24h', 0)
        change = ticker.get('change_24h', 0.0)

        # Separa forming da closed — difensivo contro variazioni del WS buffer
        if not is_closed and klines and klines[-1].get('time') == candle.get('time'):
            closed_klines = klines[:-1]
        else:
            closed_klines = klines

        attuale    = (klines[-1] if klines else candle) if is_closed else candle
        precedente = (klines[-2]        if is_closed and len(klines) >= 2 else
                      closed_klines[-1] if closed_klines else None)

        for c, bars_ago in [(attuale, 0), (precedente, 1)]:
            # prev_candles: 10 candele chiuse prima del candle analizzato, newest first
            # Costruito SOLO da closed_klines — nessuna contaminazione da barre in corso
            if bars_ago == 1:
                p_idx  = len(closed_klines) - (2 if is_closed else 1)
                start  = max(0, p_idx - 10)
                prev_c = list(reversed(closed_klines[start:p_idx]))
            else:
                prev_c = None
            coin = self._process_candle(symbol, interval, c, bars_ago, prev_c, price, volume, change)
            if coin:
                try:
                    self._alert_queue.put_nowait([coin])
                except queue.Full:
                    try:
                        self._alert_queue.get_nowait()   # scarta oldest
                        self._alert_queue.put_nowait([coin])
                        print(f'⚠️ Pattern: queue full — dropped oldest, queued {symbol} {interval}')
                    except Exception:
                        print(f'⚠️ Pattern: queue full — dropped {symbol} {interval}')

    # ── REST helpers (polling fallback) ───────────────────────────────────────

    def _fetch_tickers(self):
        try:
            r = requests.get('https://api.bybit.com/v5/market/tickers',
                             params={'category': 'linear'}, timeout=10)
            data = r.json()
            if data.get('retCode') != 0:
                return []
            result = []
            for item in data['result']['list']:
                if not item['symbol'].endswith('USDT'):
                    continue
                price = float(item.get('lastPrice', 0) or 0)
                vol   = float(item.get('turnover24h', 0) or 0)
                if price <= 0 or vol < self.min_volume_24h:
                    continue
                result.append({
                    'symbol': item['symbol'], 'price': price, 'volume': vol,
                    'change_pct': float(item.get('price24hPcnt', 0) or 0) * 100,
                })
            result.sort(key=lambda x: x['volume'], reverse=True)
            return result[:TOP_KLINE_SYMBOLS]
        except Exception as e:
            print(f'❌ Pattern: fetch tickers: {e}')
            return []

    def _fetch_klines(self, symbol, tf):
        try:
            r = requests.get('https://api.bybit.com/v5/market/kline',
                             params={'category': 'linear', 'symbol': symbol,
                                     'interval': tf, 'limit': 8},
                             timeout=10)
            data = r.json()
            if data.get('retCode') != 0:
                return []
            # API returns newest first; reverse to ascending, skip forming (last after reversal)
            raw = list(reversed(data['result']['list']))[:-1]
            return [{'time':   int(c[0]) // 1000,
                     'open':   float(c[1]), 'high':   float(c[2]),
                     'low':    float(c[3]), 'close':  float(c[4]),
                     'volume': float(c[5])}
                    for c in raw]
        except Exception:
            return []

    def _fetch_candle_pair(self, symbol, tf):
        """Returns (attuale, precedente, prev_candles): attuale=forming, precedente=last closed,
        prev_candles=4 candele prima di precedente (newest first) per il trend check hammer."""
        try:
            r = requests.get('https://api.bybit.com/v5/market/kline',
                             params={'category': 'linear', 'symbol': symbol,
                                     'interval': tf, 'limit': 12},
                             timeout=10)
            data = r.json()
            if data.get('retCode') != 0 or not data['result']['list']:
                return None, None, []
            # Bybit restituisce di norma newest-first; sort solo se necessario (O(1) check)
            lst = data['result']['list']
            if len(lst) >= 2 and int(lst[0][0]) < int(lst[1][0]):
                lst = sorted(lst, key=lambda x: int(x[0]), reverse=True)
            raw = lst
            def _parse(c):
                return {'time': int(c[0]) // 1000, 'open': float(c[1]),
                        'high': float(c[2]), 'low': float(c[3]),
                        'close': float(c[4]), 'volume': float(c[5])}
            attuale    = _parse(raw[0]) if len(raw) > 0 else None
            precedente = _parse(raw[1]) if len(raw) > 1 else None
            # raw[2..11] = fino a 10 candele chiuse prima dell'hammer (newest first)
            prev_candles = [_parse(raw[i]) for i in range(2, min(12, len(raw)))]
            return attuale, precedente, prev_candles
        except Exception:
            return None, None, []

    # ── polling scan ─────────────────────────────────────────────────────────

    def scan(self):
        if not self.enabled:
            return []
        if self._ws_manager and self._ws_manager.ready.is_set():
            return []
        print(f'🕯 Pattern Scanner — polling scan (tf={",".join(self.scan_tfs)})...')
        found = []
        try:
            tickers = self._fetch_tickers()
            self._last_count = len(tickers)
            for i, ticker in enumerate(tickers):
                symbol = ticker['symbol']
                price  = ticker['price']
                volume = ticker['volume']
                change = ticker.get('change_pct', 0.0)
                for tf in self.scan_tfs:
                    attuale, precedente, prev_candles = self._fetch_candle_pair(symbol, tf)
                    for bars_ago, c in [(0, attuale), (1, precedente)]:
                        coin = self._process_candle(
                            symbol, tf, c, bars_ago, prev_candles, price, volume, change)
                        if coin:
                            found.append(coin)
                            break   # attuale ha priorità; se trova pattern non controlla precedente
                if (i + 1) % 10 == 0:
                    time.sleep(0.3)
            found = found[:self.max_coins_per_alert]
            if found:
                self.send_alert(found)
            print(f'🕯 Pattern: {len(found)} Doji trovati')
            return found
        except Exception as e:
            print(f'❌ Pattern scanner error: {e}')
            return []

    # ── alert ─────────────────────────────────────────────────────────────────

    def send_alert(self, coins):
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            from alert_utils import send_photo, send_text, get_chart, log_alert, fmt_vol
        except ImportError:
            return
        for coin in coins[:3]:
            sym       = coin['symbol']
            tf        = coin.get('tf', self.scan_tfs[0])
            tf_label  = TF_LABEL.get(tf, tf)
            doji_type    = coin.get('doji_type', 'standard')
            emoji        = TYPE_EMOJI.get(doji_type, '➕')
            signal_score = coin.get('signal_score', 0.0)
            bars_ago     = coin.get('bars_ago', 1)
            change       = coin.get('change_pct', 0.0)
            base         = (self.base_url or 'https://cryptoscannerpro.com').rstrip('/')
            pattern_level = coin.get('pattern_level', 1)
            TIER_LABEL  = {1: '🔵 WEAK', 2: '🟠 VALID', 3: '🔴 HIGH PROB'}
            tier_str    = TIER_LABEL.get(pattern_level, '')
            l1 = coin.get('level_1', {'type': doji_type, 'score': signal_score})
            l2 = coin.get('level_2', {'event': 'indecision', 'score': 0.0})
            l3 = coin.get('level_3', {'setup': 'no_trade',   'probability': 0.0})
            L2_LABEL = {
                'sweep_bull': 'Sweep Bullish 🔻',
                'sweep_bear': 'Sweep Bearish 🔺',
                'exhaustion': 'Esaurimento',
                'indecision': 'Indecisione',
            }
            L3_LABEL = {
                'long_reversal':  '🟢 Long Reversal',
                'short_reversal': '🔴 Short Reversal',
                'no_trade':       '⚪ No Setup',
            }
            l2_label = L2_LABEL.get(l2['event'], l2['event'])
            l3_label = L3_LABEL.get(l3['setup'], l3['setup'])

            lines = [
                f'{emoji} {doji_type.capitalize()} {tier_str} — {tf_label}',
                '',
                '------------------------------------------------',
                f'- Coin: {sym}',
                f'- Var: {change:+.2f}%',
                f'- Volume: {fmt_vol(coin.get("volume", 0))}',
                f'- L1 Geometria: {l1["type"].capitalize()} — {l1["score"]:.0f}/100',
                f'- L2 Evento: {l2_label} — {l2["score"]:.0f}/100',
                f'- L3 Setup: {l3_label} — {l3["probability"]:.0f}%',
                f'- Candela: {"Attuale" if bars_ago == 0 else "Precedente"}',
                '------------------------------------------------',
                '',
                f'<a href="https://www.bybit.com/trade/usdt/{sym}">- View Bybit</a>',
                f'<a href="{base}/mtf?symbol={sym}">- View Desktop</a>',
                f'<a href="{base}/chart?symbol={sym}&layout=1x1">- View Mobile</a>',
            ]
            caption = '\n'.join(lines)
            img = get_chart(sym, interval=tf, signal={'type': 'ema'})
            if img:
                send_photo(self.telegram_token, self.telegram_chat_id, img, caption)
            else:
                send_text(self.telegram_token, self.telegram_chat_id, caption)
            log_alert(sym, f'Pattern {doji_type.capitalize()}', emoji=emoji,
                      note=f'L1={l1["score"]:.0f} L2={l2_label}({l2["score"]:.0f}) L3={l3["setup"]}({l3["probability"]:.0f}%)',
                      tf=tf_label, screenshot=img)
            print(f'🕯 Pattern alert: {sym} {tf_label} {doji_type} {"Attuale" if bars_ago == 0 else "Precedente"}')

    # ── status ────────────────────────────────────────────────────────────────

    def get_today_alerts(self):
        today = datetime.now(timezone.utc).date()
        symbols = set()
        with self._lock:
            for key, dt in self.last_alerts.items():
                if dt.date() == today:
                    # key: BTCUSDT_D_1234567890
                    symbols.add(key.split('_')[0])
        return list(symbols)

    def get_monitored_count(self):
        return self._last_count
