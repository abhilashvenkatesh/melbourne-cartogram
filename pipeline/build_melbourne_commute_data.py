#!/usr/bin/env python3
"""Build compact data assets for the Melbourne interactive commute-time website."""

from __future__ import annotations

import csv
import json
import math
import statistics
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SITE_DATA_PATH = ROOT / "site" / "data" / "commute_map_data.json"
SITE_RENDER_PATH = ROOT / "site" / "data" / "map_render.json"
SITE_COMPUTE_PATH = ROOT / "site" / "data" / "map_compute.json"

BOROUGHS_PATH = DATA_DIR / "melbourne_lga_boundaries.geojson"
PARKS_PATH = DATA_DIR / "melbourne_parks_osm.json"
STREETS_PATH = DATA_DIR / "melbourne_major_streets.json"
TRAINS_GTFS = DATA_DIR / "ptv_metro_trains.zip"
TRAMS_GTFS = DATA_DIR / "ptv_trams.zip"
BUSES_GTFS = DATA_DIR / "ptv_metro_buses.zip"
PTV_GTFS = DATA_DIR / "ptv_gtfs.zip"
VLINE_TRAINS_GTFS = (PTV_GTFS, "1/google_transit.zip")

GRID_COLS = 160
GRID_ROWS = 160
MIN_PARK_AREA = 70_000.0
WALK_METERS_PER_MINUTE = 80.0
ACCESS_WALK_METERS_PER_MINUTE = 75.0
STATION_ACCESS_PENALTY = 3.5
CELL_NEAREST_STATIONS = 4
ORIGIN_NEAREST_STATIONS = 5
MAX_SHAPES_PER_ROUTE_DIRECTION = 2
INTER_COMPLEX_WALK_RADIUS = 260.0
INTER_COMPLEX_WALK_PENALTY = 2.0
DEFAULT_BOARD_WAIT = 4.0
TRANSFER_PENALTY = 4.0
INTER_COMPLEX_TRANSFER_PENALTY = 7.0

# PTV route types
TRAIN_ROUTE_TYPES = {"400"}
VLINE_TRAIN_ROUTE_TYPES = {"2"}
TRAM_ROUTE_TYPES = {"0"}
BUS_ROUTE_TYPES = {"3", "701"}
ALL_ROUTE_TYPES = TRAIN_ROUTE_TYPES | VLINE_TRAIN_ROUTE_TYPES | TRAM_ROUTE_TYPES | BUS_ROUTE_TYPES

# Only include bus routes with enough trips to be worth routing through
MIN_BUS_TRIPS_FOR_ROUTING = 100

Point = Tuple[float, float]
Ring = List[Point]
Polygon = List[Ring]
MultiPolygon = List[Polygon]


def round_point(point: Point) -> List[float]:
    return [round(point[0], 1), round(point[1], 1)]


def round_path(points: Sequence[Point]) -> List[List[float]]:
    return [round_point(point) for point in points]


def load_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def lonlat_to_xy(lon: float, lat: float, lat0: float) -> Point:
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = meters_per_deg_lat * math.cos(math.radians(lat0))
    return lon * meters_per_deg_lon, lat * meters_per_deg_lat


def average_feature_latitude(payload: dict) -> float:
    total = 0.0
    count = 0
    for feature in payload["features"]:
        geometry = feature["geometry"]
        geom_type = geometry["type"]
        if geom_type == "MultiPolygon":
            for polygon in geometry["coordinates"]:
                for ring in polygon:
                    for _, lat in ring:
                        total += lat
                        count += 1
        elif geom_type == "Polygon":
            for ring in geometry["coordinates"]:
                for _, lat in ring:
                    total += lat
                    count += 1
    return total / max(count, 1)


def ring_area(ring: Sequence[Point]) -> float:
    area = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def polygon_centroid(ring: Sequence[Point]) -> Point:
    area = ring_area(ring) or 1.0
    factor = 1.0 / (6.0 * area)
    cx = 0.0
    cy = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return cx * factor, cy * factor


