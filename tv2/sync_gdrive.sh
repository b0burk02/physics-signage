#!/usr/bin/env bash
# sync_gdrive.sh — Pull research highlights from Google Drive and regenerate slideshow.
#
# Prerequisites:
#   rclone configured with a remote named "gdrive"
#   Run once:  rclone config  (choose Google Drive, name it "gdrive")
#
# Usage:
#   bash ~/physics-signage/tv2/sync_gdrive.sh
#
# Add to crontab for hourly refresh:
#   0 * * * * bash ~/physics-signage/tv2/sync_gdrive.sh >> ~/physics-signage/tv2/sync.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MEDIA_DIR="$SCRIPT_DIR/media"
GDRIVE_REMOTE="gdrive:Physics Signage Media"

echo "$(date '+%H:%M:%S')  Syncing from Google Drive..."
rclone sync "$GDRIVE_REMOTE" "$MEDIA_DIR" \
  --exclude "*.json" \
  --exclude ".*" \
  --progress

echo "$(date '+%H:%M:%S')  Regenerating slideshow..."
python3 "$SCRIPT_DIR/generate_slideshow.py"

echo "$(date '+%H:%M:%S')  Done."
