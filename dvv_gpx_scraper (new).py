#!/usr/bin/env python3
"""
DVV "Permanente Wanderwege" scraper - ALL GERMANY.

Crawls the single flat nationwide listing page, which links to every
route across every DVV region. Each route link's URL already encodes
its region (e.g. .../permanente-wanderwege/hessen/wanderweg/<slug>),
so no need to crawl per-region pages separately.

For each route: extracts name (+ best-effort English translation),
code, operator, full start address, start-location hours, and a merged
"trail notes" field (closing time / year-round access / winter
maintenance / directions - whichever of these the page actually has,
since not every page uses the same labels).

This is a MUCH bigger scrape than a single state: expect on the order
of ~190 route pages and ~400-500 GPX files nationwide. Be patient -
this can take 10-20+ minutes and will download tens of MB. Because of
the size, be considerate about how often you re-run this against
DVV's server.

Usage:
    pip install requests beautifulsoup4
    python dvv_gpx_scraper.py

Output:
    ./dvv_gpx_data/manifest.json
    ./dvv_gpx_data/gpx/<region>/*.gpx

Note on translation: uses a free, UNOFFICIAL Google endpoint (no API
key). It's commonly used for exactly this kind of script but isn't a
documented/guaranteed API - if it's ever blocked, translation silently
fails and routes just show their German name. Swap in a paid API
(DeepL, Google Cloud Translation) for anything more serious.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.dvv-wandern.de"
OVERVIEW_URL = f"{BASE_URL}/permanente-wanderwege/"
OUTPUT_DIR = Path("./dvv_gpx_data")
GPX_DIR = OUTPUT_DIR / "gpx"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}
REQUEST_DELAY_SECONDS = 2.0  # heavier crawl than a single state - be extra polite

session = requests.Session()
session.headers.update(HEADERS)

# Route links look like: /permanente-wanderwege/<region-slug>/wanderweg/<route-slug>
ROUTE_LINK_PATTERN = re.compile(r"^/permanente-wanderwege/([^/]+)/wanderweg/([^/?]+)/?$")

# DVV divides Bavaria into its 7 Regierungsbezirke instead of listing it as
# one state; consolidate all of these (and everything else) onto the real
# 16 German Bundesländer instead of DVV's internal regional divisions.
REGION_TO_STATE = {
    "schleswig-holstein": "schleswig-holstein",
    "hamburg": "hamburg",
    "mecklenburg-vorpommern": "mecklenburg-vorpommern",
    "niedersachsen": "niedersachsen",
    "bremen": "bremen",
    "berlin": "berlin",
    "brandenburg": "brandenburg",
    "sachsen-anhalt": "sachsen-anhalt",
    "nordrhein-westfalen": "nordrhein-westfalen",
    "rheinland-pfalz": "rheinland-pfalz",
    "hessen": "hessen",
    "saarland": "saarland",
    "thueringen": "thueringen",
    "sachsen": "sachsen",
    "baden-wuerttemberg": "baden-wuerttemberg",
    # Bavaria's 7 Regierungsbezirke all consolidate to one "bayern" entry
    "schwaben": "bayern",
    "mittelfranken": "bayern",
    "oberfranken": "bayern",
    "unterfranken": "bayern",
    "oberpfalz": "bayern",
    "niederbayern": "bayern",
    "muenchen-oberbayern": "bayern",
    "oberbayern": "bayern",
    "bayern": "bayern",
}

STATE_LABELS = {
    "schleswig-holstein": "Schleswig-Holstein",
    "hamburg": "Hamburg",
    "mecklenburg-vorpommern": "Mecklenburg-Vorpommern",
    "niedersachsen": "Niedersachsen",
    "bremen": "Bremen",
    "berlin": "Berlin",
    "brandenburg": "Brandenburg",
    "sachsen-anhalt": "Sachsen-Anhalt",
    "nordrhein-westfalen": "Nordrhein-Westfalen",
    "rheinland-pfalz": "Rheinland-Pfalz",
    "hessen": "Hessen",
    "saarland": "Saarland",
    "thueringen": "Thüringen",
    "sachsen": "Sachsen",
    "baden-wuerttemberg": "Baden-Württemberg",
    "bayern": "Bayern",
}


def canonical_region(slug: str) -> str:
    """Map a raw DVV URL region slug onto one of the 16 real states."""
    return REGION_TO_STATE.get(slug, slug)


def region_label(canonical_slug: str) -> str:
    return STATE_LABELS.get(canonical_slug, canonical_slug.replace("-", " ").title())


# Section labels as they appear once HTML is flattened to plain text.
CORE_LABELS = ["Betreiber:", "Auskunft:", "Start und Ziel:"]
STOP_TOKENS = ["Für den Download", "GPS-Download", "Ausschreibung"]

HOURS_LINE_PATTERN = re.compile(
    r"\d{1,2}[.:]\d{2}\s*-|Uhr\b|Ruhetag|durchgehend|ganzjährig ge(ö|oe)ffnet",
    re.IGNORECASE,
)
# Lines matching this near the start of the "Start und Ziel" block signal
# we've moved from address fragments into freeform prose (notes/directions).
NOTES_START_PATTERN = re.compile(
    r"^(Der\b|Die\b|Das\b|Bitte\b|Kein\b|Ohne\b|Achtung\b|Hinweis\b|"
    r"Zielschluss|Anfahrt|Ganzjährig|Info:|spätestens|Mit öffentlichen)",
    re.IGNORECASE,
)


DEBUG = True
DEBUG_DIR = OUTPUT_DIR / "debug"


def get_soup(url: str) -> BeautifulSoup:
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    if DEBUG:
        all_links = soup.find_all("a", href=True)
        print(f"    [debug] status={resp.status_code} content-length={len(resp.text)} <a> tags found={len(all_links)}")
        if len(all_links) < 5:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^\w-]", "_", urlparse(url).path.strip("/")) or "root"
            dump_path = DEBUG_DIR / (safe_name + ".html")
            dump_path.write_text(resp.text, encoding="utf-8")
            print(f"    [debug] very few links found — raw HTML saved to {dump_path}")

    return soup


def find_route_links(overview_soup: BeautifulSoup, page_url: str) -> list[tuple[str, str]]:
    """
    Returns [(region_slug, full_route_url), ...] for every route link.

    Note: these hrefs are written root-relative but WITHOUT a leading
    slash (e.g. "permanente-wanderwege/hessen/wanderweg/<slug>"), so we
    resolve them against the bare domain rather than the current page
    URL - joining against the current page (which itself lives under
    /permanente-wanderwege/) would double up the path.
    """
    found = {}
    for a in overview_soup.find_all("a", href=True):
        full_url = urljoin(BASE_URL + "/", a["href"])
        parsed = urlparse(full_url)
        m = ROUTE_LINK_PATTERN.match(parsed.path)
        if not m:
            continue
        region_slug = m.group(1)
        clean_url = parsed._replace(query="", fragment="").geturl()
        found[clean_url] = region_slug
    return sorted((region, url) for url, region in found.items())


def direct_file_url_from_query_link(href: str) -> str | None:
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    file_param = qs.get("file", [None])[0]
    if file_param:
        return urljoin(BASE_URL + "/", unquote(file_param))
    return None


def find_gpx_links(route_soup: BeautifulSoup, route_page_url: str) -> list[str]:
    gpx_urls = []
    for a in route_soup.find_all("a", href=True):
        href = a["href"]
        if ".gpx" not in href.lower():
            continue
        # same root-relative-without-leading-slash quirk as the overview page
        full_url = urljoin(BASE_URL + "/", href)
        direct = direct_file_url_from_query_link(full_url)
        gpx_urls.append(direct or full_url)
    seen = set()
    unique = []
    for u in gpx_urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def extract_title_name(route_soup: BeautifulSoup) -> str | None:
    if not route_soup.title or not route_soup.title.string:
        return None
    raw = route_soup.title.string.strip()
    prefix = "DVV Wandern - "
    if raw.startswith(prefix):
        raw = raw[len(prefix):]
    return raw.strip(" \"'„“”‚‘’").strip() or None


def extract_core_sections(text: str) -> dict:
    """Betreiber / Auskunft, bounded by the next known label."""
    positions = []
    for label in CORE_LABELS:
        idx = text.find(label)
        if idx != -1:
            positions.append((idx, label))
    positions.sort()

    sections = {}
    for i, (idx, label) in enumerate(positions):
        start = idx + len(label)
        end = positions[i + 1][0] if i + 1 < len(positions) else None
        sections[label.rstrip(":")] = (start, end)
    return sections


POSTAL_CODE_PATTERN = re.compile(r"^\d{5}\b")


def group_address_lines(address_lines: list[str]) -> str | None:
    """
    German addresses reliably end with a 5-digit postal code + city
    (e.g. "65307 Bad Schwalbach"). Some routes list up to three separate
    start-point addresses back to back with no other separator, so use
    that postal-code line as the boundary marker between one address
    block and the next, joining fragments within a block by ", " and
    blocks themselves by a newline.
    """
    if not address_lines:
        return None
    blocks = []
    current = []
    for line in address_lines:
        current.append(line)
        if POSTAL_CODE_PATTERN.match(line.strip()):
            blocks.append(", ".join(current))
            current = []
    if current:
        blocks.append(", ".join(current))
    return "\n".join(blocks)


def classify_start_block(raw_block: str) -> tuple[str | None, str | None, str | None]:
    """
    Split the "Start und Ziel" block (address + hours + possibly trailing
    prose about closing time / winter maintenance / directions, whether
    or not that prose has its own bold label) into:
    (address, hours, notes)
    """
    # Any explicit sub-labels just become part of the surrounding prose
    cleaned = raw_block.replace("Zielschluss:", " ").replace("Anfahrt:", " ")
    lines = [l.strip() for l in cleaned.split("\n") if l.strip()]

    address_lines, hour_lines, notes_lines = [], [], []
    in_notes = False
    for line in lines:
        if in_notes:
            notes_lines.append(line)
            continue
        if HOURS_LINE_PATTERN.search(line):
            hour_lines.append(line)
            continue
        word_count = len(line.split())
        looks_like_prose = line.endswith(".") or NOTES_START_PATTERN.search(line) or word_count > 8
        if looks_like_prose:
            in_notes = True
            notes_lines.append(line)
            continue
        address_lines.append(line)

    address = group_address_lines(address_lines) if address_lines else None
    hours = "; ".join(hour_lines) if hour_lines else None
    notes = " ".join(notes_lines) if notes_lines else None
    return address, hours, notes


_translation_cache: dict[str, str | None] = {}


def translate_de_to_en(text: str | None) -> str | None:
    if not text:
        return None
    if text in _translation_cache:
        return _translation_cache[text]

    url = "https://translate.googleapis.com/translate_a/single"
    params = {"client": "gtx", "sl": "de", "tl": "en", "dt": "t", "q": text}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        translated = "".join(chunk[0] for chunk in data[0] if chunk[0]).strip()
        _translation_cache[text] = translated or None
        return _translation_cache[text]
    except Exception:
        _translation_cache[text] = None
        return None


def extract_metadata(route_soup: BeautifulSoup) -> dict:
    text = route_soup.get_text("\n", strip=True)

    title = extract_title_name(route_soup)
    title_en = translate_de_to_en(title)

    code_match = re.search(r"\bPW\s?\d+\s?[A-ZÄÖÜ]{2,3}\b", text)
    code = code_match.group(0) if code_match else None

    core = extract_core_sections(text)
    operator = None
    if "Betreiber" in core:
        s, e = core["Betreiber"]
        operator = text[s:e].strip() if e else text[s:s + 300].strip()

    start_address = start_hours = accessibility_notes = None
    if "Start und Ziel" in core:
        s, _ = core["Start und Ziel"]
        # bound the raw block at the first "download links start here" marker
        remainder = text[s:]
        end_rel = len(remainder)
        for token in STOP_TOKENS:
            idx = remainder.find(token)
            if idx != -1:
                end_rel = min(end_rel, idx)
        raw_block = remainder[:end_rel]
        start_address, start_hours, accessibility_notes = classify_start_block(raw_block)

    return {
        "title": title,
        "title_en": title_en,
        "code": code,
        "operator": operator,
        "start_address": start_address,
        "start_hours": start_hours,
        "accessibility_notes": accessibility_notes,
    }


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "route").strip().lower()
    return re.sub(r"[\s_]+", "-", text)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []

    print(f"Warming up session at {BASE_URL} ...")
    try:
        warmup = session.get(BASE_URL, timeout=20)
        print(f"    [debug] homepage status={warmup.status_code}")
    except requests.RequestException as e:
        print(f"    ! Warmup request failed (continuing anyway): {e}")

    print(f"Fetching nationwide overview page: {OVERVIEW_URL}")
    overview_soup = get_soup(OVERVIEW_URL)
    route_links = find_route_links(overview_soup, OVERVIEW_URL)
    print(f"Found {len(route_links)} route pages across {len(set(r for r, _ in route_links))} regions.\n")

    if not route_links:
        all_hrefs = [a["href"] for a in overview_soup.find_all("a", href=True)]
        wanderweg_hrefs = [h for h in all_hrefs if "wanderweg" in h.lower()]
        print(f"[debug] No route links matched my pattern. Total <a href> on page: {len(all_hrefs)}")
        print(f"[debug] hrefs containing 'wanderweg' ({len(wanderweg_hrefs)} of them):")
        for h in wanderweg_hrefs[:40]:
            print(f"    {h}")
        if not wanderweg_hrefs:
            print("[debug] None contain 'wanderweg' at all — first 40 hrefs overall instead:")
            for h in all_hrefs[:40]:
                print(f"    {h}")
        print(
            "\nNo routes to scrape - check the hrefs above (and any file in "
            f"{DEBUG_DIR}/ if it was created) to see what the real link pattern looks like."
        )
        return

    for i, (raw_region_slug, route_url) in enumerate(route_links, 1):
        state_slug = canonical_region(raw_region_slug)
        print(f"[{i}/{len(route_links)}] ({region_label(state_slug)}) {route_url}")
        try:
            route_soup = get_soup(route_url)
        except requests.RequestException as e:
            print(f"    ! Failed to fetch route page: {e}")
            continue

        meta = extract_metadata(route_soup)
        print(f"    Name: {meta['title']!r} / {meta['title_en']!r}")
        gpx_links = find_gpx_links(route_soup, route_url)

        if not gpx_links:
            print("    ! No GPX links found on this page - skipping.")
            continue

        region_dir = GPX_DIR / state_slug
        region_dir.mkdir(parents=True, exist_ok=True)

        for gpx_url in gpx_links:
            filename = Path(unquote(urlparse(gpx_url).path)).name
            if not filename.lower().endswith(".gpx"):
                filename = f"{slugify(meta['title'])}.gpx"
            local_path = region_dir / filename

            try:
                resp = requests.get(gpx_url, headers=HEADERS, timeout=20)
                if resp.status_code != 200 or b"<gpx" not in resp.content[:2000].lower():
                    print(f"    ! Unexpected response for {gpx_url} (status {resp.status_code})")
                    continue
                local_path.write_bytes(resp.content)
                print(f"    - Saved {state_slug}/{filename} ({len(resp.content)} bytes)")
            except requests.RequestException as e:
                print(f"    ! Failed to download {gpx_url}: {e}")
                continue

            manifest.append(
                {
                    "title": meta["title"],
                    "title_en": meta["title_en"],
                    "code": meta["code"],
                    "operator": meta["operator"],
                    "start_address": meta["start_address"],
                    "start_hours": meta["start_hours"],
                    "accessibility_notes": meta["accessibility_notes"],
                    "region": state_slug,
                    "region_label": region_label(state_slug),
                    "source_page": route_url,
                    "gpx_source_url": gpx_url,
                    "gpx_local_path": str(local_path.as_posix()),
                }
            )

        time.sleep(REQUEST_DELAY_SECONDS)

    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. Wrote {len(manifest)} route entries to {MANIFEST_PATH}")


if __name__ == "__main__":
    main()
