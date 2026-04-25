"""
Build_EIP_ProjectLocations.py
Mason Bindl, Tahoe Regional Planning Agency

Pulls EIP project locations from two Lake Tahoe Info web services and builds
feature classes in C:\\GIS\\Scratch.gdb for a curated list of EIP project numbers.

Outputs (written to C:\\GIS\\Scratch.gdb):
    EIP_Projects_SimpleLocations     (point   - centroid + geospatial associations)
    EIP_Projects_DetailedPolygons    (polygon - project footprints, if any)
    EIP_Projects_DetailedLines       (polyline - project footprints, if any)
    EIP_Projects_DetailedPoints      (point   - project footprints, if any)

Raw JSON dumps (simple.json, detailed.geojson) and a run log are written next
to this script for debugging.

Uses the default ArcGIS Pro python environment:
    C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\envs\\arcgispro-py3\\python.exe
"""
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd
import requests
import arcpy

# ------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------
API_KEY = "e17aeb86-85e3-4260-83fd-a2b32501c476"
SIMPLE_URL = (
    f"https://www.laketahoeinfo.org/WebServices/"
    f"GetProjectSimpleLocationAndGeospatialAssociations/JSON/{API_KEY}"
)
DETAILED_URL = (
    f"https://www.laketahoeinfo.org/WebServices/"
    f"GetProjectDetailedLocationsAsFeatureCollection/JSON/{API_KEY}"
)

GDB_FOLDER = r"C:\GIS"
GDB_NAME = "Scratch.gdb"
GDB = os.path.join(GDB_FOLDER, GDB_NAME)

FC_SIMPLE = "EIP_Projects_SimpleLocations"
FC_DETAIL_POLY = "EIP_Projects_DetailedPolygons"
FC_DETAIL_LINE = "EIP_Projects_DetailedLines"
FC_DETAIL_POINT = "EIP_Projects_DetailedPoints"

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_PATH = SCRIPT_DIR / "Build_EIP_ProjectLocations.log"
SIMPLE_JSON_PATH = SCRIPT_DIR / "simple.json"
DETAILED_GEOJSON_PATH = SCRIPT_DIR / "detailed.geojson"

EIP_PROJECTS = [
    # Forest Health
    {"eip": "02.02.02.0015", "category": "Forest Health",
     "name": "Nevada Understory Burning Program"},
    {"eip": "02.01.01.0025", "category": "Forest Health",
     "name": "Nevada Urban Lot and Forest Enhancement"},
    {"eip": "02.01.01.0163", "category": "Forest Health",
     "name": "Tunnel Creek Hazardous Fuels Reduction"},
    {"eip": "02.01.01.0154", "category": "Forest Health",
     "name": "North Lake Tahoe Division: CWPP Implementation and Completion Project"},
    {"eip": "02.01.01.0124", "category": "Forest Health",
     "name": "NLTFPD Urban Defense Zone Hazardous Fuels Reduction"},
    # Watershed Restoration and Water Quality
    {"eip": "01.02.01.00.70", "category": "Watershed Restoration and Water Quality",
     "name": "Upper Truckee River Johnson Meadow Restoration Project"},
    {"eip": "01.01.01.0216", "category": "Watershed Restoration and Water Quality",
     "name": "Stormwater Management Tools for the Lake Tahoe Basin"},
    {"eip": "01.01.01.0045", "category": "Watershed Restoration and Water Quality",
     "name": "Kings Beach Watershed Improvement Project (Implementation)"},
    {"eip": "01.01.01.0194", "category": "Watershed Restoration and Water Quality",
     "name": "North Tahoe Recreational Access WQ Improvements (Implementation)"},
    {"eip": "01.01.01.0221", "category": "Watershed Restoration and Water Quality",
     "name": "Areawide Assessments and Drainage Master Planning"},
    # Other
    {"eip": "03.01.02.0122", "category": "Other",
     "name": "Emerald Bay Gateway Project (Planning and Environmental Documentation)"},
]

# EIP numbers with ambiguous source formatting -- try each candidate form.
EIP_AMBIGUOUS = {
    "01.02.01.00.70": ["01.02.01.00.70", "01.02.01.0070"],
}

