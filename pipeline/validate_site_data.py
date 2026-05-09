#!/usr/bin/env python3
"""Validate generated JSON assets for the Melbourne commute map."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SITE_DATA_DIR = ROOT / "site" / "data"
FULL_DATA_PATH = SITE_DATA_DIR / "commute_map_data.json"
RENDER_DATA_PATH = SITE_DATA_DIR / "map_render.json"
COMPUTE_DATA_PATH = SITE_DATA_DIR / "map_compute.json"


class ValidationError(Exception):
    """Raised when a generated data asset violates the app contract."""


def load_json(path: Path) -> Any:
    if not path.exists():
        raise ValidationError(f"Missing file: {path.relative_to(ROOT)}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValidationError(f"Invalid JSON in {path.relative_to(ROOT)}: {error}") from error


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def assert_finite_numbers(value: Any, path: str = "$") -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        require(math.isfinite(value), f"Non-finite number at {path}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            assert_finite_numbers(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            assert_finite_numbers(item, f"{path}.{key}")
        return
    raise ValidationError(f"Unsupported JSON value at {path}: {type(value).__name__}")


def validate_meta(meta: dict) -> None:
    required_keys = {
        "lat0",
        "bounds",
        "gridCols",
        "gridRows",
        "walkMetersPerMinute",
        "accessWalkMetersPerMinute",
        "stationAccessPenalty",
        "originStationCount",
        "cellNearestStations",
        "defaultBoardWait",
        "transferPenalty",
        "interComplexTransferPenalty",
    }
    missing = sorted(required_keys - set(meta))
    require(not missing, f"meta missing keys: {', '.join(missing)}")
    require(len(meta["bounds"]) == 4, "meta.bounds must contain 4 numbers")
    min_x, min_y, max_x, max_y = meta["bounds"]
    require(min_x < max_x and min_y < max_y, "meta.bounds must be ordered [minX, minY, maxX, maxY]")
    require(meta["gridCols"] > 0 and meta["gridRows"] > 0, "grid dimensions must be positive")


def validate_render_data(data: dict) -> None:
    for key in ("meta", "boroughs", "externalLand", "parks", "streets", "routes", "stations", "routeStyles"):
        require(key in data, f"render data missing key: {key}")
    validate_meta(data["meta"])
    require(data["boroughs"], "render data must include at least one LGA")
    require(data["stations"], "render data must include stations")

    for index, station in enumerate(data["stations"]):
        require("point" in station and len(station["point"]) == 2, f"station {index} missing point")
        require(isinstance(station.get("routes", []), list), f"station {index} routes must be a list")

    route_style_ids = set(data["routeStyles"])
    for index, route in enumerate(data["routes"]):
        require(route.get("routeId") in route_style_ids, f"route shape {index} has no routeStyles entry")
        require(len(route.get("points", [])) >= 2, f"route shape {index} must have at least 2 points")


def validate_compute_data(data: dict, render_data: dict) -> None:
    for key in ("routeStates", "stationStates", "routeWaits", "adjacency", "cells", "mask"):
        require(key in data, f"compute data missing key: {key}")

    station_count = len(render_data["stations"])
    state_count = len(data["routeStates"])
    route_style_ids = set(render_data["routeStyles"])

    require(len(data["stationStates"]) == station_count, "stationStates length must match stations length")
    require(len(data["adjacency"]) == state_count, "adjacency length must match routeStates length")

    for index, state in enumerate(data["routeStates"]):
        station_index = state.get("stationIndex")
        route_id = state.get("routeId")
        require(isinstance(station_index, int) and 0 <= station_index < station_count, f"routeState {index} has invalid stationIndex")
        require(route_id in route_style_ids, f"routeState {index} has unknown routeId {route_id!r}")

    for station_index, state_indexes in enumerate(data["stationStates"]):
        require(isinstance(state_indexes, list), f"stationStates[{station_index}] must be a list")
        for state_index in state_indexes:
            require(isinstance(state_index, int) and 0 <= state_index < state_count, f"stationStates[{station_index}] references invalid state {state_index}")

    for from_index, edges in enumerate(data["adjacency"]):
        require(isinstance(edges, list), f"adjacency[{from_index}] must be a list")
        for edge_index, edge in enumerate(edges):
            require(isinstance(edge, list) and len(edge) == 2, f"adjacency[{from_index}][{edge_index}] must be [toIndex, weight]")
            to_index, weight = edge
            require(isinstance(to_index, int) and 0 <= to_index < state_count, f"adjacency[{from_index}][{edge_index}] has invalid toIndex")
            require(isinstance(weight, (int, float)) and weight >= 0, f"adjacency[{from_index}][{edge_index}] has invalid weight")

    meta = render_data["meta"]
    expected_mask_len = meta["gridCols"] * meta["gridRows"]
    require(len(data["mask"]) == expected_mask_len, f"mask length must be {expected_mask_len}")
    cell_count = len(data["cells"])

    seen_cells = set()
    for mask_index, cell_index in enumerate(data["mask"]):
        require(cell_index == -1 or 0 <= cell_index < cell_count, f"mask[{mask_index}] references invalid cell {cell_index}")
        if cell_index != -1:
            seen_cells.add(cell_index)
    require(len(seen_cells) == cell_count, "every cell should be referenced by the mask exactly once")

    for index, cell in enumerate(data["cells"]):
        require(0 <= cell["col"] < meta["gridCols"], f"cell {index} has invalid col")
        require(0 <= cell["row"] < meta["gridRows"], f"cell {index} has invalid row")
        require(len(cell.get("point", [])) == 2, f"cell {index} missing point")
        for access_index, access in enumerate(cell.get("access", [])):
            require(isinstance(access, list) and len(access) == 2, f"cell {index} access {access_index} must be [stationIndex, minutes]")
            station_index, minutes = access
            require(isinstance(station_index, int) and 0 <= station_index < station_count, f"cell {index} access {access_index} has invalid station")
            require(isinstance(minutes, (int, float)) and minutes >= 0, f"cell {index} access {access_index} has invalid minutes")


def main() -> int:
    try:
        full_data = load_json(FULL_DATA_PATH)
        render_data = load_json(RENDER_DATA_PATH)
        compute_data = load_json(COMPUTE_DATA_PATH)

        assert_finite_numbers(full_data, "commute_map_data")
        assert_finite_numbers(render_data, "map_render")
        assert_finite_numbers(compute_data, "map_compute")

        validate_render_data(render_data)
        validate_compute_data(compute_data, render_data)

        for key in ("meta", "boroughs", "externalLand", "parks", "streets", "routes", "stations", "routeStyles"):
            require(full_data.get(key) == render_data.get(key), f"full data and render data differ for key: {key}")
        for key in ("routeStates", "stationStates", "routeWaits", "adjacency", "cells", "mask"):
            require(full_data.get(key) == compute_data.get(key), f"full data and compute data differ for key: {key}")
    except ValidationError as error:
        print(f"Data validation failed: {error}", file=sys.stderr)
        return 1

    print("Data validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
