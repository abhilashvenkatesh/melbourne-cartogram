# Melbourne Cartogram

**Live site: [melbtransit.avbuilds.dev](https://melbtransit.avbuilds.dev/)**

![Melbourne Cartogram](site/assets/site-pic.png)

Two artifacts for Melbourne:

- A static SVG cartogram that expands areas with strong PTV access and compresses areas with weak access
- An interactive commute-time web app: pin an origin, inspect travel times across the network, toggle warp and heatmap layers, share deep links

## What it does

### Static SVG (`pipeline/generate_melbourne_projection.py`)

Produces a side-by-side SVG — reference geography on the left, transit-access warped geography on the right. Each grid cell is weighted by walking distance to the nearest train, tram, or bus stop. Cells close to stops expand; cells far from stops compress. Train and tram route shapes are drawn on top.

Output: `output/melbourne_ptv_weighted_projection.svg`

### Interactive site (`pipeline/build_melbourne_commute_data.py` + `site/`)

Builds a compact JSON data bundle consumed by the browser app. The app runs a Dijkstra shortest-path search from a pinned origin across a graph of stops, computing travel times to every grid cell in Melbourne. It renders a heatmap and optionally warps the map to show commute-time distortion.

Output: `site/data/commute_map_data.json`

## Data sources

| File | What it is | Where to get it |
| --- | --- | --- |
| `data/melbourne_lga_boundaries.geojson` | LGA boundary polygons | OpenStreetMap or [data.gov.au](https://data.gov.au) |
| `data/melbourne_parks_osm.json` | Park and open-space polygons | OpenStreetMap Overpass API |
| `data/melbourne_major_streets.json` | Motorway, trunk, and primary roads | OpenStreetMap Overpass API |
| `data/ptv_metro_trains.zip` | PTV GTFS — metropolitan trains | [PTV Developer Portal](https://www.ptv.vic.gov.au/footer/data-and-reporting/datasets/ptv-timetable-api/) |
| `data/ptv_trams.zip` | PTV GTFS — trams | PTV Developer Portal |
| `data/ptv_metro_buses.zip` | PTV GTFS — metropolitan buses | PTV Developer Portal |
| `data/ptv_gtfs.zip` | Full PTV GTFS bundle, used for V/Line regional trains | PTV Developer Portal |

All GTFS files are a static snapshot. The app does not use real-time feeds.

> **Note:** GTFS zip snapshots can be large and may not all be present in a fresh clone. Download `ptv_metro_trains.zip`, `ptv_trams.zip`, `ptv_metro_buses.zip`, and the full `ptv_gtfs.zip` bundle from the [PTV Developer Portal](https://www.ptv.vic.gov.au/footer/data-and-reporting/datasets/ptv-timetable-api/) and place them in the `data/` folder before running the pipeline.

## How the travel-time model works

1. **Stops** — metro and V/Line train stops are grouped by parent station; tram and bus stops are individual nodes. Only bus routes with ≥ 100 scheduled trips are included (filters very infrequent services).

2. **Graph edges** — on-vehicle travel times come from median stop-to-stop durations across all GTFS trips. Same-station transfers add a 4-minute penalty plus half the target route's headway. Nearby stops within 260 m of each other get a walk-transfer edge (2 min penalty + inter-complex 7 min penalty + target headway).

3. **Wait times** — each route's expected wait is half its median headway, clamped to [1.5, 8] minutes.

4. **Walking** — access to/from stops modelled at 75 m/min with a 3.5-minute boarding penalty. Between stops the app uses 80 m/min.

5. **Grid** — Melbourne is divided into a 160 × 160 cell grid. Each cell stores its 4 nearest stops and walk times to them. At query time the browser runs Dijkstra from the pinned origin through the stop graph, then adds the last-mile walk to each cell.

## Key parameters

| Parameter | Value |
| --- | --- |
| Grid size | 160 × 160 |
| Walk speed (between stops) | 80 m/min |
| Walk speed (access to stop) | 75 m/min |
| Station access penalty | 3.5 min |
| Transfer penalty | 4 min |
| Inter-complex transfer penalty | 7 min |
| Default board wait | 4 min |
| Min bus trips to include route | 100 |
| SVG warp decay radius | 600 m |

## Requirements

- Python 3 (standard library only — no pip install needed)
- Node.js and npm for local preview

## Generate the static SVG

```bash
python3 pipeline/generate_melbourne_projection.py
```

Output: `output/melbourne_ptv_weighted_projection.svg`

## Build the interactive site data

```bash
python3 pipeline/build_melbourne_commute_data.py
```

Output: `site/data/commute_map_data.json`

## Validate generated site data

```bash
python3 pipeline/validate_site_data.py
```

Checks the generated JSON contracts used by the browser app, including split render/compute bundles, graph references, grid masks, station access links, and non-finite numeric values.

## Local preview

```bash
npm install
npm run dev
```

Then open `http://localhost:3000`

Notes:

- Address search uses OpenStreetMap Nominatim at runtime — needs internet access.
- Deep-link URLs like `/mel/@-37.81,144.96` work with `npm run dev` but not with plain `python3 -m http.server`.

## Deploy

Deployed via Vercel. Push to `main` to trigger a deploy. `vercel.json` sets `site/` as the output directory and adds a catch-all rewrite so deep-link URLs survive page reload.

## Project layout

| Path | What it does |
| --- | --- |
| [pipeline/generate_melbourne_projection.py](pipeline/generate_melbourne_projection.py) | Builds the static SVG cartogram |
| [pipeline/build_melbourne_commute_data.py](pipeline/build_melbourne_commute_data.py) | Builds the interactive site data bundle |
| [pipeline/validate_site_data.py](pipeline/validate_site_data.py) | Validates generated JSON data contracts |
| [site/index.html](site/index.html) | App shell and metadata |
| [site/app.js](site/app.js) | Interactive map, search, warp, heatmap, sharing |
| [site/compute-worker.js](site/compute-worker.js) | Web Worker running Dijkstra off the main thread |
| [site/styles.css](site/styles.css) | Site styles |
| [site/data/commute_map_data.json](site/data/commute_map_data.json) | Generated site dataset |
| [vercel.json](vercel.json) | Vercel deploy config |

## App features

- Hover or tap to preview travel times from any point
- Pin an origin and inspect commute times across Melbourne
- Toggle warp layer (map distorted by commute time) and heatmap layer
- Zoom and full-screen the map
- Search for Melbourne addresses
- Browser geolocation support
- Export and share views, including deep links
- 60-minute reachability score for any pinned origin

## Limitations

- **Static schedule** — GTFS is a snapshot. No real-time delays, cancellations, or timetable updates.
- **V/Line scope** — V/Line regional train routes are included from the full PTV GTFS bundle. Regional coaches and regional town buses are still excluded.
- **Bus frequency filter** — bus routes with fewer than 100 trips in the GTFS snapshot are excluded. Very infrequent or school-only routes may be missing.
- **Simplified walk model** — flat walking speed with no account for hills, barriers (freeways, rivers), or pedestrian path availability.
- **No fare zones** — travel times do not reflect myki zone costs or journey planning constraints.
- **Headway as proxy for wait** — the model uses half-headway averaged across all service patterns. Off-peak or weekend frequencies are not modelled separately.
- **No interchange accuracy** — cross-platform transfers at major hubs (e.g. Flinders Street, Southern Cross) use a fixed penalty rather than surveyed interchange times.
- **Tram stops are dense** — tram stops are modelled individually, which increases graph size significantly and may cause some routing shortcuts that differ from real journey times.