# Projects referenced by the request that have no single EIP# -- skipped by design.
SKIPPED_PROJECTS = [
    "TRPA Sustainable Recreation Threshold Update (EIP = 'Various')",
]

# Expected API schema (verified against live endpoints on 2026-04-24):
#   Simple:  ProjectID, EIPProjectNumber, ProjectName, Latitude, Longitude, Datum,
#            Region, State, Jurisdiction, Watershed, NoLocation, Notes
#   Detailed (GeoJSON properties): ProjectID, EIPProjectNumber, ProjectName, Info
# Candidate lists below keep the code resilient to future key renames.
PROJECT_NUMBER_KEYS = ["EIPProjectNumber", "ProjectNumber", "projectNumber"]
PROJECT_NAME_KEYS = ["ProjectName", "projectName", "Name"]
LATITUDE_KEYS = ["Latitude", "latitude", "Lat", "Y"]
LONGITUDE_KEYS = ["Longitude", "longitude", "Lon", "Long", "X"]
ASSOCIATION_KEYS = {
    "Region": ["Region", "Regions"],
    "State": ["State"],
    "Jurisdiction": ["Jurisdiction", "Jurisdictions"],
    "Watershed": ["Watershed", "Watersheds"],
}

log = logging.getLogger("Build_EIP_ProjectLocations")


# ------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------
def setup_logging():
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)
    file_handler = logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)


def ensure_gdb():
    if arcpy.Exists(GDB):
        log.info(f"GDB exists: {GDB}")
    else:
        if not os.path.isdir(GDB_FOLDER):
            os.makedirs(GDB_FOLDER, exist_ok=True)
            log.info(f"Created folder: {GDB_FOLDER}")
        arcpy.management.CreateFileGDB(GDB_FOLDER, GDB_NAME)
        log.info(f"Created GDB: {GDB}")
    arcpy.env.overwriteOutput = True


def pick_key(d, candidates):
    """Return the first candidate key that appears in dict-like d, else None."""
    for k in candidates:
        if k in d:
            return k
    return None


def flatten_association(value):
    """Flatten a nested association (list of dicts / list of strings) into
    a pipe-delimited string. Returns '' for missing."""
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                name = item.get("Name") or item.get("name") or item.get("DisplayName")
                if name:
                    parts.append(str(name))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "|".join(parts)
    if isinstance(value, dict):
        name = value.get("Name") or value.get("name") or value.get("DisplayName")
        return str(name) if name else json.dumps(value, ensure_ascii=False)
    return str(value)


def expected_eip_set():
    """Return a set of all EIP candidate strings we want to match against the API."""
    s = set()
    for p in EIP_PROJECTS:
        eip = p["eip"]
        if eip in EIP_AMBIGUOUS:
            for c in EIP_AMBIGUOUS[eip]:
                s.add(c)
        else:
            s.add(eip)
    return s


# ------------------------------------------------------------------------
# Simple endpoint
# ------------------------------------------------------------------------
def fetch_simple():
    log.info(f"Fetching simple locations: {SIMPLE_URL}")
    resp = requests.get(SIMPLE_URL, timeout=60)
    resp.raise_for_status()
    SIMPLE_JSON_PATH.write_text(resp.text, encoding="utf-8")
    log.info(f"Wrote raw JSON to {SIMPLE_JSON_PATH} ({len(resp.text):,} bytes)")

    data = resp.json()
    if isinstance(data, dict):
        # Some endpoints wrap rows in a container object.
        for k in ("Results", "results", "Projects", "projects", "data"):
            if k in data and isinstance(data[k], list):
                data = data[k]
                break
    if not isinstance(data, list):
        log.warning(f"Unexpected simple-response shape (type={type(data).__name__}); "
                    "returning empty frame")
        return pd.DataFrame()

    df = pd.DataFrame(data)
    if df.empty:
        log.warning("Simple endpoint returned zero rows")
        return df
    log.info(f"Simple endpoint: {len(df):,} rows, columns={list(df.columns)}")
    log.info(f"First row sample: "
             f"{json.dumps(df.head(1).to_dict(orient='records')[0], default=str)[:1200]}")
    return df


