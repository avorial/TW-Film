"""
web_app.py  —  Flask web interface for Twin Cities Art Cinema Scraper
"""

import datetime
import io
import json
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

from flask import (Flask, Response, jsonify, render_template,
                   request, send_file, stream_with_context)

BASE_DIR               = Path(__file__).parent
DATA_DIR               = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
CUSTOM_THEATERS_FILE   = DATA_DIR / "custom_theaters.json"
CACHE_FILE             = DATA_DIR / "last_scrape.json"

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

sys.path.insert(0, str(BASE_DIR))
import scraper as sc
from sites_config import THEATERS


# ---------------------------------------------------------------------------
# Cache persistence
# ---------------------------------------------------------------------------

def load_cache() -> dict | None:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return None


def save_cache(html: str, calendar_html: str, theaters: list[str],
               films: list[dict] | None = None):
    try:
        # Strip non-serialisable fields before storing films
        clean_films = [
            {k: v for k, v in f.items() if k not in ("_date_obj", "raw_text")}
            for f in (films or [])
        ]
        CACHE_FILE.write_text(json.dumps({
            "html":          html,
            "calendar_html": calendar_html or "",
            "theaters":      theaters,
            "films":         clean_films,
            "timestamp":     datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }))
    except Exception as exc:
        logging.warning("Could not save cache: %s", exc)


# ---------------------------------------------------------------------------
# Highlight film
# ---------------------------------------------------------------------------

HIGHLIGHT_FILE = DATA_DIR / "highlight.json"


def load_highlight() -> dict | None:
    if HIGHLIGHT_FILE.exists():
        try:
            return json.loads(HIGHLIGHT_FILE.read_text())
        except Exception:
            pass
    return None


def save_highlight(film: dict):
    try:
        HIGHLIGHT_FILE.write_text(json.dumps(
            {k: v for k, v in film.items() if k not in ("_date_obj", "raw_text")},
            ensure_ascii=False, indent=2,
        ))
    except Exception:
        pass


def pick_highlight(films: list[dict]) -> dict | None:
    """
    Pick a random classic/art film that has both a poster and a synopsis.
    Only considers art-theater films whose Wikipedia synopsis suggests it is
    NOT a current-year release (i.e. a true classic or repertory pick).
    """
    import random
    current_year = str(datetime.datetime.now().year)
    candidates = [
        f for f in films
        if f.get("poster")
        and f.get("desc")
        and f.get("group", "art") == "art"
        and current_year not in (f.get("desc", "")[:120])
    ]
    return random.choice(candidates) if candidates else None


# ---------------------------------------------------------------------------
# iCal feed builder
# ---------------------------------------------------------------------------

