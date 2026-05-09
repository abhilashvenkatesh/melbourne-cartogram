// Web Worker: all heavy compute (Dijkstra, warp) off the main thread.
// Communicates via postMessage. Cannot access DOM or window.

const MIN_AREA_WEIGHT = 1;
const MAX_AREA_WEIGHT = 2.67;
const WEIGHT_BLUR_PASSES = 2;
const WEIGHT_BLUR_RADIUS = 2;
const WARP_INFLUENCE_RADIUS = 8;
const WARP_SIGMA_CELLS = 3.4;
const WARP_DISPLACEMENT_SCALE = 1.0;
const WARP_MAX_SHIFT_CELLS = 6.6;
const WARP_NODE_SMOOTHING_PASSES = 3;
const WARP_EDGE_FADE_CELLS = 10;
const STATION_GRID_CELLS = 80;
const REACHABILITY_THRESHOLD_MINUTES = 60;

// Worker-local state (set on 'init' message)
let workerData = null;
let dynamicAdjacency = null;
let stationIndex = null;
let travelSettings = null;
let travelSettingsDefaults = null;

// ── Math utilities ────────────────────────────────────────────────────────────

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function smoothstep(edge0, edge1, value) {
  const t = clamp((value - edge0) / ((edge1 - edge0) || 1), 0, 1);
  return t * t * (3 - 2 * t);
}

function distance(a, b) {
  return Math.hypot(a[0] - b[0], a[1] - b[1]);
}

function lerp(a, b, t) {
  return a + (b - a) * t;
}

function bilerpPoint(p00, p10, p01, p11, tx, ty) {
  return [
    lerp(lerp(p00[0], p10[0], tx), lerp(p01[0], p11[0], tx), ty),
    lerp(lerp(p00[1], p10[1], tx), lerp(p01[1], p11[1], tx), ty),
  ];
}

function triangleArea(a, b, c) {
  return Math.abs((a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1])) / 2);
}

function quadArea(p00, p10, p11, p01) {
  return triangleArea(p00, p10, p11) + triangleArea(p00, p11, p01);
}

function barycentricWeights(point, a, b, c) {
  const denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1]);
  if (Math.abs(denominator) < 1e-9) return null;
  const w1 = ((b[1] - c[1]) * (point[0] - c[0]) + (c[0] - b[0]) * (point[1] - c[1])) / denominator;
  const w2 = ((c[1] - a[1]) * (point[0] - c[0]) + (a[0] - c[0]) * (point[1] - c[1])) / denominator;
  const w3 = 1 - w1 - w2;
  const epsilon = 1e-5;
  if (w1 < -epsilon || w2 < -epsilon || w3 < -epsilon) return null;
  return [w1, w2, w3];
}

function interpolateTriangle(weights, a, b, c) {
  return [
    weights[0] * a[0] + weights[1] * b[0] + weights[2] * c[0],
    weights[0] * a[1] + weights[1] * b[1] + weights[2] * c[1],
  ];
}

// ── Geometry utilities ────────────────────────────────────────────────────────

function pointInRing(point, ring) {
  const [x, y] = point;
  let inside = false;
  let j = ring.length - 1;
  for (let i = 0; i < ring.length; i += 1) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    const intersects = (yi > y) !== (yj > y);
    if (intersects) {
      const xHit = ((xj - xi) * (y - yi)) / ((yj - yi) || 1e-12) + xi;
      if (x < xHit) inside = !inside;
    }
    j = i;
  }
  return inside;
}

function pointInPolygon(point, polygon) {
  if (!polygon.length || !pointInRing(point, polygon[0])) return false;
  for (let index = 1; index < polygon.length; index += 1) {
    if (pointInRing(point, polygon[index])) return false;
  }
  return true;
}