def filter_simple(df):
    if df.empty:
        return df, []
    pn_key = pick_key(df.columns, PROJECT_NUMBER_KEYS)
    if pn_key is None:
        log.error(f"Could not find project-number column in: {list(df.columns)}")
        return df.iloc[0:0], [p["eip"] for p in EIP_PROJECTS]

    wanted = expected_eip_set()
    df[pn_key] = df[pn_key].astype(str)
    matched = df[df[pn_key].isin(wanted)].copy()

    got = set(matched[pn_key].tolist())
    missing = []
    for p in EIP_PROJECTS:
        eip = p["eip"]
        candidates = EIP_AMBIGUOUS.get(eip, [eip])
        hit = next((c for c in candidates if c in got), None)
        if hit is None:
            missing.append(p["eip"])
        elif eip in EIP_AMBIGUOUS and hit != eip:
            log.warning(f"Ambiguous EIP {eip!r} matched API as {hit!r} -- treat {hit!r} "
                        "as canonical for future runs")

    log.info(f"Filter: matched {len(matched)} of {len(EIP_PROJECTS)} expected; "
             f"missing={missing}")
    return matched, missing


def build_points_fc(df_matched):
    if df_matched.empty:
        log.warning("No matched simple records -- skipping simple point FC")
        return

    pn_key = pick_key(df_matched.columns, PROJECT_NUMBER_KEYS)
    name_key = pick_key(df_matched.columns, PROJECT_NAME_KEYS)
    lat_key = pick_key(df_matched.columns, LATITUDE_KEYS)
    lon_key = pick_key(df_matched.columns, LONGITUDE_KEYS)
    if lat_key is None or lon_key is None:
        log.error(f"Missing lat/long column; cannot build {FC_SIMPLE}. "
                  f"Columns: {list(df_matched.columns)}")
        return

    # Build category lookup from our constant list
    cat_lookup = {p["eip"]: p["category"] for p in EIP_PROJECTS}
    for eip, candidates in EIP_AMBIGUOUS.items():
        for c in candidates:
            cat_lookup.setdefault(c, cat_lookup.get(eip, ""))

    # Build flattened rows
    rows = []
    for _, r in df_matched.iterrows():
        pn = str(r.get(pn_key, ""))
        try:
            lat = float(r[lat_key])
            lon = float(r[lon_key])
        except (TypeError, ValueError):
            log.warning(f"Skipping {pn}: non-numeric lat/long ({r.get(lat_key)}, "
                        f"{r.get(lon_key)})")
            continue

        assoc_flat = {}
        for field, keys in ASSOCIATION_KEYS.items():
            k = pick_key(r.index, keys)
            assoc_flat[field] = flatten_association(r.get(k) if k else None)

        # Serialize the full record as JSON for full-fidelity retention
        assoc_json = json.dumps(
            {k: (v if not isinstance(v, (pd.Series,)) else v.tolist())
             for k, v in r.items()},
            default=str, ensure_ascii=False,
        )
        if len(assoc_json) > 9900:
            assoc_json = assoc_json[:9900] + "...TRUNCATED"

        rows.append({
            "EIPProjectNumber": pn[:20],
            "ProjectName": str(r.get(name_key, ""))[:255] if name_key else "",
            "Category": cat_lookup.get(pn, "")[:50],
            "Latitude": lat,
            "Longitude": lon,
            "Region": assoc_flat.get("Region", "")[:255],
            "State": assoc_flat.get("State", "")[:100],
            "Jurisdiction": assoc_flat.get("Jurisdiction", "")[:255],
            "Watershed": assoc_flat.get("Watershed", "")[:255],
            "GeospatialAssociationsJSON": assoc_json,
        })

    # Create FC with explicit schema
    out_fc = os.path.join(GDB, FC_SIMPLE)
    if arcpy.Exists(out_fc):
        arcpy.management.Delete(out_fc)
    sr = arcpy.SpatialReference(4326)
    arcpy.management.CreateFeatureclass(
        out_path=GDB, out_name=FC_SIMPLE, geometry_type="POINT",
        spatial_reference=sr,
    )
    schema = [
        ("EIPProjectNumber", "TEXT", 20),
        ("ProjectName", "TEXT", 255),
        ("Category", "TEXT", 50),
        ("Latitude", "DOUBLE", None),
        ("Longitude", "DOUBLE", None),
        ("Region", "TEXT", 255),
        ("State", "TEXT", 100),
        ("Jurisdiction", "TEXT", 255),
        ("Watershed", "TEXT", 255),
        ("GeospatialAssociationsJSON", "TEXT", 10000),
    ]
    for fname, ftype, flen in schema:
        if ftype == "TEXT":
            arcpy.management.AddField(out_fc, fname, ftype, field_length=flen)
        else:
            arcpy.management.AddField(out_fc, fname, ftype)

    cursor_fields = ["SHAPE@XY"] + [s[0] for s in schema]
    with arcpy.da.InsertCursor(out_fc, cursor_fields) as ic:
        for row in rows:
            ic.insertRow([
                (row["Longitude"], row["Latitude"]),
                row["EIPProjectNumber"],
                row["ProjectName"],
                row["Category"],
                row["Latitude"],
                row["Longitude"],
                row["Region"],
                row["State"],
                row["Jurisdiction"],
                row["Watershed"],
                row["GeospatialAssociationsJSON"],
            ])
    log.info(f"Wrote {len(rows):,} points to {out_fc}")


