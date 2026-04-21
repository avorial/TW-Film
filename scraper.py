"""
scraper.py  —  Core scraping and HTML-generation engine for the
               Twin Cities Art Cinema Scraper.

Scraping strategy (hybrid):
    - Standard sites (js_heavy=False): fast requests + BeautifulSoup
    - JS-rendered sites (js_heavy=True): headless Chromium via Playwright

    Both paths share the same CSS-selector parsing logic via
    _parse_theater_html(), so adding a new theater only requires a config
    entry in sites_config.py — no extra code needed.

Usage modes (CLI):
    python scraper.py                  # scrape all theaters, print summary
    python scraper.py --preview        # open HTML in browser
    python scraper.py --theater Trylon # single theater only

Playwright setup (first run):
    pip install playwright
    playwright install chromium
"""

import argparse
import datetime
import json
import logging
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

EMAIL_RECIPIENT    = "your@email.com"          # default To: address
EMAIL_SUBJECT      = "🎬 Twin Cities Art Cinema — What's Playing – {date}"
FILMS_PER_SITE     = 20                        # max films to scrape per theater
OUTPUT_DIR         = Path(__file__).parent
_SYNOPSIS_CACHE_FILE = OUTPUT_DIR / "data" / "synopsis_cache.json"

# ---------------------------------------------------------------------------
# Persistent synopsis cache — survives between scrape runs
# ---------------------------------------------------------------------------

def _load_synopsis_cache() -> dict[str, str]:
    """Load the on-disk synopsis cache (title_slug → text).  Returns {} on error."""
    try:
        if _SYNOPSIS_CACHE_FILE.exists():
            return json.loads(_SYNOPSIS_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_synopsis_cache(cache: dict[str, str]) -> None:
    """Persist the synopsis cache to disk."""
    try:
        _SYNOPSIS_CACHE_FILE.parent.mkdir(exist_ok=True)
        _SYNOPSIS_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        log.warning("Could not save synopsis cache: %s", exc)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

log = logging.getLogger("cinema")


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# TITLE NORMALISATION
# ---------------------------------------------------------------------------

_TITLE_LOWER_WORDS = frozenset({
    "a", "an", "the", "and", "but", "or", "for", "nor", "so", "yet",
    "at", "by", "in", "of", "on", "to", "up", "as", "via",
})


def _is_all_caps(text: str) -> bool:
    """True when 80%+ of the alphabetic characters are uppercase."""
    alpha = [c for c in text if c.isalpha()]
    return bool(alpha) and sum(1 for c in alpha if c.isupper()) / len(alpha) >= 0.80


def _smart_title_case(text: str) -> str:
    """
    Convert an ALL-CAPS title to Title Case, keeping small connective words
    lowercase (except at the start of the string).

    Handles apostrophes ("CRONIN'S" → "Cronin's"), numbers, and
    mixed tokens like "in 70MM" → "in 70mm".
    """
    def _cap_word(word: str, is_first: bool) -> str:
        low = word.lower()
        if not is_first and low in _TITLE_LOWER_WORDS:
            return low
        # Handle apostrophes: "CRONIN'S" → "Cronin's"
        # Only capitalize the first letter of the whole token; the rest stays lower.
        if "'" in low:
            return low[0].upper() + low[1:]
        return low[0].upper() + low[1:] if low else word

    words = text.split()
    return " ".join(_cap_word(w, i == 0) for i, w in enumerate(words))


# ---------------------------------------------------------------------------

def _safe_text(tag) -> str:
    """Extract clean text from a BeautifulSoup tag, collapsing whitespace."""
    if tag is None:
        return ""
    return " ".join(tag.get_text(separator=" ", strip=True).split())


def _abs_url(href: str, base_url: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return base_url.rstrip("/") + "/" + href.lstrip("/")


# ---------------------------------------------------------------------------
# DATE PARSING — extract a calendar date from arbitrary showtime strings
# ---------------------------------------------------------------------------

_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date_from_text(text: str):
    """
    Attempt to extract the first recognisable calendar date from a string.
    Returns (datetime.date, human_label) or (None, None).

    Handles:
      "April 18"  /  "April 18, 2025"  /  "Fri April 18, 2025"
      "04/18"  /  "04/18/2025"
    Does NOT match bare time strings like "7:00 PM" — those return (None, None).
    """
    if not text:
        return None, None
    now = datetime.datetime.now()

    _month_pat = (
        r"(january|february|march|april|may|june|july|august|september|"
        r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
    )

    # Month-name first  e.g. "April 18" / "April 18, 2025" / "Apr 14th"
    m = re.search(
        _month_pat + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]+(\d{4}))?",
        text, re.IGNORECASE,
    )
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            day  = int(m.group(2))
            year = int(m.group(3)) if m.group(3) else now.year
            try:
                d = datetime.date(year, month, day)
                return d, d.strftime(f"%A, %B {d.day}, %Y")
            except ValueError:
                pass

    # Day-first  e.g. "Tuesday 14, April" (Veezi format)
    m = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?[,\s]+" + _month_pat,
        text, re.IGNORECASE,
    )
    if m:
        month = _MONTHS.get(m.group(2).lower())
        if month:
            day  = int(m.group(1))
            year = now.year
            try:
                d = datetime.date(year, month, day)
                return d, d.strftime(f"%A, %B {d.day}, %Y")
            except ValueError:
                pass

    # Numeric  MM/DD[/YYYY]
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if m:
        month = int(m.group(1))
        day   = int(m.group(2))
        year  = int(m.group(3)) if m.group(3) else now.year
        if len(str(year)) == 2:
            year += 2000
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                d = datetime.date(year, month, day)
                return d, d.strftime(f"%A, %B {d.day}, %Y")
            except ValueError:
                pass

    return None, None


def _extract_times(date_text: str) -> str:
    """
    Pull showtime(s) from a date_text string and return them as a
    human-readable string, e.g. "7:30 PM" or "5:00 PM · 9:15 PM".
    Returns "" if no times are found.
    """
    times = re.findall(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", date_text, re.IGNORECASE)
    return " · ".join(t.strip() for t in times)


def _group_films_by_date(films: list[dict]) -> list[tuple]:
    """
    Group a theater's films by parsed calendar date.
    Returns [(date_obj_or_None, day_label, [films])], sorted chronologically.
    Films without a parseable date go last under "Now Showing".
    """
    dated:   dict[datetime.date, tuple[str, list]] = {}
    undated: list[dict] = []

    for film in films:
        d, label = _parse_date_from_text(film.get("date_text", ""))
        if d:
            if d not in dated:
                dated[d] = (label, [])
            dated[d][1].append(film)
        else:
            undated.append(film)

    result = [(k, v[0], v[1]) for k, v in sorted(dated.items())]
    if undated:
        result.append((None, "Now Showing", undated))
    return result


# ---------------------------------------------------------------------------
# SYNOPSIS ENRICHMENT — Wikipedia fallback for films without descriptions
# ---------------------------------------------------------------------------

def _fetch_synopsis_from_page(url: str) -> str:
    """
    Try to fetch a synopsis from the film's own detail page on the theater's site.
    Returns the best paragraph found, or "".
    """
    if not url or not url.startswith("http"):
        return ""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        # Remove nav, header, footer, script, style noise
        for tag in soup(["nav", "header", "footer", "script", "style", "noscript"]):
            tag.decompose()
        # Try progressively broader selectors for the film description
        selectors = [
            ".entry-content p",
            "article p",
            ".film-desc p", ".synopsis p", ".description p",
            "[class*='content'] p", "[class*='about'] p",
            "main p",
        ]
        for sel in selectors:
            paras = soup.select(sel)
            for p in paras:
                text = " ".join(p.get_text(separator=" ", strip=True).split())
                # Skip boilerplate / very short lines
                if len(text) < 80:
                    continue
                skip_words = ("tickets", "showtimes", "buy ticket", "select date",
                              "©", "cookie", "privacy", "newsletter", "subscribe",
                              "follow us", "sign up", "login", "register")
                if any(w in text.lower() for w in skip_words):
                    continue
                return text[:500]
    except Exception:
        pass
    return ""


def _fetch_synopsis_wikipedia(title: str) -> str:
    """
    Fetch a brief synopsis from the Wikipedia REST API.
    Returns the extract (truncated to a sentence boundary ~500 chars) or "".
    No API key required.

    Strategy:
      1. Strip theater-specific suffixes ("in 35mm", "in 70mm", etc.)
      2. Try plain title, then "{title} (film)"
      3. If both are disambiguation pages, use the Wikipedia search API
         to find the actual film article by name
      4. Skip results whose description says novel/book/video game
    """
    import urllib.parse

    # Strip theater-added format descriptors before searching
    clean = re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip()
    clean = re.sub(
        r"\s+(?:in\s+(?:35|16|70)mm|[\-–]\s*(?:35|16|70)mm|\((?:35|16|70)mm\)"
        r"|4K\s*(?:restoration|remaster)?|\(restored\)|\(remastered\)"
        r"|(?:digital\s+)?restoration)\s*$",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()

    def _extract(data: dict) -> str:
        """Pull a trimmed extract from a Wikipedia summary response."""
        extract = data.get("extract", "").strip()
        if extract and len(extract) > 60:
            short = extract[:580]
            last_dot = short.rfind(".")
            if last_dot > 180:
                short = short[: last_dot + 1]
            return short
        return ""

    def _good_article(data: dict) -> bool:
        """True if this Wikipedia article looks like a film (not a book/game)."""
        if data.get("type") == "disambiguation":
            return False
        description = data.get("description", "").lower()
        if any(w in description for w in ("novel", "book", "video game", "manga", "comic")):
            return False
        return True

    def _summary_for(article_title: str) -> str:
        encoded = urllib.parse.quote(article_title.replace(" ", "_"), safe="")
        try:
            resp = requests.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}",
                headers={**HEADERS, "Accept": "application/json; charset=utf-8"},
                timeout=6,
            )
            if resp.status_code == 200:
                data = resp.json()
                if _good_article(data):
                    return _extract(data)
        except Exception:
            pass
        return ""

    # Pass 1: plain title and "(film)" variant
    for candidate in [clean, f"{clean} (film)"]:
        result = _summary_for(candidate)
        if result:
            return result

    # Pass 2: both were disambiguation pages (or 404) — use the search API
    # to find the actual film article (e.g. "The Tribe (2014 film)")
    try:
        q = urllib.parse.quote(f"{clean} film")
        resp = requests.get(
            f"https://en.wikipedia.org/w/api.php"
            f"?action=query&list=search&srsearch={q}&srlimit=5&format=json",
            headers=HEADERS,
            timeout=6,
        )
        if resp.status_code == 200:
            search_results = resp.json().get("query", {}).get("search", [])
            for r in search_results:
                art_title = r.get("title", "")
                # Only accept articles whose title contains our film name
                if clean.lower() not in art_title.lower():
                    continue
                result = _summary_for(art_title)
                if result:
                    return result
    except Exception:
        pass

    return ""