function pointToSegmentProjection(point, start, end) {
  const dx = end[0] - start[0];
  const dy = end[1] - start[1];
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared === 0) {
    return { point: start.slice(), distance: distance(point, start) };
  }
  const t = clamp(((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / lengthSquared, 0, 1);
  const projectedPoint = [start[0] + t * dx, start[1] + t * dy];
  return { point: projectedPoint, distance: distance(point, projectedPoint) };
}

function locateNearestBoroughBorder(point) {
  let best = { point: point.slice(), distance: Infinity };
  for (const borough of workerData.boroughs) {
    for (const polygon of borough.polygons) {
      for (const ring of polygon) {
        for (let index = 0; index < ring.length - 1; index += 1) {
          const candidate = pointToSegmentProjection(point, ring[index], ring[index + 1]);
          if (candidate.distance < best.distance) best = candidate;
        }
      }
    }
  }
  return best;
}

function pointInBoroughs(point) {
  for (const borough of workerData.boroughs) {
    for (const polygon of borough.polygons) {
      if (pointInPolygon(point, polygon)) return true;
    }
  }
  return false;
}

function pointInExternalLand(point) {
  for (const polygon of workerData.externalLand || []) {
    if (pointInPolygon(point, polygon)) return true;
  }
  return false;
}

function classifySurface(point) {
  if (pointInBoroughs(point)) return "borough";
  return pointInExternalLand(point) ? "land" : "water";
}

function normalizeTravelPoint(point) {
  const surface = classifySurface(point);
  if (surface !== "water") {
    return { surface, point, swimMinutes: 0, swimDistance: 0 };
  }
  const border = locateNearestBoroughBorder(point);
  return {
    surface,
    point: border.point,
    swimMinutes: border.distance / travelSettings.swimSpeed,
    swimDistance: border.distance,
  };
}

function minuteToAreaWeight(minutes) {
  const t = clamp(minutes / travelSettings.maxTransitTime, 0, 1);
  return MIN_AREA_WEIGHT + (1 - t) * (MAX_AREA_WEIGHT - MIN_AREA_WEIGHT);
}

// ── Station spatial index ─────────────────────────────────────────────────────

function buildStationSpatialIndex() {
  const [minX, minY, maxX, maxY] = workerData.meta.bounds;
  const cellW = (maxX - minX) / STATION_GRID_CELLS;
  const cellH = (maxY - minY) / STATION_GRID_CELLS;
  const grid = Array.from({ length: STATION_GRID_CELLS * STATION_GRID_CELLS }, () => []);

  for (let i = 0; i < workerData.stations.length; i++) {
    const [x, y] = workerData.stations[i].point;
    const col = Math.min(STATION_GRID_CELLS - 1, Math.max(0, Math.floor((x - minX) / cellW)));
    const row = Math.min(STATION_GRID_CELLS - 1, Math.max(0, Math.floor((y - minY) / cellH)));
    grid[row * STATION_GRID_CELLS + col].push(i);
  }
  return { grid, cellW, cellH, minX, minY };
}

function nearestStations(point, count) {
  const accessPenalty = workerData.meta.stationAccessPenalty;
  const { grid, cellW, cellH, minX, minY } = stationIndex;
  const [px, py] = point;
  const startCol = Math.min(STATION_GRID_CELLS - 1, Math.max(0, Math.floor((px - minX) / cellW)));
  const startRow = Math.min(STATION_GRID_CELLS - 1, Math.max(0, Math.floor((py - minY) / cellH)));

  const candidates = [];
  for (let radius = 0; radius <= STATION_GRID_CELLS; radius++) {
    for (let dr = -radius; dr <= radius; dr++) {
      for (let dc = -radius; dc <= radius; dc++) {
        if (Math.abs(dr) !== radius && Math.abs(dc) !== radius) continue;
        const r = startRow + dr;
        const c = startCol + dc;
        if (r < 0 || r >= STATION_GRID_CELLS || c < 0 || c >= STATION_GRID_CELLS) continue;
        for (const idx of grid[r * STATION_GRID_CELLS + c]) {
          const station = workerData.stations[idx];
          candidates.push({
            index: idx,
            name: station.name,
            walkMinutes: distance(point, station.point) / travelSettings.walkingSpeed + accessPenalty,
          });
        }
      }
    }
    if (candidates.length >= count) break;
  }
  return candidates.sort((a, b) => a.walkMinutes - b.walkMinutes).slice(0, count);
}

// ── Dynamic adjacency ─────────────────────────────────────────────────────────

function buildDynamicAdjacency() {
  const defaults = travelSettingsDefaults;
  const routeStates = workerData.routeStates;
  const stations = workerData.stations;

  return workerData.adjacency.map((edges, fromIndex) => {
    const fromState = routeStates[fromIndex];
    return edges.map(([toIndex, weight]) => {
      const toState = routeStates[toIndex];
      const boardingDelta =
        (workerData.routeWaits?.[toState.routeId] ?? defaults.transitTime) - defaults.transitTime;
      if (fromState.routeId === toState.routeId) {
        return { toIndex, kind: "ride", rideMinutes: weight };
      }
      if (fromState.stationIndex === toState.stationIndex) {
        return { toIndex, kind: "transfer", boardingDelta };
      }
      const fromPoint = stations[fromState.stationIndex].point;
      const toPoint = stations[toState.stationIndex].point;
      const walkDistance = distance(fromPoint, toPoint);
      const walkPenalty = Math.max(
        0,
        weight -
          walkDistance / defaults.walkingSpeed -
          boardingDelta -
          defaults.transitTime -
          (workerData.meta.interComplexTransferPenalty ?? defaults.transferTime),
      );
      return { toIndex, kind: "interchange", boardingDelta, walkDistance, walkPenalty };
    });
  });
}

// ── Dijkstra ──────────────────────────────────────────────────────────────────

class MinHeap {
  constructor() { this._h = []; }
  get size() { return this._h.length; }
  push(dist, idx) {
    this._h.push([dist, idx]);
    this._siftUp(this._h.length - 1);
  }
  pop() {
    const top = this._h[0];
    const last = this._h.pop();
    if (this._h.length > 0) { this._h[0] = last; this._siftDown(0); }
    return top;
  }
  _siftUp(i) {
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (this._h[p][0] <= this._h[i][0]) break;
      [this._h[p], this._h[i]] = [this._h[i], this._h[p]];
      i = p;
    }
  }
  _siftDown(i) {
    const n = this._h.length;
    for (;;) {
      let s = i, l = 2 * i + 1, r = l + 1;
      if (l < n && this._h[l][0] < this._h[s][0]) s = l;
      if (r < n && this._h[r][0] < this._h[s][0]) s = r;
      if (s === i) break;
      [this._h[s], this._h[i]] = [this._h[i], this._h[s]];
      i = s;
    }
  }
}