# ------------------------------------------------------------------------
# Detailed endpoint
# ------------------------------------------------------------------------
def fetch_detailed():
    log.info(f"Fetching detailed locations: {DETAILED_URL}")
    resp = requests.get(DETAILED_URL, timeout=120)
    resp.raise_for_status()
    DETAILED_GEOJSON_PATH.write_text(resp.text, encoding="utf-8")
    log.info(f"Wrote raw GeoJSON to {DETAILED_GEOJSON_PATH} ({len(resp.text):,} bytes)")

    fc = resp.json()
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection":
        log.warning(f"Detailed endpoint did not return a GeoJSON FeatureCollection "
                    f"(got type={fc.get('type') if isinstance(fc, dict) else type(fc).__name__})")
        return None
    features = fc.get("features") or []
    log.info(f"Detailed endpoint: {len(features):,} features fetched")
    return fc


def filter_detailed(fc):
    if fc is None or not fc.get("features"):
        return None, []
    wanted = expected_eip_set()

    # Discover the project-number property key from the first feature that has one
    pn_key = None
    for f in fc["features"]:
        props = f.get("properties") or {}
        k = pick_key(props, PROJECT_NUMBER_KEYS)
        if k is not None:
            pn_key = k
            break
    if pn_key is None:
        log.error("Could not find project-number property on any detailed feature; "
                  "cannot filter")
        return None, [p["eip"] for p in EIP_PROJECTS]
    log.info(f"Detailed features use property key {pn_key!r} for EIP #")

    filtered = []
    for f in fc["features"]:
        props = f.get("properties") or {}
        pn = str(props.get(pn_key, ""))
        if pn in wanted:
            filtered.append(f)

    got = {str((f.get("properties") or {}).get(pn_key, "")) for f in filtered}
    missing = []
    for p in EIP_PROJECTS:
        candidates = EIP_AMBIGUOUS.get(p["eip"], [p["eip"]])
        if not any(c in got for c in candidates):
            missing.append(p["eip"])

    # Geometry-type histogram
    hist = {}
    for f in filtered:
        g = f.get("geometry") or {}
        t = g.get("type", "None")
        hist[t] = hist.get(t, 0) + 1
    log.info(f"Detailed filter: matched {len(filtered)} features "
             f"(by EIP match, multiple features per project are expected); "
             f"missing EIPs={missing}; geometry histogram={hist}")

    out = {"type": "FeatureCollection", "features": filtered}
    return out, missing


POLY_TYPES = {"Polygon", "MultiPolygon"}
LINE_TYPES = {"LineString", "MultiLineString"}
POINT_TYPES = {"Point", "MultiPoint"}