def enrich_with_synopses(films: list[dict]) -> list[dict]:
    """
    For every film missing a description, first try the theater's own film
    detail page, then fall back to Wikipedia.
    Mutates film dicts in-place and returns the list.

    Results are persisted to data/synopsis_cache.json so they survive between
    scrape runs — no repeat lookups for films we've already seen.
    Both successful synopses AND "not found" results are cached so we don't
    re-query things that have no Wikipedia article.
    """
    # Load the persistent disk cache, then layer an in-memory dict on top
    # so the same film appearing multiple times this run is only fetched once.
    disk_cache = _load_synopsis_cache()
    _session:  dict[str, str] = {}   # new entries added this run
    _all = disk_cache                # combined view (mutated in place below)

    def _slug(title: str) -> str:
        return re.sub(r"\W+", "", title.lower())[:60]

    cache_hits  = 0
    fresh_found = 0
    not_found   = 0

    for film in films:
        if film.get("desc"):
            continue

        title = film["title"]
        key   = _slug(title)

        # Disk / session cache hit — apply and move on
        if key in _all:
            if _all[key]:
                film["desc"] = _all[key]
                cache_hits += 1
            continue

        # Try the theater's own film page first — it's more accurate
        synopsis = ""
        if film.get("url"):
            synopsis = _fetch_synopsis_from_page(film["url"])
            if synopsis:
                log.info("  ✨ Fresh synopsis (theater page): '%s'", title)

        # Fall back to Wikipedia
        if not synopsis:
            synopsis = _fetch_synopsis_wikipedia(title)
            if synopsis:
                log.info("  ✨ Fresh synopsis (Wikipedia): '%s'", title)

        # Store in both layers and persist to disk immediately
        _all[key]     = synopsis
        _session[key] = synopsis
        if synopsis:
            film["desc"] = synopsis
            fresh_found += 1
        else:
            not_found += 1

    # Summary line
    parts = []
    if cache_hits:  parts.append(f"📦 {cache_hits} from cache")
    if fresh_found: parts.append(f"✨ {fresh_found} fetched fresh")
    if not_found:   parts.append(f"— {not_found} not found")
    if parts:
        log.info("  Synopses: %s", " · ".join(parts))

    # Write any new entries back to disk (merge with what was already there)
    if _session:
        log.info("  💾 Saved %d new synopsis(es) to disk cache", len(_session))
        _save_synopsis_cache(_all)

    return films


def filter_classic_mode(films: list[dict]) -> list[dict]:
    """
    Classic mode: remove any film whose synopsis first sentence contains the
    current year — i.e. a brand-new release.  Films with no description are
    kept (we can't tell how old they are).

    This runs across ALL theaters, unlike filter_current_films which only
    touches the "current" multiplex group.
    """
    current_year = datetime.datetime.now().year
    result = []
    for film in films:
        desc = film.get("desc", "")
        if not desc:
            result.append(film)
            continue
        first_sentence = desc.split(".")[0]
        years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", first_sentence)
                 if 1900 <= int(y) <= current_year]
        if years and max(years) >= current_year:
            log.info("  🎞 Classic filter: '%s' (year %d)", film["title"], max(years))
            continue
        result.append(film)
    return result


def filter_current_films(films: list[dict],
                         current_theater_names: set[str],
                         max_age_years: int = 2) -> list[dict]:
    """
    For films from "current" theaters, remove any whose Wikipedia synopsis
    indicates a release year older than max_age_years.

    Uses the year of the first 4-digit number found in the synopsis's first
    sentence — Wikipedia film articles almost always open with:
    "X is a [YEAR] [genre] film directed by …"

    Films from art theaters are always passed through unchanged.
    Films with no synopsis are kept (can't be sure they're old).
    """
    current_year = datetime.datetime.now().year
    cutoff = current_year - max_age_years
    result  = []
    for film in films:
        if film.get("theater") not in current_theater_names:
            result.append(film)
            continue

        # ── Fast path: year embedded in the title, e.g. "Beast (2026)" ────────
        # Ticketing sites like Fandango and AMC often append the release year.
        # If the title itself contains a recent year, trust it and skip Wikipedia.
        title_years = [int(y) for y in re.findall(r"\((\d{4})\)", film.get("title", ""))
                       if current_year - 2 <= int(y) <= current_year + 1]
        if title_years:
            result.append(film)
            continue

        desc = film.get("desc", "")
        if not desc:
            result.append(film)   # no info → assume current
            continue
        # Grab first sentence only — most reliable for release year
        first_sentence = desc.split(".")[0]
        years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", first_sentence)
                 if 1900 <= int(y) <= current_year]
        if years and min(years) < cutoff:
            log.info("  ✗ Filtering '%s' from current theaters (year %d)", film["title"], min(years))
            continue
        result.append(film)
    return result


# ---------------------------------------------------------------------------
# SCRAPING — shared HTML parser
# ---------------------------------------------------------------------------

