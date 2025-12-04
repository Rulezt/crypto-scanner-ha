# 🚀 Crypto Scanner Professional

**Add-on Home Assistant unificato - ZERO dipendenze esterne!**

## ✨ Features

✅ **UN SOLO ADD-ON** - Tutto integrato, niente AppDaemon
✅ **Config persistente** - Salvataggio automatico in /data
✅ **3 Scanner integrati** - EMA Touch, Daily Flip, Volume/Gainers/Losers
✅ **Dashboard web** - Interfaccia pulita e funzionale
✅ **API REST** - Backend Flask robusto
✅ **Threading** - Scanner girano in background
✅ **Telegram notifiche** - Alert real-time
✅ **Zero configurazione** - Funziona out-of-the-box

## 🚀 Installazione

### Metodo 1: Repository GitHub (Consigliato)

```bash
# In Home Assistant:
Settings → Add-ons → Add-on Store → Menu → Repositories
Aggiungi: https://github.com/yourusername/crypto-scanner-pro

# Poi:
Install "Crypto Scanner Professional"
```

### Metodo 2: Installazione Locale

```bash
# Copia directory su Home Assistant
tar -xzf crypto_scanner_professional.tar.gz
cp -r crypto_scanner_professional /addons/crypto_scanner_pro/

# In Home Assistant:
Settings → Add-ons → Check for updates
Install "Crypto Scanner Professional"
```

## ⚙️ Configurazione

### 1. Setup Telegram Bot

```
1. Apri Telegram
2. Cerca @BotFather
3. Invia /newbot
4. Segui istruzioni → Ottieni TOKEN
5. Invia /start al tuo bot
6. Vai su: https://api.telegram.org/bot<TOKEN>/getUpdates
7. Copia chat_id dalla risposta
```

### 2. Configura Add-on

```yaml
# Configuration tab:
telegram_token: "123456789:ABCdef..."
telegram_chat_id: "123456789"
```

### 3. Start!

```
Info → Start
Info → Enable "Start on boot"
Click "OPEN WEB UI"
```

## 🎛️ Dashboard

Accedi via menu laterale Home Assistant o:
```
http://homeassistant.local:8080
```

**Configura:**
- 🎯 EMA Touch Scanner
- 🔄 Daily Flip Scanner
- 📊 Volume/Gainers/Losers Scanner
- ⚙️ Impostazioni generali

**Click "Salva"** → Config persiste automaticamente!

## 🔧 Come Funziona

```
┌─────────────────────────────┐
│  Crypto Scanner Pro         │
│                             │
│  ├── Flask API (port 8080)  │
│  ├── Dashboard Web          │
│  ├── Config Storage (JSON)  │
│  └── Scanner Threads:       │
│      ├── EMA Touch          │
│      ├── Daily Flip         │
│      └── Volume Scanner     │
└─────────────────────────────┘
         ↓
    Bybit API + Telegram
```

**Tutto in un solo container!** 🎯

## 📊 Scanner

### EMA Touch
- Cerca primo tocco giornaliero EMA
- Configurabile: periodo EMA, soglia prossimità
- Alert quando prezzo vicino ma non ancora toccato

### Daily Flip
- Candele vicine al flip (verde→rosso o rosso→verde)
- Configurabile: soglia flip, tipo flip
- Identifica zone di indecisione

### Volume Scanner
- Top gainers (+10% default)
- Top losers (-10% default)
- Volume spike (opzionale)

## 🔒 Persistenza

Config salvata in: `/data/scanner_config.json`

✅ Persiste tra restart
✅ Backup automatico
✅ No database esterno
✅ Semplicemente funziona!

## 🐛 Troubleshooting

### Add-on non si avvia
```bash
# Controlla log
Settings → Add-ons → Crypto Scanner Pro → Log

# Verifica porta 8080 libera
netstat -tulpn | grep 8080
```

### Dashboard non accessibile
```
# Verifica add-on Started
Settings → Add-ons → Crypto Scanner Pro → Started ✅

# Accedi via ingress
Menu laterale → Crypto Scanner
```

### Notifiche non arrivano
```
1. Verifica Token e Chat ID corretti
2. Invia /start al bot su Telegram
3. Click "Test Scan" nella dashboard
4. Controlla log add-on per errori
```

## 📝 API Endpoints

```
GET  /api/health         - Health check
GET  /api/config         - Ottieni config
POST /api/config         - Salva config
POST /api/scan/<name>    - Trigger manual scan
```

## 🔄 Aggiornamenti

```
# Se installato da repository GitHub:
Settings → Add-ons → Crypto Scanner Pro → Update

# Se installato localmente:
1. Scarica nuova versione
2. Stop add-on
3. Sostituisci directory
4. Restart add-on
```

## 💪 Vantaggi vs Vecchia Versione

| Feature | Vecchia | Nuova |
|---------|---------|-------|
| Add-on necessari | 2 | 1 ✅ |
| Dipendenze | AppDaemon | Nessuna ✅ |
| Persistenza config | ❌ Rotta | ✅ Funziona |
| Complessità | Alta | Bassa ✅ |
| Manutenzione | Difficile | Facile ✅ |
| Installazione | Multi-step | 1-click ✅ |

## 🎯 In Breve

**Prima:** 2 add-on, AppDaemon, config rotta, casino totale ❌
**Ora:** 1 add-on, tutto integrato, semplicemente funziona ✅

## 📦 Struttura

```
crypto_scanner_professional/
├── config.yaml              # Config add-on HA
├── Dockerfile               # Build container
├── rootfs/
│   ├── run.sh              # Startup script
│   ├── app/
│   │   ├── app.py          # Flask API + Scanner manager
│   │   └── scanners/
│   │       ├── ema_touch.py
│   │       ├── daily_flip.py
│   │       └── volume.py
│   └── usr/share/nginx/html/
│       └── index.html      # Dashboard
```

## 🚀 Quick Start (30 secondi)

```bash
1. Install add-on
2. Config → Inserisci Token/Chat ID Telegram
3. Start
4. OPEN WEB UI → Configura → Salva
5. Ricevi notifiche! 🎉
```

## 📄 Licenza

MIT

## 🆘 Supporto

Issues: GitHub Issues
Forum: Home Assistant Community

---

**Made with 💪 by a frustrated developer who wanted things to JUST WORK!**

v2.0.0 - Finalmente fatto bene! ✨
