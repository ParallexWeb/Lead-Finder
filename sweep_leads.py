#!/usr/bin/env python3
"""
sweep_leads.py - find local businesses with NO website on Google Maps.

Searches a rectangular area with the Google Places API (New), keeps every
business that has no website listed on its Google profile, and maintains a
visual map of everywhere you have already searched.

--------------------------------------------------------------------------
HOW IT WORKS
  * Tiles your bounding box into ~CELL_SIZE_KM square cells (gap-free grid).
  * For each cell, runs a Text Search for every business type and keeps the
    ones with no `websiteUri` field -> those are your leads.
  * The Places API returns at most 60 results per search. If a cell hits 60
    it is hiding businesses, so the cell is split into 4 and re-searched,
    repeating until cells come back under 60 (shrink only, never grow, so the
    grid stays cleanly tiled). Sparse cells cost just one request.
  * Pulls name, phone, address and Maps link straight from the search, so
    there is no separate (and pricier) per-business lookup.

OUTPUTS (written to the folder you run this from)
  * {RUN_NAME}_leads.csv  - this run's leads (name, address, phone, maps_url)
  * coverage.geojson      - cumulative record of every cell searched
  * coverage_map.html     - open in a browser: shaded, labelled tiles per run

DOING A WHOLE NEIGHBOURHOOD IN SEVERAL BOXES
  Reuse the edge of the box you just finished so the next one butts against it
  with no gap or overlap:
    move east  -> new WEST  = old EAST   (keep SOUTH/NORTH, push EAST further)
    move north -> new SOUTH = old NORTH  (keep WEST/EAST,  push NORTH further)
  Give each box a new RUN_NAME (ndg_01, ndg_02, ...) so its CSV is not
  overwritten; the coverage map shows them side by side. When the shaded tiles
  cover the area you care about, that neighbourhood is done.
  (Or just draw one larger box and let the grid tile it in a single run.)

COST
  Website + phone are Enterprise-tier fields. Google's free tier covers 1,000
  Enterprise requests per month; one search page = 1 request, a full 60-result
  cell = 3. EVENT_BUDGET stops the run before you can exceed the free tier.
  New Google Cloud accounts also get a $300 trial credit.

SETUP
    pip install requests
    export GOOGLE_MAPS_API_KEY="your_key_here"
    python sweep_leads.py
    # then open coverage_map.html in a browser (needs internet for the map)
"""

import csv
import datetime
import json
import math
import os
import time
import requests

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

# ======================= EDIT THIS SECTION =======================
RUN_NAME = "ndg_monkland_01"        # label for this run (also names the CSV)

SOUTH, WEST = 45.4650, -73.6350     # SW corner of the box to sweep
NORTH, EAST = 45.4800, -73.6150     # NE corner

CELL_SIZE_KM = 2.0                  # starting cell size; raise it in sparse areas
MAX_DEPTH = 5                       # times a capped cell may be subdivided
EVENT_BUDGET = 900                  # hard stop before the 1,000/month free cap

BUSINESS_TYPES = [
    "restaurant", "cafe", "bakery", "bar",
    "hair salon", "barbershop", "nail salon", "spa",
    "plumber", "electrician", "general contractor", "landscaper",
    "auto repair", "dentist", "physiotherapist", "veterinarian",
    "clothing store", "florist", "jewelry store", "pet store",
]
# =================================================================

OUTPUT_FILE = f"{RUN_NAME}_leads.csv"
COVERAGE_FILE = "coverage.geojson"
MAP_FILE = "coverage_map.html"

URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress",
    "places.nationalPhoneNumber", "places.websiteUri",
    "places.googleMapsUri", "nextPageToken",
])

events_used = 0
seen_ids = set()
leads = []
incomplete_cells = []   # cells still at 60 after maximum subdivision


