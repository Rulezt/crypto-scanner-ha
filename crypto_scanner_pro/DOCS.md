# Crypto Scanner Pro - Documentation

## Installation

### Docker Compose (Recommended)

1. Clone or copy the project to your server
2. Copy the example env file and edit your credentials:
   ```bash
   cp .env.example .env
   nano .env
   ```
3. Start the app:
   ```bash
   docker compose up -d
   ```
4. Access the dashboard at `http://<your-server-ip>:8080`

## Configuration

### Environment Variables

Set these in your `.env` file or `docker-compose.yml`:

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Your Telegram Bot Token (from [@BotFather](https://t.me/botfather)) |
| `TELEGRAM_CHAT_ID` | Your Telegram Chat ID (from [@userinfobot](https://t.me/userinfobot)) |

### Dashboard Settings

All scanner parameters can be configured from the Dashboard → Settings tab without restarting the app.

## Usage

1. Open the dashboard at `http://<your-server-ip>:8080`
2. Go to **Telegram** tab to enter your bot credentials
3. Configure each scanner from the **Settings** tab
4. Scanners start automatically on launch

## Features

### EMA Touch Scanner
- Detects when price touches key EMAs (5, 10, 60, 223)
- Configurable touch threshold
- Persistent cooldown to avoid spam

### Daily Flip Scanner
- Monitors EMA crossovers (bullish/bearish flips)
- Filters by volume and significance

### ATH/ATL Scanner
- Alerts when price approaches All-Time High or All-Time Low
- Configurable proximity threshold

### ICO Levels Scanner
- Tracks historical ICO price levels
- Configurable threshold

### Double Touch Scanner
- Detects double touch patterns on key levels
- Multi-timeframe support

### Orderbook
- Real-time orderbook visualization
- WebSocket-based live updates

### Telegram Notifications
- Formatted messages with chart images
- Direct links to the dashboard

## Troubleshooting

### App won't start
- Check logs: `docker compose logs -f`
- Verify Telegram credentials in the Dashboard

### No notifications
- Verify bot token and chat ID are correct
- Start a conversation with your bot first on Telegram

### Charts not showing
- Requires internet connection for market data
- Check logs for errors: `docker compose logs -f`

## Version

Current version: **4.7.0**
