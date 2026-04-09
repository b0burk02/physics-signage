#!/usr/bin/env python3
"""
Rutgers Physics & Astronomy Department Signage
==============================================
Scrapes the weekly newsletter (always) and optionally the department
website (via Playwright, for abstracts), then generates a self-contained
HTML file for a TV kiosk.

Usage:
  python3 update.py              # generate output/display.html
  python3 update.py --serve      # generate + serve on :8080
  python3 update.py --debug      # verbose logging

Cron example (every hour, Mon-Fri, 7am-10pm):
  0 7-22 * * 1-5 /path/to/physics-signage/venv/bin/python3 \
      /path/to/physics-signage/update.py >> /var/log/signage.log 2>&1
"""

import argparse
import base64
import json
import logging
import re
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    import qrcode
    import qrcode.image.svg
    HAS_QR = True
except ImportError:
    HAS_QR = False

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

NEWSLETTER_URL = "http://www.physics.rutgers.edu/newsletter/"
COLLOQUIUM_URL = "https://physics.rutgers.edu/events/colloquium"
SEMINARS_URL   = "https://physics.rutgers.edu/events/seminars"
# News URL is best-effort; 404 is handled gracefully.
NEWS_URL       = "https://physics.rutgers.edu/news/2026-news"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux aarch64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SEMINAR_LABELS = {
    "COL": "Colloquium",
    "CMS": "Condensed Matter",
    "HEX": "High Energy Experiment",
    "HET": "High Energy Theory",
    "MPS": "Math-Physics",
    "NUC": "Nuclear Physics",
    "IQB": "Quantum Biology",
    "SIP": "Student in Physics",
    "SPS": "Society of Physics Students",
    "AST": "Astrophysics",
    "APS": "Astrophysics",
    "GRA": "Gravitational",
    "AMO": "Atomic/Molecular/Optical",
}

BASE_DIR   = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
CACHE_FILE = OUTPUT_DIR / "events.json"
HTML_FILE  = OUTPUT_DIR / "display.html"

# ── Newsletter scraper ────────────────────────────────────────────────────────