def simplify_polyline(points: Sequence[Point], min_distance: float) -> List[Point]:
    if len(points) <= 2:
        return list(points)
    simplified = [points[0]]
    for point in points[1:-1]:
        if math.hypot(point[0] - simplified[-1][0], point[1] - simplified[-1][1]) >= min_distance:
            simplified.append(point)
    if points[-1] != simplified[-1]:
        simplified.append(points[-1])
    return simplified


def simplify_ring(ring: Sequence[Point], min_distance: float) -> Ring:
    if len(ring) <= 4:
        return list(ring)
    core = list(ring[:-1]) if ring[0] == ring[-1] else list(ring)
    simplified = [core[0]]
    for point in core[1:]:
        if math.hypot(point[0] - simplified[-1][0], point[1] - simplified[-1][1]) >= min_distance:
            simplified.append(point)
    if len(simplified) < 3:
        simplified = core[:3]
    simplified.append(simplified[0])
    return simplified


def bounds_of_ring(ring: Sequence[Point]) -> Tuple[float, float, float, float]:
    xs = [x for x, _ in ring]
    ys = [y for _, y in ring]
    return min(xs), min(ys), max(xs), max(ys)


def bounds_of_multipolygon(multipolygon: MultiPolygon) -> Tuple[float, float, float, float]:
    xs = [x for polygon in multipolygon for ring in polygon for x, _ in ring]
    ys = [y for polygon in multipolygon for ring in polygon for _, y in ring]
    return min(xs), min(ys), max(xs), max(ys)


