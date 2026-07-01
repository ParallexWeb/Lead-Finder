# Claude Code Context — Lead Finder (businesses with no website)

## What this project is
A small Python tool that finds local small businesses with **no website listed on
Google Maps**, to use as sales prospects. It queries the Google Places API (New),
keeps the businesses with no website, and outputs a CSV call-list plus a visual
coverage map of the areas already searched. It feeds the sales pipeline for the
web-agency project (where the actual client sites are built — separate repo).

The tool now has two halves: (1) the **sweeper** (`sweep_leads.py`), and (2) a
**shared, login-gated web map** (`coverage_map.html`) hosted on GitHub Pages and
backed by a **Firebase Realtime Database**, so the same map — coverage, lead pins,
notes, colours — is live on phone and PC. `sweep_leads.py` writes local files
**and** (optionally) syncs each run to Firebase. The script is heavily commented;
**this file is the project-level intent and the guardrails, not a restatement of
the code.**

## Running it
- Requires `GOOGLE_MAPS_API_KEY` in the environment — a Google Cloud key with
  **Places API (New)** enabled and billing turned on.
- `pip install requests`, then `python sweep_leads.py`.
- The only thing edited per run is the CONFIG block at the top of the script:
  `RUN_NAME`, the four corner coordinates, and optionally `CELL_SIZE_KM`.
- Outputs land in the working directory: `{RUN_NAME}_leads.csv`,
  `coverage.geojson`, `coverage_data.js` / `leads_data.js` (data the map reads;
  they also seed Firebase on first login and act as an offline fallback), and
  `coverage_map.html`. **The data files and `*.csv` are gitignored** — they hold
  prospect data and must never reach the public repo/Pages.
- Optional cloud-sync env vars: `FIREBASE_API_KEY`, `FIREBASE_DB_URL`,
  `FIREBASE_EMAIL`, `FIREBASE_PASSWORD`. With them set, each run pushes coverage +
  leads to the shared map; unset, the script behaves exactly as before (local
  files only).
- The script **auto-loads a `.env` file beside it** (tiny stdlib loader — no
  `python-dotenv`), so it runs from the VS Code ▶ Run button or any terminal
  without `export`. Real environment variables override `.env`; values are
  whitespace-trimmed.

## Cloud sync & the shared map (Firebase)
- **Hosting:** GitHub Pages serves the static shell (`coverage_map.html`). The
  shell holds *no* prospect data — that lives in Firebase.
- **Store:** one Firebase Realtime Database is the source of truth for all dynamic
  data. Paths: `/coverage` + `/leads` (written by the script); `/state` +
  `/manual` + `/geo` (written by the map — your edits/manual pins/geocode cache).
- **Auth:** a single email/password user; DB rules lock read+write to that one
  UID. That is what keeps a public Pages URL private.
- **Script → DB:** `push_to_firebase()` signs in over the Identity Toolkit REST
  API, then PUTs `/coverage` (overwrite) and PATCHes `/leads` (merge). REST only —
  no Firebase SDK.
- **Map ↔ DB:** the browser uses the Firebase JS SDK (compat build, from the
  gstatic CDN) to read everything and write your edits live; devices stay in sync
  via listeners.

## Design decisions — DO NOT regress these
These were deliberate. Reversing them re-introduces bugs or cost blowups:

- **Text Search, not Nearby Search.** Nearby Search (New) only accepts a circle.
  We use Text Search with a **rectangle** `locationRestriction` because rectangles
  tile an area with no gaps and no overlap.
- **Website + phone come straight from the Text Search response.** Never add a
  separate Place Details call per business — that turns a ~3-request cell into
  60+ requests and blows the free tier.
- **Shrink-only on the 60-cap.** When a cell returns 60 results it subdivides into
  4 quadrants. Do **not** add a "grow when under 60" rule — it breaks the clean
  tiling and makes the coverage map overlap and leave gaps.
- **Square cells use a cos(latitude) correction.** A square in *degrees* is a
  rectangle on the *ground* at Montreal's latitude; `make_grid` corrects for this.
  Keep the correction.
- **Minimal field mask.** Request only the fields actually used. Extra fields can
  bump the request into a higher (pricier) billing SKU.
