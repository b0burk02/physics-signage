#!/usr/bin/env bash
# ============================================================
# Raspberry Pi setup script for Physics Department Signage
# Run once after cloning this repo onto the Pi.
#
# Tested on: Raspberry Pi OS (Bookworm) with Desktop
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"

echo "=== Physics Signage Setup ==="

# ── 1. System packages ──────────────────────────────────────
echo "[1/6] Installing system packages…"
sudo apt-get update -q
sudo apt-get install -y -q \
    python3 python3-pip python3-venv \
    chromium-browser \
    unclutter              # hides the mouse cursor on the TV
echo "  OK"

# ── 2. Python virtual environment ──────────────────────────
echo "[2/6] Creating Python virtualenv at $VENV…"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet requests beautifulsoup4
echo "  OK"

# ── 3. Optional: Playwright for abstract scraping ──────────
echo "[3/6] Installing Playwright (optional, may take a few minutes)…"
"$VENV/bin/pip" install --quiet playwright
"$VENV/bin/python" -m playwright install chromium
"$VENV/bin/python" -m playwright install-deps chromium
echo "  OK (Playwright installed — abstracts will be scraped from website)"

# ── 4. First run ────────────────────────────────────────────
echo "[4/6] Generating initial display.html…"
"$VENV/bin/python" "$SCRIPT_DIR/update.py"
echo "  OK → $SCRIPT_DIR/output/display.html"

# ── 5. Cron job (hourly updates) ────────────────────────────
echo "[5/6] Installing hourly cron job…"
CRON_CMD="0 * * * * $VENV/bin/python $SCRIPT_DIR/update.py >> /var/log/physics-signage.log 2>&1"
# Add only if not already present
( crontab -l 2>/dev/null | grep -v "physics-signage"; echo "$CRON_CMD" ) | crontab -
echo "  OK (runs every hour)"

# ── 6. Kiosk autostart ─────────────────────────────────────
echo "[6/6] Configuring Chromium kiosk autostart…"
AUTOSTART_DIR="$HOME/.config/lxsession/LXDE-pi"
mkdir -p "$AUTOSTART_DIR"
AUTOSTART="$AUTOSTART_DIR/autostart"

# Disable screen blanking and configure kiosk
cat > "$AUTOSTART" << AUTOSTART_EOF
@lxpanel --profile LXDE-pi
@pcmanfm --desktop --profile LXDE-pi
@xset s off
@xset -dpms
@xset s noblank
@unclutter -idle 0.1 -root
@chromium-browser \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-restore-session-state \
    --autoplay-policy=no-user-gesture-required \
    --check-for-update-interval=31536000 \
    "file://$SCRIPT_DIR/output/display.html"
AUTOSTART_EOF

echo "  OK → $AUTOSTART"

echo ""
echo "=== Setup complete! ==="
echo ""
echo "To test the display now:"
echo "  chromium-browser --kiosk file://$SCRIPT_DIR/output/display.html"
echo ""
echo "The display will auto-launch on next desktop login."
echo "Data refreshes every hour via cron."
echo ""
echo "To update data immediately:"
echo "  $VENV/bin/python $SCRIPT_DIR/update.py"
echo ""
echo "To test the scraper with a local preview server:"
echo "  $VENV/bin/python $SCRIPT_DIR/update.py --serve"
echo "  then open http://localhost:8080 in a browser"