def bounds_of_points(points: Sequence[Point]) -> Tuple[float, float, float, float]:
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_intersects(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def point_in_ring(point: Point, ring: Sequence[Point]) -> bool:
    x, y = point
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        intersects = (yi > y) != (yj > y)
        if intersects:
            x_hit = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
            if x < x_hit:
                inside = not inside
        j = i
    return inside


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    if not polygon:
        return False
    if not point_in_ring(point, polygon[0]):
        return False
    for hole in polygon[1:]:
        if point_in_ring(point, hole):
            return False
    return True


def point_in_multipolygon(point: Point, multipolygon: MultiPolygon) -> bool:
    return any(point_in_polygon(point, polygon) for polygon in multipolygon)


def assemble_osm_ways_into_rings(way_coords_list: List[List[Point]]) -> List[Ring]:
    if not way_coords_list:
        return []
    ways = [list(w) for w in way_coords_list]
    rings = []
    while ways:
        ring = ways.pop(0)
        changed = True
        while changed:
            changed = False
            if ring[0] == ring[-1]:
                break
            for i, way in enumerate(ways):
                if ring[-1] == way[0]:
                    ring = ring + way[1:]
                    ways.pop(i)
                    changed = True
                    break
                elif ring[-1] == way[-1]:
                    ring = ring + list(reversed(way))[1:]
                    ways.pop(i)
                    changed = True
                    break
                elif ring[0] == way[-1]:
                    ring = way + ring[1:]
                    ways.pop(i)
                    changed = True
                    break
                elif ring[0] == way[0]:
                    ring = list(reversed(way)) + ring[1:]
                    ways.pop(i)
                    changed = True
                    break
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        if len(ring) >= 4:
            rings.append(ring)
    return rings


def extract_boroughs(payload: dict, lat0: float) -> Tuple[list, MultiPolygon]:
    boroughs = []
    all_polygons: MultiPolygon = []
    for feature in payload["features"]:
        geometry = feature["geometry"]
        geom_type = geometry["type"]
        multipolygon: MultiPolygon = []
        if geom_type == "MultiPolygon":
            coords_list = geometry["coordinates"]
        elif geom_type == "Polygon":
            coords_list = [geometry["coordinates"]]
        else:
            continue
        for polygon_coords in coords_list:
            polygon: Polygon = []
            for ring_coords in polygon_coords:
                ring = [lonlat_to_xy(lon, lat, lat0) for lon, lat in ring_coords]
                polygon.append(simplify_ring(ring, 120.0))
            multipolygon.append(polygon)
            all_polygons.append(polygon)
        if not multipolygon:
            continue
        largest_polygon = max(multipolygon, key=lambda p: abs(ring_area(p[0])))
        raw_name = feature["properties"].get("name", "")
        name = raw_name.replace("City of ", "").replace("Shire of ", "").strip()
        boroughs.append(
            {
                "name": name,
                "label": round_point(polygon_centroid(largest_polygon[0])),
                "polygons": [[round_path(ring) for ring in polygon] for polygon in multipolygon],
            }
        )
    return boroughs, all_polygons


def extract_parks(lat0: float, bbox: Tuple[float, float, float, float]) -> list:
    if not PARKS_PATH.exists():
        return []
    payload = load_json(PARKS_PATH)
    parks = []

    for element in payload.get("elements", []):
        etype = element.get("type")
        if etype == "way":
            geom = element.get("geometry", [])
            if len(geom) < 4:
                continue
            lonlat = [(n["lon"], n["lat"]) for n in geom]
            ring = [lonlat_to_xy(lon, lat, lat0) for lon, lat in lonlat]
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            area = abs(ring_area(ring))
            if area < MIN_PARK_AREA:
                continue
            simplified = simplify_ring(ring, 90.0)
            polygon = [simplified]
            if bbox_intersects(bounds_of_ring(simplified), bbox):
                parks.append([round_path(ring) for ring in polygon])

        elif etype == "relation":
            outer_ways: List[List[Point]] = []
            for member in element.get("members", []):
                if member.get("type") != "way" or member.get("role") != "outer":
                    continue
                geom = member.get("geometry", [])
                if not geom:
                    continue
                coords = [(round(n["lon"], 7), round(n["lat"], 7)) for n in geom]
                outer_ways.append(coords)
            if not outer_ways:
                continue
            for ring_lonlat in assemble_osm_ways_into_rings(outer_ways):
                ring = [lonlat_to_xy(lon, lat, lat0) for lon, lat in ring_lonlat]
                area = abs(ring_area(ring))
                if area < MIN_PARK_AREA:
                    continue
                simplified = simplify_ring(ring, 90.0)
                if bbox_intersects(bounds_of_ring(simplified), bbox):
                    parks.append([[round_point(p) for p in simplified]])

    return parks


def extract_streets(lat0: float, bbox: Tuple[float, float, float, float]) -> list:
    if not STREETS_PATH.exists():
        return []
    payload = load_json(STREETS_PATH)
    allowed = {"motorway", "trunk", "primary"}
    streets = []
    for element in payload.get("elements", []):
        if element.get("type") != "way":
            continue
        tags = element.get("tags", {})
        kind = tags.get("highway")
        if kind not in allowed or "geometry" not in element or "name" not in tags:
            continue
        points = [lonlat_to_xy(node["lon"], node["lat"], lat0) for node in element["geometry"]]
        if len(points) < 2:
            continue
        simplified = simplify_polyline(points, 220.0)
        if len(simplified) < 2 or not bbox_intersects(bounds_of_points(simplified), bbox):
            continue
        streets.append({"kind": kind, "name": tags["name"], "points": round_path(simplified)})
    return streets


def read_csv_from_zip(gtfs_source, member: str) -> Iterable[dict]:
    if isinstance(gtfs_source, tuple):
        outer_path, inner_path = gtfs_source
        with zipfile.ZipFile(outer_path) as outer_archive:
            with outer_archive.open(inner_path) as inner_handle:
                with zipfile.ZipFile(inner_handle) as archive:
                    with archive.open(member) as handle:
                        reader = csv.DictReader(line.decode("utf-8-sig") for line in handle)
                        yield from reader
        return

    with zipfile.ZipFile(gtfs_source) as archive:
        with archive.open(member) as handle:
            reader = csv.DictReader(line.decode("utf-8-sig") for line in handle)
            yield from reader


def parse_gtfs_time(value: str) -> int:
    hours, minutes, seconds = map(int, value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def find_frequent_bus_routes() -> set:
    """Return set of bus route_ids with enough trips to include in routing graph."""
    trip_counts: Counter = Counter()
    for row in read_csv_from_zip(BUSES_GTFS, "trips.txt"):
        trip_counts[row["route_id"]] += 1
    return {route_id for route_id, count in trip_counts.items() if count >= MIN_BUS_TRIPS_FOR_ROUTING}


def build_station_data(lat0: float) -> Tuple[list, Dict[str, int], Dict[str, str]]:
    """Build station list from all PTV GTFS feeds.

    Trains: grouped by parent_station (location_type==1).
    Trams/buses: each individual stop is its own station.
    Returns (stations, station_index_by_id, stop_to_station_id).
    """
    stations: list = []
    station_index_by_id: Dict[str, int] = {}
    stop_to_complex: Dict[str, str] = {}

    def add_grouped_train_stops(gtfs_source, fallback_prefix: str) -> None:
        train_parents: Dict[str, dict] = {}
        train_stop_to_parent: Dict[str, str] = {}

        for row in read_csv_from_zip(gtfs_source, "stops.txt"):
            loc_type = row.get("location_type", "")
            stop_id = row["stop_id"]
            if loc_type == "1":
                name = row["stop_name"].replace(" Railway Station", "").replace(" Station", "").strip()
                train_parents[stop_id] = {
                    "id": stop_id,
                    "name": name,
                    "point": lonlat_to_xy(float(row["stop_lon"]), float(row["stop_lat"]), lat0),
                    "routes": set(),
                }
            elif row.get("parent_station"):
                train_stop_to_parent[stop_id] = row["parent_station"]
            elif loc_type not in ("2", "3"):
                parent_id = f"{fallback_prefix}:{stop_id}"
                name = row["stop_name"].replace(" Railway Station", "").replace(" Station", "").strip()
                train_parents[parent_id] = {
                    "id": parent_id,
                    "name": name,
                    "point": lonlat_to_xy(float(row["stop_lon"]), float(row["stop_lat"]), lat0),
                    "routes": set(),
                }
                train_stop_to_parent[stop_id] = parent_id

        for parent_id, info in train_parents.items():
            if parent_id in station_index_by_id:
                continue
            idx = len(stations)
            station_index_by_id[parent_id] = idx
            stop_to_complex[parent_id] = parent_id
            stations.append(info)

        for stop_id, parent_id in train_stop_to_parent.items():
            if parent_id in station_index_by_id:
                stop_to_complex[stop_id] = parent_id

    # --- Metro and V/Line trains: group stops by parent_station ---
    add_grouped_train_stops(TRAINS_GTFS, "metro-train")
    add_grouped_train_stops(VLINE_TRAINS_GTFS, "vline-train")

    # --- Trams: each stop is its own station ---
    for row in read_csv_from_zip(TRAMS_GTFS, "stops.txt"):
        if row.get("location_type") in ("1", "2", "3"):
            continue
        stop_id = row["stop_id"]
        if stop_id in stop_to_complex:
            continue
        idx = len(stations)
        station_index_by_id[stop_id] = idx
        stop_to_complex[stop_id] = stop_id
        stations.append(
            {
                "id": stop_id,
                "name": row["stop_name"],
                "point": lonlat_to_xy(float(row["stop_lon"]), float(row["stop_lat"]), lat0),
                "routes": set(),
            }
        )

    # --- Metro buses: each stop is its own station ---
    for row in read_csv_from_zip(BUSES_GTFS, "stops.txt"):
        if row.get("location_type") in ("1", "2", "3"):
            continue
        stop_id = row["stop_id"]
        if stop_id in stop_to_complex:
            continue
        idx = len(stations)
        station_index_by_id[stop_id] = idx
        stop_to_complex[stop_id] = stop_id
        stations.append(
            {
                "id": stop_id,
                "name": row.get("stop_name", ""),
                "point": lonlat_to_xy(float(row["stop_lon"]), float(row["stop_lat"]), lat0),
                "routes": set(),
            }
        )

    return stations, station_index_by_id, stop_to_complex


def build_routes_and_shapes(
    lat0: float,
    bbox: Tuple[float, float, float, float],
    frequent_bus_routes: set,
) -> Tuple[dict, list, dict]:
    route_styles: dict = {}
    trips_by_id: dict = {}
    shapes: list = []

    gtfs_configs = [
        (TRAINS_GTFS, TRAIN_ROUTE_TYPES, None),
        (VLINE_TRAINS_GTFS, VLINE_TRAIN_ROUTE_TYPES, None),
        (TRAMS_GTFS, TRAM_ROUTE_TYPES, None),
        (BUSES_GTFS, BUS_ROUTE_TYPES, frequent_bus_routes),
    ]

    for gtfs_path, allowed_types, route_filter in gtfs_configs:
        local_route_styles: dict = {}
        for row in read_csv_from_zip(gtfs_path, "routes.txt"):
            if row.get("route_type") not in allowed_types:
                continue
            if route_filter is not None and row["route_id"] not in route_filter:
                continue
            local_route_styles[row["route_id"]] = {
                "color": f"#{row['route_color'] or '808183'}",
                "textColor": f"#{row['route_text_color'] or 'FFFFFF'}",
                "label": row.get("route_short_name") or row["route_id"],
            }
        route_styles.update(local_route_styles)

        shape_counts: Dict[Tuple[str, str], Counter] = {}
        for row in read_csv_from_zip(gtfs_path, "trips.txt"):
            route_id = row["route_id"]
            if route_id not in local_route_styles:
                continue
            trips_by_id[row["trip_id"]] = {
                "route_id": route_id,
                "direction_id": row.get("direction_id", "0"),
                "service_id": row.get("service_id", ""),
            }
            shape_counts.setdefault((route_id, row.get("direction_id", "0")), Counter())[
                row["shape_id"]
            ] += 1

        selected_shape_ids: dict = {}
        for (route_id, _direction), counter in shape_counts.items():
            for shape_id, _count in counter.most_common(MAX_SHAPES_PER_ROUTE_DIRECTION):
                selected_shape_ids[shape_id] = route_id

        points_by_shape: Dict[str, list] = defaultdict(list)
        for row in read_csv_from_zip(gtfs_path, "shapes.txt"):
            shape_id = row["shape_id"]
            if shape_id not in selected_shape_ids:
                continue
            point = lonlat_to_xy(float(row["shape_pt_lon"]), float(row["shape_pt_lat"]), lat0)
            points_by_shape[shape_id].append((int(row["shape_pt_sequence"]), point))

        for shape_id, route_id in selected_shape_ids.items():
            style = local_route_styles[route_id]
            points = [point for _, point in sorted(points_by_shape.get(shape_id, []))]
            points = simplify_polyline(points, 90.0)
            if len(points) < 2 or not bbox_intersects(bounds_of_points(points), bbox):
                continue
            shapes.append(
                {
                    "routeId": route_id,
                    "color": style["color"],
                    "textColor": style["textColor"],
                    "label": style["label"],
                    "points": round_path(points),
                }
            )

    return route_styles, shapes, trips_by_id


def build_route_waits(trips_by_id: dict) -> Dict[str, float]:
    """Compute half-headway wait times per route from all GTFS feeds."""
    departures_by_route_service: Dict[Tuple[str, str], List[int]] = defaultdict(list)

    for gtfs_path in [TRAINS_GTFS, VLINE_TRAINS_GTFS, TRAMS_GTFS, BUSES_GTFS]:
        current_trip_id = None
        first_departure = None
        for row in read_csv_from_zip(gtfs_path, "stop_times.txt"):
            trip_id = row["trip_id"]
            stop_sequence = int(row["stop_sequence"])
            if trip_id != current_trip_id:
                if current_trip_id and first_departure is not None and current_trip_id in trips_by_id:
                    trip = trips_by_id[current_trip_id]
                    departures_by_route_service[(trip["route_id"], trip["service_id"])].append(
                        first_departure
                    )
                current_trip_id = trip_id
                first_departure = parse_gtfs_time(row["departure_time"]) if stop_sequence == 1 else None
            elif stop_sequence == 1 and first_departure is None:
                first_departure = parse_gtfs_time(row["departure_time"])

        if current_trip_id and first_departure is not None and current_trip_id in trips_by_id:
            trip = trips_by_id[current_trip_id]
            departures_by_route_service[(trip["route_id"], trip["service_id"])].append(first_departure)

    waits_by_route: Dict[str, List[float]] = defaultdict(list)
    for (route_id, _service_id), departures in departures_by_route_service.items():
        departures = sorted(set(departures))
        gaps = [
            (departures[i + 1] - departures[i]) / 60.0
            for i in range(len(departures) - 1)
            if 2 * 60 <= departures[i + 1] - departures[i] <= 30 * 60
        ]
        if gaps:
            waits_by_route[route_id].append(statistics.median(gaps) / 2.0)

    route_waits: Dict[str, float] = {}
    for route_id, waits in waits_by_route.items():
        route_waits[route_id] = round(clamp(statistics.median(waits), 1.5, 8.0), 2)
    return route_waits


def build_graph(
    stations: list,
    station_index_by_id: Dict[str, int],
    stop_to_complex: Dict[str, str],
    trips_by_id: dict,
    route_waits: Dict[str, float],
) -> Tuple[list, list, list]:
    durations_by_edge: Dict[Tuple[int, int, str], List[float]] = defaultdict(list)
    current_trip_id = None
    current_rows: List[dict] = []

    def process_trip(trip_id: str, rows: List[dict]) -> None:
        trip = trips_by_id.get(trip_id)
        if not trip or len(rows) < 2:
            return
        route_id = trip["route_id"]
        ordered = sorted(rows, key=lambda row: int(row["stop_sequence"]))
        for row in ordered:
            stop_id = row["stop_id"]
            complex_id = stop_to_complex.get(stop_id)
            if complex_id in station_index_by_id:
                stations[station_index_by_id[complex_id]]["routes"].add(route_id)
        for prev, nxt in zip(ordered, ordered[1:]):
            from_complex = stop_to_complex.get(prev["stop_id"])
            to_complex = stop_to_complex.get(nxt["stop_id"])
            if not from_complex or not to_complex or from_complex == to_complex:
                continue
            if from_complex not in station_index_by_id or to_complex not in station_index_by_id:
                continue
            duration_seconds = parse_gtfs_time(nxt["arrival_time"]) - parse_gtfs_time(
                prev["departure_time"]
            )
            if 20 <= duration_seconds <= 1800:
                from_index = station_index_by_id[from_complex]
                to_index = station_index_by_id[to_complex]
                durations_by_edge[(from_index, to_index, route_id)].append(duration_seconds / 60.0)

    for gtfs_path in [TRAINS_GTFS, VLINE_TRAINS_GTFS, TRAMS_GTFS, BUSES_GTFS]:
        current_trip_id = None
        current_rows = []
        for row in read_csv_from_zip(gtfs_path, "stop_times.txt"):
            trip_id = row["trip_id"]
            if current_trip_id is None:
                current_trip_id = trip_id
            if trip_id != current_trip_id:
                process_trip(current_trip_id, current_rows)
                current_trip_id = trip_id
                current_rows = []
            current_rows.append(row)
        if current_trip_id and current_rows:
            process_trip(current_trip_id, current_rows)

    route_states = []
    state_index_by_key: Dict[Tuple[int, str], int] = {}
    station_states: List[List[int]] = [[] for _ in stations]
    for station_index, station in enumerate(stations):
        for route_id in sorted(station["routes"]):
            state_index_by_key[(station_index, route_id)] = len(route_states)
            route_states.append({"stationIndex": station_index, "routeId": route_id})
            station_states[station_index].append(state_index_by_key[(station_index, route_id)])

    adjacency = [dict() for _ in route_states]
    for (from_station, to_station, route_id), durations in durations_by_edge.items():
        from_state = state_index_by_key.get((from_station, route_id))
        to_state = state_index_by_key.get((to_station, route_id))
        if from_state is None or to_state is None:
            continue
        weight = round(statistics.median(durations), 2)
        existing = adjacency[from_state].get(to_state)
        if existing is None or weight < existing:
            adjacency[from_state][to_state] = weight

    for station_index, state_indexes in enumerate(station_states):
        for from_state in state_indexes:
            for to_state in state_indexes:
                if from_state == to_state:
                    continue
                to_route = route_states[to_state]["routeId"]
                transfer_cost = round(
                    TRANSFER_PENALTY + route_waits.get(to_route, DEFAULT_BOARD_WAIT), 2
                )
                existing = adjacency[from_state].get(to_state)
                if existing is None or transfer_cost < existing:
                    adjacency[from_state][to_state] = transfer_cost

    for i, source in enumerate(stations):
        sx, sy = source["point"]
        for j in range(i + 1, len(stations)):
            tx, ty = stations[j]["point"]
            distance = math.hypot(tx - sx, ty - sy)
            if distance > INTER_COMPLEX_WALK_RADIUS:
                continue
            walk_minutes = distance / WALK_METERS_PER_MINUTE + INTER_COMPLEX_WALK_PENALTY
            for from_state in station_states[i]:
                for to_state in station_states[j]:
                    to_route = route_states[to_state]["routeId"]
                    from_route = route_states[from_state]["routeId"]
                    forward_cost = round(
                        walk_minutes
                        + INTER_COMPLEX_TRANSFER_PENALTY
                        + route_waits.get(to_route, DEFAULT_BOARD_WAIT),
                        2,
                    )
                    backward_cost = round(
                        walk_minutes
                        + INTER_COMPLEX_TRANSFER_PENALTY
                        + route_waits.get(from_route, DEFAULT_BOARD_WAIT),
                        2,
                    )
                    existing_forward = adjacency[from_state].get(to_state)
                    existing_backward = adjacency[to_state].get(from_state)
                    if existing_forward is None or forward_cost < existing_forward:
                        adjacency[from_state][to_state] = forward_cost
                    if existing_backward is None or backward_cost < existing_backward:
                        adjacency[to_state][from_state] = backward_cost

    return (
        route_states,
        station_states,
        [
            [[to_index, weight] for to_index, weight in sorted(edges.items())]
            for edges in adjacency
        ],
    )


def build_grid_cells(
    polygons: MultiPolygon,
    stations: list,
    bbox: Tuple[float, float, float, float],
) -> Tuple[list, list]:
    min_x, min_y, max_x, max_y = bbox
    cell_w = (max_x - min_x) / GRID_COLS
    cell_h = (max_y - min_y) / GRID_ROWS
    mask = []
    cells = []
    station_points = [station["point"] for station in stations]

    # Build spatial index for nearest-station lookups
    bucket_size = 500.0
    buckets: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for idx, (sx, sy) in enumerate(station_points):
        key = (int(sx // bucket_size), int(sy // bucket_size))
        buckets[key].append(idx)

    def nearest_k_stations(x: float, y: float, k: int) -> List[Tuple[int, float]]:
        bx = int(x // bucket_size)
        by = int(y // bucket_size)
        candidates: List[Tuple[float, int]] = []
        radius = 0
        while len(candidates) < k or (candidates and radius * bucket_size < candidates[k - 1][0] * 2):
            for ix in range(bx - radius, bx + radius + 1):
                for iy in range(by - radius, by + radius + 1):
                    for station_idx in buckets.get((ix, iy), []):
                        sx, sy = station_points[station_idx]
                        dist = math.hypot(sx - x, sy - y)
                        candidates.append((dist, station_idx))
            radius += 1
            if radius > 20:
                break
        candidates.sort()
        return [(idx, dist) for dist, idx in candidates[:k]]

    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            x = min_x + (col + 0.5) * cell_w
            y = min_y + (row + 0.5) * cell_h
            point = (x, y)
            if not point_in_multipolygon(point, polygons):
                mask.append(-1)
                continue
            ranked = [
                (
                    station_index,
                    round(
                        dist / ACCESS_WALK_METERS_PER_MINUTE + STATION_ACCESS_PENALTY,
                        2,
                    ),
                )
                for station_index, dist in nearest_k_stations(x, y, CELL_NEAREST_STATIONS)
            ]
            cells.append(
                {
                    "col": col,
                    "row": row,
                    "point": round_point(point),
                    "access": [[station_index, walk_minutes] for station_index, walk_minutes in ranked],
                }
            )
            mask.append(len(cells) - 1)
    return cells, mask


def main() -> None:
    print("Loading LGA boundaries...")
    borough_payload = load_json(BOROUGHS_PATH)
    lat0 = average_feature_latitude(borough_payload)
    print(f"Reference latitude: {lat0:.4f}")

    boroughs, all_polygons = extract_boroughs(borough_payload, lat0)
    bbox = bounds_of_multipolygon(all_polygons)
    print(f"Bounds: {bbox}, LGAs: {len(boroughs)}")

    print("Extracting parks...")
    parks = extract_parks(lat0, bbox)
    print(f"  {len(parks)} parks")

    print("Extracting streets...")
    streets = extract_streets(lat0, bbox)
    print(f"  {len(streets)} streets")

    print("Finding frequent bus routes...")
    frequent_bus_routes = find_frequent_bus_routes()
    print(f"  {len(frequent_bus_routes)} frequent bus routes (>= {MIN_BUS_TRIPS_FOR_ROUTING} trips)")

    print("Building station data...")
    stations, station_index_by_id, stop_to_complex = build_station_data(lat0)
    print(f"  {len(stations)} stations/stops total")

    print("Building routes and shapes...")
    route_styles, route_shapes, trips_by_id = build_routes_and_shapes(lat0, bbox, frequent_bus_routes)
    print(f"  {len(route_styles)} routes, {len(route_shapes)} shapes")

    print("Computing route wait times...")
    route_waits = build_route_waits(trips_by_id)
    print(f"  {len(route_waits)} routes with wait times computed")

    print("Building transit graph...")
    route_states, station_states, adjacency = build_graph(
        stations, station_index_by_id, stop_to_complex, trips_by_id, route_waits
    )
    print(f"  {len(route_states)} route states, {sum(len(e) for e in adjacency)} edges")

    print("Building grid cells...")
    cells, mask = build_grid_cells(all_polygons, stations, bbox)
    print(f"  {len(cells)} valid cells out of {GRID_COLS * GRID_ROWS}")

    output = {
        "meta": {
            "lat0": round(lat0, 6),
            "bounds": [round(value, 1) for value in bbox],
            "gridCols": GRID_COLS,
            "gridRows": GRID_ROWS,
            "walkMetersPerMinute": WALK_METERS_PER_MINUTE,
            "accessWalkMetersPerMinute": ACCESS_WALK_METERS_PER_MINUTE,
            "stationAccessPenalty": STATION_ACCESS_PENALTY,
            "originStationCount": ORIGIN_NEAREST_STATIONS,
            "cellNearestStations": CELL_NEAREST_STATIONS,
            "defaultBoardWait": DEFAULT_BOARD_WAIT,
            "transferPenalty": TRANSFER_PENALTY,
            "interComplexTransferPenalty": INTER_COMPLEX_TRANSFER_PENALTY,
        },
        "boroughs": boroughs,
        "externalLand": [],
        "parks": parks,
        "streets": streets,
        "routes": route_shapes,
        "stations": [
            {
                "id": station["id"],
                "name": station["name"],
                "point": round_point(station["point"]),
                "routes": sorted(station["routes"]),
            }
            for station in stations
        ],
        "routeStates": route_states,
        "stationStates": station_states,
        "routeWaits": route_waits,
        "adjacency": adjacency,
        "cells": cells,
        "mask": mask,
        "routeStyles": route_styles,
    }

    SITE_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SITE_DATA_PATH.write_text(json.dumps(output, separators=(",", ":")), encoding="utf-8")
    size_mb = SITE_DATA_PATH.stat().st_size / 1024 / 1024
    print(f"Wrote {SITE_DATA_PATH} ({size_mb:.1f} MB)")

    render_keys = {"meta", "boroughs", "externalLand", "parks", "streets", "routes", "stations", "routeStyles"}
    compute_keys = {"routeStates", "stationStates", "routeWaits", "adjacency", "cells", "mask"}
    render_output = {key: value for key, value in output.items() if key in render_keys}
    compute_output = {key: value for key, value in output.items() if key in compute_keys}

    SITE_RENDER_PATH.write_text(json.dumps(render_output, separators=(",", ":")), encoding="utf-8")
    SITE_COMPUTE_PATH.write_text(json.dumps(compute_output, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {SITE_RENDER_PATH} ({SITE_RENDER_PATH.stat().st_size // 1024} KB)")
    print(f"Wrote {SITE_COMPUTE_PATH} ({SITE_COMPUTE_PATH.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