function runDijkstra(origin) {
  const stateCount = workerData.routeStates.length;
  const distances = new Float64Array(stateCount).fill(Infinity);
  const previous = new Int32Array(stateCount).fill(-1);
  const seeds = nearestStations(origin.point, workerData.meta.originStationCount);
  const heap = new MinHeap();

  for (const seed of seeds) {
    for (const routeStateIndex of workerData.stationStates[seed.index] || []) {
      const routeId = workerData.routeStates[routeStateIndex].routeId;
      const boardingDelta =
        (workerData.routeWaits?.[routeId] ?? travelSettingsDefaults.transitTime) -
        travelSettingsDefaults.transitTime;
      const dist = origin.swimMinutes + seed.walkMinutes + travelSettings.transitTime + boardingDelta;
      if (dist < distances[routeStateIndex]) {
        distances[routeStateIndex] = dist;
        previous[routeStateIndex] = -1;
        heap.push(dist, routeStateIndex);
      }
    }
  }

  while (heap.size > 0) {
    const [dist, current] = heap.pop();
    if (dist > distances[current]) continue;
    for (const edge of dynamicAdjacency[current]) {
      const weight =
        edge.kind === "ride"
          ? edge.rideMinutes
          : edge.kind === "transfer"
            ? travelSettings.transferTime + travelSettings.transitTime + edge.boardingDelta
            : edge.walkDistance / travelSettings.walkingSpeed +
              edge.walkPenalty +
              travelSettings.transferTime +
              travelSettings.transitTime +
              edge.boardingDelta;
      const nextIndex = edge.toIndex;
      const candidate = distances[current] + weight;
      if (candidate < distances[nextIndex]) {
        distances[nextIndex] = candidate;
        previous[nextIndex] = current;
        heap.push(candidate, nextIndex);
      }
    }
  }

  return { distances, previous, seeds };
}

// ── Reachability ──────────────────────────────────────────────────────────────

function summarizeReachability(origin, originDistances) {
  const totalStations = workerData.stations.length;
  let reachableStations = 0;
  const accessPenalty = workerData.meta.stationAccessPenalty;

  for (let i = 0; i < totalStations; i++) {
    const station = workerData.stations[i];
    let bestMinutes = distance(origin.point, station.point) / travelSettings.walkingSpeed + origin.swimMinutes;
    for (const rsi of (workerData.stationStates[i] || [])) {
      const t = originDistances[rsi] + accessPenalty;
      if (t < bestMinutes) bestMinutes = t;
    }
    if (bestMinutes <= travelSettings.maxTransitTime) reachableStations++;
  }

  return {
    reachableStations,
    totalStations,
    ratio: totalStations ? reachableStations / totalStations : 0,
    thresholdMinutes: travelSettings.maxTransitTime,
  };
}

// ── computeWarp ───────────────────────────────────────────────────────────────
// Returns plain data (no functions) — main thread reconstructs warpPoint/inverseWarpPoint.

