"""
sites_config.py  —  Theater scraping configurations for Twin Cities Art Cinema Scraper

Each theater dict supports two scraping modes:
  - HTML scraping: provide article_sel, title_sel, link_sel, desc_sel, date_sel
  - RSS (if ever available): provide an "rss" key

group field:
  "art"     — independent / repertory cinema (art house)
  "current" — mainstream multiplex; scraper will filter out classic/revival films

Selector notes:
  - Use browser DevTools (F12 → Inspector) to verify/update selectors if a
    theater redesigns their site.
  - For sites that render content via JavaScript, set "js_heavy": True — these
    require a browser automation tool (Playwright/Selenium).
"""

# ── ART THEATERS ──────────────────────────────────────────────────────────────
# Independent, repertory, and specialty cinemas.  All films are included as-is;
# no release-year filtering is applied to these.

THEATERS = [
    # ── Trylon Cinema ──────────────────────────────────────────────────────
    # Uses the My Calendar plugin's ?format=calendar page which embeds a
    # Schema.org JSON-LD array of Event objects — one per individual screening.
    # Each event has: name (title), startDate (ISO datetime), url (film page).
    # The page requires a Referer header from the Trylon domain to avoid 403.
    # json_ld=True routes the scraper to _parse_json_ld() instead of HTML selectors.
    {
        "name":        "Trylon Cinema",
        "url":         "https://www.trylon.org/calendar/?format=calendar",
        "base_url":    "https://www.trylon.org",
        "article_sel": "",           # unused in json_ld mode
        "title_sel":   "",           # unused in json_ld mode
        "link_sel":    "",           # unused in json_ld mode
        "desc_sel":    None,
        "date_sel":    None,
        "poster_sel":  None,
        "referer":     "https://www.trylon.org/",
        "json_ld":     True,
        "max_films":   60,
        "address":     "2820 E 33rd St, Minneapolis, MN 55406",
        "map_url":     "https://maps.google.com/?q=Trylon+Cinema+2820+E+33rd+St+Minneapolis+MN",
        "group":       "art",
        "js_heavy":    False,
    },

    # ── Parkway Theater ────────────────────────────────────────────────────
    {
        "name":        "Parkway Theater",
        "url":         "https://theparkwaytheater.com/movies/",
        "base_url":    "https://theparkwaytheater.com",
        "article_sel": ".summary-item",
        "title_sel":   "a.summary-title-link",
        "link_sel":    "a.summary-title-link",
        "desc_sel":    ".summary-excerpt",
        "date_sel":    "time.summary-metadata-item--date",
        "time_sel":    "span.event-time-12hr",
        "poster_sel":  ".summary-thumbnail-container img",
        "address":     "4814 Chicago Ave, Minneapolis, MN 55417",
        "map_url":     "https://maps.google.com/?q=Parkway+Theater+4814+Chicago+Ave+Minneapolis+MN",
        "group":       "art",
        "js_heavy":    False,
    },

    # ── Riverview Theater ──────────────────────────────────────────────────
    {
        "name":              "Riverview Theater",
        "url":               "https://www.riverviewtheater.com/",
        "base_url":          "https://www.riverviewtheater.com",
        "article_sel":       "li",
        "title_sel":         "a",
        "link_sel":          "a",
        "desc_sel":          None,
        "date_sel":          None,
        "poster_sel":        None,
        "link_filter":       "/show/show/",
        "strip_times_from_title": True,
        "address":           "3800 42nd Ave S, Minneapolis, MN 55406",
        "map_url":           "https://maps.google.com/?q=Riverview+Theater+3800+42nd+Ave+S+Minneapolis+MN",
        "group":             "art",
        "js_heavy":          False,
    },

    # ── St. Anthony Main Theatre (Landmark) ────────────────────────────────
    {
        "name":        "St. Anthony Main Theatre",
        "url":         "https://www.landmarktheatres.com/movies?theatre=st-anthony-main-theatre-minneapolis-mn",
        "base_url":    "https://www.landmarktheatres.com",
        "article_sel": ".now-playing-movie, .movie-card, [data-testid='movie-card']",
        "title_sel":   ".movie-title, h3, [data-testid='movie-title']",
        "link_sel":    "a",
        "desc_sel":    ".movie-synopsis, p",
        "date_sel":    ".showtime, .schedule, time",
        "address":     "115 SE Main St, Minneapolis, MN 55414",
        "map_url":     "https://maps.google.com/?q=St+Anthony+Main+Theatre+115+SE+Main+St+Minneapolis+MN",
        "group":       "art",
        "js_heavy":           True,
        "use_landmark_scraper": True,
    },

    # ── Heights Theater ────────────────────────────────────────────────────
    # Veezi ticketing page — server-rendered, no JS required.
    # no_deduplicate=True: each .film card is one film on one specific date.
    # Date format: "Tuesday 14, April" (Day, Month order — handled by parser).
    {
        "name":             "Heights Theater",
        "url":              "https://ticketing.useast.veezi.com/sessions/3408r7zh5fyw2jpcrakzvh6mp0",
        "base_url":         "https://ticketing.useast.veezi.com",
        "article_sel":      ".film",
        "title_sel":        "h3.title",
        "link_sel":         ".session-times a",
        "desc_sel":         None,
        "date_sel":         ".date-container h4.date",
        "time_sel":         ".session-times time",
        "poster_sel":       ".poster-container img.poster",
        "no_deduplicate":   True,
        "address":          "3951 Central Ave NE, Columbia Heights, MN 55421",
        "map_url":          "https://maps.google.com/?q=Heights+Theater+3951+Central+Ave+NE+Columbia+Heights+MN",
        "group":            "art",
        "js_heavy":         False,
    },

    # ── MSP Film Society (Main Cinema) ────────────────────────────────────
    # Homepage shows current/upcoming films as .show-card elements —
    # works year-round for both MSPIFF festival and regular programming.
    # Card structure:
    #   <div class="show-card">
    #     <a class="show-card__header" href="/show/...">
    #       <div class="show-card__image" style="background-image:url(...)"></div>
    #       <h2 class="show-card__title">Film Title</h2>
    #     </a>
    #     <div class="show-card-events__date">Monday, Apr 13th</div>
    #   </div>
    # Poster is a CSS background-image → use poster_bg_sel.
    # film_page_* selectors fetch individual film pages for exact showtimes.
    {
        "name":               "MSP Film Society",
        "url":                "https://mspfilm.org/",
        "base_url":           "https://mspfilm.org",
        "article_sel":        ".show-card",
        "title_sel":          ".show-card__title",
        "link_sel":           ".show-card__header",
        "desc_sel":           None,
        "date_sel":           ".show-card-events__date",
        "poster_sel":         None,
        "poster_bg_sel":      ".show-card__image",
        "max_films":          60,
        "film_page_day_sel":  ".gecko-show-events__day",
        "film_page_date_sel": ".gecko-show-events__date",
        "film_page_time_sel": ".gecko-show-events__showtime span",
        "film_page_workers":  10,
        "address":            "115 SE Main St, Minneapolis, MN 55414",
        "map_url":            "https://maps.google.com/?q=MSP+Film+Society+115+SE+Main+St+Minneapolis+MN",
        "group":              "art",
        "js_heavy":           False,
    },

    # ── Picturegoer Film Club ──────────────────────────────────────────────
    # Squarespace 7.1 site. Upcoming events page is server-rendered.
    # Each event is a summary-item card with:
    #   - title link: e.g. '"Gold Diggers of 1933" (1933) at Open Eye Theatre'
    #   - date text:  e.g. 'May 2nd, 2026. Click for details'
    #   - poster img in .summary-thumbnail-container
    # The title includes the venue name, so no fixed address — they screen
    # at various Twin Cities locations (Open Eye, North Garden, etc.).
    # Date parser strips the trailing ' Click for details' and ordinal suffix.
    {
        "name":        "Picturegoer Film Club",
        "url":         "https://www.picturegoerfilmclub.com/upcoming-events-1",
        "base_url":    "https://www.picturegoerfilmclub.com",
        "article_sel": ".summary-item",
        "title_sel":   "a.summary-title-link, .summary-title a, h2 a, h3 a",
        "link_sel":    "a.summary-title-link, .summary-title a, h2 a, h3 a",
        "desc_sel":    ".summary-excerpt p, .summary-excerpt",
        "date_sel":    ".summary-metadata-item--date, time, .summary-excerpt",
        "poster_sel":  ".summary-thumbnail-container img, .summary-item-image img, img",
        "address":     "Various Twin Cities venues",
        "map_url":     "https://maps.google.com/?q=Minneapolis+MN",
        "group":       "art",
        "js_heavy":    False,
    },

    # ── Walker Art Center Cinema ───────────────────────────────────────────
    # React SPA — custom Playwright scraper filters to SCREENING events only.
    {
        "name":               "Walker Art Center Cinema",
        "url":                "https://walkerart.org/calendar?type=screenings",
        "base_url":           "https://walkerart.org",
        "article_sel":        "article, [class*='card'], [class*='event']",
        "title_sel":          "h3, h4, [class*='title']",
        "link_sel":           "a",
        "desc_sel":           "p",
        "date_sel":           "time, [class*='date']",
        "address":            "725 Vineland Pl, Minneapolis, MN 55403",
        "map_url":            "https://maps.google.com/?q=Walker+Art+Center+725+Vineland+Pl+Minneapolis+MN",
        "group":              "art",
        "js_heavy":           True,
        "use_walker_scraper": True,
    },


    # ── CURRENT THEATERS ──────────────────────────────────────────────────
    # Mainstream multiplexes showing new releases.  After scraping, Wikipedia
    # synopsis data is used to filter out any film released more than 2 years
    # ago — keeping only current blockbusters and new releases.

    # ── Emagine Willow Creek ───────────────────────────────────────────────
    # Server-rendered WordPress site.  Movies are listed as .movies-row__item
    # cards — no JavaScript required.  No showtime dates in listing view;
    # films appear under "Now Showing" in the day-grouped output.
    {
        "name":        "Emagine Willow Creek",
        "url":         "https://emagine-entertainment.com/theatres/emagine-willow-creek/",
        "base_url":    "https://emagine-entertainment.com",
        "article_sel": ".movies-row__item",
        "title_sel":   "h3",
        "link_sel":    "a.js-TheaterMoviePosterLink",
        "desc_sel":    None,
        "date_sel":    None,
        "poster_sel":  ".movies-row__item-poster img",
        "address":     "11500 Wayzata Blvd, Minnetonka, MN 55305",
        "map_url":     "https://maps.google.com/?q=Emagine+Willow+Creek+11500+Wayzata+Blvd+Minnetonka+MN",
        "group":       "current",
        "js_heavy":    False,
    },

    # ── Mann Theatre Edina 4 ───────────────────────────────────────────────
    # Server-rendered. Each .schedule-dates div = one film on one date.
    # The date is encoded in the CSS class: "date-20260414" → April 14, 2026.
    # Synopsis and poster are included inline on the listing page.
    {
        "name":                    "Mann Theatre Edina 4",
        "url":                     "https://manntheatres.com/theatre/89/Edina-4",
        "base_url":                "https://manntheatres.com",
        "article_sel":             ".schedule-dates",
        "title_sel":               "h3 a",
        "link_sel":                "h3 a",
        "desc_sel":                ".movie-text-box p",
        "date_sel":                None,
        "date_from_class_prefix":  "date-",   # "date-20260414" → April 14, 2026
        "poster_sel":              ".comming-movie-box img",
        "address":                 "3911 W 50th St, Edina, MN 55424",
        "map_url":                 "https://maps.google.com/?q=Mann+Theatre+Edina+4+3911+W+50th+St+Edina+MN",
        "group":                   "current",
        "js_heavy":                False,
    },

    # ── AMC Rosedale 14 ────────────────────────────────────────────────────
    # AMC blocks standard requests (403).  Playwright renders the page in
    # headless Chromium and the generic multiplex JS extractor pulls movie
    # titles/links semantically from the rendered DOM.
    {
        "name":                  "AMC Rosedale 14",
        "url":                   "https://www.amctheatres.com/movie-theatres/minneapolis-st-paul/amc-rosedale-14",
        "base_url":              "https://www.amctheatres.com",
        "article_sel":           "[data-testid='movie-card'], .MovieTile, .movie-card",
        "title_sel":             "h2, h3, [data-testid='movie-title']",
        "link_sel":              "a",
        "desc_sel":              "p",
        "date_sel":              "time",
        "address":               "1595 MN-36, Roseville, MN 55113",
        "map_url":               "https://maps.google.com/?q=AMC+Rosedale+14+1595+MN-36+Roseville+MN",
        "group":                 "current",
        "js_heavy":              True,
        "use_multiplex_scraper": True,
    },

    # ── AMC Eden Prairie 18 (via Fandango) ────────────────────────────────
    {
        "name":                  "AMC Eden Prairie 18",
        "url":                   "https://www.fandango.com/amc-eden-prairie-mall-18-aaqiu/theater-page?format=all",
        "base_url":              "https://www.fandango.com",
        "article_sel":           "[data-testid='movie-card'], .MovieTile, .movie-card",
        "title_sel":             "h2, h3, [data-testid='movie-title']",
        "link_sel":              "a",
        "desc_sel":              "p",
        "date_sel":              "time",
        "address":               "8251 Flying Cloud Dr, Eden Prairie, MN 55344",
        "map_url":               "https://maps.google.com/?q=AMC+Eden+Prairie+18+8251+Flying+Cloud+Dr+Eden+Prairie+MN",
        "group":                 "current",
        "js_heavy":              True,
        "use_multiplex_scraper": True,
    },

    # ── Alamo Drafthouse Woodbury Lakes (via Fandango) ────────────────────
    {
        "name":                  "Alamo Drafthouse Woodbury Lakes",
        "url":                   "https://www.fandango.com/alamo-drafthouse-woodbury-lakes-aayjs/theater-page",
        "base_url":              "https://www.fandango.com",
        "article_sel":           "[class*='movie'], [class*='film'], article, li",
        "title_sel":             "h2, h3, h4",
        "link_sel":              "a",
        "desc_sel":              "p",
        "date_sel":              "time, [class*='date']",
        "address":               "9060 Hudson Rd, Woodbury, MN 55125",
        "map_url":               "https://maps.google.com/?q=Alamo+Drafthouse+Woodbury+Lakes+9060+Hudson+Rd+Woodbury+MN",
        "group":                 "current",
        "js_heavy":              True,
        "use_multiplex_scraper": True,
    },

    # ── Marcus West End Cinema (via Fandango) ──────────────────────────────
    # Fandango theater pages are JS-rendered; generic multiplex extractor
    # pulls movie titles/links from headings in the rendered DOM.
    {
        "name":                  "Marcus West End Cinema",
        "url":                   "https://www.fandango.com/marcus-west-end-cinema-aavnh/theater-page?format=all",
        "base_url":              "https://www.fandango.com",
        "article_sel":           "[class*='movie'], [class*='film'], article, li",
        "title_sel":             "h2, h3, h4",
        "link_sel":              "a",
        "desc_sel":              "p",
        "date_sel":              "time, [class*='date']",
        "address":               "1715 International Pkwy, St. Louis Park, MN 55416",
        "map_url":               "https://maps.google.com/?q=Marcus+West+End+Cinema+St+Louis+Park+MN",
        "group":                 "current",
        "js_heavy":              True,
        "use_multiplex_scraper": True,
    },
]

# ── Custom theaters added via the web UI ──────────────────────────────────
# Persisted to data/custom_theaters.json at runtime; this list is empty here.
CUSTOM_THEATERS = []
