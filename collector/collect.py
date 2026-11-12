"""
Shared-mobility collector: takes ONE snapshot of all free shared vehicles in
Switzerland (GBFS feed of sharedmobility.ch) and stores

  availability   number of available vehicles per
                 provider x vehicle_type_id x municipality (BFS number)
  trips          trips detected by comparing the snapshot with the previous one
  runs           one log line per run (timestamps, feed age, counts)

Output: data/raw/<kind>/<YYYY-MM-DD>/<kind>_<YYYYMMDDTHHMMSSZ>.csv.gz

Run it every ~10 minutes (cron / launchd / GitHub Actions).

Trip detection (only for providers with stable vehicle IDs):
  * vehicle visible before, missing now        -> "pending" (rented, charging, ...)
  * pending vehicle re-appears >= MIN_TRIP_M away -> trip (start between last
                                                     seen and first missing)
  * vehicle visible in both snapshots but moved
    >= MIN_TRIP_M                               -> trip (short trip between snapshots)
  * pending > MAX_PENDING_H                     -> dropped (removed from fleet)
Providers that rotate their IDs on every request (e.g. Bird, GBFS privacy
rule) are detected automatically and only counted in 'availability'.

Usage:
    python collector/collect.py [--data-dir data/raw] [--state state/last_state.json.gz]
"""

import argparse
import csv
import gzip
import hashlib
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from shapely import STRtree
from shapely.geometry import Point, shape

ROOT = Path(__file__).resolve().parents[1]
FEED_URL = "https://sharedmobility.ch/free_bike_status.json"
MUNI_FILE = ROOT / "data" / "reference" / "municipalities.geojson"
HEADERS = {"User-Agent": "zhaw-data-analytics-student-project/1.0"}

MIN_TRIP_M = 150          # minimum displacement to count as a trip (GPS noise)
MAX_PENDING_H = 24        # forget vehicles missing longer than this
ROTATING_ID_SHARE = 0.5   # provider rotates IDs if < 50% of IDs are seen again

AVAIL_FIELDS = ["snapshot_utc", "provider_id", "vehicle_type_id", "bfs_nr",
                "n_available", "n_reserved", "n_disabled"]
TRIP_FIELDS = ["vehicle_hash", "provider_id", "vehicle_type_id",
               "start_utc_min", "start_utc_max", "end_utc_max",
               "start_lat", "start_lon", "end_lat", "end_lon",
               "start_bfs_nr", "end_bfs_nr", "distance_m", "detection"]
RUN_FIELDS = ["run_utc", "feed_last_updated_utc", "feed_age_s", "n_vehicles",
              "n_assigned", "n_trips", "n_pending", "rotating_providers",
              "skipped", "runtime_s"]


