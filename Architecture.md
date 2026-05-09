# Architecture

This project turns Melbourne public transport timetable data and map data into an interactive web map. In plain English: it builds a small "model" of Melbourne, its suburbs, its public transport stops, and the travel times between them. The website then uses that model to answer questions like:

- If I start here, how long does it take to get elsewhere?
- Which parts of Melbourne are reachable within about an hour?
- What would the city look like if easy-to-reach places visually expanded and hard-to-reach places compressed?

The project has two main parts:

1. A Python data pipeline that prepares map and transit data.
2. A browser app that loads that prepared data and draws the interactive map.

## Big Picture

```text
Raw data in data/
  |
  | Python scripts read boundaries, parks, roads, and PTV GTFS timetables
  v
Generated JSON in site/data/
  |
  | Browser loads compact render data and compute data
  v
Interactive website in site/
  |
  | User pins an origin
  v
Web Worker calculates travel times without freezing the page
  |
  v
Canvas draws the basemap, heatmap, warp, labels, and pins
```

## The Input Data

The `data/` folder contains the source material:

- `melbourne_lga_boundaries.geojson` has the shapes of the 31 Greater/Metropolitan Melbourne local government areas.
- `melbourne_parks_osm.json` has large parks and open spaces from OpenStreetMap.
- `melbourne_major_streets.json` has major roads from OpenStreetMap.
- `ptv_metro_trains.zip`, `ptv_trams.zip`, and `ptv_metro_buses.zip` are GTFS timetable files from PTV.

GTFS is a standard public transport data format. It contains stops, routes, trips, shapes, and stop times. This project uses it as a static timetable snapshot. The app does not ask PTV for live delays or cancellations.

## The Data Pipeline

The main pipeline is:

```bash
python3 pipeline/build_melbourne_commute_data.py
```

That script builds the files the website needs:

- `site/data/commute_map_data.json`
- `site/data/map_render.json`
- `site/data/map_compute.json`

The split matters because the browser uses the data for two different jobs.

- `map_render.json` contains things the app needs to draw: LGA boundaries, parks, roads, transit route shapes, station names, and route colors.
- `map_compute.json` contains things the app needs to calculate travel times: route states, station states, wait times, graph edges, grid cells, and the land mask.
- `commute_map_data.json` contains both together as the full bundle.

## What The Pipeline Builds

### 1. Melbourne as a flat working map

Latitude and longitude are awkward for distance math, so the pipeline converts coordinates into simple x/y metres. This makes "how far apart are these two stops?" a straightforward distance calculation.

The pipeline also simplifies detailed shapes. For example, a complex suburb boundary or road line can have many points. The app does not need every tiny bend, so the script keeps enough detail to look good while making the JSON smaller and faster to draw.

### 2. The visible basemap

The pipeline extracts:

- LGA polygons and label points.
- Park polygons above a minimum size.
- Major street lines.
- Public transport route shapes and colors.
- Station and stop locations.

This is the visual skeleton of the map.

The generated data contains the full Greater Melbourne council set. The browser defaults to the inner Melbourne council view and hides the outer Greater Melbourne-only LGAs:

- Cardinia
- Mornington Peninsula
- Yarra Ranges

Users can turn on the Greater Melbourne toggle to expand the visible map bounds and council polygons to the full 31-LGA dataset without loading a different data bundle.

### 3. The transit network

The key idea is that the pipeline turns the public transport timetable into a graph.

A graph is just a network of points and connections:

- Points are station/route combinations.
- Connections are ride segments, transfers, or short walking links between nearby stops.
- Each connection has a cost in minutes.

For example, "being at Richmond on the Cranbourne line" and "being at Richmond on the Sandringham line" are separate route states, because transferring between lines costs time.

The pipeline calculates:

- Ride times from median stop-to-stop times in GTFS.
- Expected waiting time from route headways.
- Transfer time between routes at the same station.
- Walking transfer time between nearby stop complexes.
- A filter that drops very infrequent bus routes so the network is useful and not overwhelmed by rare services.

### 4. A 160 x 160 grid over Melbourne

The app does not calculate travel time for every possible pixel. Instead, the pipeline lays a grid over Melbourne.

For each land cell, it stores:

- The cell's map position.
- Its row and column.
- The nearest few stations/stops.
- The walking time from the cell to those stops.

Later, when the user pins an origin, the browser can quickly estimate travel time to every cell by combining:

```text
origin -> nearby stop -> transit network -> nearby destination stop -> destination cell
```

## Runtime In The Browser

The website lives in `site/`.

Important files:

- `site/index.html` defines the page structure, controls, metadata, and canvas.
- `site/styles.css` controls the layout and visual design.
- `site/app.js` runs the main app: loading data, handling pointer input, drawing the map, search, sharing, zoom, settings, and UI state.
- `site/compute-worker.js` performs expensive travel-time and warp calculations away from the main UI thread.

