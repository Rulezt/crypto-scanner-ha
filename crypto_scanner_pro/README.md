# Crypto Scanner Pro

Scanner crypto avanzato con EMA touch, daily flip, ATH/ATL, ICO levels e double touch via Telegram.

## Features

- **EMA Touch Scanner** — rileva quando il prezzo tocca le EMA chiave (5, 10, 60, 223)
- **Daily Flip Scanner** — crossover EMA rialzista/ribassista
- **ATH/ATL Scanner** — alert quando il prezzo si avvicina ai massimi/minimi storici
- **ICO Levels Scanner** — livelli storici ICO
- **Double Touch Scanner** — pattern double touch su livelli chiave (multi-timeframe)
- **Orderbook** — visualizzazione real-time via WebSocket
- **Telegram** — notifiche con immagini grafici e link diretti alla dashboard

## Installazione

### Docker Compose

```bash
docker compose up -d
```

Accedi alla dashboard su `http://<ip-server>:8080`

## Configurazione

### Variabili d'ambiente (opzionale)

```env
TELEGRAM_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID=123456789
```

### Dashboard

Tutte le impostazioni sono configurabili dalla Dashboard → tab Telegram / Settings, senza riavviare l'app.

## Struttura

```
crypto_scanner_pro/
├── Dockerfile
├── rootfs/
│   ├── app/
│   │   ├── app.py                  # Backend Flask
│   │   ├── ws_manager.py           # WebSocket Bybit
│   │   ├── alert_utils.py
│   │   └── scanners/
│   │       ├── ath_atl_scanner.py
│   │       ├── daily_flip.py
│   │       ├── double_touch.py
│   │       ├── ema_proximity.py
│   │       ├── ema_touch.py
│   │       └── ico_levels_scanner.py
│   └── usr/share/nginx/html/       # Frontend statico (nginx)
```

## API

```
GET  /api/health         - Health check
GET  /api/config         - Configurazione corrente
POST /api/config         - Salva configurazione
POST /api/scan/<name>    - Trigger scan manuale
```

## Troubleshooting

```bash
# Controlla i log
docker compose logs -f

# Verifica porta
netstat -tulpn | grep 8080
```
