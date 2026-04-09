#!/usr/bin/env python3
"""
TV 2 — Research Highlights Slideshow
=====================================
Scans the media/ folder for images and videos, reads optional captions from
captions.json, and generates a full-screen kiosk display at index.html.

Usage:
  python3 generate_slideshow.py

Add content:
  Drop .jpg/.png/.mp4/.webm files into the tv2/media/ folder.
  Edit media/captions.json to add titles and descriptions.
  Files are shown in alphabetical order — prefix filenames with 01_, 02_ etc.
  to control the order.

Sync from Google Drive (after rclone is configured):
  rclone sync "gdrive:Physics Signage Media" ~/physics-signage/tv2/media/
  python3 ~/physics-signage/tv2/generate_slideshow.py
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

BASE_DIR   = Path(__file__).resolve().parent
MEDIA_DIR  = BASE_DIR / "media"
OUTPUT     = BASE_DIR / "index.html"
CAPTIONS   = MEDIA_DIR / "captions.json"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}

# How long to show each image (seconds). Videos advance when they finish.
IMAGE_DURATION = 12


def load_captions() -> dict:
    if CAPTIONS.exists():
        try:
            data = json.loads(CAPTIONS.read_text())
            return {k: v for k, v in data.items() if not k.startswith("_")}
        except Exception as e:
            log.warning(f"Could not read captions.json: {e}")
    return {}


def title_from_filename(name: str) -> str:
    """Turn '03_dark_matter_lab.jpg' into 'Dark Matter Lab'."""
    stem = Path(name).stem
    stem = re.sub(r"^\d+[_\-\s]*", "", stem)   # strip leading number
    stem = stem.replace("_", " ").replace("-", " ")
    return stem.title()


def scan_media() -> list:
    files = sorted(
        f for f in MEDIA_DIR.iterdir()
        if f.suffix.lower() in IMAGE_EXTS | VIDEO_EXTS
    )
    if not files:
        log.warning("No media files found in media/ — generating placeholder slide")
    return files


def build_slides(files: list, captions: dict) -> list:
    slides = []
    for f in files:
        cap   = captions.get(f.name, {})
        title = cap.get("title", "") or title_from_filename(f.name)
        desc  = cap.get("description", "")
        kind  = "video" if f.suffix.lower() in VIDEO_EXTS else "image"
        slides.append({
            "file":  f.name,
            "kind":  kind,
            "title": title,
            "desc":  desc,
        })
    return slides


def esc(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def render_slide(s: dict, idx: int) -> str:
    active = ' active' if idx == 0 else ''
    media_path = f"media/{esc(s['file'])}"

    if s["kind"] == "video":
        media_tag = (
            f'<video src="{media_path}" '
            f'id="vid-{idx}" playsinline muted '
            f'onended="slideEnded({idx})"></video>'
        )
    else:
        media_tag = f'<img src="{media_path}" alt="{esc(s["title"])}">'

    caption_html = ""
    if s["title"] or s["desc"]:
        caption_html = f"""
        <div class="caption">
          {'<div class="cap-title">' + esc(s["title"]) + '</div>' if s["title"] else ''}
          {'<div class="cap-desc">'  + esc(s["desc"])  + '</div>' if s["desc"]  else ''}
        </div>"""

    return f"""
  <div class="slide{active}" data-kind="{s['kind']}" data-idx="{idx}">
    {media_tag}
    {caption_html}
  </div>"""


HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=1920">
<title>Rutgers Physics &amp; Astronomy — Research Highlights</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{
  width: 100%; height: 100vh;
  overflow: hidden;
  background: #000;
  font-family: 'Segoe UI', system-ui, sans-serif;
}}

/* ── Media slides ── */
#slideshow {{
  position: relative;
  width: 100%; height: 100vh;
}}
.slide {{
  position: absolute;
  inset: 0;
  opacity: 0;
  transition: opacity 1s ease;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #000;
}}
.slide.active {{ opacity: 1; }}

.slide img,
.slide video {{
  width: 100%;
  height: 100%;
  object-fit: contain;
}}

/* ── Top banner ── */
.banner {{
  position: fixed;
  top: 0; left: 0; right: 0;
  height: 80px;
  background: linear-gradient(
    to bottom,
    rgba(180, 0, 30, 0.92) 0%,
    rgba(180, 0, 30, 0.55) 70%,
    transparent 100%
  );
  display: flex;
  align-items: center;
  padding: 0 44px;
  gap: 18px;
  z-index: 200;
  pointer-events: none;
}}
.banner-atom {{
  font-size: 2rem;
  opacity: .85;
}}
.banner-text {{
  display: flex;
  flex-direction: column;
}}
.banner-title {{
  color: #fff;
  font-size: 1.55rem;
  font-weight: 700;
  letter-spacing: .07em;
  line-height: 1.2;
}}
.banner-sub {{
  color: rgba(255,255,255,.75);
  font-size: .82rem;
  letter-spacing: .15em;
  text-transform: uppercase;
}}

/* ── Bottom caption ── */
.caption {{
  position: absolute;
  bottom: 0; left: 0; right: 0;
  padding: 60px 44px 28px;
  background: linear-gradient(to top, rgba(0,0,0,.82) 0%, transparent 100%);
  z-index: 100;
}}
.cap-title {{
  color: #fff;
  font-size: 1.45rem;
  font-weight: 600;
  line-height: 1.3;
  margin-bottom: 6px;
  text-shadow: 0 1px 4px rgba(0,0,0,.6);
}}
.cap-desc {{
  color: rgba(255,255,255,.75);
  font-size: 1rem;
  line-height: 1.5;
  text-shadow: 0 1px 3px rgba(0,0,0,.6);
}}

/* ── Progress bar ── */
#progress-bar {{
  position: fixed;
  bottom: 0; left: 0;
  height: 4px;
  background: #cc0033;
  width: 0%;
  transition: width 0s linear;
  z-index: 300;
}}

/* ── Slide counter ── */
#counter {{
  position: fixed;
  top: 28px; right: 36px;
  color: rgba(255,255,255,.5);
  font-size: .8rem;
  letter-spacing: .1em;
  z-index: 300;
  pointer-events: none;
}}

/* ── No-content placeholder ── */
.placeholder {{
  color: rgba(255,255,255,.3);
  font-size: 1.4rem;
  text-align: center;
  padding: 40px;
}}
</style>
</head>
<body>

<!-- Banner (always visible) -->
<div class="banner">
  <div class="banner-atom">&#9883;</div>
  <div class="banner-text">
    <div class="banner-title">Rutgers Physics &amp; Astronomy</div>
    <div class="banner-sub">Research Highlights</div>
  </div>
</div>

<!-- Slide counter -->
<div id="counter"></div>

<!-- Progress bar -->
<div id="progress-bar"></div>

<!-- Slideshow -->
<div id="slideshow">
%%SLIDES%%
</div>

<script>
const TOTAL     = %%TOTAL%%;
const IMG_DUR   = %%IMG_DUR%% * 1000;   // ms
let   current   = 0;
let   timer     = null;

const slides  = document.querySelectorAll('.slide');
const progBar = document.getElementById('progress-bar');
const counter = document.getElementById('counter');

function updateCounter() {
  if (TOTAL > 0)
    counter.textContent = (current + 1) + ' / ' + TOTAL;
}

function showSlide(idx) {
  if (TOTAL === 0) return;

  // Pause previous video if any
  const prevVid = slides[current]?.querySelector('video');
  if (prevVid) { prevVid.pause(); prevVid.currentTime = 0; }

  slides[current].classList.remove('active');
  current = (idx + TOTAL) % TOTAL;
  slides[current].classList.add('active');
  updateCounter();

  const kind = slides[current].dataset.kind;
  if (kind === 'video') {
    startVideo();
  } else {
    startProgress(IMG_DUR);
    clearTimeout(timer);
    timer = setTimeout(() => showSlide(current + 1), IMG_DUR);
  }
}

function startVideo() {
  // Stop any running progress bar/timer
  clearTimeout(timer);
  progBar.style.transition = 'none';
  progBar.style.width = '0%';

  const vid = slides[current].querySelector('video');
  if (!vid) { showSlide(current + 1); return; }

  vid.play().catch(() => {
    // Autoplay blocked — advance after IMG_DUR
    startProgress(IMG_DUR);
    timer = setTimeout(() => showSlide(current + 1), IMG_DUR);
  });

  // Safety timeout: advance after 3 minutes even if video doesn't end
  timer = setTimeout(() => showSlide(current + 1), 180_000);
}

// Called by video onended attribute
function slideEnded(idx) {
  if (idx === current) {
    clearTimeout(timer);
    showSlide(current + 1);
  }
}

function startProgress(duration) {
  progBar.style.transition = 'none';
  progBar.style.width = '0%';
  progBar.getBoundingClientRect();  // force reflow
  progBar.style.transition = `width ${duration}ms linear`;
  progBar.style.width = '100%';
}

// Kick off
if (TOTAL > 0) {
  updateCounter();
  const firstKind = slides[0].dataset.kind;
  if (firstKind === 'video') {
    slides[0].classList.add('active');
    startVideo();
  } else {
    slides[0].classList.add('active');
    startProgress(IMG_DUR);
    timer = setTimeout(() => showSlide(1), IMG_DUR);
  }
}

// Reload every 30 min to pick up newly synced media
setTimeout(() => location.reload(), 30 * 60 * 1000);
</script>
</body>
</html>
"""


def generate(slides: list) -> None:
    if slides:
        slides_html = "\n".join(render_slide(s, i) for i, s in enumerate(slides))
        total = len(slides)
    else:
        slides_html = (
            '  <div class="slide active">'
            '<div class="placeholder">No media files yet.<br>'
            'Add images or videos to the media/ folder.</div></div>'
        )
        total = 0

    html = (HTML_TEMPLATE
            .replace("%%SLIDES%%", slides_html)
            .replace("%%TOTAL%%",   str(total))
            .replace("%%IMG_DUR%%", str(IMAGE_DURATION)))

    OUTPUT.write_text(html, encoding="utf-8")
    log.info(f"Slideshow written → {OUTPUT}  ({total} slides)")


def main():
    MEDIA_DIR.mkdir(exist_ok=True)
    captions = load_captions()
    files    = scan_media()
    slides   = build_slides(files, captions)
    generate(slides)
    log.info("Done.")


if __name__ == "__main__":
    main()
