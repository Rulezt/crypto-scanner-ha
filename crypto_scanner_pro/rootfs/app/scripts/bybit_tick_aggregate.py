#!/usr/bin/env python3
"""
Scarica lo storico tick-by-tick pubblico di Bybit (public.bybit.com/trading/<symbol>/)
per un simbolo e lo aggrega in candele OHLCV a timeframe custom in minuti — utile
per i timeframe che l'API kline di Bybit non offre (es. 6m/7m/8m/9m, l'API supporta
solo 1/3/5/15/30/60/120/240/360/720/D/W/M).

Uso:
    python3 bybit_tick_aggregate.py --symbol SOLUSDT --start 2025-01-01 --end 2025-01-31 \
        --intervals 5,6,7,8,9,10 --outdir ./tick_cache

Un solo passaggio sui tick per tutti gli intervalli richiesti (niente ri-download
se chiedi combinazioni diverse di TF sugli stessi giorni: i .csv.gz grezzi restano
in <outdir>/raw/ e vengono riusati se già presenti).

Output: <outdir>/<symbol>_<N>m.csv per ciascun intervallo, colonne:
    time,open,high,low,close,volume
(time = secondi unix UTC, inizio barra — stesso formato usato da _fetch_klines_bybit
in app.py, che poi applica l'offset di timezone configurato).

Nota: un giorno di tick di un simbolo liquido come SOLUSDT pesa ~30-40MB compressi
e contiene centinaia di migliaia di trade — uno storico di un anno intero significa
scaricare diversi GB. Lo script processa un giorno alla volta e scarta i tick subito
dopo averli aggregati (non tiene mai tutto lo storico tick in memoria).
"""
import argparse
import csv
import gzip
import os
import sys
import urllib.request
from datetime import date, timedelta

BASE_URL = "https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz"


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def download_day(symbol, day, cache_dir):
    fname = f"{symbol}{day.isoformat()}.csv.gz"
    path = os.path.join(cache_dir, fname)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    url = BASE_URL.format(symbol=symbol, date=day.isoformat())
    tmp = path + '.part'
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, 'wb') as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:
        print(f"  ! errore download {day}: {e}", file=sys.stderr)
        if os.path.exists(tmp):
            os.remove(tmp)
        return None
    os.rename(tmp, path)
    return path


def iter_ticks(gz_path):
    with gzip.open(gz_path, 'rt', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = float(row['timestamp'])
                price = float(row['price'])
                size = float(row['size'])
            except (KeyError, ValueError, TypeError):
                continue
            yield ts, price, size


class Bucketer:
    """Accumula tick in candele OHLCV per un intervallo (in minuti) dato."""

    def __init__(self, interval_minutes):
        self.span = interval_minutes * 60
        self.cur_bucket = None
        self.o = self.h = self.l = self.c = None
        self.v = 0.0
        self.out = []

    def add(self, ts, price, size):
        bucket = int(ts // self.span) * self.span
        if self.cur_bucket is None:
            self.cur_bucket = bucket
        elif bucket != self.cur_bucket:
            self._flush()
            self.cur_bucket = bucket
        if self.o is None:
            self.o = self.h = self.l = price
        if price > self.h:
            self.h = price
        if price < self.l:
            self.l = price
        self.c = price
        self.v += size

    def _flush(self):
        if self.o is None:
            return
        self.out.append({'time': self.cur_bucket, 'open': self.o, 'high': self.h,
                          'low': self.l, 'close': self.c, 'volume': round(self.v, 6)})
        self.o = self.h = self.l = self.c = None
        self.v = 0.0

    def finish(self):
        self._flush()
        return self.out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--symbol', required=True, help='es. SOLUSDT')
    ap.add_argument('--start', required=True, help='YYYY-MM-DD')
    ap.add_argument('--end', required=True, help='YYYY-MM-DD')
    ap.add_argument('--intervals', required=True, help='minuti separati da virgola, es. 5,6,7,8,9,10')
    ap.add_argument('--outdir', default='./tick_cache')
    ap.add_argument('--keep-raw', action='store_true',
                     help="non cancellare i .csv.gz grezzi scaricati (di default restano già in outdir/raw/)")
    args = ap.parse_args()

    symbol = args.symbol.upper()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        print("errore: --end precede --start", file=sys.stderr)
        sys.exit(1)
    intervals = [int(x) for x in args.intervals.split(',') if x.strip()]

    raw_dir = os.path.join(args.outdir, 'raw')
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(args.outdir, exist_ok=True)

    bucketers = {n: Bucketer(n) for n in intervals}

    days = list(daterange(start, end))
    print(f"Scarico e aggrego {symbol}: {len(days)} giorni, TF {intervals} minuti -> {args.outdir}")

    for i, day in enumerate(days, 1):
        print(f"[{i}/{len(days)}] {day} ...", end=' ', flush=True)
        path = download_day(symbol, day, raw_dir)
        if not path:
            print("SALTATO (download fallito)")
            continue
        n_ticks = 0
        for ts, price, size in iter_ticks(path):
            n_ticks += 1
            for b in bucketers.values():
                b.add(ts, price, size)
        print(f"{n_ticks} tick")
        if not args.keep_raw:
            os.remove(path)

    for n, b in bucketers.items():
        candles = b.finish()
        out_path = os.path.join(args.outdir, f"{symbol}_{n}m.csv")
        with open(out_path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['time', 'open', 'high', 'low', 'close', 'volume'])
            for c in candles:
                w.writerow([c['time'], c['open'], c['high'], c['low'], c['close'], c['volume']])
        print(f"-> {out_path} ({len(candles)} candele)")


if __name__ == '__main__':
    main()