- **Dedup by `place_id`.** Businesses are deduped by id within a run. Preserve this
  when adding features.
- **Leads sync is a MERGE; coverage is an OVERWRITE.** The script PATCHes `/leads`
  (upsert) and PUTs the full cumulative `/coverage`. Never switch `/leads` to
  overwrite (it would drop other runs' leads); never partially-merge `/coverage`.
  Your on-map edits live in `/state` + `/manual`, which the script never touches —
  so re-runs never clobber notes/colours/deletions. Keep that separation.
- **Prospect data never enters the repo.** The data files are gitignored; the
  public repo/Pages holds only code.
- **The Firebase web config is public by design.** The `apiKey`/`databaseURL` in
  `coverage_map.html` are not secrets — security is Auth + DB rules. Don't try to
  hide them; never commit `FIREBASE_PASSWORD` (it lives only in `.env`).
- **`requests`-only for Firebase too.** The Python side uses plain REST (sign-in +
  RTDB). Don't add `firebase-admin` or other SDKs.
- **The map stays a single static HTML file, no build step.** Leaflet + Firebase
  load from CDNs so GitHub Pages can serve it directly.
- **RTDB returns arrays as keyed objects** — the map normalizes `/coverage`
  features back to an array on read. Keep that when touching coverage rendering.

## Cost & API limits to respect
- `websiteUri` + phone put every request in the **Enterprise SKU tier**. Google's
  free tier covers **1,000 Enterprise requests per month**. One search page = 1
  request; a full 60-result cell = 3. (New Cloud accounts also get a $300 trial
  credit.)
- `EVENT_BUDGET` (default 900) hard-stops the run before the free cap. Don't remove
  or raise it without understanding the cost implications.
- Hard limits set by Google, not us: **60 results max per search** (20/page × 3
  pages) and a **50 km radius cap**.

## Workflow — sweeping a neighbourhood across several boxes
- One run = one box = one `RUN_NAME` = one CSV. Re-using a `RUN_NAME` **overwrites**
  that CSV, so give each box a fresh name (`ndg_01`, `ndg_02`, …).
- To place the next box with no gap or overlap, reuse the finished box's edge:
  moving east, new `WEST` = old `EAST`; moving north, new `SOUTH` = old `NORTH`.
- `coverage_map.html` accumulates across runs (colour-coded per run). Merge the
  per-run CSVs at the end of a neighbourhood.
- Alternative: draw one larger box and let the 2 km grid tile it in a single run.

## Gotchas
- `coverage_map.html` loads Leaflet and map tiles from the web — open it **online**.
  The CSV and GeoJSON are fully offline.
- A missing `websiteUri` means no site **on the Google listing**
- The map also needs internet for Firebase; the CSV and GeoJSON are still offline.
- **Deploy = commit + push `coverage_map.html` to `master`**; GitHub Pages
  redeploys the shell. Data files are not part of the deploy.
- Phones cache the Pages shell aggressively — pull-to-refresh after a deploy.
- Pin colours carry meaning: **green = not yet visited, yellow = visited,
  red = attention, purple = already a client** (shown in the Info legend).
  `COLOR_CYCLE` / `COLOR_HEX` / `COLOR_MEANING` must stay in sync.
- The map's `setStatus('Loading…')` must be cleared on every load path, not only
  as a geocoding side effect (this bit us once).

## If extending this tool
- Keep it dependency-light: standard library plus `requests`. No self-hosted
  backend, no heavy frameworks, no build step — Firebase is a managed BaaS reached
  over plain REST (Python) and a CDN SDK (the map).
- Preserve the invariants above (tiling, dedup, budget guard, minimal field mask).
- Likely future asks: curated/expanded business-type lists, an Excel output with
  clickable Maps links, or auto-generating a bounding box from a neighbourhood name.

## Note on data use
Pulling business name, address, and phone into a private outreach list is normal use
of the official API. Treat the output as a working prospect list — not a database to
redistribute or resell.

Because the map is reachable at a public Pages URL and the database URL is visible
in the shell, the **Firebase account password is the real gate** on your prospect
list — keep it strong. The data stays private (behind login); still not a database
to redistribute or resell.