def _split_by_geom(filtered_fc):
    polys, lines, points, other = [], [], [], []
    for f in filtered_fc["features"]:
        t = (f.get("geometry") or {}).get("type")
        if t in POLY_TYPES:
            polys.append(f)
        elif t in LINE_TYPES:
            lines.append(f)
        elif t in POINT_TYPES:
            points.append(f)
        else:
            other.append(t)
    if other:
        log.warning(f"Skipped {len(other)} features with unsupported geometry types: "
                    f"{sorted(set(other))}")
    return polys, lines, points


def _geojson_to_fc(features, geom_type, fc_name):
    """Write a bucket of GeoJSON features to a temp file and convert to a gdb FC.
    geom_type: 'POLYGON' | 'POLYLINE' | 'POINT'."""
    if not features:
        log.info(f"No {geom_type} features -- skipping {fc_name}")
        # If an older FC exists from a prior run with features, leave it; otherwise noop
        return
    out_fc = os.path.join(GDB, fc_name)
    if arcpy.Exists(out_fc):
        arcpy.management.Delete(out_fc)

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False,
                                      encoding="utf-8")
    try:
        json.dump({"type": "FeatureCollection", "features": features}, tmp,
                  ensure_ascii=False)
        tmp.close()
        try:
            # JSONToFeatures auto-detects GeoJSON in Pro 2.9+. Use it as the
            # primary path; fall back to geopandas -> GPKG for older installs.
            arcpy.conversion.JSONToFeatures(tmp.name, out_fc, geometry_type=geom_type)
            count = int(arcpy.management.GetCount(out_fc).getOutput(0))
            log.info(f"JSONToFeatures -> {out_fc} ({count:,} features)")
        except Exception as primary_err:
            log.warning(f"JSONToFeatures failed for {fc_name} ({primary_err}); "
                        "using geopandas GPKG fallback")
            try:
                import geopandas as gpd
                gdf = gpd.read_file(tmp.name)
                gpkg = tmp.name.replace(".geojson", ".gpkg")
                gdf.to_file(gpkg, layer=fc_name, driver="GPKG")
                src = os.path.join(gpkg, fc_name)
                arcpy.conversion.ExportFeatures(src, out_fc)
                count = int(arcpy.management.GetCount(out_fc).getOutput(0))
                log.info(f"GPKG fallback -> {out_fc} ({count:,} features)")
            except Exception:
                log.exception(f"Fallback path also failed for {fc_name}")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def build_detailed_fcs(filtered_fc):
    if filtered_fc is None or not filtered_fc.get("features"):
        log.warning("No filtered detailed features -- skipping detailed FCs")
        return
    polys, lines, points = _split_by_geom(filtered_fc)
    _geojson_to_fc(polys, "POLYGON", FC_DETAIL_POLY)
    _geojson_to_fc(lines, "POLYLINE", FC_DETAIL_LINE)
    _geojson_to_fc(points, "POINT", FC_DETAIL_POINT)


# ------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------
def main():
    setup_logging()
    t0 = time.time()
    log.info("=" * 70)
    log.info(f"Build_EIP_ProjectLocations starting "
             f"({len(EIP_PROJECTS)} EIP projects to match)")
    for s in SKIPPED_PROJECTS:
        log.info(f"Skipped by design (no EIP#): {s}")

    try:
        ensure_gdb()

        # Simple (point) endpoint
        df_simple = fetch_simple()
        df_matched, simple_missing = filter_simple(df_simple)
        build_points_fc(df_matched)

        # Detailed (polygon/line/point footprints)
        fc = fetch_detailed()
        filtered, detailed_missing = filter_detailed(fc) if fc else (None, [])
        build_detailed_fcs(filtered)

        log.info("-" * 70)
        log.info("Summary")
        log.info(f"  Simple FC missing EIPs : {simple_missing}")
        log.info(f"  Detailed missing EIPs  : {detailed_missing}")
        log.info(f"  Elapsed                : {time.time() - t0:.1f}s")
        log.info("Done.")
    except Exception:
        log.exception("Pipeline failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
