"""
Chart Generator per Crypto Scanner
Genera grafici con candele e EMA usando matplotlib
"""

import matplotlib
matplotlib.use('Agg')  # Backend headless per server
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.dates import DateFormatter
import pandas as pd
from datetime import datetime
import io
import requests

def generate_chart(symbol, candles_data, ema_period=60, ema_value=None):
    """
    Genera grafico candlestick con 4 EMA (5, 10, 60, 223)
    
    Args:
        symbol: Symbol crypto (es. BTCUSDT)
        candles_data: Lista di dict con: timestamp, open, high, low, close, volume
        ema_period: Periodo EMA principale (usato per annotazione)
        ema_value: Valore EMA attuale (opzionale, viene calcolato se None)
    
    Returns:
        bytes: Immagine PNG del grafico
    """
    
    # Converti in DataFrame
    df = pd.DataFrame(candles_data)
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df = df.sort_values('timestamp')
    
    # Calcola tutte le EMA
    df['ema5'] = df['close'].ewm(span=5, adjust=False).mean()
    df['ema10'] = df['close'].ewm(span=10, adjust=False).mean()
    df['ema60'] = df['close'].ewm(span=60, adjust=False).mean()
    df['ema223'] = df['close'].ewm(span=223, adjust=False).mean()
    
    # Crea figura (solo 1 subplot ora, senza volumi)
    fig, ax = plt.subplots(1, 1, figsize=(14, 8))

    # Aggiungi margine sinistro significativo per migliore leggibilità (100px extra)
    plt.subplots_adjust(left=0.16)
    
    # Colori tema scuro
    bg_color = '#1a1a2e'
    grid_color = '#2d2d44'
    text_color = '#eaeaea'
    
    fig.patch.set_facecolor(bg_color)
    ax.set_facecolor(bg_color)
    
    # Plot candele
    for idx, row in df.iterrows():
        # Colore candela
        color = '#26a69a' if row['close'] >= row['open'] else '#ef5350'
        
        # Body
        body_height = abs(row['close'] - row['open'])
        body_bottom = min(row['open'], row['close'])
        
        rect = patches.Rectangle(
            (idx, body_bottom), 0.8, body_height,
            linewidth=0, facecolor=color, alpha=0.9
        )
        ax.add_patch(rect)
        
        # Wick (ombra)
        ax.plot([idx+0.4, idx+0.4], [row['low'], row['high']], 
                color=color, linewidth=1, alpha=0.7)
    
    # Plot 4 EMA con colori richiesti
    ax.plot(df.index, df['ema5'], color='#ef5350', linewidth=2, 
             label='EMA 5', alpha=0.9)
    ax.plot(df.index, df['ema10'], color='#ffd700', linewidth=2, 
             label='EMA 10', alpha=0.9)
    ax.plot(df.index, df['ema60'], color='#64b5f6', linewidth=2, 
             label='EMA 60', alpha=0.9)
    ax.plot(df.index, df['ema223'], color='#ab47bc', linewidth=2, 
             label='EMA 223', alpha=0.9)
    
    # Ultima candela e distanza dall'EMA principale
    last_price = df.iloc[-1]['close']
    last_ema = df.iloc[-1][f'ema{ema_period}']
    distance_pct = abs((last_price - last_ema) / last_ema * 100)
    
    # Linea prezzo attuale
    ax.axhline(y=last_price, color='#ffffff', linestyle='--', 
                linewidth=1, alpha=0.5, label=f'Price: ${last_price:.2f}')
    
    # Annotazione distanza dall'EMA principale
    ax.text(len(df)-1, last_price, 
             f'  {distance_pct:.2f}% from EMA{ema_period}', 
             color='#ffffff', fontsize=10, va='center', 
             bbox=dict(boxstyle='round', facecolor=bg_color, alpha=0.7, edgecolor='#64b5f6'))
    
    # Configurazione asse
    ax.set_xlim(-0.5, len(df)-0.5)
    ax.set_ylabel('Price ($)', color=text_color, fontsize=12, fontweight='bold')
    ax.set_title(f'{symbol} - Multi EMA Analysis', 
                  color=text_color, fontsize=16, fontweight='bold', pad=20)
    ax.legend(loc='upper left', framealpha=0.9, facecolor=bg_color, 
               edgecolor=grid_color, fontsize=10)
    ax.grid(True, alpha=0.2, color=grid_color)
    ax.tick_params(colors=text_color)
    
    # Formatta asse X con date
    time_labels = []
    for i in range(len(df)):
        if i % max(1, len(df)//10) == 0:
            time_labels.append(df.iloc[i]['timestamp'].strftime('%m-%d %H:%M'))
        else:
            time_labels.append('')
    ax.set_xticks(range(len(df)))
    ax.set_xticklabels(time_labels, rotation=45, ha='right', fontsize=9)
    ax.set_xlabel('Time', color=text_color, fontsize=10, fontweight='bold')
    
    # Timestamp e link TradingView
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # Converti BTCUSDT -> BTCUSDT.P per perpetual
    tv_symbol = symbol.replace('USDT', 'USDT.P')
    tv_link = f"https://it.tradingview.com/chart/KDtSSRjB/?symbol=BYBIT:{tv_symbol}"
    
    fig.text(0.01, 0.01, f'Generated: {now}', 
             ha='left', va='bottom', fontsize=8, color=text_color, alpha=0.5)
    fig.text(0.99, 0.01, f'TV: BYBIT:{tv_symbol}', 
             ha='right', va='bottom', fontsize=8, color='#64b5f6', alpha=0.7)
    
    plt.tight_layout()
    
    # Salva in bytes
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=120, facecolor=bg_color, 
                edgecolor='none', bbox_inches='tight')
    buf.seek(0)
    plt.close()
    
    return buf.getvalue()