def post_search(body):
    """POST one search with retry on transient errors. Returns parsed JSON."""
    headers = {"Content-Type": "application/json",
               "X-Goog-Api-Key": API_KEY,
               "X-Goog-FieldMask": FIELD_MASK}
    delay = 2
    for attempt in range(4):
        resp = requests.post(URL, headers=headers, json=body, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(delay)
            delay *= 2
            continue
        # non-retryable, or out of retries: surface Google's message then raise
        print(f"  API error {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
    return {}


def fetch_all_pages(text_query, rect):
    """Return up to 60 places for one type in one rectangle. Counts requests."""
    global events_used
    places, token = [], None
    for _ in range(3):                       # max 3 pages = 60 results
        if events_used >= EVENT_BUDGET:
            return places, True
        body = {"textQuery": text_query,
                "locationRestriction": {"rectangle": rect}}
        if token:
            body["pageToken"] = token
        data = post_search(body)
        events_used += 1
        places.extend(data.get("places", []))
        token = data.get("nextPageToken")
        if not token:
            break
        time.sleep(2)                        # page token needs a moment to go live
    return places, False


def quarters(rect):
    """Split a rectangle into 4 equal sub-rectangles (keeps the aspect ratio)."""
    lo, hi = rect["low"], rect["high"]
    mlat = (lo["latitude"] + hi["latitude"]) / 2
    mlng = (lo["longitude"] + hi["longitude"]) / 2

    def r(slat, wlng, nlat, elng):
        return {"low": {"latitude": slat, "longitude": wlng},
                "high": {"latitude": nlat, "longitude": elng}}

    return [r(lo["latitude"], lo["longitude"], mlat, mlng),
            r(lo["latitude"], mlng, mlat, hi["longitude"]),
            r(mlat, lo["longitude"], hi["latitude"], mlng),
            r(mlat, mlng, hi["latitude"], hi["longitude"])]


def record(places):
    """Keep businesses with no website; dedup by place id across the run."""
    for p in places:
        pid = p.get("id")
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        if "websiteUri" not in p:            # no website listed on Google
            leads.append({"name": p.get("displayName", {}).get("text", ""),
                          "address": p.get("formattedAddress", ""),
                          "phone": p.get("nationalPhoneNumber", ""),
                          "maps_url": p.get("googleMapsUri", "")})


def sweep(text_query, rect, depth=0):
    places, budget_hit = fetch_all_pages(text_query, rect)
    if len(places) >= 60 and depth < MAX_DEPTH and not budget_hit:
        for sub in quarters(rect):
            sweep(text_query, sub, depth + 1)
    else:
        record(places)
        if len(places) >= 60:                # capped even at max depth / budget
            incomplete_cells.append(rect)


def make_grid(area, cell_km):
    """Tile a box into ~cell_km squares, corrected for longitude shrink."""
    s, w = area["low"]["latitude"], area["low"]["longitude"]
    n, e = area["high"]["latitude"], area["high"]["longitude"]
    dlat = cell_km / 111.0
    dlng = cell_km / (111.0 * math.cos(math.radians((s + n) / 2)))
    cells, lat = [], s
    while lat < n:
        lat2 = min(lat + dlat, n)
        lng = w
        while lng < e:
            lng2 = min(lng + dlng, e)
            cells.append({"low": {"latitude": lat, "longitude": lng},
                          "high": {"latitude": lat2, "longitude": lng2}})
            lng = lng2
        lat = lat2
    return cells


def cell_feature(rect, biz_n, leads_n, events_n):
    s, w = rect["low"]["latitude"], rect["low"]["longitude"]
    n, e = rect["high"]["latitude"], rect["high"]["longitude"]
    ring = [[w, s], [e, s], [e, n], [w, n], [w, s]]
    return {"type": "Feature",
            "properties": {"run": RUN_NAME, "csv": OUTPUT_FILE,
                           "businesses": biz_n, "leads": leads_n,
                           "events": events_n,
                           "date": datetime.date.today().isoformat(),
                           "id": f"{RUN_NAME}:{s:.4f},{w:.4f}"},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


def load_coverage():
    if os.path.exists(COVERAGE_FILE):
        with open(COVERAGE_FILE) as f:
            return json.load(f)
    return {"type": "FeatureCollection", "features": []}


MAP_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Search coverage</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body{margin:0;height:100%}#map{height:100%}
.legend{background:#fff;padding:8px 10px;border-radius:6px;
font:13px sans-serif;box-shadow:0 1px 4px rgba(0,0,0,.3)}
.legend b{display:block;margin-bottom:4px}</style></head>
<body><div id="map"></div><script>
const DATA = __GEOJSON__;
const map = L.map('map');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'&copy; OpenStreetMap'}).addTo(map);
function colorFor(run){let h=0;for(const c of run)h=(h*31+c.charCodeAt(0))%360;
  return 'hsl('+h+',70%,45%)';}
const layer = L.geoJSON(DATA, {
  style: f => ({color: colorFor(f.properties.run), weight:1,
    fillColor: colorFor(f.properties.run), fillOpacity:0.25}),
  onEachFeature: (f,l) => {const p=f.properties;
    l.bindTooltip(p.run);
    l.bindPopup('<b>'+p.run+'</b><br>CSV: '+p.csv+
      '<br>Businesses: '+p.businesses+'<br>No-website leads: '+p.leads+
      '<br>Requests: '+p.events+'<br>Searched: '+p.date);}
}).addTo(map);
if (DATA.features.length) map.fitBounds(layer.getBounds(),{padding:[20,20]});
else map.setView([45.5,-73.65],11);
const runs=[...new Set(DATA.features.map(f=>f.properties.run))];
const legend=L.control({position:'bottomright'});
legend.onAdd=()=>{const d=L.DomUtil.create('div','legend');
  d.innerHTML='<b>Runs</b>'+runs.map(r=>'<span style="color:'+colorFor(r)+
    '">&#9632;</span> '+r).join('<br>');return d;};
legend.addTo(map);
</script></body></html>"""


def write_map(fc):
    html = MAP_TEMPLATE.replace("__GEOJSON__", json.dumps(fc))
    with open(MAP_FILE, "w", encoding="utf-8") as f:
        f.write(html)


def save_outputs(new_features):
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "address", "phone", "maps_url"])
        w.writeheader()
        w.writerows(leads)
    fc = load_coverage()
    by_id = {ft["properties"]["id"]: ft for ft in fc["features"]}
    for ft in new_features:
        by_id[ft["properties"]["id"]] = ft       # replace on re-run
    fc["features"] = list(by_id.values())
    with open(COVERAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    write_map(fc)


def main():
    if not API_KEY:
        raise SystemExit("Set GOOGLE_MAPS_API_KEY in your environment first.")

    area = {"low": {"latitude": SOUTH, "longitude": WEST},
            "high": {"latitude": NORTH, "longitude": EAST}}
    grid = make_grid(area, CELL_SIZE_KM)
    print(f"Run '{RUN_NAME}': {len(grid)} cell(s) of ~{CELL_SIZE_KM} km")

    new_features = []
    try:
        for i, cell in enumerate(grid, 1):
            if events_used >= EVENT_BUDGET:
                print(f"\n!! Event budget ({EVENT_BUDGET}) reached - stopping.")
                break
            sb, lb, eb = len(seen_ids), len(leads), events_used
            for btype in BUSINESS_TYPES:
                if events_used >= EVENT_BUDGET:
                    break
                sweep(btype, cell)
            biz, cl, ce = len(seen_ids) - sb, len(leads) - lb, events_used - eb
            new_features.append(cell_feature(cell, biz, cl, ce))
            print(f"  cell {i}/{len(grid)}: {biz} businesses, "
                  f"{cl} no-website leads, {ce} requests "
                  f"(total {events_used})")
    finally:
        save_outputs(new_features)
        print(f"\n{'=' * 52}")
        print(f"Requests used:      {events_used} / {EVENT_BUDGET}")
        print(f"Leads (no website): {len(leads)}  ->  {OUTPUT_FILE}")
        print(f"Coverage map:       {MAP_FILE}  (open in a browser)")
        if incomplete_cells:
            print(f"\n{len(incomplete_cells)} cell(s) still capped at 60 after max "
                  f"subdivision - those patches are very dense.")
            print("   Lower CELL_SIZE_KM or raise MAX_DEPTH to capture the rest.")


if __name__ == "__main__":
    main()