def fetch_newsletter() -> dict:
    """Fetch and parse the current weekly newsletter."""
    log.info("Fetching newsletter…")
    try:
        r = requests.get(NEWSLETTER_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as exc:
        log.error(f"Newsletter unavailable: {exc}")
        return {}
    return _parse_newsletter(BeautifulSoup(r.text, "html.parser"))


def _add_ampm(time_str: str) -> str:
    """Convert bare time like '3:30' to '3:30 PM' using physics seminar conventions."""
    try:
        h = int(time_str.split(":")[0])
        # 9-11 → AM  |  12, 1-8 → PM (covers all typical seminar/colloquium slots)
        suffix = "AM" if 9 <= h <= 11 else "PM"
        return f"{time_str} {suffix}"
    except ValueError:
        return time_str


_DAY_EXPAND = {
    "mon": "Monday", "tues": "Tuesday", "tue": "Tuesday",
    "wed": "Wednesday", "wednes": "Wednesday",
    "thurs": "Thursday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}

def _expand_day(abbr: str) -> str:
    return _DAY_EXPAND.get(abbr.lower().rstrip("."), abbr.capitalize())


# Known seminar type codes — used to anchor event-line detection.
_TYPE_CODES = {
    "COL","CMS","HEX","HET","MPS","IQB","SIP","SPS",
    "AST","APS","NUC","GRA","AMO","BIO","PHY",
}

# Line 1 of an event block:
#   CMS  Tues Apr 7 \t Patrick Woodward, The Ohio State University
_TYPE_LINE = re.compile(
    r'^([A-Z]{2,5})\s+'          # type code
    r'(\w+)\s+'                   # day abbreviation (Tues, Wed, Thurs…)
    r'(\w+)\s+'                   # month abbreviation (Apr, Jan…)
    r'(\d{1,2})\s+'               # day number
    r'(.+)',                       # speaker, Institution
)

# Line 2 of an event block:
#   1:30   330W and Zoom    "Title text"       or just TBA
_DETAIL_LINE = re.compile(r'^(\d{1,2}:\d{2})\s+(.*)')

# Optional line 3:
#   link:  https://…
_LINK_LINE = re.compile(r'link:\s+(https?://\S+)', re.IGNORECASE)


def _parse_newsletter(soup: BeautifulSoup) -> dict:
    """
    The newsletter is a <pre> block with this two-line event format:

      TYPE  DAY_ABBR MONTH_ABBR DAY_NUM \\t Speaker Name, Institution
      TIME   LOCATION    "Title"
                         link:  https://…   (optional)

    Example:
      COL  Wed Apr 8       Brian Metzger, Columbia University and Flatiron Institute
      3:30   330W and Zoom   "From Mergers to Magnetars: Quest for the Origin of the Heaviest Elements"
                             link:  https://go.rutgers.edu/26z9hrza
    """
    pre = soup.find("pre")
    raw_text = pre.get_text() if pre else soup.get_text()

    # Pull issue number and date from the header line
    issue_m = re.search(r'Number\s+([SF]\d{2}-\d+)', raw_text)
    date_m  = re.search(r'(\d{4}-\w{3}\s+\d{1,2})', raw_text)
    nl_issue = issue_m.group(1) if issue_m else ""
    nl_date  = date_m.group(1)  if date_m  else ""

    events  = []
    pending = None    # event being assembled (type line seen, waiting for detail)
    section = "current"

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # ── Section separator ────────────────────────────────
        if re.search(r'\bnext\s+week\b', line, re.IGNORECASE):
            # Save any pending event before changing section
            if pending and pending.get("title"):
                events.append(pending)
                pending = None
            section = "next"
            continue

        # ── Event type line ──────────────────────────────────
        m = _TYPE_LINE.match(line)
        if m and m.group(1) in _TYPE_CODES:
            # Save previous pending event if complete
            if pending and pending.get("title"):
                events.append(pending)

            etype, day_abbr, month, day_num, speaker_inst = m.groups()

            # speaker_inst: "Brian Metzger, Columbia University and Flatiron Institute"
            # Use rsplit so multi-speaker lines ("A, B, C, University") keep institution
            si = [p.strip() for p in speaker_inst.rsplit(",", 1)]
            speaker     = si[0]
            institution = si[1] if len(si) > 1 else ""

            pending = {
                "type":        etype,
                "label":       SEMINAR_LABELS.get(etype, etype),
                "section":     section,
                "day":         _expand_day(day_abbr),
                "date":        f"{month} {day_num}",
                "time":        "",
                "location":    "",
                "speaker":     speaker,
                "institution": institution,
                "title":       None,   # filled in by detail line
                "abstract":    None,
                "url":         None,
            }
            continue

        # ── Detail line (time + location + title) ────────────
        if pending is not None:
            m = _DETAIL_LINE.match(line)
            if m and pending["time"] == "":   # only accept first detail line
                raw_time, rest = m.group(1), m.group(2).strip()
                pending["time"] = _add_ampm(raw_time)

                # Split rest into location and title
                # Title is always in double-quotes, or literally TBA
                if '"' in rest:
                    q1 = rest.index('"')
                    q2 = rest.rindex('"')
                    pending["location"] = rest[:q1].strip()
                    pending["title"]    = rest[q1 + 1 : q2] if q2 > q1 else rest[q1 + 1:]
                elif "TBA" in rest.upper():
                    idx = rest.upper().index("TBA")
                    pending["location"] = rest[:idx].strip()
                    pending["title"]    = "TBA"
                else:
                    pending["location"] = rest
                    pending["title"]    = ""
                continue

            # Link line
            m = _LINK_LINE.search(line)
            if m:
                pending["url"] = m.group(1)
                continue

    # Flush last pending event
    if pending and pending.get("title"):
        events.append(pending)

    n_col = sum(1 for e in events if e["type"] == "COL")
    n_sem = len(events) - n_col
    log.info(f"Newsletter parsed: {n_col} colloquium, {n_sem} seminars "
             f"({sum(1 for e in events if e['section']=='next')} next-week)")

    return {
        "colloquium":  [e for e in events if e["type"] == "COL"],
        "seminars":    [e for e in events if e["type"] != "COL"],
        "all_events":  events,
        "nl_issue":    nl_issue,
        "nl_date":     nl_date,
    }


# ── Website scraper (Playwright, optional) ────────────────────────────────────

def _solve_slider_challenge(page) -> bool:
    """Attempt to solve the slider verification challenge."""
    try:
        slider = page.locator("#verificationSlider")
        if not slider.is_visible(timeout=4000):
            return True  # No challenge on this page

        log.info("Solving slider challenge…")
        box = slider.bounding_box()
        if not box:
            return False

        x0 = box["x"] + 4
        x1 = box["x"] + box["width"] * 0.97
        y  = box["y"] + box["height"] * 0.5

        page.mouse.move(x0, y)
        page.mouse.down()
        page.wait_for_timeout(80)

        steps = 40
        for i in range(1, steps + 1):
            t  = i / steps
            xc = x0 + (x1 - x0) * t
            yc = y + (2 if i % 3 == 0 else -1)
            page.mouse.move(xc, yc)
            page.wait_for_timeout(8 + (i % 5) * 4)

        page.mouse.up()
        page.wait_for_timeout(600)

        verify = page.locator("#verifyBtn")
        if verify.is_visible(timeout=2000):
            verify.click()
        page.wait_for_timeout(2500)
        return True

    except Exception as exc:
        log.warning(f"Slider solve failed: {exc}")
        return False


def fetch_website_events(url: str, etype: str) -> list:
    """
    Use Playwright to load a challenge-protected events page and parse events.
    Also follows individual event-detail links to retrieve abstracts.
    Returns [] if Playwright is unavailable or the fetch fails.
    """
    if not HAS_PLAYWRIGHT:
        return []

    log.info(f"Playwright: fetching {url}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx  = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = ctx.new_page()
            page.set_default_timeout(45_000)
            page.goto(url, wait_until="domcontentloaded")
            _solve_slider_challenge(page)
            html = page.content()

            events = _parse_events_html(BeautifulSoup(html, "html.parser"), etype)

            # Follow each event's detail page to grab the abstract (same browser
            # session, so the challenge cookie is already set).
            for ev in events:
                if not ev.get("url") or ev.get("abstract"):
                    continue
                try:
                    dp = ctx.new_page()
                    dp.set_default_timeout(20_000)
                    dp.goto(ev["url"], wait_until="domcontentloaded")
                    detail_soup = BeautifulSoup(dp.content(), "html.parser")
                    dp.close()
                    abstract_el = (
                        detail_soup.find(class_=re.compile(
                            r"jev_evdesc|ev_description|abstract|article-body", re.I))
                        or detail_soup.find("div", class_=re.compile(r"body|content", re.I))
                    )
                    if abstract_el:
                        text = abstract_el.get_text(strip=True)
                        if len(text) > 60:
                            ev["abstract"] = text
                            log.debug(f"Abstract fetched for: {ev['title'][:50]}")
                except Exception as exc:
                    log.debug(f"Detail page failed ({ev.get('url')}): {exc}")

            browser.close()
    except Exception as exc:
        log.error(f"Playwright failed for {url}: {exc}")
        return []

    log.info(f"Website scrape ({etype}): {len(events)} events "
             f"({sum(1 for e in events if e.get('abstract'))} with abstracts)")
    return events


def _parse_events_html(soup: BeautifulSoup, etype: str) -> list:
    """
    Parse Joomla/JEvents event pages. Tries several common CSS selectors.
    Returns whatever it can find; may be empty if the site structure changed.
    """
    # JEvents and common Joomla event selectors (add more if the site changes)
    for selector in [
        ".jev_listrow", ".ev_td_left", "table.jevents_table tr",
        ".vevent", "article.item", ".event-item", ".eventlist",
    ]:
        containers = soup.select(selector)
        if containers:
            break
    else:
        log.debug("No event containers matched; HTML may still be a challenge page")
        return []

    events = []
    for c in containers:
        title_el = (
            c.find(class_=re.compile(r"summary|title|subject", re.I))
            or c.find(["h2", "h3", "h4"])
        )
        if not title_el:
            continue

        date_el     = c.find(class_=re.compile(r"date|dtstart|when",    re.I))
        speaker_el  = c.find(class_=re.compile(r"speaker|presenter|who", re.I))
        abstract_el = c.find(class_=re.compile(r"abstract|desc|body",    re.I))
        link_el     = title_el.find("a", href=True) or c.find("a", href=True)

        link = link_el["href"] if link_el else None
        if link and not link.startswith("http"):
            link = "https://physics.rutgers.edu" + link

        events.append({
            "type":        etype,
            "label":       SEMINAR_LABELS.get(etype, etype),
            "section":     "current",
            "day":         "",
            "date":        date_el.get_text(strip=True)     if date_el     else "",
            "time":        "",
            "location":    "",
            "speaker":     speaker_el.get_text(strip=True)  if speaker_el  else "",
            "institution": "",
            "title":       title_el.get_text(strip=True),
            "abstract":    abstract_el.get_text(strip=True) if abstract_el else None,
            "url":         link,
        })

    return events


# ── QR code generator ────────────────────────────────────────────────────────

def make_qr_b64(url: str) -> str:
    """
    Generate a QR code for url and return a base64-encoded SVG data URI.
    Returns '' if the qrcode library is not installed.
    """
    if not HAS_QR:
        return ""
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buf = BytesIO()
    img.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/svg+xml;base64,{b64}"


def generate_qr_codes() -> dict:
    """Pre-generate QR codes for all key department URLs."""
    if not HAS_QR:
        log.info("qrcode library not installed — QR codes skipped. "
                 "Run: pip install qrcode")
        return {}
    log.info("Generating QR codes…")
    urls = {
        "colloquium": ("https://physics.rutgers.edu/events/colloquium",
                       "events/colloquium"),
        "seminars":   ("https://physics.rutgers.edu/events/seminars",
                       "events/seminars"),
        "irons":      ("https://www.physics.rutgers.edu/irons/",
                       "irons lecture"),
        "news":       ("https://physics.rutgers.edu/news/2026-news",
                       "dept. news"),
        "newsletter": ("http://www.physics.rutgers.edu/newsletter/",
                       "newsletter"),
        "home":       ("https://physics.rutgers.edu",
                       "physics.rutgers.edu"),
    }
    result = {}
    for key, (url, label) in urls.items():
        result[key] = {"data": make_qr_b64(url), "url": url, "label": label}
    log.info(f"QR codes generated for: {', '.join(result)}")
    return result


# ── News scraper ──────────────────────────────────────────────────────────────

def fetch_news() -> list:
    """Fetch recent department news items from the 2026 news page."""
    log.info("Fetching department news…")
    try:
        r = requests.get(NEWS_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        items = []
        for el in soup.select(".com-content-category-blog__item, .blog-item"):
            title_el = el.select_one(".page-header h2, h2, h3")
            date_el  = el.select_one("p em")
            # First <p> that doesn't contain the date em-tag is the lead text
            body_el  = next(
                (p for p in el.select("p") if not p.find("em")),
                None
            )
            if not title_el:
                continue
            date_str = ""
            if date_el:
                m = re.search(r'\((.+?)\)', date_el.get_text())
                date_str = m.group(1) if m else date_el.get_text(strip=True).strip("()")
            items.append({
                "title": title_el.get_text(strip=True),
                "date":  date_str,
                "body":  body_el.get_text(strip=True)[:220] if body_el else "",
            })
        log.info(f"News: {len(items[:6])} items")
        return items[:6]
    except Exception as exc:
        log.warning(f"News fetch failed (non-fatal): {exc}")
        return []


# ── Irons Public Lecture scraper ──────────────────────────────────────────────

IRONS_URL = "https://www.physics.rutgers.edu/irons/"


def fetch_irons_lecture() -> dict:
    """
    Fetch the Irons Public Lecture page.
    Parses event details from the poster's alt-text and downloads the poster image.
    """
    log.info("Fetching Irons Lecture page…")
    try:
        r = requests.get(IRONS_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        return _parse_irons(soup)
    except Exception as exc:
        log.warning(f"Irons lecture fetch failed (non-fatal): {exc}")
        return {}


def _parse_irons(soup: BeautifulSoup) -> dict:
    """
    The Irons page is a minimal page with one <img> whose alt attribute
    encodes all event details, e.g.:
      "...entitled Magic Angle Graphene..., by Pablo Jarillo-Herrero of MIT,
       held on Tuesday April 28, 2026 in room 11 SERC building..."
    """
    from urllib.parse import urljoin

    img = soup.find("img")
    if not img:
        log.warning("Irons page: no <img> found")
        return {}

    src = img.get("src", "")
    alt = img.get("alt", "")

    poster_remote = urljoin(IRONS_URL, src)

    # Download poster locally so the display works even if internet is spotty
    poster_local = _download_image(poster_remote, "irons_poster.jpg")

    # Parse the alt text for structured event details.
    # Pattern: "...entitled TITLE, by SPEAKER of INSTITUTION, held on DATE in room LOCATION..."
    title_m      = re.search(r'entitled (.+?),?\s+by\s', alt, re.IGNORECASE)
    # Anchor speaker+institution together so "of" matches the right occurrence
    spk_inst_m   = re.search(r'\bby\s+(.+?)\s+of\s+(.+?),\s+held\b', alt, re.IGNORECASE)
    date_m       = re.search(r'held on\s+(.+?)\s+in\s+room', alt, re.IGNORECASE)
    loc_m        = re.search(
        r'in\s+(room\s+\d+\s+.+?)(?:,\s*(?:Busch|Rutgers)|,\s*NJ|$)', alt, re.IGNORECASE
    )

    title = (title_m.group(1).strip() if title_m else "")
    # Fix common OCR/typo artifacts in alt text (e.g., "GrapheneL" → "Graphene:")
    title = re.sub(r'([a-zA-Z])L\s', r'\1: ', title)

    speaker     = spk_inst_m.group(1).strip() if spk_inst_m else ""
    institution = spk_inst_m.group(2).strip() if spk_inst_m else ""

    location = loc_m.group(1).strip() if loc_m else ""
    # Fix common alt-text typos and normalise capitalisation
    location = re.sub(r'\bCamput\b', 'Campus', location, flags=re.IGNORECASE)
    location = re.sub(r'\broom\b', 'Room', location, flags=re.IGNORECASE)
    location = re.sub(r'\bbuilding\b', 'Building', location, flags=re.IGNORECASE)

    return {
        "type":          "IRONS",
        "label":         "Irons Public Lecture",
        "title":         title,
        "speaker":       speaker,
        "institution":   institution,
        "date":          date_m.group(1).strip() if date_m else "",
        "time":          "",
        "location":      location,
        "abstract":      None,
        "poster_url":    poster_local or poster_remote,
        "poster_remote": poster_remote,
        "url":           IRONS_URL,
    }


def _download_image(url: str, filename: str) -> str:
    """Download an image into OUTPUT_DIR. Returns relative filename or '' on failure."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        dest = OUTPUT_DIR / filename
        dest.write_bytes(r.content)
        log.info(f"Image saved → {dest}")
        return filename
    except Exception as exc:
        log.warning(f"Image download failed ({url}): {exc}")
        return ""


# ── HTML generation ───────────────────────────────────────────────────────────

# Inline HTML template — self-contained slideshow, no external dependencies.
# %%DATA%% is replaced with the JSON payload at generation time.
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=1920">
<title>Rutgers Physics & Astronomy</title>
<style>
:root {
  --bg:     #07071c;
  --bg2:    #0c1030;
  --bg3:    #101438;
  --red:    #cc0033;
  --blue:   #4a9eff;
  --gold:   #f5a623;
  --text:   #dce0f2;
  --dim:    #6a709a;
  --border: #1a2060;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { width: 100%; height: 100vh; overflow: hidden; font-size: 18px; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Segoe UI', 'Helvetica Neue', system-ui, sans-serif;
  display: grid;
  grid-template-rows: 72px 1fr 56px;
}

/* ── Header ── */
header {
  background: var(--red);
  display: flex;
  align-items: center;
  padding: 0 44px;
  gap: 18px;
  flex-shrink: 0;
}
.hdr-logo {
  font-size: 1.9rem;
  opacity: .9;
  flex-shrink: 0;
}
.hdr-text h1  { font-size: 1.65rem; font-weight: 700; letter-spacing: .05em; }
.hdr-text sub { font-size: .8rem;  opacity: .82; letter-spacing: .06em; display: block; margin-top: 1px; }
#clock {
  margin-left: auto;
  text-align: right;
  line-height: 1.35;
}
#clock-time { font-size: 1.75rem; font-weight: 500; }
#clock-date { font-size: .82rem;  opacity: .85; }

/* ── Slideshow container ── */
#slideshow {
  position: relative;
  overflow: hidden;
  min-height: 0;
}
.slide {
  position: absolute;
  inset: 0;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.75s ease;
  display: flex;
  flex-direction: column;
  padding: 42px 64px;
  overflow: hidden;
}
.slide.active {
  opacity: 1;
  pointer-events: auto;
}

/* ── Shared slide parts ── */
.slide-label {
  font-size: .72rem;
  font-weight: 800;
  letter-spacing: .25em;
  text-transform: uppercase;
  color: var(--blue);
  margin-bottom: 18px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.slide-label::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
}
.badge {
  display: inline-block;
  font-size: .62rem;
  font-weight: 800;
  letter-spacing: .12em;
  text-transform: uppercase;
  padding: 3px 10px;
  border-radius: 3px;
  vertical-align: middle;
}
.badge-red  { background: var(--red);  color: #fff; }
.badge-blue { background: var(--blue); color: var(--bg); }
.badge-gold { background: var(--gold); color: #1a0e00; }

/* ── Colloquium slide ── */
.col-slide { justify-content: center; }
.col-meta {
  font-size: .85rem;
  color: var(--blue);
  font-weight: 600;
  letter-spacing: .07em;
  text-transform: uppercase;
  margin-bottom: 22px;
}
.col-title {
  font-size: 2.3rem;
  font-weight: 700;
  line-height: 1.22;
  margin-bottom: 22px;
  color: #fff;
}
.col-speaker {
  font-size: 1.35rem;
  font-weight: 500;
  margin-bottom: 4px;
}
.col-inst {
  font-size: 1rem;
  color: var(--dim);
  margin-bottom: 10px;
}
.col-loc {
  font-size: .88rem;
  color: var(--dim);
  margin-bottom: 28px;
}
.col-divider {
  height: 1px;
  background: var(--border);
  margin-bottom: 22px;
}
.col-abstract {
  font-size: 1rem;
  line-height: 1.72;
  color: #b0b4d8;
  display: -webkit-box;
  -webkit-line-clamp: 8;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* ── Day seminars slide ── */
.day-header {
  font-size: 1.55rem;
  font-weight: 700;
  margin-bottom: 24px;
  color: #fff;
}
.sem-row {
  display: grid;
  grid-template-columns: 230px 1fr;
  gap: 0 24px;
  align-items: start;
  padding: 16px 0;
  border-bottom: 1px solid var(--border);
}
.sem-row:last-child { border-bottom: none; }
.sem-left { display: flex; flex-direction: column; gap: 5px; padding-top: 3px; }
.sem-time { font-size: .85rem; color: var(--blue); font-weight: 600; }
.sem-loc  { font-size: .78rem; color: var(--dim); }
.sem-right {}
.sem-title   { font-size: 1.15rem; font-weight: 600; line-height: 1.3; margin-bottom: 5px; }
.sem-speaker { font-size: .92rem; color: #9aa0c8; }
.sem-inst    { color: var(--dim); font-style: italic; }

/* ── Next-week overview slide ── */
.nw-list { display: flex; flex-direction: column; gap: 0; }
.nw-row {
  display: grid;
  grid-template-columns: 200px 200px 1fr;
  gap: 0 20px;
  align-items: center;
  padding: 13px 0;
  border-bottom: 1px solid var(--border);
  font-size: .95rem;
}
.nw-row:last-child { border-bottom: none; }
.nw-when { color: var(--dim); font-size: .83rem; }
.nw-title  { font-weight: 600; }
.nw-speaker { color: #9aa0c8; font-size: .9rem; }

/* ── Irons Public Lecture slide ── */
.irons-slide {
  flex-direction: row !important;
  align-items: center;
  gap: 56px;
  padding-top: 56px !important;
}
.irons-left { flex: 1; display: flex; flex-direction: column; gap: 14px; }
.irons-super {
  font-size: .72rem; font-weight: 800; letter-spacing: .25em;
  text-transform: uppercase; color: var(--gold);
}
.irons-title {
  font-size: 1.75rem; font-weight: 700; line-height: 1.25; color: #fff;
}
.irons-speaker { font-size: 1.3rem; font-weight: 500; }
.irons-inst    { font-size: 1rem; color: var(--dim); }
.irons-when    { font-size: 1.05rem; color: var(--blue); font-weight: 600; margin-top: 6px; }
.irons-loc     { font-size: .88rem; color: var(--dim); }
.irons-link    { font-size: .72rem; color: var(--dim); margin-top: 8px; }
.irons-right {
  flex: 1.3;
  display: flex;
  align-items: center;
  justify-content: center;
  max-height: 78vh;
  overflow: hidden;
}
.irons-right img {
  max-width: 100%;
  max-height: 78vh;
  object-fit: contain;
  border-radius: 6px;
  box-shadow: 0 10px 50px rgba(0,0,0,.7);
}

/* ── News slide ── */
.news-item {
  padding: 17px 0;
  border-bottom: 1px solid var(--border);
}
.news-item:last-child { border-bottom: none; }
.news-title { font-size: 1.15rem; font-weight: 600; margin-bottom: 4px; line-height: 1.3; }
.news-date  { font-size: .78rem; color: var(--dim); margin-bottom: 5px; }
.news-body  { font-size: .9rem; color: #9aa0c8; line-height: 1.55; }

/* ── Empty state ── */
.empty { color: var(--dim); font-style: italic; font-size: 1.1rem; padding: 40px 0; }

/* ── Footer ── */
footer {
  background: #030312;
  border-top: 2px solid var(--border);
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  padding: 0 22px 0 0;
  gap: 18px;
  flex-shrink: 0;
  height: 64px;
}
/* Progress bar across the very top of the footer */
footer::before {
  content: '';
  position: absolute;
  bottom: 64px;
  left: 0;
  height: 3px;
  background: var(--border);
  width: 100%;
  pointer-events: none;
}
#progress-bar {
  position: absolute;
  bottom: 64px;
  left: 0;
  height: 3px;
  background: var(--blue);
  width: 0%;
  transition: width 0s linear;
  z-index: 10;
}
/* Ticker section */
.ticker-section {
  display: flex;
  align-items: center;
  min-width: 0;
  height: 100%;
  overflow: hidden;
}
.ticker-badge {
  background: var(--red);
  color: #fff;
  font-size: .65rem;
  font-weight: 800;
  letter-spacing: .18em;
  text-transform: uppercase;
  padding: 6px 14px;
  white-space: nowrap;
  flex-shrink: 0;
  height: 100%;
  display: flex;
  align-items: center;
  margin-right: 14px;
}
.ticker-wrap {
  flex: 1;
  overflow: hidden;
  mask-image: linear-gradient(to right, transparent, black 2%, black 96%, transparent);
}
.ticker-inner {
  display: inline-block;
  white-space: nowrap;
  font-size: 1.25rem;
  color: #dde4ff;
  font-weight: 400;
  animation: ticker-scroll 70s linear infinite;
}
@keyframes ticker-scroll {
  from { transform: translateX(100vw); }
  to   { transform: translateX(-100%); }
}
/* Dots */
#slide-dots {
  display: flex;
  gap: 7px;
  align-items: center;
  flex-shrink: 0;
}
.dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--border);
  transition: background .3s, transform .3s;
}
.dot.active { background: var(--blue); transform: scale(1.4); }
#footer-meta {
  font-size: .72rem;
  color: var(--dim);
  text-align: right;
  flex-shrink: 0;
  line-height: 1.5;
}

/* ── QR Code corner ── */
.slide-qr {
  position: absolute;
  bottom: 22px;
  right: 32px;
  background: #fff;
  border-radius: 8px;
  padding: 8px 8px 5px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  box-shadow: 0 4px 20px rgba(0,0,0,.5);
  z-index: 5;
}
.slide-qr img {
  width: 108px;
  height: 108px;
  display: block;
}
.slide-qr .qr-label {
  font-size: .52rem;
  color: #333;
  font-weight: 700;
  text-align: center;
  text-transform: uppercase;
  letter-spacing: .06em;
  max-width: 108px;
}
</style>
</head>
<body>

<header>
  <div class="hdr-logo">⚛</div>
  <div class="hdr-text">
    <h1>Rutgers Physics &amp; Astronomy</h1>
    <sub>Department Events &amp; Seminars</sub>
  </div>
  <div id="clock">
    <div id="clock-time"></div>
    <div id="clock-date"></div>
  </div>
</header>

<div id="slideshow"></div>
<div id="progress-bar"></div>

<footer>
  <div class="ticker-section">
    <div class="ticker-badge">Updates</div>
    <div class="ticker-wrap"><span class="ticker-inner" id="ticker"></span></div>
  </div>
  <div id="slide-dots"></div>
  <div id="footer-meta"></div>
</footer>

<script>
const DATA = %%DATA%%;

// ── Utilities ───────────────────────────────────────────────────────
function esc(s) {
  return String(s || '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function when(e) {
  return [e.day, e.date, e.time, e.location].filter(Boolean).join(' \u2022 ');
}

// ── Clock ───────────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  document.getElementById('clock-time').textContent =
    now.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});
  document.getElementById('clock-date').textContent =
    now.toLocaleDateString('en-US',{weekday:'long',month:'long',day:'numeric',year:'numeric'});
}
updateClock();
setInterval(updateClock, 30000);

// ── QR code helper ──────────────────────────────────────────────────
function qrCorner(key) {
  const qrs = DATA.qr_codes || {};
  const q   = qrs[key];
  if (!q || !q.data) return '';
  return `<div class="slide-qr">
    <img src="${q.data}" alt="QR code">
    <div class="qr-label">${esc(q.label)}</div>
  </div>`;
}

// ── Slide builders ──────────────────────────────────────────────────

function colloquiumSlide(e, isNext) {
  const labelText = isNext ? 'Upcoming Colloquium' : 'Colloquium';
  const badge     = isNext ? 'badge-gold' : 'badge-red';
  const titleQuoted = (e.title && e.title !== 'TBA')
    ? `\u201C${esc(e.title)}\u201D` : esc(e.title || 'TBA');

  return `
<div class="slide col-slide">
  <div class="slide-label">
    <span class="badge ${badge}">${labelText}</span>
  </div>
  <div class="col-meta">${esc(e.day)} &bull; ${esc(e.date)} &bull; ${esc(e.time)}</div>
  <div class="col-title">${titleQuoted}</div>
  <div class="col-speaker">${esc(e.speaker)}</div>
  <div class="col-inst">${esc(e.institution)}</div>
  <div class="col-loc">\uD83D\uDCCD ${esc(e.location)}</div>
  ${e.abstract ? `<div class="col-divider"></div><div class="col-abstract">${esc(e.abstract)}</div>` : ''}
  ${qrCorner('colloquium')}
</div>`;
}

function daySeminarsSlide(dayLabel, dateLabel, events, isNext) {
  const badge = isNext ? 'badge-gold' : 'badge-blue';
  const label = isNext ? 'Next Week \u2014 ' + dayLabel : dayLabel;

  const rows = events.map(e => `
<div class="sem-row">
  <div class="sem-left">
    <span class="badge ${badge}">${esc(e.label || e.type)}</span>
    <span class="sem-time">${esc(e.time)}</span>
    <span class="sem-loc">${esc(e.location)}</span>
  </div>
  <div class="sem-right">
    <div class="sem-title">${esc(e.title || 'TBA')}</div>
    <div class="sem-speaker">${esc(e.speaker)}${e.institution
      ? ' <span class="sem-inst">\u2014 ' + esc(e.institution) + '</span>' : ''}</div>
  </div>
</div>`).join('');

  return `
<div class="slide">
  <div class="slide-label"><span class="badge ${badge}">${label}</span> &nbsp; ${esc(dateLabel)}</div>
  <div class="day-header">${esc(dayLabel)}</div>
  ${rows}
  ${qrCorner('seminars')}
</div>`;
}

// ── Irons Lecture slide ─────────────────────────────────────────────
function ironsSlide(d) {
  if (!d || !d.title) return null;
  const title = d.title ? `\u201C${esc(d.title)}\u201D` : 'TBA';
  const dateStr = d.date || '';
  return `
<div class="slide irons-slide">
  <div class="slide-label" style="position:absolute;top:28px;left:64px;right:64px;margin:0;">
    <span class="badge badge-gold">Special Public Lecture</span>
  </div>
  <div class="irons-left">
    <div class="irons-super">Irons Public Lecture</div>
    <div class="irons-title">${title}</div>
    <div class="irons-speaker">${esc(d.speaker)}</div>
    <div class="irons-inst">${esc(d.institution)}</div>
    <div class="irons-when">\uD83D\uDCC5 ${esc(dateStr)}</div>
    ${d.location ? `<div class="irons-loc">\uD83D\uDCCD ${esc(d.location)}, Busch Campus</div>` : ''}
  </div>
  <div class="irons-right">
    <img src="${esc(d.poster_url)}" alt="Irons Lecture poster"
         onerror="this.src='${esc(d.poster_remote || '')}';this.onerror=null;">
  </div>
  ${qrCorner('irons')}
</div>`;
}

// ── Department News slide ────────────────────────────────────────────
function newsSlide(items) {
  if (!items || !items.length) return null;
  const rows = items.slice(0, 4).map(n => `
    <div class="news-item">
      <div class="news-title">${esc(n.title)}</div>
      ${n.date ? `<div class="news-date">${esc(n.date)}</div>` : ''}
      ${n.body ? `<div class="news-body">${esc(n.body)}</div>` : ''}
    </div>`).join('');
  return `
<div class="slide">
  <div class="slide-label"><span class="badge badge-blue">Department News</span></div>
  ${rows}
  ${qrCorner('news')}
</div>`;
}

// ── Build slide list ────────────────────────────────────────────────
function buildSlides() {
  const slides = [];
  const col   = DATA.colloquium || [];
  const sems  = DATA.seminars   || [];
  const news  = DATA.news       || [];
  const irons = DATA.irons      || null;

  // 1. This week's colloquium (with abstract if Playwright was used)
  col.filter(e => e.section !== 'next')
     .forEach(e => slides.push({ html: colloquiumSlide(e, false), duration: e.abstract ? 22000 : 14000 }));

  // 2. Irons Public Lecture (special event, prominently placed)
  const ironsHtml = ironsSlide(irons);
  if (ironsHtml) slides.push({ html: ironsHtml, duration: 20000 });

  // 3. This week's seminars, one slide per day
  const curSems = sems.filter(e => e.section !== 'next');
  const curDays = [...new Set(curSems.map(e => e.date))];
  curDays.forEach(date => {
    const evs = curSems.filter(e => e.date === date);
    const dur = 8000 + evs.length * 3500;
    slides.push({ html: daySeminarsSlide(evs[0].day, date, evs, false), duration: dur });
  });

  // 4. Department news slide
  const newsHtml = newsSlide(news);
  if (newsHtml) slides.push({ html: newsHtml, duration: 16000 });

  // 5. Next week's colloquium
  col.filter(e => e.section === 'next')
     .forEach(e => slides.push({ html: colloquiumSlide(e, true), duration: 14000 }));

  // 6. Next week's seminars, one slide per day
  const nextSems = sems.filter(e => e.section === 'next');
  const nextDays = [...new Set(nextSems.map(e => e.date))];
  nextDays.forEach(date => {
    const evs = nextSems.filter(e => e.date === date);
    const dur = 8000 + evs.length * 3000;
    slides.push({ html: daySeminarsSlide(evs[0].day, date, evs, true), duration: dur });
  });

  // Fallback
  if (!slides.length) {
    slides.push({
      html: '<div class="slide" style="justify-content:center;align-items:center">'
          + '<div class="empty">No event data available. Check back soon.</div></div>',
      duration: 10000
    });
  }

  return slides;
}

// ── Slideshow controller ────────────────────────────────────────────
const SLIDES = buildSlides();
let current  = 0;
let timer    = null;

const container = document.getElementById('slideshow');
const dotsEl    = document.getElementById('slide-dots');
const progBar   = document.getElementById('progress-bar');

// Inject all slide divs
container.innerHTML = SLIDES.map(s => s.html).join('');
const slideDivs = container.querySelectorAll('.slide');

// Build dots
dotsEl.innerHTML = SLIDES.map((_, i) =>
  `<span class="dot${i === 0 ? ' active' : ''}"></span>`
).join('');
const dots = dotsEl.querySelectorAll('.dot');

function showSlide(idx) {
  slideDivs[current].classList.remove('active');
  dots[current].classList.remove('active');
  current = (idx + SLIDES.length) % SLIDES.length;
  slideDivs[current].classList.add('active');
  dots[current].classList.add('active');
  startProgress(SLIDES[current].duration);
}

function startProgress(duration) {
  // Reset bar
  progBar.style.transition = 'none';
  progBar.style.width = '0%';
  // Force reflow so transition resets visually
  progBar.getBoundingClientRect();
  progBar.style.transition = `width ${duration}ms linear`;
  progBar.style.width = '100%';

  clearTimeout(timer);
  timer = setTimeout(() => showSlide(current + 1), duration);
}

// Kick off
slideDivs[0].classList.add('active');
startProgress(SLIDES[0].duration);

// ── Ticker ──────────────────────────────────────────────────────────
function renderTicker() {
  const news  = DATA.news || [];
  const items = news.map(n => n.date ? `${n.title}  (${n.date})` : n.title);
  if (items.length) {
    document.getElementById('ticker').textContent = items.join('   \u25CF   ');
  }
}
renderTicker();

// ── Footer meta ─────────────────────────────────────────────────────
(function renderMeta() {
  const lines = [];
  if (DATA.nl_issue) lines.push(`Newsletter ${DATA.nl_issue}${DATA.nl_date ? ' \u2022 ' + DATA.nl_date : ''}`);
  if (DATA.updated_at) {
    lines.push('Updated ' + new Date(DATA.updated_at)
      .toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'}));
  }
  document.getElementById('footer-meta').innerHTML = lines.join('<br>');
})();

// Reload every 60 min to pick up freshly generated data
setTimeout(() => location.reload(), 60 * 60 * 1000);
</script>
</body>
</html>
"""


def generate_html(data: dict) -> Path:
    """Inject JSON data into the HTML template and write display.html."""
    json_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    html = _HTML_TEMPLATE.replace("%%DATA%%", json_str)
    HTML_FILE.write_text(html, encoding="utf-8")
    log.info(f"Display written → {HTML_FILE}")
    return HTML_FILE


# ── Orchestration ─────────────────────────────────────────────────────────────

def load_cache() -> dict:
    """Load last successful events.json as fallback."""
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def run() -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)

    # 1. Newsletter (primary, always attempted)
    nl = fetch_newsletter()
    colloquium = nl.get("colloquium", [])
    seminars   = nl.get("seminars",   [])

    # 2. Department website via Playwright (for abstracts — optional)
    if HAS_PLAYWRIGHT:
        web_col = fetch_website_events(COLLOQUIUM_URL, "COL")
        web_sem = fetch_website_events(SEMINARS_URL,   "SEM")
        # Prefer website data when available (it may include abstracts)
        if web_col:
            log.info("Using website colloquium data (may include abstracts)")
            colloquium = web_col
        if web_sem:
            log.info("Using website seminar data")
            # Merge: website first (richer), newsletter as fallback for extras
            seen_titles = {e["title"] for e in web_sem}
            extras = [e for e in seminars if e["title"] not in seen_titles]
            seminars = web_sem + extras
    else:
        log.info(
            "Playwright not installed — newsletter-only mode "
            "(no abstracts). Run: pip install playwright && "
            "python -m playwright install chromium"
        )

    # 3. News, special events, and QR codes (best-effort, no challenge wall)
    news     = fetch_news()
    irons    = fetch_irons_lecture()
    qr_codes = generate_qr_codes()

    data = {
        "colloquium": colloquium,
        "seminars":   seminars,
        "news":       news,
        "irons":      irons,
        "qr_codes":   qr_codes,
        "nl_issue":   nl.get("nl_issue", ""),
        "nl_date":    nl.get("nl_date",  ""),
        "updated_at": datetime.now().isoformat(),
    }

    # 4. If nothing was scraped, fall back to last good cache
    if not colloquium and not seminars:
        cached = load_cache()
        if cached:
            log.warning("No fresh data — using cached events")
            cached["updated_at"] = data["updated_at"]  # keep timestamp fresh
            data = cached

    # 5. Save cache
    CACHE_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info(f"Cache saved → {CACHE_FILE}")

    return generate_html(data)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Physics Department Signage Updater")
    parser.add_argument("--serve",  action="store_true",
                        help="Serve output/ on :8080 after generating")
    parser.add_argument("--debug",  action="store_true",
                        help="Enable verbose logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    out = run()

    if args.serve:
        import http.server
        import os
        os.chdir(OUTPUT_DIR)

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

        port = 8080
        with http.server.HTTPServer(("", port), QuietHandler) as srv:
            log.info(f"Serving at http://localhost:{port}  (Ctrl-C to stop)")
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