function computeWarp(origin) {
  const { distances, previous, seeds } = runDijkstra(origin);
  const { gridCols, gridRows, bounds } = workerData.meta;
  const [minX, minY, maxX, maxY] = bounds;
  const spanX = maxX - minX;
  const spanY = maxY - minY;
  const cellW = spanX / gridCols;
  const cellH = spanY / gridRows;
  const minuteGrid = Array.from({ length: gridRows }, () => new Array(gridCols).fill(Infinity));
  const validMask = Array.from({ length: gridRows }, () => new Array(gridCols).fill(false));

  for (let maskIndex = 0; maskIndex < workerData.mask.length; maskIndex += 1) {
    const cellIndex = workerData.mask[maskIndex];
    if (cellIndex === -1) continue;
    const cell = workerData.cells[cellIndex];
    let bestMinutes = distance(origin.point, cell.point) / travelSettings.walkingSpeed + origin.swimMinutes;
    for (const [stationIndex] of cell.access) {
      const egressMinutes =
        distance(cell.point, workerData.stations[stationIndex].point) / travelSettings.walkingSpeed +
        workerData.meta.stationAccessPenalty;
      for (const routeStateIndex of workerData.stationStates[stationIndex] || []) {
        bestMinutes = Math.min(bestMinutes, distances[routeStateIndex] + egressMinutes);
      }
    }
    minuteGrid[cell.row][cell.col] = bestMinutes;
    validMask[cell.row][cell.col] = true;
  }

  let smoothedMinutes = minuteGrid.map((row) => row.slice());
  for (let pass = 0; pass < WEIGHT_BLUR_PASSES; pass += 1) {
    const nextMinutes = Array.from({ length: gridRows }, () => new Array(gridCols).fill(Infinity));
    for (let row = 0; row < gridRows; row += 1) {
      for (let col = 0; col < gridCols; col += 1) {
        if (!validMask[row][col]) continue;
        let totalMinutes = 0;
        let count = 0;
        for (let y = Math.max(0, row - WEIGHT_BLUR_RADIUS); y <= Math.min(gridRows - 1, row + WEIGHT_BLUR_RADIUS); y += 1) {
          for (let x = Math.max(0, col - WEIGHT_BLUR_RADIUS); x <= Math.min(gridCols - 1, col + WEIGHT_BLUR_RADIUS); x += 1) {
            if (!validMask[y][x]) continue;
            totalMinutes += smoothedMinutes[y][x];
            count += 1;
          }
        }
        nextMinutes[row][col] = count ? totalMinutes / count : smoothedMinutes[row][col];
      }
    }
    smoothedMinutes = nextMinutes;
  }

  const areaWeights = Array.from({ length: gridRows }, () => new Array(gridCols).fill(1));
  const anomalyGrid = Array.from({ length: gridRows }, () => new Array(gridCols).fill(0));
  for (let row = 0; row < gridRows; row += 1) {
    for (let col = 0; col < gridCols; col += 1) {
      if (!validMask[row][col]) continue;
      const areaWeight = minuteToAreaWeight(smoothedMinutes[row][col]);
      areaWeights[row][col] = areaWeight;
      anomalyGrid[row][col] = areaWeight - 1;
    }
  }

  const reachability = summarizeReachability(origin, distances);

  const warpNodes = Array.from({ length: gridRows + 1 }, () => new Array(gridCols + 1).fill(null));
  const sigmaSq = WARP_SIGMA_CELLS * WARP_SIGMA_CELLS;
  const maxShiftX = cellW * WARP_MAX_SHIFT_CELLS;
  const maxShiftY = cellH * WARP_MAX_SHIFT_CELLS;

  for (let nodeRow = 0; nodeRow <= gridRows; nodeRow += 1) {
    for (let nodeCol = 0; nodeCol <= gridCols; nodeCol += 1) {
      const baseX = minX + nodeCol * cellW;
      const baseY = minY + nodeRow * cellH;
      let offsetX = 0;
      let offsetY = 0;

      const rowStart = Math.max(0, nodeRow - WARP_INFLUENCE_RADIUS);
      const rowEnd = Math.min(gridRows - 1, nodeRow + WARP_INFLUENCE_RADIUS - 1);
      const colStart = Math.max(0, nodeCol - WARP_INFLUENCE_RADIUS);
      const colEnd = Math.min(gridCols - 1, nodeCol + WARP_INFLUENCE_RADIUS - 1);

      for (let row = rowStart; row <= rowEnd; row += 1) {
        for (let col = colStart; col <= colEnd; col += 1) {
          if (!validMask[row][col]) continue;
          const anomaly = anomalyGrid[row][col];
          if (Math.abs(anomaly) < 1e-6) continue;
          const centerX = minX + (col + 0.5) * cellW;
          const centerY = minY + (row + 0.5) * cellH;
          const dxCells = (baseX - centerX) / cellW;
          const dyCells = (baseY - centerY) / cellH;
          const distSqCells = dxCells * dxCells + dyCells * dyCells;
          const distCells = Math.sqrt(distSqCells + 1e-9);
          const gaussian = Math.exp(-distSqCells / (2 * sigmaSq));
          const strength = anomaly * gaussian * WARP_DISPLACEMENT_SCALE;
          offsetX += (dxCells / distCells) * strength * cellW;
          offsetY += (dyCells / distCells) * strength * cellH;
        }
      }

      offsetX = clamp(offsetX, -maxShiftX, maxShiftX);
      offsetY = clamp(offsetY, -maxShiftY, maxShiftY);

      const edgeDistance = Math.min(nodeCol, gridCols - nodeCol, nodeRow, gridRows - nodeRow);
      const edgeFade = smoothstep(0, WARP_EDGE_FADE_CELLS, edgeDistance);
      warpNodes[nodeRow][nodeCol] = [baseX + offsetX * edgeFade, baseY + offsetY * edgeFade];
    }
  }

  for (let pass = 0; pass < WARP_NODE_SMOOTHING_PASSES; pass += 1) {
    const nextNodes = warpNodes.map((row) => row.map((point) => point.slice()));
    for (let nodeRow = 1; nodeRow < gridRows; nodeRow += 1) {
      for (let nodeCol = 1; nodeCol < gridCols; nodeCol += 1) {
        let totalX = 0;
        let totalY = 0;
        let count = 0;
        for (let y = nodeRow - 1; y <= nodeRow + 1; y += 1) {
          for (let x = nodeCol - 1; x <= nodeCol + 1; x += 1) {
            totalX += warpNodes[y][x][0];
            totalY += warpNodes[y][x][1];
            count += 1;
          }
        }
        const edgeDistance = Math.min(nodeCol, gridCols - nodeCol, nodeRow, gridRows - nodeRow);
        const edgeFade = smoothstep(0, WARP_EDGE_FADE_CELLS, edgeDistance);
        const smoothedX = totalX / count;
        const smoothedY = totalY / count;
        nextNodes[nodeRow][nodeCol] = [
          lerp(minX + nodeCol * cellW, smoothedX, 0.72 * edgeFade),
          lerp(minY + nodeRow * cellH, smoothedY, 0.72 * edgeFade),
        ];
      }
    }
    for (let nodeRow = 0; nodeRow <= gridRows; nodeRow += 1) {
      for (let nodeCol = 0; nodeCol <= gridCols; nodeCol += 1) {
        warpNodes[nodeRow][nodeCol] = nextNodes[nodeRow][nodeCol];
      }
    }
  }

  const allWarpedNodes = warpNodes.flat();
  const xs = allWarpedNodes.map((point) => point[0]);
  const ys = allWarpedNodes.map((point) => point[1]);
  const warpedBounds = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];

  const expansion = Array.from({ length: gridRows }, () => new Array(gridCols).fill(0));
  for (let row = 0; row < gridRows; row += 1) {
    for (let col = 0; col < gridCols; col += 1) {
      if (!validMask[row][col]) continue;
      expansion[row][col] =
        quadArea(
          warpNodes[row][col],
          warpNodes[row][col + 1],
          warpNodes[row + 1][col + 1],
          warpNodes[row + 1][col],
        ) / (cellW * cellH);
    }
  }

  return {
    distances,
    previous,
    seeds,
    reachability,
    warpNodes,
    warpedBounds,
    minutes: smoothedMinutes,
    expansion,
    areaWeights,
    validMask,
    gridInfo: { minX, minY, maxX, maxY, cellW, cellH, gridCols, gridRows },
  };
}

// ── Message handler ───────────────────────────────────────────────────────────

self.onmessage = (event) => {
  const { type } = event.data;

  if (type === "init") {
    workerData = event.data.data;
    travelSettings = event.data.travelSettings;
    travelSettingsDefaults = event.data.travelSettingsDefaults;
    dynamicAdjacency = buildDynamicAdjacency();
    stationIndex = buildStationSpatialIndex();
    self.postMessage({ type: "ready" });
    return;
  }

  if (type === "updateSettings") {
    travelSettings = event.data.travelSettings;
    dynamicAdjacency = buildDynamicAdjacency();
    self.postMessage({ type: "settingsUpdated" });
    return;
  }

  if (type === "compute") {
    const { origin, key } = event.data;
    const result = computeWarp(origin);
    self.postMessage({ type: "warpResult", key, result }, [result.distances.buffer, result.previous.buffer]);
    return;
  }
};
