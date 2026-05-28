#!/bin/bash
# ============================================================
# Savan Travels WhatsApp Sender — Start Script
# Run: bash start.sh
# Then open: http://localhost:5000
# ============================================================

set -e
cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Savan Travels WhatsApp Sender          ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "❌ Python3 not found. Install from python.org"
  exit 1
fi

# Install dependencies
echo "📦 Installing dependencies..."
python3 -m pip install -r requirements.txt -q

# Initialize database (safe to run multiple times)
echo "🗄  Initializing database..."
python3 -c "from database import init_db; init_db(); print('✅ Database ready')"

echo ""
echo "✅ All ready!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🌐 Open in Chrome: http://localhost:5000"
echo "👤 Login:  admin"
echo "🔑 Password: savan123"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

# Start Flask
python3 app.py
