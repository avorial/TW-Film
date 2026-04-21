# 🎬 TW-Film — Twin Cities Cinema Listings

A self-hosted web app that scrapes Minneapolis / Saint Paul movie theaters daily and serves a clean, dark-mode listings page with email export, a subscribable calendar feed, and a rotating Classic Spotlight.

[![Buy Me a Coffee](https://img.shields.io/badge/☕-Buy%20Me%20a%20Coffee-FFDD00?style=flat-square&labelColor=222)](https://buymeacoffee.com/thomasfilm)

---

## Features

- **Live scraping** of 13+ Twin Cities theaters — art houses, repertory cinemas, and mainstream multiplexes
- **Instant page load** — results are cached to disk and served immediately; a background refresh button triggers a new scrape
- **Classic Spotlight** — a randomly selected repertory/art-house film (with poster and synopsis) is highlighted at the top of each page
- **Email export** — generate a ready-to-send `.eml` file of the week's listings, formatted for Gmail/Outlook
- **Subscribable iCal feed** at `/calendar.ics` — add to Apple Calendar, Google Calendar, or Outlook and it auto-updates
- **Wikipedia synopsis enrichment** — plot summaries fetched and cached to disk so repeat scrapes are fast
- **Auto-scrape watchdog** — if the cache is older than 7 days the app triggers a fresh scrape automatically
- **4-week calendar view** — tab between the film listing and a date-grid overview
- **Docker-ready** — runs in a single container; code updates deploy via GitHub without rebuilding the image

---

## Theaters Covered

### Art Houses & Repertory Cinemas
| Theater | Location |
|---|---|
| Trylon Cinema | Minneapolis |
| Parkway Theater | Minneapolis |
| Riverview Theater | Minneapolis |
| Heights Theater | Columbia Heights |
| St. Anthony Main Theatre (Landmark) | Minneapolis |
| MSP Film Society | Minneapolis |
| Walker Art Center Cinema | Minneapolis |

### Current / Mainstream
| Theater | Location |
|---|---|
| Emagine Willow Creek | Minnetonka |
| Mann Theatre Edina 4 | Edina |
| AMC Rosedale 14 | Roseville |
| AMC Eden Prairie 18 | Eden Prairie |
| Alamo Drafthouse Woodbury Lakes | Woodbury |
| Marcus West End Cinema | St. Louis Park |

---

## Tech Stack

- **Python 3.11** / Flask
- **BeautifulSoup 4** — HTML scraping
- **Playwright** — JavaScript-heavy sites (AMC, Walker, Landmark, Fandango)
- **ThreadPoolExecutor** — concurrent film-page fetching (MSP Film Society showtimes)
- **Wikipedia API** — synopsis enrichment with persistent disk cache
- **Server-Sent Events** — real-time scrape log streaming to the browser
- **iCalendar (VCALENDAR)** — subscribable calendar feed

---

## Quick Start (Docker)

### 1. Pull and run

```bash
docker run -d \
  -p 5050:5050 \
  --name twfilm \
  --restart unless-stopped \
  ghcr.io/avorial/tw-film:latest
```

Then open **http://localhost:5050** in your browser.

### 2. Portainer / self-hosted

Use the `docker-compose.yml` in this repo. After deploying, simply **restart the container** to pull the latest code from GitHub — no image rebuild required.

---

## Local Development

```bash
# 1. Clone
git clone https://github.com/avorial/TW-Film.git
cd TW-Film

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements_web.txt
playwright install chromium

# 4. Run
python web_app.py
```

Open **http://localhost:5050**, then click **▶ Scrape Cinema Listings** to populate the page.

---

## Project Layout

```
TW-Film/
├── web_app.py            # Flask app — routes, caching, iCal, watchdog
├── scraper.py            # Theater scraping engine
├── sites_config.py       # Per-theater selectors & metadata
├── requirements_web.txt  # Python dependencies
├── Dockerfile
├── entrypoint.sh         # git pull + start (used inside Docker)
├── docker-compose.yml
├── push_to_github.bat    # One-click push helper (Windows)
├── templates/
│   └── index.html        # Single-page UI (dark mode, SSE log, highlight card)
├── static/
│   └── favicon.*
└── data/                 # Runtime data (created automatically)
    ├── last_scrape.json   # Cached HTML + raw film list
    ├── synopsis_cache.json# Wikipedia synopsis disk cache
    └── highlight.json     # Current Classic Spotlight film
```

---

## Updating

Because the Docker container clones from GitHub at startup, deploying a new version is just:

1. Push your changes to `main` (run `push_to_github.bat` on Windows)
2. Restart the container in Portainer (or `docker restart twfilm`)

The `entrypoint.sh` runs `git fetch origin main && git reset --hard origin/main` before starting Flask, so the container always boots with the latest code.

---

## Support

If you find this useful, consider buying me a coffee!

☕ [buymeacoffee.com/thomasfilm](https://buymeacoffee.com/thomasfilm)