def _parse_theater_html(html_content: str, config: dict) -> list[dict]:
    """
    Parse a theater's listing page HTML using the CSS selectors in config.
    Called by both the requests path and the Playwright path — no duplication.

    Returns a list of film dicts: theater, title, url, desc, date_text, raw_text
    """
    name = config["name"]
    soup = BeautifulSoup(html_content, "lxml")

    # Try each comma-separated selector until we get hits
    article_sels = [s.strip() for s in config["article_sel"].split(",")]
    containers   = []
    for sel in article_sels:
        containers = soup.select(sel)
        if containers:
            break

    if not containers:
        log.warning("  %s: no containers found with selectors: %s",
                    name, config["article_sel"])
        return []

    films, seen = [], set()

    limit = config.get("max_films", FILMS_PER_SITE)
    for c in containers[:limit]:
        # ── Title ──────────────────────────────────────────────────────────
        title_tag = None
        for sel in [s.strip() for s in config["title_sel"].split(",")]:
            title_tag = c.select_one(sel)
            if title_tag:
                break
        if not title_tag:
            continue
        raw_title = _safe_text(title_tag)
        if not raw_title:
            continue

        # ── Link ───────────────────────────────────────────────────────────
        link_tag = None
        for sel in [s.strip() for s in config["link_sel"].split(",")]:
            link_tag = c.select_one(sel)
            if link_tag:
                break
        if link_tag is None and c.name == "a":
            link_tag = c
        href = _abs_url(
            link_tag.get("href", "") if link_tag else "",
            config["base_url"],
        )

        # ── Link filter (e.g. Riverview: only /show/show/ URLs are real movies)
        if config.get("link_filter") and config["link_filter"] not in href:
            continue

        # ── Strip embedded showtimes from title (Riverview smashes them in) ──
        if config.get("strip_times_from_title"):
            time_pattern = r"\d{1,2}:\d{2}\s*(?:AM|PM)"
            embedded_times = re.findall(time_pattern, raw_title, re.IGNORECASE)
            title = re.sub(time_pattern, "", raw_title, flags=re.IGNORECASE).strip()
        else:
            title     = raw_title
            embedded_times = []

        if not title:
            continue

        # Deduplicate by title slug (skip if config opts out, e.g. per-day Veezi listings)
        # Also skip dedup when the site provides multiple date elements per card —
        # in that case we'll create one entry per date below (not duplicate entries).
        slug = re.sub(r"\W+", "", title.lower())[:60]
        if not config.get("no_deduplicate"):
            if slug in seen:
                continue
            seen.add(slug)
        # (If the site has multi-date cards, seen-dedup above still prevents the
        # same film appearing twice in the single-date path, which is correct.)

        # ── Description ────────────────────────────────────────────────────
        desc = ""
        if config.get("desc_sel"):
            for sel in [s.strip() for s in config["desc_sel"].split(",")]:
                d_tag = c.select_one(sel)
                if d_tag:
                    desc = _safe_text(d_tag)[:500]
                    break

        # ── Date / showtime text ────────────────────────────────────────────
        date_text = ""
        date_tags_all: list = []   # may hold multiple date elements (e.g. MSP)
        if config.get("date_sel"):
            for sel in [s.strip() for s in config["date_sel"].split(",")]:
                date_tags_all = c.select(sel)   # ALL matches, not just first
                if date_tags_all:
                    date_text = _safe_text(date_tags_all[0])
                    break
        # Date encoded in container's CSS class  e.g. "date-20260414" (Mann Theatre)
        if not date_text and config.get("date_from_class_prefix"):
            prefix = config["date_from_class_prefix"]
            for cls in (c.get("class") or []):
                if cls.startswith(prefix):
                    raw = cls[len(prefix):]
                    m_cls = re.match(r"(\d{4})(\d{2})(\d{2})", raw)
                    if m_cls:
                        try:
                            d_obj = datetime.date(int(m_cls.group(1)),
                                                  int(m_cls.group(2)),
                                                  int(m_cls.group(3)))
                            date_text = d_obj.strftime(f"%A, %B {d_obj.day}, %Y")
                        except ValueError:
                            pass
                    break
        # Append showtime if a separate time selector is configured (Parkway)
        if config.get("time_sel"):
            t_tag = c.select_one(config["time_sel"])
            if t_tag:
                time_str = _safe_text(t_tag)
                date_text = f"{date_text}  ·  {time_str}".strip(" · ") if date_text else time_str
        # Use embedded times stripped from title if no other date found (Riverview)
        if not date_text and embedded_times:
            date_text = "  ·  ".join(embedded_times)

        # ── Poster image ───────────────────────────────────────────────────
        poster = ""
        if config.get("poster_sel"):
            img_tag = c.select_one(config["poster_sel"])
            if img_tag:
                poster = (img_tag.get("src") or img_tag.get("data-src")
                          or img_tag.get("data-lazy-src") or "")
                if poster and not poster.startswith("http"):
                    poster = _abs_url(poster, config["base_url"])
        # Background-image fallback (e.g. MSP Film Society uses CSS bg images)
        if not poster and config.get("poster_bg_sel"):
            bg_tag = c.select_one(config["poster_bg_sel"])
            if bg_tag:
                style = bg_tag.get("style", "")
                bg_m  = re.search(r"background-image:\s*url\(['\"]?([^'\")\s]+)['\"]?\)", style)
                if bg_m:
                    poster = bg_m.group(1)
                    if not poster.startswith("http"):
                        poster = _abs_url(poster, config["base_url"])

        # ── Raw container text ─────────────────────────────────────────────
        raw_text = _safe_text(c)

        # ── Skip sold-out entries ───────────────────────────────────────────
        if re.search(r"\bsold\s*out\b", raw_text, re.IGNORECASE):
            continue

        # ── Normalise ALL-CAPS titles ───────────────────────────────────────
        if _is_all_caps(title):
            title = _smart_title_case(title)

        base_film = {
            "theater":   name,
            "title":     title,
            "url":       href,
            "desc":      desc,
            "date_text": date_text,
            "poster":    poster,
            "raw_text":  raw_text,
        }

        # If there are multiple date elements (e.g. MSP Film Society shows a
        # film on several different days), create one entry per date so the
        # calendar and grouped listings reflect every showing date.
        if len(date_tags_all) > 1:
            for dt_tag in date_tags_all:
                dt_str = _safe_text(dt_tag)
                if dt_str:
                    entry = dict(base_film)
                    entry["date_text"] = dt_str
                    films.append(entry)
        else:
            films.append(base_film)

    return films


# ---------------------------------------------------------------------------
# SCRAPING — requests path (fast, for standard server-rendered sites)
# ---------------------------------------------------------------------------

def _parse_json_ld(html_content: str, config: dict) -> list[dict]:
    """
    Parse film events from JSON-LD structured data embedded in a page.
    Expects a <script type="application/ld+json"> block containing an array
    of Schema.org Event objects with at least: name, startDate, url.

    Each Event becomes one film entry (one screening = one calendar row).
    """
    import json as _json

    name     = config["name"]
    base_url = config.get("base_url", "")
    soup     = BeautifulSoup(html_content, "lxml")

    # Find ALL JSON-LD blocks and collect the one that's an event array
    events = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads(tag.string or "")
        except Exception:
            continue
        if isinstance(data, list):
            for item in data:
                if item.get("@type") == "Event" and item.get("name"):
                    events.append(item)
        elif isinstance(data, dict) and data.get("@type") == "Event":
            events.append(data)

    if not events:
        log.warning("  %s: no JSON-LD Event objects found", name)
        return []

    films, seen = [], set()
    limit = config.get("max_films", FILMS_PER_SITE)

    for ev in events[:limit]:
        title = (ev.get("name") or "").strip()
        if not title:
            continue

        # Normalise title (Trylon sometimes uses ALL-CAPS)
        if _is_all_caps(title):
            title = _smart_title_case(title)

        # URL
        url = (ev.get("url") or "").replace("&amp;", "&").strip()
        if url and not url.startswith("http"):
            url = _abs_url(url, base_url)

        # Date + time from startDate ISO string "2026-04-14T19:00:00+00:00"
        start_raw = ev.get("startDate", "")
        date_obj, date_label = None, ""
        time_str = ""
        if start_raw:
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", start_raw)
            if m:
                yr, mo, dy, hr, mn = (int(x) for x in m.groups())
                import datetime as _dt
                date_obj = _dt.date(yr, mo, dy)
                # Human-readable day label
                date_label = date_obj.strftime("%A, %B %-d, %Y")
                # 12-hour time
                ampm = "AM" if hr < 12 else "PM"
                hr12 = hr % 12 or 12
                time_str = f"{hr12}:{mn:02d} {ampm}"

        date_text = f"{date_label}  ·  {time_str}" if time_str else date_label

        # Description
        desc = (ev.get("description") or "").strip()
        # Strip HTML tags from description
        desc = re.sub(r"<[^>]+>", "", desc).strip()

        # Poster image
        poster = ""
        img = ev.get("image")
        if isinstance(img, str):
            poster = img
        elif isinstance(img, list) and img:
            poster = img[0] if isinstance(img[0], str) else ""

        dedup_key = (name, title, date_text)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        films.append({
            "theater":   name,
            "title":     title,
            "url":       url,
            "desc":      desc,
            "date_text": date_text,
            "raw_text":  f"{title} {date_text}",
            "poster":    poster,
            "address":   config.get("address", ""),
            "map_url":   config.get("map_url", ""),
            "group":     config.get("group", "art"),
            "_date_obj": date_obj,
        })

    log.info("  %s: found %d screenings via JSON-LD", name, len(films))
    return films


def _scrape_with_requests(config: dict) -> list[dict]:
    """Fetch page HTML with requests and parse it."""
    name = config["name"]

    # Build headers — allow per-theater Referer override
    req_headers = dict(HEADERS)
    if config.get("referer"):
        req_headers["Referer"] = config["referer"]

    try:
        resp = requests.get(config["url"], headers=req_headers, timeout=12)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("  %s (requests): HTTP error — %s", name, exc)
        return []
    log.info("  %s: fetched via requests (%d bytes)", name, len(resp.text))

    # JSON-LD mode: parse Schema.org Event structured data instead of HTML selectors
    if config.get("json_ld"):
        return _parse_json_ld(resp.text, config)

    return _parse_theater_html(resp.text, config)


# ---------------------------------------------------------------------------
# SCRAPING — Playwright path (headless Chromium, for JS-rendered sites)
# ---------------------------------------------------------------------------

def _scrape_with_playwright(config: dict) -> list[dict]:
    """
    Fetch page HTML using headless Chromium via Playwright, then parse it.

    Playwright is only imported here so the rest of the app works fine even
    if `playwright install chromium` hasn't been run yet — it'll fail with a
    clear error message rather than crashing on import.
    """
    name = config["name"]

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error(
            "  %s: Playwright is not installed.\n"
            "  Run:  pip install playwright && playwright install chromium",
            name,
        )
        return []

    html_content = ""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page    = browser.new_page(user_agent=HEADERS["User-Agent"])

            log.info("  %s: launching headless Chromium → %s", name, config["url"])
            page.goto(config["url"], wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2_500)   # let JS finish rendering

            # Wait for at least one article container to appear in the DOM
            article_sels = [s.strip() for s in config["article_sel"].split(",")]
            for sel in article_sels:
                try:
                    page.wait_for_selector(sel, timeout=8_000)
                    log.info("  %s: selector '%s' found in DOM", name, sel)
                    break
                except PWTimeout:
                    continue   # try the next selector

            # Scroll to bottom once to trigger any lazy-loaded content
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1_000)   # brief settle

            html_content = page.content()
            log.info("  %s: Playwright fetched %d bytes", name, len(html_content))
            browser.close()

    except Exception as exc:
        log.warning("  %s (Playwright): failed — %s", name, exc)
        return []

    return _parse_theater_html(html_content, config)


# ---------------------------------------------------------------------------
# SCRAPING — Landmark custom scraper (JS SPA, semantic extraction)
# ---------------------------------------------------------------------------

