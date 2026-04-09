#!/usr/bin/env bash
# setup_tv2.sh — One-time setup for the TV2 research highlights kiosk.
#
# Run on the Raspberry Pi:
#   bash ~/physics-signage/tv2/setup_tv2.sh
#
# What this does:
#   1. Installs rclone (for Google Drive sync)
#   2. Installs Python 3 (if missing)
#   3. Generates the initial slideshow (placeholder if no media yet)
#   4. Installs an hourly cron job to sync from Drive and regenerate
#   5. Configures Chromium kiosk mode on LXDE desktop login

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
INDEX="$SCRIPT_DIR/index.html"

echo "=== TV2 Research Highlights Kiosk Setup ==="

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y rclone python3 chromium-browser unclutter

# ── 2. Generate initial placeholder slideshow ─────────────────────────────────
echo "[2/5] Generating initial slideshow..."
python3 "$SCRIPT_DIR/generate_slideshow.py"

# ── 3. rclone config reminder ─────────────────────────────────────────────────
echo ""
echo "[3/5] rclone Google Drive setup"
if rclone listremotes | grep -q "^gdrive:"; then
    echo "      ✓ gdrive remote already configured."
else
    echo "      ✗ No 'gdrive' remote found."
    echo "      Run:  rclone config"
    echo "      Create a new remote, name it 'gdrive', type: Google Drive."
    echo "      After configuring, test with:  rclone ls 'gdrive:Physics Signage Media'"
fi

# ── 4. Hourly cron job ────────────────────────────────────────────────────────
echo "[4/5] Installing hourly sync cron job..."
CRON_CMD="0 * * * * bash $SCRIPT_DIR/sync_gdrive.sh >> $SCRIPT_DIR/sync.log 2>&1"
# Add only if not already present
(crontab -l 2>/dev/null | grep -qF "$SCRIPT_DIR/sync_gdrive.sh") \
  && echo "      ✓ Cron job already installed." \
  || (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
echo "      ✓ Cron job active."

# ── 5. Kiosk autostart ───────────────────────────────────────────────────────
echo "[5/5] Configuring kiosk autostart..."
AUTOSTART_DIR="$HOME/.config/lxsession/LXDE-pi"
AUTOSTART="$AUTOSTART_DIR/autostart"
mkdir -p "$AUTOSTART_DIR"

# Hide cursor after 3 s of inactivity
grep -qF "@unclutter" "$AUTOSTART" 2>/dev/null \
  || echo "@unclutter -idle 3 -root" >> "$AUTOSTART"

# Launch Chromium in kiosk mode pointing at tv2/index.html
KIOSK_CMD="@chromium-browser --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble --check-for-update-interval=31536000 file://$INDEX"
grep -qF "tv2/index.html" "$AUTOSTART" 2>/dev/null \
  || echo "$KIOSK_CMD" >> "$AUTOSTART"

echo "      ✓ Autostart configured: $AUTOSTART"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Configure rclone if not done:  rclone config  (name remote 'gdrive')"
echo "  2. Run a manual sync:             bash $SCRIPT_DIR/sync_gdrive.sh"
echo "  3. Reboot:                        sudo reboot"
echo ""
echo "The TV will open the research highlights slideshow automatically on login."
echo "Media syncs from Google Drive every hour."