def fetch_candles_bybit(symbol, interval='30', limit=200):
    """
    Scarica candele da Bybit
    
    Args:
        symbol: Symbol (es. BTCUSDT)
        interval: Intervallo (1, 3, 5, 15, 30, 60, 120, 240, D, W, M)
        limit: Numero candele (max 200)
    
    Returns:
        list: Lista di dict con candele
    """
    url = 'https://api.bybit.com/v5/market/kline'
    params = {
        'category': 'linear',
        'symbol': symbol,
        'interval': interval,
        'limit': limit
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data['retCode'] != 0:
            raise Exception(f"Bybit API error: {data['retMsg']}")
        
        # Converti formato Bybit
        candles = []
        for k in data['result']['list']:
            candles.append({
                'timestamp': int(k[0]),
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5])
            })
        
        # Ordina per timestamp crescente
        candles.sort(key=lambda x: x['timestamp'])
        
        return candles
        
    except Exception as e:
        print(f"Error fetching candles for {symbol}: {e}")
        return []


def generate_chart_for_coin(symbol, ema_period=60):
    """
    Genera grafico per una coin (scarica dati e genera immagine)
    
    Args:
        symbol: Symbol crypto
        ema_period: Periodo EMA
    
    Returns:
        bytes: Immagine PNG o None se errore
    """
    candles = fetch_candles_bybit(symbol, interval='30', limit=200)
    
    if not candles:
        print(f"No candles data for {symbol}")
        return None
    
    return generate_chart(symbol, candles, ema_period)


# Test standalone
if __name__ == '__main__':
    print("🎨 Testing Chart Generator...")
    
    # Test con BTCUSDT
    chart_bytes = generate_chart_for_coin('BTCUSDT', ema_period=60)
    
    if chart_bytes:
        # Salva file test
        with open('/tmp/test_chart.png', 'wb') as f:
            f.write(chart_bytes)
        print("✅ Chart saved to /tmp/test_chart.png")
    else:
        print("❌ Failed to generate chart")