def build_ical(films: list[dict]) -> str:
    """Return a VCALENDAR string suitable for calendar app subscriptions."""
    import re as _re

    def _esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace(";", "\\;") \
                        .replace(",", "\\,").replace("\n", "\\n")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Twin Cities Cinema Scraper//EN",
        "X-WR-CALNAME:Twin Cities Cinema",
        "X-WR-TIMEZONE:America/Chicago",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    seen: set[str] = set()

    for film in films:
        date_text = film.get("date_text", "")
        if not date_text:
            continue
        parts     = [p.strip() for p in date_text.split("·")]
        date_part = parts[0]
        time_part = parts[1] if len(parts) > 1 else ""

        d, _ = sc._parse_date_from_text(date_part)
        if not d:
            continue

        tm = _re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_part, _re.IGNORECASE)
        if tm:
            hr, mn = int(tm.group(1)), int(tm.group(2))
            if tm.group(3).upper() == "PM" and hr != 12:
                hr += 12
            elif tm.group(3).upper() == "AM" and hr == 12:
                hr = 0
            dtstart   = datetime.datetime(d.year, d.month, d.day, hr, mn)
            start_str = dtstart.strftime("%Y%m%dT%H%M%S")
            end_str   = (dtstart + datetime.timedelta(hours=2)).strftime("%Y%m%dT%H%M%S")
            vtype     = ""
        else:
            start_str = d.strftime("%Y%m%d")
            end_str   = (d + datetime.timedelta(days=1)).strftime("%Y%m%d")
            vtype     = ";VALUE=DATE"

        title   = film.get("title", "")
        theater = film.get("theater", "")
        uid     = _re.sub(r"\W+", "-", f"{title}-{theater}-{start_str}".lower())[:72]
        uid    += "@twincitiescinema"
        if uid in seen:
            continue
        seen.add(uid)

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART{vtype}:{start_str}",
            f"DTEND{vtype}:{end_str}",
            f"SUMMARY:{_esc(title)} — {_esc(theater)}",
        ]
        if film.get("desc"):
            lines.append(f"DESCRIPTION:{_esc(film['desc'][:280])}")
        if film.get("url"):
            lines.append(f"URL:{film['url']}")
        if film.get("address"):
            lines.append(f"LOCATION:{_esc(film['address'])}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ---------------------------------------------------------------------------
# Custom theater persistence
# ---------------------------------------------------------------------------

def load_custom_theaters() -> list[dict]:
    if CUSTOM_THEATERS_FILE.exists():
        try:
            return json.loads(CUSTOM_THEATERS_FILE.read_text())
        except Exception:
            pass
    return []


def save_custom_theaters(theaters: list[dict]):
    CUSTOM_THEATERS_FILE.write_text(json.dumps(theaters, indent=2))


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------

_job: dict = {"running": False, "log": [], "html": None, "calendar_html": None,
              "films": None, "error": None}
_job_lock  = threading.Lock()

# Pre-populate from disk so results are available immediately on startup
_startup_cache = load_cache()
if _startup_cache:
    _job["html"]          = _startup_cache.get("html")
    _job["calendar_html"] = _startup_cache.get("calendar_html")
    _job["films"]         = _startup_cache.get("films")


def _reset_job():
    with _job_lock:
        _job["running"]       = True
        _job["log"]           = []
        _job["html"]          = None
        _job["calendar_html"] = None
        _job["films"]         = None
        _job["error"]         = None


def _push_log(msg: str):
    with _job_lock:
        _job["log"].append(msg)


def _finish_job(html=None, calendar_html=None, films=None, error=None):
    with _job_lock:
        _job["running"]       = False
        _job["html"]          = html
        _job["calendar_html"] = calendar_html
        _job["films"]         = films
        _job["error"]         = error


# ---------------------------------------------------------------------------
# Scrape worker
# ---------------------------------------------------------------------------

def _scrape_worker(selected_theaters: list[str], classic_mode: bool = False):
    try:
        # Attach SSE log handler so progress streams to the browser
        class SSEHandler(logging.Handler):
            def emit(self, record):
                _push_log(self.format(record))

        handler = SSEHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        sc.log.addHandler(handler)
        sc.log.setLevel(logging.INFO)

        mode_label = "🎞 CLASSIC MODE" if classic_mode else "🎬"
        _push_log(f"{mode_label} Starting scrape for {len(selected_theaters)} theater(s)…")

        # Merge built-in + custom configs
        custom = load_custom_theaters()
        all_configs = THEATERS + custom

        # Filter to selected
        active_configs = [
            t for t in all_configs if t["name"] in selected_theaters
        ]

        all_films: list[dict] = []
        for config in active_configs:
            _push_log(f"  Fetching {config['name']}…")
            films = sc.scrape_theater(config)
            # Tag each film with its theater group for highlight/filter logic
            for f in films:
                f.setdefault("group",   config.get("group", "art"))
                f.setdefault("address", config.get("address", ""))
                f.setdefault("map_url", config.get("map_url", ""))
            _push_log(f"  ✓ {config['name']}: {len(films)} listing(s)")
            all_films.extend(films)

        _push_log(f"✅ Total: {len(all_films)} film listing(s) across "
                  f"{len(active_configs)} theater(s)")

        _push_log("🔍 Fetching synopses for films without descriptions…")
        sc.enrich_with_synopses(all_films)
        filled = sum(1 for f in all_films if f.get("desc"))
        _push_log(f"✅ Synopses ready ({filled}/{len(all_films)} films have descriptions)")

        # Filter classic/revival films from "current" theaters
        custom = load_custom_theaters()
        all_configs_map = {t["name"]: t for t in THEATERS + custom}
        current_names = {
            t["name"] for t in active_configs
            if all_configs_map.get(t["name"], {}).get("group") == "current"
        }
        if current_names:
            before = len(all_films)
            all_films = sc.filter_current_films(all_films, current_names)
            removed = before - len(all_films)
            if removed:
                _push_log(f"🎯 Filtered {removed} classic/revival film(s) from current theaters")

        # Classic mode: strip any film released in the current year
        if classic_mode:
            before = len(all_films)
            all_films = sc.filter_classic_mode(all_films)
            removed = before - len(all_films)
            _push_log(f"🎞 Classic mode: removed {removed} current-year film(s), "
                      f"{len(all_films)} classic film(s) remain")

        _push_log("📧 Building listings HTML…")
        html = sc.build_html_from_flat(all_films, active_configs)

        # Build calendar separately — a failure here must NOT prevent the
        # email listing from being downloadable.
        calendar_html = None
        try:
            _push_log("📅 Building 4-week calendar…")
            calendar_html = sc.build_calendar_html(all_films, active_configs)
        except Exception as cal_exc:
            _push_log(f"⚠️ Calendar build failed: {cal_exc}")

        _push_log("✅ Done! Preview and download ready.")
        _finish_job(html=html, calendar_html=calendar_html, films=all_films)
        save_cache(html, calendar_html, selected_theaters, films=all_films)
        _push_log("💾 Results cached — will reload instantly next visit.")

        # Pick a classic highlight film and persist it
        highlight = pick_highlight(all_films)
        if highlight:
            save_highlight(highlight)
            _push_log(f"🎬 Highlight: '{highlight['title']}'")

    except Exception as exc:
        _push_log(f"❌ Error: {exc}")
        _finish_job(error=str(exc))
    finally:
        sc.log.handlers = [h for h in sc.log.handlers
                           if not isinstance(h, SSEHandler)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/cache-meta")
def cache_meta():
    cache = load_cache()
    if not cache:
        return jsonify({"has_cache": False})
    return jsonify({
        "has_cache":    True,
        "timestamp":    cache.get("timestamp"),
        "theaters":     cache.get("theaters", []),
    })


@app.route("/")
def index():
    art_theaters     = []
    current_theaters = []
    seen = set()
    for t in THEATERS:
        if t["name"] not in seen:
            seen.add(t["name"])
            entry = {
                "name":    t["name"],
                "url":     t["url"],
                "address": t.get("address", ""),
                "map_url": t.get("map_url", ""),
                "group":   t.get("group", "art"),
            }
            if entry["group"] == "current":
                current_theaters.append(entry)
            else:
                art_theaters.append(entry)

    custom_theaters = load_custom_theaters()

    return render_template(
        "index.html",
        art_theaters=art_theaters,
        current_theaters=current_theaters,
        custom_theaters=custom_theaters,
        default_email=sc.EMAIL_RECIPIENT,
    )


@app.route("/run", methods=["POST"])
def run_scraper():
    if _job["running"]:
        return jsonify({"error": "Already running"}), 409
    data              = request.get_json(force=True)
    selected_theaters = data.get("theaters", [])
    classic_mode      = bool(data.get("classic_mode", False))
    if not selected_theaters:
        return jsonify({"error": "No theaters selected"}), 400
    _reset_job()
    threading.Thread(
        target=_scrape_worker,
        args=(selected_theaters,),
        kwargs={"classic_mode": classic_mode},
        daemon=True,
    ).start()
    return jsonify({"started": True})


@app.route("/add-theater", methods=["POST"])
def add_theater():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    url  = data.get("url",  "").strip()
    if not name or not url:
        return jsonify({"error": "Name and URL are required"}), 400
    theaters = load_custom_theaters()
    if any(t["url"] == url for t in theaters):
        return jsonify({"error": "Theater URL already exists"}), 409
    group = data.get("group", "art")
    theaters.append({
        "name":        name,
        "url":         url,
        "base_url":    url.rstrip("/").rsplit("/", 1)[0] if "/" in url else url,
        "article_sel": "article, .event, li",
        "title_sel":   "h2 a, h3 a, a",
        "link_sel":    "h2 a, h3 a, a",
        "desc_sel":    "p",
        "date_sel":    "time, .date",
        "group":       group,
        "js_heavy":    False,
    })
    save_custom_theaters(theaters)
    return jsonify({"ok": True, "theaters": theaters})


@app.route("/remove-theater", methods=["POST"])
def remove_theater():
    data      = request.get_json(force=True)
    url       = data.get("url", "")
    theaters  = [t for t in load_custom_theaters() if t["url"] != url]
    save_custom_theaters(theaters)
    return jsonify({"ok": True, "theaters": theaters})


@app.route("/stream")
def stream():
    def generate():
        import time
        sent = 0
        while True:
            with _job_lock:
                logs    = list(_job["log"])
                running = _job["running"]
            while sent < len(logs):
                yield f"data: {json.dumps({'type': 'log', 'msg': logs[sent]})}\n\n"
                sent += 1
            if not running:
                with _job_lock:
                    err = _job["error"]
                if err:
                    yield f"data: {json.dumps({'type': 'error', 'msg': err})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                break
            time.sleep(0.4)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/get-html")
def get_html():
    with _job_lock:
        html = _job.get("html")
    if not html:
        return jsonify({"error": "No HTML ready"}), 400
    return jsonify({"html": html})


@app.route("/get-calendar-html")
def get_calendar_html():
    with _job_lock:
        html = _job.get("calendar_html")
    if not html:
        return jsonify({"error": "No calendar ready"}), 400
    return jsonify({"html": html})


@app.route("/download-eml", methods=["POST"])
def download_eml():
    data        = request.get_json(force=True)
    email_to    = data.get("email_to", sc.EMAIL_RECIPIENT).strip() or sc.EMAIL_RECIPIENT
    with _job_lock:
        html = _job.get("html")
    if not html:
        return jsonify({"error": "No listings ready yet"}), 400
    eml_bytes = sc.build_eml(html, recipient=email_to)
    now       = datetime.datetime.now()
    day       = str(now.day)
    filename  = f"CinemaListings_{now.strftime('%Y%m%d')}.eml"
    return send_file(
        io.BytesIO(eml_bytes),
        mimetype="message/rfc822",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/calendar.ics")
def serve_ical():
    with _job_lock:
        films = _job.get("films") or []
    if not films:
        cache = load_cache()
        films = (cache or {}).get("films", [])
    if not films:
        return "No data yet — run a scrape first.", 404, {"Content-Type": "text/plain"}
    ical = build_ical(films)
    return Response(
        ical,
        mimetype="text/calendar",
        headers={"Content-Disposition": "inline; filename=twin-cities-cinema.ics"},
    )


@app.route("/highlight")
def get_highlight():
    h = load_highlight()
    if not h:
        # Try in-memory films first, then fall back to disk cache
        with _job_lock:
            films = _job.get("films") or []
        if not films:
            cache = load_cache()
            films = (cache or {}).get("films", [])
        h = pick_highlight(films)
        if h:
            save_highlight(h)
    if not h:
        return jsonify({}), 204
    return jsonify(h)


@app.route("/highlight/shuffle", methods=["POST"])
def shuffle_highlight():
    with _job_lock:
        films = _job.get("films") or []
    if not films:
        cache = load_cache()
        films = (cache or {}).get("films", [])
    h = pick_highlight(films)
    if h:
        save_highlight(h)
        return jsonify(h)
    return jsonify({}), 204


# ---------------------------------------------------------------------------
# Auto-scrape watchdog
# ---------------------------------------------------------------------------

AUTO_SCRAPE_DAYS = 7   # trigger a scrape if cache is older than this


def _auto_scrape_watchdog():
    """
    Background thread: wakes every hour and triggers a scrape if the cache
    is older than AUTO_SCRAPE_DAYS days (or has never been run).
    Uses the same theater list as the last manual scrape, falling back to
    all art theaters if no previous selection exists.
    """
    import time

    # Brief startup delay so the Flask server is fully ready first
    time.sleep(60)

    while True:
        try:
            with _job_lock:
                already_running = _job["running"]
            if not already_running:
                cache      = load_cache()
                needs_run  = True

                if cache and cache.get("timestamp"):
                    last = datetime.datetime.fromisoformat(cache["timestamp"])
                    age  = datetime.datetime.now(datetime.timezone.utc) - last
                    if age.days < AUTO_SCRAPE_DAYS:
                        needs_run = False

                if needs_run:
                    theaters = (cache or {}).get("theaters") or [
                        t["name"] for t in THEATERS if t.get("group") == "art"
                    ]
                    logging.info(
                        "🕐 Auto-scrape triggered — cache is older than %d days",
                        AUTO_SCRAPE_DAYS,
                    )
                    _reset_job()
                    threading.Thread(
                        target=_scrape_worker,
                        args=(theaters,),
                        daemon=True,
                    ).start()

        except Exception as exc:
            logging.warning("Auto-scrape watchdog error: %s", exc)

        time.sleep(3600)   # check again in 1 hour


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8763))
    if not os.environ.get("DOCKER_ENV"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    # Start the auto-scrape watchdog
    threading.Thread(target=_auto_scrape_watchdog, daemon=True).start()
    print(f"Twin Cities Art Cinema Scraper running at http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