# ------------------------------------------------------------------ helpers
def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def short_hash(text):
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def append_csv_gz(path, fields, rows):
    """Append rows to a gzip CSV (gzip members can simply be concatenated)."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with gzip.open(path, "at", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            w.writeheader()
        w.writerows(rows)


def fetch_feed(retries=4):
    for attempt in range(retries):
        try:
            r = requests.get(FEED_URL, headers=HEADERS, timeout=90)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            print(f"fetch attempt {attempt + 1} failed: {exc}", file=sys.stderr)
            if attempt == retries - 1:
                raise
            time.sleep(10 * (attempt + 1))


class MunicipalityLocator:
    def __init__(self, geojson_file):
        feats = json.loads(Path(geojson_file).read_text())["features"]
        self.geoms = [shape(f["geometry"]) for f in feats]
        self.bfs = [f["properties"]["bfs_nr"] for f in feats]
        self.tree = STRtree(self.geoms)

    def locate(self, lats, lons):
        """Vectorised point-in-polygon; returns BFS number or 0 (= unassigned)."""
        pts = [Point(lon, lat) for lat, lon in zip(lats, lons)]
        result = [0] * len(pts)
        pt_idx, geom_idx = self.tree.query(pts, predicate="within")
        for p, g in zip(pt_idx, geom_idx):
            result[p] = self.bfs[g]
        return result


def load_state(path):
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return {"feed_ts": None, "visible": {}, "pending": {}}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    tmp.replace(path)


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "raw"))
    ap.add_argument("--state", default=str(ROOT / "state" / "last_state.json.gz"))
    args = ap.parse_args()
    data_dir, state_file = Path(args.data_dir), Path(args.state)

    t0 = time.time()
    run_ts = int(t0)
    feed = fetch_feed()
    feed_ts = int(feed["last_updated"])
    bikes = feed["data"]["bikes"]
    state = load_state(state_file)
    day = iso(feed_ts)[:10]
    stamp = datetime.fromtimestamp(feed_ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def out(kind):  # one small file per run -> git-friendly (no growing files)
        return data_dir / kind / day / f"{kind}_{stamp}.csv.gz"
    run_row = dict(run_utc=iso(run_ts), feed_last_updated_utc=iso(feed_ts),
                   feed_age_s=run_ts - feed_ts, n_vehicles=len(bikes),
                   n_assigned="", n_trips=0, n_pending=len(state["pending"]),
                   rotating_providers="", skipped=0, runtime_s="")

    # Feed not updated since last run -> nothing new to store
    if state["feed_ts"] is not None and feed_ts <= state["feed_ts"]:
        run_row.update(skipped=1, runtime_s=round(time.time() - t0, 1))
        append_csv_gz(out("runs"), RUN_FIELDS, [run_row])
        print("feed unchanged, skipped")
        return

    locator = MunicipalityLocator(MUNI_FILE)
    bfs = locator.locate([b["lat"] for b in bikes], [b["lon"] for b in bikes])
    run_row["n_assigned"] = sum(1 for x in bfs if x)

    # ---------------- availability (aggregated)
    agg = {}
    for b, nr in zip(bikes, bfs):
        key = (b["provider_id"], b.get("vehicle_type_id") or "", nr)
        a = agg.setdefault(key, [0, 0, 0])
        if b.get("is_disabled"):
            a[2] += 1
        elif b.get("is_reserved"):
            a[1] += 1
        else:
            a[0] += 1
    avail_rows = [dict(snapshot_utc=iso(feed_ts), provider_id=p, vehicle_type_id=t,
                       bfs_nr=nr, n_available=a[0], n_reserved=a[1], n_disabled=a[2])
                  for (p, t, nr), a in sorted(agg.items(), key=lambda kv: str(kv[0]))]
    append_csv_gz(out("availability"), AVAIL_FIELDS, avail_rows)

    # ---------------- trip detection
    prev_ts = state["feed_ts"]
    prev_visible = state["visible"]      # id -> [lat, lon, bfs, type, provider]
    pending = state["pending"]           # id -> [lat, lon, bfs, type, provider, last_seen, first_missing]

    current = {}
    for b, nr in zip(bikes, bfs):
        if b.get("is_disabled"):
            continue
        current[b["bike_id"]] = [round(b["lat"], 5), round(b["lon"], 5), nr,
                                 b.get("vehicle_type_id") or "", b["provider_id"]]

    # providers that rotate IDs: most of their previous IDs vanished at once
    rotating = set(state.get("rotating", []))   # sticky once detected
    if prev_visible:
        seen, total = {}, {}
        for vid, v in prev_visible.items():
            total[v[4]] = total.get(v[4], 0) + 1
            if vid in current:
                seen[v[4]] = seen.get(v[4], 0) + 1
        rotating |= {p for p, n in total.items()
                     if n >= 5 and seen.get(p, 0) / n < ROTATING_ID_SHARE}
    run_row["rotating_providers"] = "|".join(sorted(rotating))

    def trip_row(vid, start, end, t_min, t_max, t_end, detection):
        return dict(vehicle_hash=short_hash(vid), provider_id=start[4],
                    vehicle_type_id=start[3], start_utc_min=iso(t_min),
                    start_utc_max=iso(t_max), end_utc_max=iso(t_end),
                    start_lat=start[0], start_lon=start[1], end_lat=end[0],
                    end_lon=end[1], start_bfs_nr=start[2], end_bfs_nr=end[2],
                    distance_m=round(haversine_m(start[0], start[1], end[0], end[1])),
                    detection=detection)

    trips = []
    if prev_ts is not None:
        for vid, cur in current.items():
            if cur[4] in rotating:
                continue
            if vid in pending:                       # re-appeared after a gap
                p = pending.pop(vid)
                if haversine_m(p[0], p[1], cur[0], cur[1]) >= MIN_TRIP_M:
                    trips.append(trip_row(vid, p, cur, p[5], p[6], feed_ts, "reappeared"))
            elif vid in prev_visible:                # visible in both snapshots
                pv = prev_visible[vid]
                if haversine_m(pv[0], pv[1], cur[0], cur[1]) >= MIN_TRIP_M:
                    trips.append(trip_row(vid, pv, cur, prev_ts, feed_ts, feed_ts, "moved"))
        # vehicles that disappeared now
        for vid, pv in prev_visible.items():
            if vid not in current and pv[4] not in rotating:
                pending[vid] = pv + [prev_ts, feed_ts]
        # forget vehicles that are missing for too long
        cutoff = feed_ts - MAX_PENDING_H * 3600
        pending = {k: v for k, v in pending.items() if v[5] >= cutoff}

    append_csv_gz(out("trips"), TRIP_FIELDS, trips)

    # ---------------- save state + run log
    visible = {k: v for k, v in current.items() if v[4] not in rotating}
    save_state(state_file, {"feed_ts": feed_ts, "visible": visible, "pending": pending,
                            "rotating": sorted(rotating)})
    run_row.update(n_trips=len(trips), n_pending=len(pending),
                   runtime_s=round(time.time() - t0, 1))
    append_csv_gz(out("runs"), RUN_FIELDS, [run_row])
    print(json.dumps(run_row))


if __name__ == "__main__":
    main()
