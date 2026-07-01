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
    # optional — sync the map across devices (phone + PC) via Firebase:
    export FIREBASE_API_KEY="..."     # Firebase web API key
    export FIREBASE_DB_URL="https://<project>-default-rtdb.firebaseio.com"
    export FIREBASE_EMAIL="you@example.com"
    export FIREBASE_PASSWORD="..."
    python sweep_leads.py
    # then open the hosted coverage_map.html (GitHub Pages) and sign in

CLOUD SYNC
  If the FIREBASE_* vars are set, each run signs in over the Identity Toolkit
  REST API (no SDK — stays requests-only) and pushes coverage + leads to the
  shared Realtime Database, so new runs show up on every device. Coverage is
  overwritten with the full cumulative collection; leads are merged (upserted),
  so your on-map edits, notes and deletions are never clobbered. With the vars
  unset the script runs exactly as before, writing local files only.
"""

import csv
import datetime
import json
import math
import os
import re
import time
import requests

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

# Firebase (optional): read from the environment like GOOGLE_MAPS_API_KEY. When
# all four are set, coverage + leads are synced to the shared map after a run.
FIREBASE_API_KEY  = os.environ.get("FIREBASE_API_KEY")
FIREBASE_DB_URL   = os.environ.get("FIREBASE_DB_URL")
FIREBASE_EMAIL    = os.environ.get("FIREBASE_EMAIL")
FIREBASE_PASSWORD = os.environ.get("FIREBASE_PASSWORD")

# ======================= EDIT THIS SECTION =======================
RUN_NAME = "ndg_monkland_03"        # label for this run (also names the CSV)

SOUTH, WEST = 45.464646, -73.6500     # SW corner of the box to sweep
NORTH, EAST = 45.479994, -73.635027     # NE corner

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

OUTPUT_FILE       = f"{RUN_NAME}_leads.csv"
COVERAGE_FILE     = "coverage.geojson"
MAP_FILE          = "coverage_map.html"
COVERAGE_DATA_FILE = "coverage_data.js"
LEADS_JS_FILE     = "leads_data.js"

URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress",
    "places.nationalPhoneNumber", "places.websiteUri",
    "places.googleMapsUri", "places.location", "nextPageToken",
])
# NOTE: places.location is a Pro-tier field. The request already pulls
# websiteUri + phone (Enterprise tier), and billing is set by the highest
# tier requested, so adding location stays in the Enterprise SKU — no extra
# cost. It lets every lead carry exact coordinates for the map's pins.

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
            loc = p.get("location", {})
            leads.append({"name": p.get("displayName", {}).get("text", ""),
                          "address": p.get("formattedAddress", ""),
                          "phone": p.get("nationalPhoneNumber", ""),
                          "maps_url": p.get("googleMapsUri", ""),
                          "lat": loc.get("latitude"),
                          "lng": loc.get("longitude")})


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


def write_map(fc):
    """Write coverage_data.js; generate coverage_map.html only on first run."""
    with open(COVERAGE_DATA_FILE, "w", encoding="utf-8") as f:
        f.write("/* Auto-generated by sweep_leads.py — do not edit by hand */\n")
        f.write("window.COVERAGE_DATA = " + json.dumps(fc) + ";\n")
    if not os.path.exists(MAP_FILE):
        print(f"  Note: {MAP_FILE} not found. "
              "Get it from the repo or create it manually.")


def write_leads_js(new_leads):
    """Merge new leads into leads_data.js, deduplicated by Google Maps CID."""
    registry = {}
    if os.path.exists(LEADS_JS_FILE):
        with open(LEADS_JS_FILE, encoding="utf-8") as f:
            content = f.read()
        m = re.search(r"window\.LEADS_REGISTRY\s*=\s*(\[[\s\S]*?\]);", content)
        if m:
            try:
                registry = {e["id"]: e for e in json.loads(m.group(1))}
            except Exception:
                pass

    for lead in new_leads:
        m = re.search(r"cid=(\d+)", lead.get("maps_url", ""))
        if m:
            cid = m.group(1)
        else:
            import hashlib
            raw = (lead.get("name", "") + lead.get("address", "")).encode()
            cid = str(int(hashlib.md5(raw).hexdigest()[:16], 16))
        entry = {
            "id":       cid,
            "run":      RUN_NAME,
            "name":     lead.get("name", ""),
            "address":  lead.get("address", ""),
            "phone":    lead.get("phone", ""),
            "maps_url": lead.get("maps_url", ""),
        }
        if lead.get("lat") is not None and lead.get("lng") is not None:
            entry["lat"] = round(lead["lat"], 6)
            entry["lng"] = round(lead["lng"], 6)
        registry[cid] = entry

    entries = list(registry.values())
    content = ("/* Auto-generated by sweep_leads.py — do not edit by hand */\n"
               "window.LEADS_REGISTRY = "
               + json.dumps(entries, ensure_ascii=False, indent=2) + ";\n")
    with open(LEADS_JS_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    return entries


def _firebase_signin():
    """Trade email+password for a short-lived ID token (Identity Toolkit REST)."""
    url = ("https://identitytoolkit.googleapis.com/v1/accounts:"
           "signInWithPassword?key=" + FIREBASE_API_KEY)
    r = requests.post(url, timeout=30, json={
        "email": FIREBASE_EMAIL, "password": FIREBASE_PASSWORD,
        "returnSecureToken": True})
    r.raise_for_status()
    return r.json()["idToken"]


def push_to_firebase(fc, lead_entries):
    """Sync coverage (overwrite) and leads (merge) to the shared Realtime DB."""
    if not all([FIREBASE_API_KEY, FIREBASE_DB_URL,
                FIREBASE_EMAIL, FIREBASE_PASSWORD]):
        print("  Firebase vars not set - skipping cloud sync "
              "(map won't refresh on your phone).")
        return
    try:
        auth = {"auth": _firebase_signin()}
        base = FIREBASE_DB_URL.rstrip("/")
        # Coverage: the full cumulative collection replaces the node.
        r = requests.put(f"{base}/coverage.json", params=auth, json=fc, timeout=30)
        r.raise_for_status()
        # Leads: shallow-merge by id -> upsert; never deletes your on-map edits.
        leads_obj = {e["id"]: e for e in lead_entries}
        r = requests.patch(f"{base}/leads.json", params=auth,
                           json=leads_obj, timeout=30)
        r.raise_for_status()
        print(f"  Firebase sync: {len(fc['features'])} cells, "
              f"{len(leads_obj)} leads pushed - live on all devices.")
    except Exception as e:
        print(f"  Firebase sync failed ({e}); local files were still written.")


def save_outputs(new_features):
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "address", "phone", "maps_url"],
                           extrasaction="ignore")   # lat/lng kept for the map only
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
    entries = write_leads_js(leads)
    push_to_firebase(fc, entries)


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
