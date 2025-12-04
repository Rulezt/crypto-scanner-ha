#!/usr/bin/env bash
set -e

# Crypto Scanner Professional - Update Script
# Downloads and installs the latest release from GitHub

REPO="Rulezt/crypto-scanner-ha"
ADDON_DIR="/addons/local"
ADDON_NAME="crypto_scanner_professional"
TEMP_DIR="/tmp/crypto_scanner_update"

echo "🚀 Crypto Scanner Professional - Update Script"
echo "=============================================="

# Check if running on Home Assistant
if [ ! -d "/homeassistant" ]; then
    echo "⚠️  Warning: This doesn't appear to be a Home Assistant system"
    echo "   Continuing anyway..."
fi

# Create temp directory
echo "📁 Creating temporary directory..."
mkdir -p "$TEMP_DIR"
cd "$TEMP_DIR"

# Get latest release URL
echo "🔍 Fetching latest release info..."
LATEST_URL=$(curl -s "https://api.github.com/repos/$REPO/releases/latest" | grep "browser_download_url.*tar.gz" | cut -d '"' -f 4)

if [ -z "$LATEST_URL" ]; then
    echo "❌ Error: Could not find latest release"
    exit 1
fi

echo "📥 Downloading latest release..."
echo "   URL: $LATEST_URL"
curl -L -o "${ADDON_NAME}.tar.gz" "$LATEST_URL"

# Extract the archive
echo "📦 Extracting archive..."
tar -xzf "${ADDON_NAME}.tar.gz"

# Create addon directory if it doesn't exist
echo "📂 Preparing addon directory..."
mkdir -p "$ADDON_DIR"

# Backup existing installation if it exists
if [ -d "$ADDON_DIR/$ADDON_NAME" ]; then
    echo "💾 Backing up existing installation..."
    mv "$ADDON_DIR/$ADDON_NAME" "$ADDON_DIR/${ADDON_NAME}_backup_$(date +%Y%m%d_%H%M%S)"
fi

# Install the new version
echo "🔧 Installing new version..."
mv "$ADDON_NAME" "$ADDON_DIR/"

# Set correct permissions
echo "🔐 Setting permissions..."
chmod -R 755 "$ADDON_DIR/$ADDON_NAME"

# Cleanup
echo "🧹 Cleaning up..."
cd ~
rm -rf "$TEMP_DIR"

echo ""
echo "✅ Update completed successfully!"
echo ""
echo "Next steps:"
echo "1. Go to Home Assistant → Settings → Add-ons"
echo "2. Refresh the add-on list"
echo "3. Find 'Crypto Scanner Professional' in Local add-ons"
echo "4. Configure and start the add-on"
echo ""
echo "📖 For configuration help, check the README.md in the add-on"
