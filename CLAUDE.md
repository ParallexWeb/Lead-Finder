# Claude Code Context — Lead Finder (businesses with no website)

## What this project is
A small Python tool that finds local small businesses with **no website listed on
Google Maps**, to use as sales prospects. It queries the Google Places API (New),
keeps the businesses with no website, and outputs a CSV call-list plus a visual
coverage map of the areas already searched. It feeds the sales pipeline for the
web-agency project (where the actual client sites are built — separate repo).

Everything lives in one script: `sweep_leads.py`. The script is heavily commented;
**this file is the project-level intent and the guardrails, not a restatement of
the code.**

## Running it
- Requires `GOOGLE_MAPS_API_KEY` in the environment — a Google Cloud key with
  **Places API (New)** enabled and billing turned on.
- `pip install requests`, then `python sweep_leads.py`.
- The only thing edited per run is the CONFIG block at the top of the script:
  `RUN_NAME`, the four corner coordinates, and optionally `CELL_SIZE_KM`.
- Outputs land in the working directory: `{RUN_NAME}_leads.csv`,
  `coverage.geojson`, and `coverage_map.html`.

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

## If extending this tool
- Keep it dependency-light: standard library plus `requests`. No backend, no heavy
  frameworks.
- Preserve the invariants above (tiling, dedup, budget guard, minimal field mask).
- Likely future asks: curated/expanded business-type lists, an Excel output with
  clickable Maps links, or auto-generating a bounding box from a neighbourhood name.

## Note on data use
Pulling business name, address, and phone into a private outreach list is normal use
of the official API. Treat the output as a working prospect list — not a database to
redistribute or resell.
