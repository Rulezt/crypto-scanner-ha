"""
TRUMP MARKET RADAR — v1 (rule-based, no AI)

Monitora i post di Donald Trump su Truth Social (via trumpstruth.org RSS), riconosce
quelli finanziariamente rilevanti con un filtro keyword/entita + pattern sui verbi +
euristica di novita + deduplica per similarita, assegna uno score 0-100 e — se supera
la soglia — invia un alert Telegram (stesso bot/chat degli altri scanner).

Nessun trading automatico. Il sistema e' puramente informativo: dice "guarda questo
mercato", la decisione la prende l'utente.

L'analisi AI NON e' inclusa in questa v1: il punto di innesto e' _classify(), che oggi
e' interamente rule-based.
"""
import os
import re
import json
import html
import time
import sqlite3
import logging
import threading
import difflib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # per 'import alert_utils'

logger = logging.getLogger(__name__)

FEED_URL      = 'https://www.trumpstruth.org/feed'
TRUTH_NS      = '{https://truthsocial.com/ns}'
DB_PATH       = '/data/trump_radar.db'
KEYWORDS_PATH = '/data/trump_keywords.json'
KEYWORDS_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trump_keywords_default.json')
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

# Paesi citati di frequente da Trump — usati per il bonus "novita" (nuovo paese nel testo)
COUNTRIES = [
    'china', 'canada', 'mexico', 'japan', 'india', 'russia', 'iran', 'israel', 'ukraine',
    'germany', 'france', 'brazil', 'south korea', 'north korea', 'taiwan', 'vietnam',
    'saudi arabia', 'venezuela', 'colombia', 'panama', 'greenland', 'denmark', 'europe',
    'european union', ' eu ',
]
MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
          'september', 'october', 'november', 'december']
DATE_HINTS = MONTHS + ['today', 'tomorrow', 'tonight', 'monday', 'tuesday', 'wednesday',
                       'thursday', 'friday', 'next week', 'this week', 'coming days']

PCT_RE   = re.compile(r'\b\d{1,3}(?:\.\d+)?\s*%')
MONEY_RE = re.compile(r'\$\s?\d[\d,\.]*\s*(?:billion|million|trillion|b\b|m\b|k\b)?', re.I)


def _strip_html(s):
    if not s:
        return ''
    s = re.sub(r'<[^>]+>', ' ', s)
    s = html.unescape(s)
    return re.sub(r'\s+', ' ', s).strip()


def _norm(s):
    return re.sub(r'[^a-z0-9 ]+', ' ', re.sub(r'\s+', ' ', (s or '').lower())).strip()