The app draws to a `<canvas>`, not to SVG or map tiles. That means the project owns the whole map-rendering process: polygons, streets, transit lines, heatmaps, labels, pins, and warped geometry are all drawn by JavaScript.

`app.js` keeps the region selection as UI state. The active region controls:

- Which LGA polygons and labels are drawn.
- Which map bounds are used to fit the canvas.
- Which points are accepted for direct map interaction, search results, and geolocation.
- Whether shared links include the expanded Greater Melbourne view.

## What Happens When The Page Loads

1. `index.html` sets the asset base path so the app works locally and when deployed under a subpath.
2. `app.js` loads the generated data files from `site/data/`.
3. The app prepares the canvas, map transform, UI controls, and default travel settings.
4. A Web Worker is created from `compute-worker.js`.
5. The worker receives the compute data and builds helper indexes for fast station lookup.
6. The app draws the unwarped basemap while waiting for user input.

By default, the initial basemap is framed to the inner 28-LGA Melbourne view. If a shared URL includes the Greater Melbourne flag, or the user enables the Greater Melbourne toggle, the app reframes to the full 31-LGA bounds.

## What Happens When A User Pins An Origin

When the user clicks, taps, drags, searches an address, or uses their current location:

1. The app converts that screen position or lat/lon into the internal x/y map coordinate.
2. The app sends the origin to the Web Worker.
3. The worker finds the nearest origin stops.
4. The worker runs Dijkstra's shortest-path algorithm across the transit graph.
5. The worker calculates the best travel time to each grid cell.
6. The worker builds a warp grid where easier-to-reach cells expand and slower cells shrink back.
7. The worker sends the result back to `app.js`.
8. `app.js` redraws the canvas with the heatmap, optional reachability outline, and optional warped map.

Dijkstra is the "find the shortest path through a network" part. Here, "shortest" means lowest travel time in minutes.

## Heatmap And Warp

The heatmap colors each grid cell by travel time from the pinned origin:

- Warm colors mean closer/faster.
- Cooler colors mean farther/slower.

The warp is a second visual layer. It changes the map's shape so areas with shorter travel times take up more visual space. It does not move the real stations in the data permanently; it is a drawing transform applied at runtime.

To keep the warp readable, the worker smooths the travel-time grid and limits how far any grid node can move. That avoids extreme distortions while still making the commute-time pattern obvious.

## Search, Location, And Sharing

Address search happens at runtime in the browser using OpenStreetMap Nominatim. That is why address search needs internet access even though the map data itself is prebuilt.

The app can also use browser geolocation if the user allows it.

Sharing works by encoding the view into the URL. A shared link can include:

- The pinned origin.
- A measured destination point.
- Zoom.
- Whether warp, heatmap, or outline layers are on.
- Whether the Greater Melbourne view is enabled.

Vercel rewrites in `vercel.json` make deep links like `/@-37.81,144.96` load the app correctly.

## Static SVG Cartogram

There is also a separate static output:

```bash
python3 pipeline/generate_melbourne_projection.py
```

This creates:

```text
output/melbourne_ptv_weighted_projection.svg
```

That script is related, but separate from the interactive site. It builds a side-by-side SVG comparison: normal Melbourne geography on one side and transit-access-weighted geography on the other. It uses proximity to train and tram access rather than the full interactive commute-time graph.

## Validation

After regenerating data, run:

```bash
python3 pipeline/validate_site_data.py
```

This checks that the generated JSON files still match what the browser expects. It catches problems like:

- Missing required keys.
- Invalid graph references.
- Bad grid masks.
- Non-finite numbers.
- Mismatches between the full data bundle and the split render/compute bundles.

## Deployment

The site is a static app deployed through Vercel.

`vercel.json` tells Vercel:

- Use `site/` as the output directory.
- Rewrite all routes to the app so deep links survive page refresh.

There is no backend server for route calculation. The heavy work happens either before deployment in the Python pipeline or inside the user's browser in the Web Worker.

## Mental Model

Think of the project like a kitchen:

- `data/` is the raw ingredients.
- `pipeline/build_melbourne_commute_data.py` is the prep cook.
- `site/data/` is the prepared mise en place.
- `site/app.js` is the person plating and serving the dish.
- `site/compute-worker.js` is the assistant doing the hard calculations in the background.
- `pipeline/validate_site_data.py` is the quality check before service.

The reason the app feels interactive is that almost all slow, messy work is done ahead of time. By the time someone opens the website, the browser already has a compact model of Melbourne and only needs to answer one question quickly: "from this chosen point, what is the fastest way to everywhere else?"