def _scrape_landmark_playwright(config: dict) -> list[dict]:
    """
    Custom Playwright scraper for Landmark Theatres (St. Anthony Main).

    Their site is a Gatsby/React SPA with auto-generated CSS class names that
    change on every build, so CSS selectors are useless.  Instead we:
      1. Load the showtimes page for the specific theater
      2. Wait for the JS to finish rendering movie cards
      3. Extract movies using stable semantic patterns:
           - Any <a href="/movies/..."> links (movie detail URLs)
           - Heading text near those links as the title
      4. Deduplicate and return film dicts
    """
    name = config["name"]

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error(
            "  %s: Playwright not installed. Run: pip install playwright && playwright install chromium",
            name,
        )
        return []

    films = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page    = browser.new_page(user_agent=HEADERS["User-Agent"])

            log.info("  %s: launching Chromium → %s", name, config["url"])
            page.goto(config["url"], wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3_000)   # let React/SPA finish rendering

            # Wait for at least one movie link to appear in the DOM
            try:
                page.wait_for_selector('a[href*="/movies/"]', timeout=12_000)
                log.info("  %s: movie links found in DOM", name)
            except PWTimeout:
                log.warning("  %s: timed out waiting for movie links", name)

            # Scroll to trigger any lazy-loaded content
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1_500)

            # Use JS to extract movies directly from the live DOM —
            # avoids fragile BeautifulSoup parsing of minified React output
            movies_js = page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();

                    // Find all links pointing to movie detail pages
                    document.querySelectorAll('a[href*="/movies/"]').forEach(a => {
                        const href = a.href;
                        if (seen.has(href)) return;
                        seen.add(href);

                        // Title: prefer heading inside the link, else the link text itself
                        let title = '';
                        const heading = a.querySelector('h1,h2,h3,h4,h5');
                        if (heading) {
                            title = heading.innerText.trim();
                        } else {
                            title = a.innerText.trim().split('\\n')[0].trim();
                        }
                        if (!title || title.length < 2) return;

                        // Card container — used for poster, desc, and date
                        const parent = a.closest('li, article, div[class]') || a.parentElement;

                        // Poster: look for an <img> inside the link or nearby card
                        let poster = '';
                        const img = a.querySelector('img') ||
                                    (parent && parent.querySelector('img'));
                        if (img) poster = img.src || img.dataset.src || img.dataset.lazySrc || '';

                        // Description: look for a <p> sibling or nearby element
                        let desc = '';
                        if (parent) {
                            const p = parent.querySelector('p');
                            if (p) desc = p.innerText.trim().substring(0, 300);
                        }

                        // Showtime/date: look for time elements or text like "7:00 PM"
                        let dateText = '';
                        if (parent) {
                            const timeEl = parent.querySelector('time');
                            if (timeEl) {
                                dateText = timeEl.innerText.trim();
                            } else {
                                const text = parent.innerText || '';
                                const m = text.match(/\\d{1,2}:\\d{2}\\s*(?:AM|PM)/i);
                                if (m) dateText = m[0];
                            }
                        }

                        results.push({ href, title, desc, dateText, poster });
                    });
                    return results;
                }
            """)

            browser.close()

            base = config.get("base_url", "https://www.landmarktheatres.com")
            seen_slugs = set()
            for m in movies_js[:FILMS_PER_SITE]:
                raw_title = (m["title"] or "").strip()
                if not raw_title:
                    continue
                # Normalize ALL-CAPS titles (Landmark sometimes returns them capitalized
                # differently depending on their CMS)
                title = _smart_title_case(raw_title) if _is_all_caps(raw_title) else raw_title
                slug  = re.sub(r"\W+", "", title.lower())[:60]
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                poster = m.get("poster", "")
                if poster and not poster.startswith("http") and base:
                    poster = base + poster
                films.append({
                    "theater":   name,
                    "title":     title,
                    "url":       m["href"] if m["href"].startswith("http")
                                 else base + m["href"],
                    "desc":      m["desc"],
                    "date_text": m["dateText"],
                    "poster":    poster,
                    "raw_text":  title + " " + m["dateText"],
                })

            log.info("  %s: extracted %d films via Playwright JS", name, len(films))

    except Exception as exc:
        log.warning("  %s (Playwright): failed — %s", name, exc)

    return films


# ---------------------------------------------------------------------------
# SCRAPING — generic multiplex Playwright scraper (AMC, Marcus, etc.)
# ---------------------------------------------------------------------------

def _scrape_multiplex_playwright(config: dict) -> list[dict]:
    """
    Generic Playwright scraper for mainstream multiplex sites (AMC, Marcus,
    Emagine, etc.) that are JavaScript-rendered or bot-protected.

    Extracts movies semantically from the rendered DOM — no fragile CSS
    class names required.  Looks for:
      - Heading elements (h2/h3/h4) whose text looks like a movie title
      - A nearby/wrapping <a> for the link
      - A nearby <img> for the poster
      - Any date-like text near each movie heading
    """
    name = config["name"]

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error("  %s: Playwright not installed.", name)
        return []

    films = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page    = browser.new_page(user_agent=HEADERS["User-Agent"])

            log.info("  %s: launching Chromium → %s", name, config["url"])
            page.goto(config["url"], wait_until="domcontentloaded", timeout=35_000)
            page.wait_for_timeout(3_000)   # let JS finish rendering

            # Wait for any heading to load (coarse signal that content rendered)
            try:
                page.wait_for_selector("h2, h3, h4", timeout=10_000)
            except PWTimeout:
                pass

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1_500)

            movies_js = page.evaluate("""
                () => {
                    const results = [];
                    const seen    = new Set();

                    // ── Strategy 1: look for links to individual movie pages ────
                    // AMC uses /movies/<slug>, Marcus uses /movies/<slug>, etc.
                    // This is far more accurate than scanning h2/h3/h4 headings.
                    const MOVIE_PATH = /\\/(movies?|films?)\\/[^/?#]{3,}/;
                    const GENERIC_MOVIE = /\\/(movies?|films?)\\/?(\\?.*)?$/;
                    // Prefixes that indicate promo copy, not a film title
                    const PROMO = /^(get |save |see |sign |log |join |find |buy |view |watch |learn |more |help |terms |stay |you'|amc |imax |dolby|fandango)/i;

                    document.querySelectorAll('a[href]').forEach(a => {
                        const href = a.href || '';
                        if (!MOVIE_PATH.test(href)) return;
                        try { if (GENERIC_MOVIE.test(new URL(href).pathname)) return; } catch(e) { return; }
                        if (seen.has(href)) return;

                        // Title: prefer a heading inside the link
                        let title = '';
                        const h = a.querySelector('h1,h2,h3,h4,h5,h6');
                        if (h) {
                            title = h.innerText.trim();
                        } else {
                            // First meaningful line of the link text
                            const lines = a.innerText.trim().split('\\n')
                                .map(l => l.trim()).filter(l => l.length > 1);
                            title = lines[0] || '';
                        }
                        if (!title || title.length < 2 || title.length > 90) return;
                        if (PROMO.test(title)) return;

                        seen.add(href);

                        let poster = '';
                        const card = a.closest('article,li,[class*="card"],[class*="movie"],[class*="item"]') || a;
                        const img  = card.querySelector('img');
                        if (img) poster = img.src || '';

                        results.push({ title, href, poster, dateText: '', desc: '' });
                    });

                    // If Strategy 1 found any movie links, return them.
                    // /movies/<slug> URLs are unambiguous — even 1 is reliable.
                    if (results.length >= 1) return results;

                    // ── Strategy 2: heading scan fallback (Fandango, etc.) ──────
                    results.length = 0;
                    seen.clear();

                    const SKIP = new Set([
                        'synopsis','showtimes','get tickets','now playing',
                        'coming soon','movies','films','schedule',
                        'theatre details','theatre features','theatre amenities',
                        'amenities details','movie times calendar',
                        'our company','more','get directions','watch trailer',
                        'enhanced concessions','senior cinema luncheon',
                        'fathom entertainment','flashback cinema',
                        'standard','imax','dolby','dolby cinema','4dx','prime',
                        'screenx','d-box','laser','reserved seating','open caption',
                        'audio description','sensory friendly',
                        // Fandango / Marcus junk
                        'nearby theaters','new & coming soon','special offer',
                        'gift with purchase','opt-out form','going to the movies',
                        'marcus for all','value tuesday','marcus mystery movie',
                        'special','legal','concepts','business together',
                    ]);
                    const UI_PAT  = /today.s date|screen format|amenities|filter movie|times calendar|movie times|select date|sold out/i;
                    const NAV_PAT = /^(explore|experience|visit|about|follow us?|contact|news|blog|photos|videos|exhibitions?|editorial|careers|offers|gift card|loyalty|rewards|sign in|log in|register|more info|see all|view all|learn more|stay |you've|amc |save |get |see \d|join |allow sale|notice of right|kids movie|animal farm|mother.s day|special offers?|pack passport)/i;

                    document.querySelectorAll('h2, h3, h4').forEach(h => {
                        const title = h.innerText.trim().replace(/\\s+/g, ' ');
                        if (!title || title.length < 2 || title.length > 80) return;
                        if (SKIP.has(title.toLowerCase())) return;
                        if (UI_PAT.test(title)) return;
                        if (NAV_PAT.test(title)) return;
                        if (/^\\d/.test(title)) return;
                        if (seen.has(title)) return;
                        seen.add(title);

                        let href = '';
                        const parentA = h.closest('a');
                        if (parentA) { href = parentA.href; }
                        else {
                            const siblA = h.parentElement && h.parentElement.querySelector('a');
                            if (siblA) href = siblA.href;
                        }

                        let poster = '';
                        const card = h.closest('article,li,[class*="item"],[class*="card"],[class*="movie"],[class*="film"],[class*="box"]') || h.parentElement;
                        if (card) { const img = card.querySelector('img'); if (img) poster = img.src || ''; }

                        let dateText = '';
                        if (card) {
                            const dm = (card.innerText||'').match(/(january|february|march|april|may|june|july|august|september|october|november|december)\\s+\\d{1,2}/i);
                            if (dm) dateText = dm[0];
                        }

                        let desc = '';
                        if (card) { const p = card.querySelector('p'); if (p) desc = p.innerText.trim().substring(0,400); }

                        results.push({ title, href, poster, dateText, desc });
                    });
                    return results;
                }
            """)

            browser.close()

            base = config.get("base_url", "")
            seen_slugs: set[str] = set()
            for m in movies_js[:FILMS_PER_SITE]:
                raw_title = m["title"].strip()
                if not raw_title:
                    continue
                # Normalise ALL-CAPS titles from multiplexes
                title = _smart_title_case(raw_title) if _is_all_caps(raw_title) else raw_title
                slug = re.sub(r"\W+", "", title.lower())[:60]
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                url = m["href"] or (base + "/")
                poster = m["poster"]
                if poster and not poster.startswith("http") and base:
                    poster = _abs_url(poster, base)
                films.append({
                    "theater":   name,
                    "title":     title,
                    "url":       url,
                    "desc":      m["desc"],
                    "date_text": m["dateText"],
                    "poster":    poster,
                    "raw_text":  m["title"] + " " + m["dateText"],
                })

            log.info("  %s: extracted %d films via Playwright", name, len(films))

    except Exception as exc:
        log.warning("  %s (Playwright): failed — %s", name, exc)

    return films


# ---------------------------------------------------------------------------
# SCRAPING — Walker Art Center custom scraper (React SPA)
# ---------------------------------------------------------------------------

def _scrape_walker_playwright(config: dict) -> list[dict]:
    """
    Custom scraper for Walker Art Center's calendar.

    The Walker site is a React SPA — server HTML is an empty shell.
    Strategy:
      1. Load the screenings-filtered calendar page with Playwright
      2. Wait for event cards to appear
      3. Use page.evaluate() to extract structured data from the live DOM:
         - Links pointing to /calendar/ detail pages
         - Heading text as title
         - Date/time text
         - Image src as poster
         - Short description if present
    """
    name = config["name"]

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error("  %s: Playwright not installed. Run: pip install playwright && playwright install chromium", name)
        return []

    films = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page    = browser.new_page(user_agent=HEADERS["User-Agent"])

            log.info("  %s: launching Chromium → %s", name, config["url"])
            page.goto(config["url"], wait_until="domcontentloaded", timeout=35_000)
            page.wait_for_timeout(3_000)   # let React finish rendering

            # Wait for at least one event/card link to appear
            try:
                page.wait_for_selector('a[href*="/calendar/"]', timeout=12_000)
                log.info("  %s: calendar event links found in DOM", name)
            except PWTimeout:
                log.warning("  %s: timed out waiting for event links", name)

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1_500)

            events_js = page.evaluate("""
                () => {
                    const results = [];
                    const seen    = new Set();

                    // We load ?type=screenings so Walker already filters the page
                    // to only show screening/film events on their end. We just need
                    // to exclude bare calendar index links and known nav/category slugs.
                    // No "SCREENING" text check needed — it was blocking valid events.
                    const NON_FILM = /\/calendar\/(exhibitions?|talks?|performances?|open-?field|visual-?art|film-?talk|music|dance|tours?)($|[/?#])/i;

                    document.querySelectorAll('a[href*="/calendar/"]').forEach(a => {
                        const href = a.href;
                        // Skip bare calendar index links
                        if (href.match(/\/calendar\/([?#].*)?$/)) return;
                        // Skip non-film category pages
                        if (NON_FILM.test(href)) return;
                        if (seen.has(href)) return;
                        seen.add(href);

                        // cardRoot used for date/desc extraction
                        const cardRoot = a;
                        const cardText = (a.innerText || '').toUpperCase();

                        // ── Title ──────────────────────────────────────────────
                        // Walker card structure (innerText lines, roughly):
                        //   "Screening"
                        //   "Sat, Apr 18, 2026"
                        //   "Sans Soleil by Chris Marker"
                        //   "View Details"
                        // Title is the line that is NOT a type label, NOT a date,
                        // and NOT "View Details".
                        let title = '';
                        const h = a.querySelector('h1,h2,h3,h4,h5,h6,[class*="title"],[class*="heading"],[class*="name"]');
                        if (h) {
                            title = h.innerText.trim();
                        } else {
                            const DATE_LINE = /^(MON|TUE|WED|THU|FRI|SAT|SUN),|^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s/i;
                            const SKIP_LINE = /^(screening|exhibition|performance|talk|film|view details|see all)$/i;
                            const lines = a.innerText.trim().split('\\n')
                                .map(l => l.trim())
                                .filter(l => l && !SKIP_LINE.test(l) && !DATE_LINE.test(l));
                            title = lines[0] || '';
                        }
                        if (!title || title.length < 2) return;

                        // ── Poster ─────────────────────────────────────────────
                        let poster = '';
                        const img = a.querySelector('img');
                        if (img) poster = img.src || img.dataset.src || '';

                        // ── Date ───────────────────────────────────────────────
                        // Walker dates: "Sat, Apr 18, 2026" or "May 20–Jun 11, 2026"
                        let dateText = '';
                        const timeEl = a.querySelector('time');
                        if (timeEl) {
                            dateText = (timeEl.getAttribute('datetime') || timeEl.innerText).trim();
                        } else {
                            // Match "SAT, APR 18, 2026" style
                            const dm = cardText.match(/(?:MON|TUE|WED|THU|FRI|SAT|SUN),\\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\\s+\\d{1,2},?(?:\\s+\\d{4})?/i);
                            if (dm) { dateText = dm[0]; }
                            else {
                                // Match "MAY 20" or "MAY 20-JUN 11" range
                                const rm = cardText.match(/(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\\s+\\d{1,2}(?:[^\\n]*\\d{4})?/i);
                                if (rm) dateText = rm[0];
                            }
                        }

                        // ── Description ────────────────────────────────────────
                        let desc = '';
                        const p = a.querySelector('p');
                        if (p) desc = p.innerText.trim().substring(0, 400);

                        results.push({ href, title, poster, dateText, desc });
                    });
                    return results;
                }
            """)

            browser.close()

            base = config.get("base_url", "https://walkerart.org")
            seen_slugs: set[str] = set()
            for ev in events_js[:FILMS_PER_SITE]:
                raw_title = ev["title"].strip()
                if not raw_title:
                    continue
                title = _smart_title_case(raw_title) if _is_all_caps(raw_title) else raw_title
                slug  = re.sub(r"\W+", "", title.lower())[:60]
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                films.append({
                    "theater":   name,
                    "title":     title,
                    "url":       ev["href"] if ev["href"].startswith("http") else base + ev["href"],
                    "desc":      ev["desc"],
                    "date_text": ev["dateText"],
                    "poster":    ev["poster"],
                    "raw_text":  ev["title"] + " " + ev["dateText"],
                })

            log.info("  %s: extracted %d events via Playwright JS", name, len(films))

    except Exception as exc:
        log.warning("  %s (Playwright): failed — %s", name, exc)

    return films


# ---------------------------------------------------------------------------
# SCRAPING — unified entry point
# ---------------------------------------------------------------------------

def _expand_film_dates(films: list[dict], config: dict) -> list[dict]:
    """
    For theaters whose listing page only shows one date per film (e.g. MSP Film
    Society), visit each film's own detail page to collect ALL showing dates and
    times, then expand the list to one entry per (date, time) slot.

    Film pages are fetched concurrently (default 8 workers) so 290 pages take
    ~15 s instead of ~5 min.

    Supported config keys:
      film_page_day_sel   — CSS selector for the per-day wrapper element that
                            contains both a date heading and showtime buttons.
                            MSP structure:
                              .gecko-show-events__day
                                ├── .gecko-show-events__date   "Tuesday, April 14th"
                                └── .gecko-show-events__showtime span  "4:10 pm"
      film_page_date_sel  — CSS selector for the date element, evaluated inside
                            each day wrapper (or globally if day_sel absent)
      film_page_time_sel  — (optional) CSS selector for showtime elements inside
                            each day wrapper
      film_page_desc_sel  — (optional) CSS selector for the synopsis paragraph
      film_page_workers   — thread-pool size (default 8)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    day_sel  = config.get("film_page_day_sel")
    date_sel = config.get("film_page_date_sel")
    time_sel = config.get("film_page_time_sel")
    desc_sel = config.get("film_page_desc_sel")
    if not date_sel:
        return films

    # Deduplicate by URL — one fetch per unique film page
    unique: list[dict] = []
    seen_urls: set[str] = set()
    for film in films:
        url = film.get("url", "")
        if url and url.startswith("http") and url not in seen_urls:
            seen_urls.add(url)
            unique.append(film)

    log.info("  %s: fetching %d film pages for showtimes (concurrent)…",
             config["name"], len(unique))

    def _fetch_one(film: dict) -> list[dict]:
        """Fetch a single film page and return a list of expanded screening entries."""
        url = film.get("url", "")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                return [film]
            soup = BeautifulSoup(resp.text, "lxml")

            # ── Optionally grab description ────────────────────────────────
            page_desc = film.get("desc", "")
            if not page_desc and desc_sel:
                for d_tag in soup.select(desc_sel):
                    txt = " ".join(d_tag.get_text(separator=" ", strip=True).split())
                    if len(txt) > 80:
                        page_desc = txt[:500]
                        break

            # ── Build (date_text, time_text) pairs ─────────────────────────
            date_time_pairs: list[tuple[str, str]] = []

            if day_sel:
                # Day-wrapper mode: each wrapper → one date + N showtimes.
                for day_el in soup.select(day_sel):
                    date_tag = day_el.select_one(date_sel)
                    dt_text  = _safe_text(date_tag) if date_tag else ""
                    if not dt_text:
                        continue
                    # Strip ordinal suffix so date parser handles it cleanly:
                    # "Wednesday, April 15th" → "Wednesday, April 15"
                    dt_text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", dt_text)
                    times = ([_safe_text(t) for t in day_el.select(time_sel)
                               if _safe_text(t)]
                             if time_sel else [])
                    if times:
                        for tm in times:
                            date_time_pairs.append((dt_text, tm.upper()))
                    else:
                        date_time_pairs.append((dt_text, ""))
            else:
                # Simple mode: collect date elements globally
                for tag in soup.select(date_sel):
                    dt_text = _safe_text(tag)
                    if dt_text:
                        dt_text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", dt_text)
                        date_time_pairs.append((dt_text, ""))

            if date_time_pairs:
                entries = []
                for dt_text, tm_text in date_time_pairs:
                    entry = dict(film)
                    entry["date_text"] = (f"{dt_text}  ·  {tm_text}"
                                          if tm_text else dt_text)
                    if page_desc:
                        entry["desc"] = page_desc
                    entries.append(entry)
                return entries
            else:
                if page_desc:
                    film = dict(film)
                    film["desc"] = page_desc
                return [film]

        except Exception as exc:
            log.warning("  %s: could not fetch film page %s — %s",
                        config["name"], url, exc)
            return [film]

    # Fetch concurrently
    workers = config.get("film_page_workers", 8)
    expanded: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, f): f for f in unique}
        for future in as_completed(futures):
            try:
                expanded.extend(future.result())
            except Exception:
                expanded.append(futures[future])

    log.info("  %s: expanded to %d screening entries",
             config["name"], len(expanded))
    return expanded


def scrape_theater(config: dict) -> list[dict]:
    """
    Scrape a single theater.  Routes automatically:
      - js_heavy=False              →  fast requests + BeautifulSoup
      - js_heavy=True, generic      →  headless Chromium via Playwright (CSS selectors)
      - js_heavy=True, use_landmark →  custom Landmark semantic JS extractor
    """
    if config.get("js_heavy"):
        if config.get("use_landmark_scraper"):
            return _scrape_landmark_playwright(config)
        if config.get("use_walker_scraper"):
            return _scrape_walker_playwright(config)
        if config.get("use_multiplex_scraper"):
            return _scrape_multiplex_playwright(config)
        return _scrape_with_playwright(config)
    films = _scrape_with_requests(config)
    # Expand multi-date films by visiting each film's detail page (e.g. MSP)
    if config.get("film_page_date_sel") and films:
        films = _expand_film_dates(films, config)
    return films


def fetch_all_films(selected_theaters: list[str] | None = None) -> list[dict]:
    """
    Scrape all configured theaters and return a flat list of film dicts.
    Pass selected_theaters (list of theater names) to filter.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from sites_config import THEATERS, CUSTOM_THEATERS

    all_configs = THEATERS + CUSTOM_THEATERS

    if selected_theaters:
        all_configs = [t for t in all_configs if t["name"] in selected_theaters]

    all_films: list[dict] = []
    log.info("=== Scraping %d theater(s) ===", len(all_configs))
    for config in all_configs:
        log.info("Fetching %s …", config["name"])
        films = scrape_theater(config)
        all_films.extend(films)

    log.info("Total: %d films across %d theaters", len(all_films), len(all_configs))
    return all_films


# ---------------------------------------------------------------------------
# HTML GENERATION
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{subject}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d0d0d; font-family: 'Segoe UI', Arial, sans-serif; color: #e8e0d5; }}
  a {{ color: #f5c842; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  .wrapper {{ max-width: 680px; margin: 32px auto; background: #1a1a1a;
              border-radius: 10px; overflow: hidden;
              box-shadow: 0 4px 32px rgba(0,0,0,.6); }}

  /* Header */
  .header {{ background: linear-gradient(135deg, #1a0a00 0%, #3d1f00 60%, #1a0a00 100%);
             padding: 32px 36px 28px; border-bottom: 2px solid #f5c842; }}
  .header-eyebrow {{ font-size:11px; font-weight:700; letter-spacing:2.5px;
                     color: #f5c842; text-transform:uppercase; margin-bottom:10px; }}
  .header h1 {{ font-size:28px; font-weight:800; color:#fff; line-height:1.2;
                margin-bottom:8px; letter-spacing: -0.5px; }}
  .header .date {{ font-size:13px; color:rgba(255,255,255,.65); font-weight:500; }}
  .header .tagline {{ font-size:13px; color:rgba(255,255,255,.75); margin-top:12px;
                      border-top:1px solid rgba(245,200,66,.25); padding-top:10px; }}

  /* Intro */
  .intro {{ padding:20px 36px 16px; border-bottom:1px solid #2a2a2a; }}
  .intro p {{ font-size:14px; line-height:1.7; color:#a09888; }}

  /* Theater section */
  .theater-section {{ border-bottom: 1px solid #2a2a2a; }}
  .theater-header {{ background: #111; padding: 16px 36px 14px;
                     border-left: 4px solid #f5c842; }}
  .theater-header h2 {{ font-size: 14px; font-weight: 800; color: #f5c842;
                        text-transform: uppercase; letter-spacing: 1.5px; }}
  .theater-header .theater-url {{ font-size: 11px; color: #5a5040; margin-top: 3px; }}
  .theater-header .theater-address {{ font-size: 11px; margin-top: 4px; }}
  .theater-header .theater-address a {{ color: #8a7060; }}
  .theater-header .theater-address a:hover {{ color: #f5c842; }}

  /* Day sections */
  .day-header {{ padding: 8px 36px 7px; background: #0f0f0f;
                 font-size: 10px; font-weight: 800; color: #7a6850;
                 text-transform: uppercase; letter-spacing: 2px;
                 border-top: 1px solid #1e1e1e; border-bottom: 1px solid #1e1e1e; }}

  /* Film cards */
  .films {{ padding: 8px 36px 16px; }}
  .film {{ padding: 18px 0; border-bottom: 1px solid #252525; }}
  .film:last-child {{ border-bottom: none; }}
  .film-inner {{ display: flex; gap: 14px; align-items: flex-start; }}
  .film-poster {{ width: 72px; height: 105px; object-fit: cover;
                  border-radius: 4px; flex-shrink: 0;
                  border: 1px solid #2a2a2a; background: #111; }}
  .film-poster-placeholder {{ width: 72px; height: 105px; flex-shrink: 0;
                               background: #111; border-radius: 4px;
                               border: 1px solid #2a2a2a; display: flex;
                               align-items: center; justify-content: center;
                               font-size: 24px; }}
  .film-content {{ flex: 1; min-width: 0; }}
  .film-meta {{ display: flex; align-items: center; gap: 8px; margin-bottom: 7px; flex-wrap: wrap; }}
  .film-num {{ display:inline-block; background: #f5c842; color: #0d0d0d;
               font-size:10px; font-weight:800; border-radius:3px;
               padding:2px 7px; letter-spacing:.5px; }}
  .film-date {{ font-size: 11px; font-weight: 600; color: #b8a060;
                letter-spacing: .3px; }}
  .film h3 {{ font-size: 16px; font-weight: 700; line-height: 1.35; margin-bottom: 6px; }}
  .film h3 a {{ color: #e8e0d5; }}
  .film h3 a:hover {{ color: #f5c842; }}
  .film-desc {{ font-size: 12px; color: #6a6050; line-height: 1.65; margin-bottom: 0; }}

  /* No films */
  .no-films {{ padding: 16px 36px; font-size: 13px; color: #5a5040; font-style: italic; }}

  /* Footer */
  .footer {{ background: #0d0d0d; padding: 24px 36px; text-align: center;
             border-top: 1px solid #2a2a2a; }}
  .footer p {{ font-size: 12px; color: #5a5040; line-height: 1.7; }}
  .footer a {{ color: #f5c842; }}
  .footer .theaters-line {{ margin-top: 8px; font-size: 11px; color: #3a3228; }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <div class="header-eyebrow">Twin Cities Art Cinema</div>
    <h1>🎬 What's Playing This Week</h1>
    <div class="date">{date_long} &nbsp;·&nbsp; {total_films} films across {num_theaters} theaters</div>
    <div class="tagline">Independent and repertory cinema in Minneapolis–Saint Paul — scraped fresh from each theater's website.</div>
  </div>
  <div class="intro">
    <p>Listings pulled directly from theater websites. Showtimes and dates are as listed on each theater's page — click a film title to see tickets and full schedule details.</p>
  </div>
  {theater_sections}
  <div class="footer">
    <p>Generated by the <strong style="color:#f5c842;">Twin Cities Art Cinema Scraper</strong> &nbsp;·&nbsp; {date_short}</p>
    <p class="theaters-line">Theaters: {theater_names}</p>
  </div>
</div>
</body>
</html>
"""

THEATER_SECTION_TEMPLATE = """\
  <div class="theater-section">
    <div class="theater-header">
      <h2>{theater_name}</h2>
      <div class="theater-url"><a href="{theater_url}" style="color:#5a5040;">{theater_url}</a></div>
      {address_html}
    </div>
{day_sections}
  </div>
"""

DAY_SECTION_TEMPLATE = """\
    <div class="day-section">
      <div class="day-header">{day_label}</div>
      <div class="films">
{film_blocks}
      </div>
    </div>
"""

NO_FILMS_TEMPLATE = """\
  <div class="theater-section">
    <div class="theater-header">
      <h2>{theater_name}</h2>
      <div class="theater-url"><a href="{theater_url}" style="color:#5a5040;">{theater_url}</a></div>
      {address_html}
    </div>
    <div class="no-films">No listings found — the site may be temporarily unavailable or the page structure may have changed.</div>
  </div>
"""


def _film_block(rank: int, film: dict) -> str:
    title_esc = film["title"].replace("<", "&lt;").replace(">", "&gt;")
    href      = film.get("url", "#") or "#"

    # ── Poster ─────────────────────────────────────────────────────────────
    poster = film.get("poster", "").strip()
    if poster:
        poster_html = f'<img class="film-poster" src="{poster}" alt="{title_esc}" loading="lazy"/>'
    else:
        poster_html = '<div class="film-poster-placeholder">🎬</div>'

    # ── Date / showtime ─────────────────────────────────────────────────────
    date_display = film.get("date_text", "").strip()
    date_block = ""
    if date_display:
        date_esc   = date_display.replace("<", "&lt;").replace(">", "&gt;")
        date_block = f'<span class="film-date">🗓 {date_esc}</span>'

    # ── Description ─────────────────────────────────────────────────────────
    desc_block = ""
    if film.get("desc"):
        desc_esc   = film["desc"].replace("<", "&lt;").replace(">", "&gt;")
        desc_block = f'\n          <div class="film-desc">{desc_esc}</div>'

    return (
        f'      <div class="film">\n'
        f'        <div class="film-inner">\n'
        f'          {poster_html}\n'
        f'          <div class="film-content">\n'
        f'            <div class="film-meta">'
        f'<span class="film-num">#{rank}</span>{date_block}</div>\n'
        f'            <h3><a href="{href}" target="_blank">{title_esc}</a></h3>'
        f'{desc_block}\n'
        f'          </div>\n'
        f'        </div>\n'
        f'      </div>\n'
    )


def build_html(films_by_theater: dict[str, list[dict]],
               theater_configs: list[dict]) -> str:
    """
    Build the full listings HTML.

    films_by_theater: {theater_name: [film, ...]}
    theater_configs:  the THEATERS list from sites_config (for URLs)
    """
    now        = datetime.datetime.now()
    date_long  = now.strftime(f"%A, %B {now.day}, %Y")
    date_short = now.strftime("%Y-%m-%d")
    subject    = EMAIL_SUBJECT.format(date=now.strftime(f"%B {now.day}, %Y"))

    url_map     = {t["name"]: t["url"]             for t in theater_configs}
    address_map = {t["name"]: t.get("address", "") for t in theater_configs}
    map_url_map = {t["name"]: t.get("map_url",  "") for t in theater_configs}

    sections    = []
    total_films = 0
    for theater_name, films in films_by_theater.items():
        theater_url     = url_map.get(theater_name, "#")
        theater_address = address_map.get(theater_name, "")
        theater_map_url = map_url_map.get(theater_name, "")

        address_html = (
            f'<div class="theater-address">'
            f'<a href="{theater_map_url}" target="_blank">📍 {theater_address}</a>'
            f'</div>'
        ) if theater_address else ""

        if not films:
            sections.append(NO_FILMS_TEMPLATE.format(
                theater_name=theater_name,
                theater_url=theater_url,
                address_html=address_html,
            ))
            continue

        total_films += len(films)

        # Group films by calendar date; undated go under "Now Showing"
        grouped = _group_films_by_date(films)
        rank = 1
        day_sections_html = ""
        for _date_obj, day_label, day_films in grouped:
            blocks = "".join(_film_block(rank + i, f) for i, f in enumerate(day_films))
            rank  += len(day_films)
            day_sections_html += DAY_SECTION_TEMPLATE.format(
                day_label=day_label,
                film_blocks=blocks,
            )

        sections.append(THEATER_SECTION_TEMPLATE.format(
            theater_name=theater_name,
            theater_url=theater_url,
            address_html=address_html,
            day_sections=day_sections_html,
        ))

    theater_names = " · ".join(films_by_theater.keys())
    return HTML_TEMPLATE.format(
        subject          = subject,
        date_long        = date_long,
        date_short       = date_short,
        total_films      = total_films,
        num_theaters     = len(films_by_theater),
        theater_sections = "\n".join(sections),
        theater_names    = theater_names,
    )


def build_html_from_flat(all_films: list[dict], theater_configs: list[dict]) -> str:
    """Convenience wrapper: groups a flat film list by theater, then builds HTML."""
    films_by_theater: dict[str, list[dict]] = {}
    for t in theater_configs:
        films_by_theater[t["name"]] = []
    for film in all_films:
        theater = film.get("theater", "Unknown")
        if theater not in films_by_theater:
            films_by_theater[theater] = []
        films_by_theater[theater].append(film)
    return build_html(films_by_theater, theater_configs)


# ---------------------------------------------------------------------------
# CALENDAR HTML GENERATION — 4-week grid view
# ---------------------------------------------------------------------------

def build_calendar_html(films: list[dict], theater_configs: list[dict]) -> str:
    """
    Build a standalone 4-week calendar HTML showing every film that has a
    parseable date within the next 28 days.

    Films without a date (e.g. "Now Showing" entries) are listed in a
    separate "Undated / Now Showing" section below the calendar.
    """
    now   = datetime.datetime.now()
    today = now.date()
    end_date = today + datetime.timedelta(days=28)

    # ── Theater colour map (for per-entry badges) ──────────────────────────
    theater_colors = {}
    art_palette     = ["#f5c842", "#e8a030", "#d4803a", "#c86040",
                       "#b84050", "#a84070", "#9850a0"]
    current_palette = ["#5a9fc0", "#4a80a8", "#3a6090", "#2a5080"]
    art_idx = current_idx = 0
    for tc in theater_configs:
        if tc["name"] not in theater_colors:
            if tc.get("group") == "current":
                theater_colors[tc["name"]] = current_palette[current_idx % len(current_palette)]
                current_idx += 1
            else:
                theater_colors[tc["name"]] = art_palette[art_idx % len(art_palette)]
                art_idx += 1

    # ── Bucket films by date ───────────────────────────────────────────────
    films_by_date: dict[datetime.date, list[dict]] = {}
    undated: list[dict] = []

    # deduplicate same title+theater+date+time combos for the calendar view
    # (include time in key so multiple showings of the same film on the same day
    #  each get their own row, e.g. 7:00 PM and 9:30 PM are separate entries)
    seen_cal: set[str] = set()
    for film in films:
        d, _ = _parse_date_from_text(film.get("date_text", ""))
        t    = _extract_times(film.get("date_text", ""))
        key  = f"{film['theater']}|{film['title']}|{d}|{t}"
        if key in seen_cal:
            continue
        seen_cal.add(key)
        if d and today <= d <= end_date:
            films_by_date.setdefault(d, []).append(film)
        elif not d:
            undated.append(film)

    # ── Calendar grid: 4 weeks, Mon→Sun ────────────────────────────────────
    start_monday = today - datetime.timedelta(days=today.weekday())
    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def _cal_film_entry(film: dict) -> str:
        title = film["title"].replace("<", "&lt;").replace(">", "&gt;")
        href  = film.get("url", "#") or "#"
        color = theater_colors.get(film["theater"], "#f5c842")
        short = film["theater"].split()[0]  # first word of theater name
        theater_name = film["theater"]
        time_str  = _extract_times(film.get("date_text", ""))
        time_html = (f'<span class="cal-time">{time_str}</span>'
                     if time_str else "")
        return (
            f'<div class="cal-entry">'
            f'<span class="cal-dot" style="background:{color}"></span>'
            f'<a href="{href}" target="_blank" title="{theater_name}">'
            f'<span class="cal-theater" style="color:{color}">{short}</span>'
            f' {title}</a>'
            f'{time_html}'
            f'</div>'
        )

    week_rows = ""
    for w in range(4):
        cells = ""
        for d in range(7):
            day = start_monday + datetime.timedelta(days=w * 7 + d)
            if day < today:
                cls = "day past"
            elif day == today:
                cls = "day today"
            elif day > end_date:
                cls = "day out"
            else:
                cls = "day"

            day_films = films_by_date.get(day, [])
            films_html = "".join(_cal_film_entry(f) for f in day_films)
            films_div  = f'<div class="day-films">{films_html}</div>' if films_html else \
                         '<div class="day-films empty">—</div>'

            month_label = day.strftime("%b") if (d == 0 or day.day == 1) else ""
            month_html  = f'<span class="day-month">{month_label}</span>' if month_label else ""

            day_label = day.strftime("%a %b %-d")
            cells += (
                f'<td class="{cls}" data-day="{day_label}">'
                f'<div class="day-hd">{month_html}'
                f'<span class="day-num">{day.day}</span></div>'
                f'{films_div}</td>'
            )
        week_rows += f"<tr>{cells}</tr>\n"

    header_cells = "".join(f"<th>{n}</th>" for n in DAY_NAMES)

    # ── Undated section ─────────────────────────────────────────────────────
    undated_html = ""
    if undated:
        # group undated by theater
        by_theater: dict[str, list[dict]] = {}
        for film in undated:
            by_theater.setdefault(film["theater"], []).append(film)
        rows = ""
        for theater, tfilms in by_theater.items():
            color = theater_colors.get(theater, "#f5c842")
            entries = "".join(
                f'<a class="now-entry" href="{f.get("url","#") or "#"}" target="_blank">'
                f'{f["title"].replace("<","&lt;").replace(">","&gt;")}</a>'
                for f in tfilms
            )
            rows += (
                f'<div class="now-row">'
                f'<span class="now-theater" style="color:{color}">{theater}</span>'
                f'<div class="now-films">{entries}</div>'
                f'</div>'
            )
        undated_html = f'<div class="now-section"><div class="now-title">Now Showing (no specific date)</div>{rows}</div>'

    # ── Legend ──────────────────────────────────────────────────────────────
    legend_items = "".join(
        f'<span class="leg-item"><span class="leg-dot" style="background:{color}"></span>'
        f'{name}</span>'
        for name, color in theater_colors.items()
    )

    date_long = now.strftime(f"%B {now.day}, %Y")

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Twin Cities Cinema — 4-Week Calendar</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d0d0d; font-family: 'Segoe UI', Arial, sans-serif;
          color: #e8e0d5; padding: 20px; }}
  h1   {{ font-size: 18px; color: #f5c842; margin-bottom: 4px; }}
  .sub {{ font-size: 12px; color: #5a5040; margin-bottom: 14px; }}

  /* Legend */
  .legend {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }}
  .leg-item {{ display: flex; align-items: center; gap: 5px;
               font-size: 11px; color: #8a7a60; }}
  .leg-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}

  /* Calendar table */
  table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  th {{ font-size: 10px; font-weight: 800; color: #5a5040; text-transform: uppercase;
        letter-spacing: 1.5px; padding: 6px 4px; border-bottom: 2px solid #222; }}
  td.day {{ vertical-align: top; border: 1px solid #1e1e1e;
             padding: 6px 5px; min-height: 90px; }}
  td.past {{ opacity: 0.3; }}
  td.out  {{ opacity: 0.1; }}
  td.today {{ border-color: #f5c842 !important; background: rgba(245,200,66,0.04); }}
  td.today .day-num {{ color: #f5c842; font-weight: 800; }}

  .day-hd {{ display: flex; align-items: baseline; gap: 4px; margin-bottom: 5px; }}
  .day-num {{ font-size: 13px; font-weight: 700; color: #6a6050; }}
  .day-month {{ font-size: 9px; font-weight: 700; color: #4a4030;
                text-transform: uppercase; letter-spacing: 1px; }}

  /* Film entries */
  .day-films {{ display: flex; flex-direction: column; gap: 3px; }}
  .day-films.empty {{ color: #2a2420; font-size: 10px; }}
  .cal-entry {{ font-size: 10.5px; line-height: 1.4; display: flex;
                align-items: baseline; gap: 3px; flex-wrap: wrap; }}
  .cal-entry a {{ color: #c8b890; text-decoration: none;
                  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                  max-width: 100%; }}
  .cal-entry a:hover {{ color: #f5c842; }}
  .cal-dot {{ width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0;
              margin-top: 4px; }}
  .cal-theater {{ font-size: 9px; font-weight: 700; text-transform: uppercase;
                   letter-spacing: 0.5px; }}
  .cal-time {{ font-size: 9px; color: #6a6050; white-space: nowrap;
               font-variant-numeric: tabular-nums; margin-left: 1px; }}

  /* Now showing */
  .now-section {{ margin-top: 24px; border-top: 1px solid #252525; padding-top: 16px; }}
  .now-title {{ font-size: 10px; font-weight: 800; text-transform: uppercase;
                 letter-spacing: 2px; color: #4a4030; margin-bottom: 12px; }}
  .now-row {{ display: flex; gap: 14px; margin-bottom: 10px; align-items: baseline; }}
  .now-theater {{ font-size: 11px; font-weight: 700; min-width: 120px;
                   flex-shrink: 0; }}
  .now-films {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .now-entry {{ font-size: 12px; color: #8a7a60; text-decoration: none;
                 border: 1px solid #2a2a2a; border-radius: 4px; padding: 2px 8px; }}
  .now-entry:hover {{ border-color: #f5c842; color: #f5c842; }}

  /* ── Mobile: stack one day per row ─────────────────────────────────────── */
  @media (max-width: 700px) {{
    body {{ padding: 14px 12px; }}
    table, thead, tbody, tr {{ display: block; width: 100%; }}
    thead {{ display: none; }}   /* hide Mon/Tue/... header row */

    td.day {{
      display: block;
      border: none;
      border-bottom: 1px solid #1e1e1e;
      min-height: unset;
      padding: 10px 4px;
    }}
    /* Hide completely empty days on mobile to keep scrolling short */
    td.day .day-films.empty {{ display: none; }}
    td.day:has(.day-films.empty) {{ display: none; }}

    /* Show day name from data-day attribute */
    td.day::before {{
      content: attr(data-day);
      display: block;
      font-size: 10px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 1.5px;
      color: #5a5040;
      margin-bottom: 6px;
    }}
    td.today::before {{ color: #f5c842; }}
    td.past {{ display: none; }}   /* hide past days on mobile */

    /* Hide the day-hd number (we show it in the ::before label instead) */
    .day-hd {{ display: none; }}

    /* Bigger tap targets for film links */
    .cal-entry {{ font-size: 12px; padding: 2px 0; }}
    .cal-entry a {{ white-space: normal; }}
    .cal-time {{ font-size: 10px; }}

    /* Now showing: stack theater + films vertically */
    .now-row {{ flex-direction: column; gap: 4px; margin-bottom: 14px; }}
    .now-theater {{ min-width: unset; font-size: 12px; }}
  }}

</style>
</head>
<body>
<h1>🎬 Twin Cities Cinema — 4-Week Calendar</h1>
<div class="sub">Generated {date_long} &nbsp;·&nbsp; Next 28 days</div>
<div class="legend">{legend_items}</div>
<table>
  <thead><tr>{header_cells}</tr></thead>
  <tbody>
{week_rows}  </tbody>
</table>
{undated_html}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# EMAIL (.eml) GENERATION
# ---------------------------------------------------------------------------

def build_eml(html: str, recipient: str = "", subject_override: str = "") -> bytes:
    """Return a fully-formed RFC 2045 .eml file as bytes."""
    import base64

    recipient = recipient or EMAIL_RECIPIENT
    now       = datetime.datetime.now()
    day       = str(now.day)
    subject   = subject_override or EMAIL_SUBJECT.format(
        date=now.strftime(f"%B {day}, %Y")
    )

    subject_b64     = base64.b64encode(subject.encode("utf-8")).decode("ascii")
    subject_encoded = f"=?utf-8?b?{subject_b64}?="
    boundary        = "=_CinemaScraper"
    html_b64        = base64.b64encode(html.encode("utf-8")).decode("ascii")
    html_b64_wrapped = "\r\n".join(
        html_b64[i:i + 76] for i in range(0, len(html_b64), 76)
    )

    eml = (
        "MIME-Version: 1.0\r\n"
        f"Subject: {subject_encoded}\r\n"
        f"To: {recipient}\r\n"
        f"From: {recipient}\r\n"
        f'Content-Type: multipart/alternative; boundary="{boundary}"\r\n'
        "\r\n"
        f"--{boundary}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n"
        "\r\n"
        "Twin Cities Art Cinema Listings - please view in HTML.\r\n"
        "\r\n"
        f"--{boundary}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{html_b64_wrapped}\r\n"
        f"--{boundary}--\r\n"
    )
    return eml.encode("utf-8")


# ---------------------------------------------------------------------------
# CLI ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape Twin Cities art cinema listings and build a listings email."
    )
    parser.add_argument("--preview", action="store_true",
                        help="Open HTML in browser instead of saving .eml")
    parser.add_argument("--theater", metavar="NAME",
                        help="Scrape only this theater (partial name match)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    sys.path.insert(0, str(Path(__file__).parent))
    from sites_config import THEATERS, CUSTOM_THEATERS
    all_configs = THEATERS + CUSTOM_THEATERS

    selected = None
    if args.theater:
        selected = [
            t["name"] for t in all_configs
            if args.theater.lower() in t["name"].lower()
        ]
        if not selected:
            log.error("No theater matching '%s' found.", args.theater)
            sys.exit(1)
        log.info("Filtering to: %s", selected)

    films = fetch_all_films(selected_theaters=selected)

    active_configs = [t for t in all_configs if (not selected or t["name"] in selected)]
    html = build_html_from_flat(films, active_configs)

    ts        = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    html_path = OUTPUT_DIR / f"cinema_listings_{ts}.html"
    html_path.write_text(html, encoding="utf-8")
    log.info("HTML saved: %s", html_path)

    if args.preview:
        import webbrowser
        log.info("Preview mode — opening in browser")
        webbrowser.open(html_path.as_uri())
    else:
        eml_path = OUTPUT_DIR / f"cinema_listings_{ts}.eml"
        eml_path.write_bytes(build_eml(html))
        log.info(".eml saved: %s", eml_path)

        import subprocess, platform
        if platform.system() == "Windows":
            subprocess.Popen(["cmd", "/c", "start", "", str(eml_path)])
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(eml_path)])
        else:
            log.info("Open %s in your mail client.", eml_path)


if __name__ == "__main__":
    main()