class TrumpRadarScanner:
    def __init__(self, telegram_config, live_config=None, ws_manager=None,
                 enabled=True, poll_interval_minutes=1,
                 alert_score_threshold=50, info_score_threshold=30,
                 similarity_threshold=0.85, **kwargs):
        self.telegram_token   = telegram_config.get('token', '')
        self.telegram_chat_id = telegram_config.get('chat_id', '')
        self._live_config     = live_config
        self.enabled          = enabled
        self.alert_threshold  = float(alert_score_threshold)
        self.info_threshold   = float(info_score_threshold)
        self.similarity_threshold = float(similarity_threshold)
        self._lock            = threading.Lock()
        self.last_fetch_ts    = 0
        self.last_fetch_ok    = None
        self.last_error       = ''

        self._init_db()
        self._keywords = self._load_keywords()
        logger.info(f"📻 TrumpRadarScanner init — soglia alert={self.alert_threshold} "
                    f"categorie={len(self._keywords.get('categories', {}))}")

    # ── storage ──────────────────────────────────────────────────────────────
    def _conn(self):
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id TEXT PRIMARY KEY, ts INTEGER, source TEXT, text TEXT, url TEXT,
                    status TEXT, categories TEXT, action_level TEXT, novelty INTEGER,
                    score INTEGER, tickers TEXT, snapshot TEXT, reason TEXT, created_at INTEGER
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT, ts INTEGER,
                    score INTEGER, sent_ok INTEGER
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_posts_ts ON posts(ts DESC)")

    # ── keyword file ─────────────────────────────────────────────────────────
    def _load_keywords(self):
        if os.path.exists(KEYWORDS_PATH):
            try:
                with open(KEYWORDS_PATH, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"trump keywords load fail ({KEYWORDS_PATH}), uso il default: {e}")
        try:
            with open(KEYWORDS_DEFAULT, 'r') as f:
                data = json.load(f)
            try:
                os.makedirs(os.path.dirname(KEYWORDS_PATH), exist_ok=True)
                with open(KEYWORDS_PATH, 'w') as out:
                    json.dump(data, out, indent=2)
            except Exception:
                pass
            return data
        except Exception as e:
            logger.error(f"trump keywords default illeggibile: {e}")
            return {'categories': {}, 'action_levels': {}}

    def reload_keywords(self):
        self._keywords = self._load_keywords()

    # ── schedule (riusa la finestra oraria globale dello scanner) ─────────────
    def _in_schedule(self):
        try:
            g = (self._live_config or {}).get('general', {})
            s, e = g.get('schedule_start', ''), g.get('schedule_end', '')
            if not s or not e:
                return True
            off = float(g.get('utc_offset') or 0)
            now = datetime.utcnow() + timedelta(hours=off)
            sh, sm = map(int, s.split(':'))
            eh, em = map(int, e.split(':'))
            nm, stm, etm = now.hour * 60 + now.minute, sh * 60 + sm, eh * 60 + em
            return stm <= nm <= etm if stm <= etm else (nm >= stm or nm <= etm)
        except Exception:
            return True

    # ── classification ──────────────────────────────────────────────────────
    def _match_categories(self, low):
        out = []
        for name, cat in self._keywords.get('categories', {}).items():
            hits = [t for t in cat.get('terms', []) if t.lower() in low]
            if hits:
                out.append({'name': name, 'weight': cat.get('weight', 10),
                            'terms': hits, 'assets': cat.get('assets', {})})
        out.sort(key=lambda x: x['weight'], reverse=True)
        return out

    def _action_level(self, low):
        best, best_w = 'COMMENT', 1
        for name, lvl in self._keywords.get('action_levels', {}).items():
            for pat in lvl.get('patterns', []):
                if pat.lower() in low:
                    if lvl.get('weight', 0) > best_w:
                        best, best_w = name, lvl.get('weight', 0)
                    break
        return best, best_w

    def _recent_texts(self, cat_names, days=14, limit=60):
        since = int(time.time()) - days * 86400
        with self._conn() as c:
            rows = c.execute(
                "SELECT text, categories FROM posts WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (since, limit)).fetchall()
        same, allt = [], []
        cset = set(cat_names)
        for r in rows:
            allt.append(r['text'] or '')
            try:
                rc = set(json.loads(r['categories'] or '[]'))
            except Exception:
                rc = set()
            if rc & cset:
                same.append(r['text'] or '')
        return same, allt

    def _novelty(self, low, same_cat_texts):
        score, bits = 0, []
        if PCT_RE.search(low):
            score += 8; bits.append('percentuale')
        if MONEY_RE.search(low):
            score += 4; bits.append('cifra $')
        if any(h in low for h in DATE_HINTS):
            score += 4; bits.append('data')
        prev = ' '.join(_norm(t) for t in same_cat_texts)
        new_countries = [c.strip() for c in COUNTRIES if c in low and c.strip() not in prev]
        if new_countries:
            score += 4; bits.append('paese nuovo: ' + new_countries[0])
        return min(score, 18), bits

    def _is_duplicate(self, text_norm, cand_texts):
        # troppo corto per giudicare: non e' un doppione, e' solo un post breve
        if len(text_norm) < 120:
            return False, 0.0
        best = 0.0
        for t in cand_texts:
            tn = _norm(t)
            if len(tn) < 120:
                continue
            sm = difflib.SequenceMatcher(None, text_norm, tn)
            if sm.quick_ratio() < self.similarity_threshold:
                continue
            r = sm.ratio()
            if r > best:
                best = r
            if best >= self.similarity_threshold:
                break
        return best >= self.similarity_threshold, best

    def _assets_for(self, cats):
        direct, sector, indirect = [], [], []
        for cat in cats:
            a = cat.get('assets', {})
            direct += a.get('direct', [])
            sector += a.get('sector', [])
            indirect += a.get('indirect', [])
        dedup = lambda seq: list(dict.fromkeys(seq))
        return dedup(direct), dedup(sector), dedup(indirect)

    def _classify(self, text):
        """Rule-based. Ritorna dict con status/score/categories/action_level/tickers/reason.
        Punto di innesto per l'AI in una v2 (stesso contratto di ritorno)."""
        low = ' ' + text.lower() + ' '
        cats = self._match_categories(low)
        if not cats:
            return {'status': 'archived', 'score': 0, 'categories': [], 'action_level': 'COMMENT',
                    'novelty': 0, 'tickers': {}, 'reason': 'nessuna keyword finanziaria'}

        cat_names = [c['name'] for c in cats]
        same_cat, all_texts = self._recent_texts(cat_names)

        is_dup, sim = self._is_duplicate(_norm(text), same_cat)
        has_number = bool(PCT_RE.search(low) or MONEY_RE.search(low))
        if is_dup and not has_number:
            return {'status': 'duplicate', 'score': 0, 'categories': cat_names,
                    'action_level': 'COMMENT', 'novelty': 0, 'tickers': {},
                    'reason': f'quasi identico a un post recente (sim {sim:.2f})'}

        action, action_w = self._action_level(low)
        novelty, novelty_bits = self._novelty(low, same_cat)

        # ── scoring 0-100 (tunabile) ──────────────────────────────────────────
        action_score   = min(35, round(action_w * 1.4))
        category_score  = min(25, round(cats[0]['weight'] * 1.2))
        multi_bonus     = min(6, (len(cats) - 1) * 3)
        # market_moving_bonus viene aggiunto dopo lo snapshot (in scan())
        score = action_score + category_score + multi_bonus + novelty
        score = max(0, min(100, score))

        direct, sector, indirect = self._assets_for(cats)
        reason = f"{action} · {', '.join(cat_names[:3])}"
        if novelty_bits:
            reason += ' · novità: ' + ', '.join(novelty_bits)

        return {
            'status': 'info' if score < self.info_threshold else 'watch',
            'score': score, 'categories': cat_names, 'action_level': action,
            'novelty': novelty, 'reason': reason,
            'tickers': {'direct': direct, 'sector': sector, 'indirect': indirect},
        }

    # ── market snapshot (best-effort, mai bloccante) ─────────────────────────
    def _crypto_snapshot(self, symbols):
        out = {}
        for sym in symbols[:4]:
            try:
                r = requests.get('https://api.bybit.com/v5/market/tickers',
                                 params={'category': 'linear', 'symbol': sym}, timeout=6)
                lst = r.json().get('result', {}).get('list', [])
                if lst:
                    pct = float(lst[0].get('price24hPcnt', 0)) * 100
                    out[sym.replace('USDT', '')] = {'price': float(lst[0]['lastPrice']), 'pct': round(pct, 2)}
            except Exception:
                pass
        return out

    def _snapshot(self, tickers):
        """v1: solo crypto (Bybit, real-time e senza rate-limit). Le quotazioni equity
        richiedono una fonte con API key affidabile — rimandate a una v2 (Yahoo senza
        chiave viene rate-limitato per IP). Per gli asset azionari l'alert elenca
        comunque i ticker da guardare, il grafico lo apre l'utente."""
        key = (tuple(sorted(t for t in tickers if t.endswith('USDT'))), int(time.time()) // 60)
        if getattr(self, '_snap_cache_key', None) == key:
            return self._snap_cache
        snap = self._crypto_snapshot([t for t in tickers if t.endswith('USDT')])
        self._snap_cache_key, self._snap_cache = key, snap
        return snap

    # ── telegram ────────────────────────────────────────────────────────────
    def _emoji(self, score):
        if score >= 85:
            return '🔴'
        if score >= 70:
            return '🟠'
        return '👀'

    def _send_alert(self, text, url, cls, snapshot):
        if not self.telegram_token or not self.telegram_chat_id:
            return False
        from alert_utils import send_text
        score = cls['score']
        watch = list(dict.fromkeys(cls['tickers'].get('direct', []) + cls['tickers'].get('sector', [])))[:8]
        quote = text if len(text) <= 280 else text[:277] + '…'
        lines = [
            f"{self._emoji(score)} <b>TRUMP — {cls['categories'][0].replace('_', ' ').title()}</b>   score {score}/100",
            '', f"«{html.escape(quote)}»", '',
            f"Guarda: {', '.join(watch) if watch else '—'}",
            f"Azione: {cls['action_level']}   Novità: {'sì' if cls['novelty'] >= 8 else 'no'}",
        ]
        if snapshot:
            parts = []
            for tk, d in snapshot.items():
                if d.get('pct') is not None:
                    parts.append(f"{tk} {d['pct']:+.2f}%")
                else:
                    parts.append(f"{tk} {d['price']:g}")
            lines += ['', 'Prezzo ora: ' + '   '.join(parts)]
        lines += ['', f"⏱ {datetime.now(timezone.utc):%H:%M} UTC   🔗 {url}"]
        try:
            send_text(self.telegram_token, self.telegram_chat_id, '\n'.join(lines))
            return True
        except Exception as e:
            logger.error(f"trump radar telegram error: {e}")
            return False

    # ── main loop entry ─────────────────────────────────────────────────────
    def _fetch_items(self):
        r = requests.get(FEED_URL, headers={'User-Agent': UA}, timeout=12)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = []
        for it in root.iter('item'):
            guid = (it.findtext('guid') or it.findtext('link') or '').strip()
            if not guid:
                continue
            desc = it.findtext('description') or ''
            title = it.findtext('title') or ''
            body = _strip_html(desc)
            if not body:
                t = _strip_html(title)
                body = '' if t.lower().startswith('[no title]') else t
            try:
                ts = int(parsedate_to_datetime(it.findtext('pubDate')).timestamp())
            except Exception:
                ts = int(time.time())
            url = (it.findtext(TRUTH_NS + 'originalUrl') or it.findtext('link') or guid).strip()
            items.append({'guid': guid, 'ts': ts, 'text': body, 'url': url})
        return items

    def scan(self):
        if not self.enabled:
            return {'skipped': 'disabled'}
        with self._lock:
            try:
                items = self._fetch_items()
                self.last_fetch_ok = True
                self.last_error = ''
            except Exception as e:
                self.last_fetch_ok = False
                self.last_error = str(e)
                logger.warning(f"trump radar fetch fail: {e}")
                return {'error': str(e)}
            self.last_fetch_ts = int(time.time())

            with self._conn() as c:
                known = {r['id'] for r in c.execute(
                    "SELECT id FROM posts ORDER BY ts DESC LIMIT 400").fetchall()}

            new_items = [i for i in items if i['guid'] not in known]
            processed, alerted = 0, 0
            # dal piu' vecchio al piu' recente, cosi' _recent_texts vede la storia giusta
            for it in sorted(new_items, key=lambda x: x['ts']):
                try:
                    res = self._process(it)
                    processed += 1
                    if res == 'alerted':
                        alerted += 1
                except Exception as e:
                    logger.error(f"trump radar process error {it['guid']}: {e}")
            if processed or alerted:
                logger.info(f"📻 trump radar: {len(items)} nel feed, {processed} nuovi, {alerted} alert")
            return {'fetched': len(items), 'new': processed, 'alerted': alerted}

    def _process(self, it):
        text = it['text']
        cls = self._classify(text)
        snapshot, status = {}, cls['status']
        score = cls['score']

        if status in ('watch', 'info'):
            all_tickers = (cls['tickers'].get('direct', []) + cls['tickers'].get('sector', []))
            snapshot = self._snapshot(all_tickers)
            # market_moving_bonus: se un ticker taggato si muove gia' oltre ~1.5%
            mm = 0
            for d in snapshot.values():
                if d.get('pct') is not None and abs(d['pct']) >= 1.5:
                    mm = max(mm, min(15, int(abs(d['pct']) * 3)))
            if mm:
                score = min(100, score + mm)
                cls['reason'] += f' · mercato in movimento (+{mm})'
            status = 'watch' if score >= self.alert_threshold else ('info' if score < self.info_threshold else 'watch')

        do_alert = (score >= self.alert_threshold and status == 'watch'
                    and self._in_schedule()
                    and (self._live_config or {}).get('telegram', {}).get('enabled', True))
        sent = False
        if do_alert:
            sent = self._send_alert(text, it['url'], {**cls, 'score': score}, snapshot)
            status = 'alerted'

        with self._conn() as c:
            c.execute("""INSERT OR REPLACE INTO posts
                (id, ts, source, text, url, status, categories, action_level, novelty,
                 score, tickers, snapshot, reason, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (it['guid'], it['ts'], 'truth', text, it['url'], status,
                 json.dumps(cls['categories']), cls['action_level'], cls['novelty'],
                 score, json.dumps(cls['tickers']), json.dumps(snapshot),
                 cls['reason'], int(time.time())))
            if do_alert:
                c.execute("INSERT INTO alerts (post_id, ts, score, sent_ok) VALUES (?,?,?,?)",
                          (it['guid'], int(time.time()), score, 1 if sent else 0))
        return status

    # ── API per la pagina ───────────────────────────────────────────────────
    def get_recent(self, limit=60):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM posts ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            out.append({
                'id': r['id'], 'ts': r['ts'], 'text': r['text'], 'url': r['url'],
                'status': r['status'], 'score': r['score'], 'action_level': r['action_level'],
                'novelty': r['novelty'], 'reason': r['reason'],
                'categories': json.loads(r['categories'] or '[]'),
                'tickers': json.loads(r['tickers'] or '{}'),
                'snapshot': json.loads(r['snapshot'] or '{}'),
            })
        return out

    def get_stats(self):
        today = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                       microsecond=0).timestamp())
        with self._conn() as c:
            def n(q, *a):
                return c.execute(q, a).fetchone()[0]
            return {
                'enabled': self.enabled,
                'alert_threshold': self.alert_threshold,
                'info_threshold': self.info_threshold,
                'last_fetch_ts': self.last_fetch_ts,
                'last_fetch_ok': self.last_fetch_ok,
                'last_error': self.last_error,
                'today_total': n("SELECT COUNT(*) FROM posts WHERE ts>=?", today),
                'today_archived': n("SELECT COUNT(*) FROM posts WHERE ts>=? AND status='archived'", today),
                'today_watch': n("SELECT COUNT(*) FROM posts WHERE ts>=? AND status='watch'", today),
                'today_alerted': n("SELECT COUNT(*) FROM posts WHERE ts>=? AND status='alerted'", today),
                'total_posts': n("SELECT COUNT(*) FROM posts"),
            }

    def classify_preview(self, text):
        """Classifica un testo incollato SENZA salvarlo (per il tuning della pagina)."""
        text = _strip_html(text)[:2000]
        cls = self._classify(text)
        snap = {}
        if cls['status'] in ('watch', 'info'):
            snap = self._snapshot(cls['tickers'].get('direct', []) + cls['tickers'].get('sector', []))
        return {**cls, 'text': text, 'snapshot': snap}
