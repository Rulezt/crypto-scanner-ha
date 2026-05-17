#!/bin/bash
set -e

CONFIG_PATH=/data/options.json
TELEGRAM_TOKEN=$(jq -r '.telegram_token' $CONFIG_PATH)
TELEGRAM_CHAT_ID=$(jq -r '.telegram_chat_id' $CONFIG_PATH)

export TELEGRAM_TOKEN
export TELEGRAM_CHAT_ID

echo "🚀 Starting Crypto Scanner Professional..."

# Start Flask app with integrated scanners
cd /app
python3 -u app.py
