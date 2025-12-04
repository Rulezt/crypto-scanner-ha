# Crypto Scanner Professional - Documentation

## Installation

### Method 1: Add Repository to Home Assistant

1. Go to **Settings** → **Add-ons** → **Add-on Store**
2. Click the **3 dots menu** (top right) → **Repositories**
3. Add this repository URL:
   ```
   https://github.com/Rulezt/crypto-scanner-ha
   ```
4. Click **Add** and close the dialog
5. Refresh the page
6. Find **Crypto Scanner Professional** in the list of available add-ons
7. Click on it and press **Install**

### Method 2: Manual Installation via SSH

1. Connect to your Home Assistant via SSH
2. Run this command:
   ```bash
   cd /addons/local
   wget https://github.com/Rulezt/crypto-scanner-ha/releases/latest/download/crypto_scanner_professional.tar.gz
   tar -xzf crypto_scanner_professional.tar.gz
   rm crypto_scanner_professional.tar.gz
   ```
3. Go to **Settings** → **Add-ons**
4. Refresh the page
5. Find **Crypto Scanner Professional** under **Local add-ons**
6. Click **Install**

### Method 3: Using the Update Script

1. Download the update script:
   ```bash
   wget https://raw.githubusercontent.com/Rulezt/crypto-scanner-ha/main/update_scanner.sh
   chmod +x update_scanner.sh
   ```
2. Run the script:
   ```bash
   ./update_scanner.sh
   ```
3. Follow the on-screen instructions

## Configuration

After installation, configure the add-on:

1. Go to **Configuration** tab
2. Set the following options:

### Required Settings

- **telegram_token**: Your Telegram Bot Token (get it from [@BotFather](https://t.me/botfather))
- **telegram_chat_id**: Your Telegram Chat ID (get it from [@userinfobot](https://t.me/userinfobot))

### Example Configuration

```yaml
telegram_token: "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"
telegram_chat_id: "123456789"
```

## Usage

1. After configuring, click **Start** to run the add-on
2. Access the dashboard by clicking **Open Web UI**
3. The scanner will automatically:
   - Monitor cryptocurrency pairs on Binance
   - Detect EMA touches, daily flips, and volume spikes
   - Send Telegram notifications with TradingView charts
   - Apply cooldown to avoid spam

## Features

### 📊 4 EMA Analysis
- Monitors 4 EMAs: 5, 10, 60, 223
- Calculates distances and percentages
- Visualizes on charts

### 🎯 EMA Touch Scanner
- Detects when price touches key EMAs
- Configurable touch threshold (default: 0.2%)
- Persistent cooldown system (24h default)

### 🔄 Daily Flip Scanner
- Monitors EMA crossovers
- Detects bullish/bearish flips
- Filters by volume and significance

### 📈 Volume Scanner
- Tracks unusual volume spikes
- Compares against 24h average
- Customizable threshold

### 📱 Telegram Notifications
- Beautiful formatted messages
- TradingView chart images
- Direct links to charts
- Real-time alerts

### ⏳ Persistent Cooldown System
- Prevents notification spam
- Survives restarts
- Per-pair cooldown tracking
- Configurable duration

## Troubleshooting

### Add-on won't start
- Check logs: **Log** tab
- Verify Telegram credentials
- Ensure Home Assistant has internet access

### No notifications
- Verify bot token is correct
- Check chat ID is correct
- Start a conversation with your bot first
- Check Telegram bot permissions

### Charts not showing
- Requires internet connection
- Chart generation may take a few seconds
- Check logs for errors

## Support

- **Issues**: https://github.com/Rulezt/crypto-scanner-ha/issues
- **Repository**: https://github.com/Rulezt/crypto-scanner-ha

## Version

Current version: **2.1.6**
