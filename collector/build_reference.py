"""
Build static reference data used by the collector (run once, re-run if needed).

1. municipalities.geojson
   Polygons (WGS84) of all Swiss municipalities in which shared vehicles are
   currently parked. Source: swisstopo swissBOUNDARIES3D via api3.geo.admin.ch.
   Key: BFS municipality number (bfs_nr) -> later join key for BFS data.

2. vehicle_types.csv
   Mapping vehicle_type_id (provider specific, e.g. 'lime_zurich:2') to a
   harmonised vehicle category (Bike, E-Bike, E-Scooter, E-CargoBike, ...).
   Source: api.sharedmobility.ch 'identify' endpoint (returns vehicle_type).

Usage:
    python collector/build_reference.py
"""

import collections
import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from shapely.geometry import mapping, shape

ROOT = Path(__file__).resolve().parents[1]
REF_DIR = ROOT / "data" / "reference"

FEED_URL = "https://sharedmobility.ch/free_bike_status.json"
PROVIDERS_URL = "https://api.sharedmobility.ch/v1/sharedmobility/providers"
SM_IDENTIFY_URL = "https://api.sharedmobility.ch/v1/sharedmobility/identify"
GEO_IDENTIFY_URL = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
GEO_FEATURE_URL = ("https://api3.geo.admin.ch/rest/services/api/MapServer/"
                   "ch.swisstopo.swissboundaries3d-gemeinde-flaeche.fill/{bfs}")
MUNI_LAYER = "ch.swisstopo.swissboundaries3d-gemeinde-flaeche.fill"

HEADERS = {"User-Agent": "zhaw-data-analytics-student-project/1.0"}
MIN_VEHICLES_PER_CELL = 3      # cells (~1 km) with fewer vehicles are ignored
SIMPLIFY_TOLERANCE = 0.0001    # degrees (~10 m), keeps file small

session = requests.Session()
session.headers.update(HEADERS)


def get_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


# --------------------------------------------------------------------------
# 1. Municipalities
# --------------------------------------------------------------------------
def municipality_at(cell):
    lat, lon = cell
    params = dict(geometry=f"{lon},{lat}", geometryType="esriGeometryPoint",
                  sr=4326, layers=f"all:{MUNI_LAYER}", tolerance=0,
                  returnGeometry="false")
    try:
        res = get_json(GEO_IDENTIFY_URL, params)["results"]
    except Exception:
        return None
    current = [x["attributes"] for x in res if x["attributes"].get("is_current_jahr")]
    return current[0]["gde_nr"] if current else None


def municipality_polygon(bfs_nr):
    feat = get_json(GEO_FEATURE_URL.format(bfs=bfs_nr),
                    dict(geometryFormat="geojson", sr=4326))["feature"]
    geom = shape(feat["geometry"]).simplify(SIMPLIFY_TOLERANCE, preserve_topology=True)
    props = feat["properties"]
    return {"type": "Feature",
            "properties": {"bfs_nr": int(bfs_nr),
                           "name": props.get("gemname"),
                           "canton": props.get("kanton")},
            "geometry": mapping(geom)}


def build_municipalities(bikes):
    cells = collections.Counter((round(b["lat"], 2), round(b["lon"], 2)) for b in bikes)
    cells = [c for c, n in cells.items() if n >= MIN_VEHICLES_PER_CELL]
    print(f"Identifying municipalities for {len(cells)} grid cells ...")
    with ThreadPoolExecutor(6) as ex:
        bfs_numbers = {b for b in ex.map(municipality_at, cells) if b}

    out_file = REF_DIR / "municipalities.geojson"
    existing = {}
    if out_file.exists():  # keep previously fetched municipalities
        for f in json.loads(out_file.read_text())["features"]:
            existing[f["properties"]["bfs_nr"]] = f
    todo = sorted(set(bfs_numbers) - set(existing))
    print(f"{len(bfs_numbers)} municipalities found, {len(todo)} new polygons to fetch")
    with ThreadPoolExecutor(6) as ex:
        for feat in ex.map(municipality_polygon, todo):
            existing[feat["properties"]["bfs_nr"]] = feat

    fc = {"type": "FeatureCollection",
          "features": [existing[k] for k in sorted(existing)]}
    out_file.write_text(json.dumps(fc))
    print(f"-> {out_file} ({len(fc['features'])} municipalities)")


# --------------------------------------------------------------------------
# 2. Vehicle types
# --------------------------------------------------------------------------
def guess_from_id(type_id):
    t = type_id.lower()
    if "cargo" in t:
        return "E-CargoBike"
    if "scooter" in t:
        return "E-Scooter"
    if "ebike" in t or "e-bike" in t:
        return "E-Bike"
    if t.endswith(":bike") or t.endswith(":mbike") or "bicycle" in t:
        return "Bike"
    return None


def type_from_api(bike):
    params = dict(Geometry=f"{bike['lon']},{bike['lat']}", Tolerance=15,
                  offset=0, geometryFormat="esrijson")
    try:
        res = get_json(SM_IDENTIFY_URL, params)
    except Exception:
        return None
    for feat in res:
        if feat.get("id") == bike["bike_id"]:
            vt = feat["attributes"].get("vehicle_type")
            return vt[0] if isinstance(vt, list) and len(vt) == 1 else None
    return None


def build_vehicle_types(bikes):
    providers = {p["provider_id"]: p.get("vehicle_type", []) for p in get_json(PROVIDERS_URL)}
    samples = collections.defaultdict(list)
    for b in bikes:
        samples[b.get("vehicle_type_id") or f"{b['provider_id']}:none"].append(b)

    rows = []
    for type_id, vehicles in sorted(samples.items()):
        provider = vehicles[0]["provider_id"]
        category, source = None, None
        for b in vehicles[:3]:  # ask the API for up to 3 sample vehicles
            category = type_from_api(b)
            if category:
                source = "api_identify"
                break
            time.sleep(0.2)
        if not category and len(providers.get(provider, [])) == 1:
            category, source = providers[provider][0], "provider_single_type"
        if not category:
            category = guess_from_id(type_id)
            source = "id_heuristic" if category else "unknown"
        rows.append(dict(vehicle_type_id=type_id, provider_id=provider,
                         vehicle_category=category or "Unknown", source=source,
                         n_vehicles_sample=len(vehicles),
                         provider_types="|".join(providers.get(provider, []))))
        print(f"{type_id:60s} {category!s:12s} ({source})")

    out_file = REF_DIR / "vehicle_types.csv"
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"-> {out_file}")


if __name__ == "__main__":
    REF_DIR.mkdir(parents=True, exist_ok=True)
    feed = get_json(FEED_URL)
    bikes = feed["data"]["bikes"]
    print(f"{len(bikes)} vehicles in feed")
    build_municipalities(bikes)
    build_vehicle_types(bikes)
