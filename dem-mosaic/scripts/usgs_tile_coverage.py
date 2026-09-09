"""Which USGS OPR bare-earth tiles cover the Tahoe Basin, from the lidar-download link lists.

Usage (arcgispro-py3):
    python usgs_tile_coverage.py                      # writes outputs next to this repo's lidar-download lists
    python usgs_tile_coverage.py --buffer 500         # include tiles within 500 m of the AOI

Reads every 0_file_download_links_*.txt under lidar-download/DEM_downloads/raster_list, decodes each
tile's MGRS 1 km ID to a footprint in its own UTM zone, projects to NAD83 UTM 10N, and keeps tiles
that intersect the TRPA boundary union the lake high-water polygon (both read from Vector.sde,
read-only). Outputs, in dem-mosaic/data:
    usgs_2022_tiles_in_aoi.csv   tile id, work unit, zone, url  (the download list for the new 2022 source)
    usgs_2022_tiles_in_aoi.png   coverage map
    usgs_2022_tiles.gdb/tiles    footprints with in_aoi flag, for Pro
"""

import argparse
import re
from pathlib import Path

import arcpy
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import yaml

REPO = Path(__file__).resolve().parent.parent
LISTS = REPO.parent / "lidar-download" / "DEM_downloads" / "raster_list"
TILE_RE = re.compile(r"_(\d{2})S([A-Z]{2})(\d{2})(\d{2})\.tif$")
TILE_M = 1000.0

# MGRS 100 km square letters -> km offsets. Column letters cycle by zone set; row letters by zone parity.
COL_SETS = {1: "ABCDEFGH", 2: "JKLMNPQR", 0: "STUVWXYZ"}   # zone % 3
ROWS = "ABCDEFGHJKLMNPQRSTUV"                              # 20 letters, 2000 km cycle


def mgrs_to_utm(zone, sq, e2, n2, lat_band_north_m=4_000_000):
    """Lower-left corner (m) of a 1 km MGRS tile in its UTM zone. Latitude band S: northing ~3.5-4.5e6 m."""
    col = COL_SETS[zone % 3].index(sq[0]) + 1          # 1..8 -> 100..800 km
    row_off = 0 if zone % 2 else 5                       # even zones start row letters at 'F'
    row = (ROWS.index(sq[1]) - row_off) % 20             # 0..19 -> 0..1900 km within a 2000 km cycle
    easting = col * 100_000 + int(e2) * 1000
    northing = row * 100_000 + int(n2) * 1000
    # pick the 2000 km cycle that lands in latitude band S (northings roughly 3.5e6-4.5e6)
    while northing < 3_400_000:
        northing += 2_000_000
    return easting, northing


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(REPO / "config.yaml"))
    ap.add_argument("--buffer", type=float, default=200.0, help="metres beyond the AOI to still count a tile")
    args = ap.parse_args(argv)
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    out_dir = REPO / "data"
    out_dir.mkdir(exist_ok=True)

    # 1. Parse every list, dedupe by tile id (todo/ and todo/next/ repeat each other)
    rows = {}
    for txt in sorted(LISTS.rglob("0_file_download_links_*.txt")):
        unit = re.sub(r"^0_file_download_links_", "", txt.stem)
        for line in txt.read_text(encoding="utf-8", errors="ignore").splitlines():
            url = line.strip()
            m = TILE_RE.search(url)
            if not m:
                continue
            zone, sq, e2, n2 = int(m.group(1)), m.group(2), m.group(3), m.group(4)
            tid = f"{zone}S{sq}{e2}{n2}"
            if tid in rows:
                continue
            try:
                e, n = mgrs_to_utm(zone, sq, e2, n2)
            except ValueError:
                print(f"skip unparseable square {tid} ({unit})")
                continue
            rows[tid] = dict(tile=tid, unit=unit, zone=zone, easting=e, northing=n, url=url)
    tiles = pd.DataFrame(rows.values())
    print(f"{len(tiles)} unique tiles across {tiles['unit'].nunique()} work units")

    # 2. Footprints in UTM 10N
    sr10 = arcpy.SpatialReference(26910)
    gdb = out_dir / "usgs_2022_tiles.gdb"
    if not arcpy.Exists(str(gdb)):
        arcpy.management.CreateFileGDB(str(out_dir), gdb.name)
    fc = str(gdb / "tiles")
    arcpy.env.overwriteOutput = True
    arcpy.management.CreateFeatureclass(str(gdb), "tiles", "POLYGON", spatial_reference=sr10)
    for name, typ in (("tile", "TEXT"), ("unit", "TEXT"), ("zone", "SHORT"), ("url", "TEXT"), ("in_aoi", "SHORT")):
        arcpy.management.AddField(fc, name, typ, field_length=255 if typ == "TEXT" else None)
    srs = {10: arcpy.SpatialReference(26910), 11: arcpy.SpatialReference(26911)}
    with arcpy.da.InsertCursor(fc, ["SHAPE@", "tile", "unit", "zone", "url", "in_aoi"]) as cur:
        for r in tiles.itertuples():
            e, n = r.easting, r.northing
            pts = [arcpy.Point(e, n), arcpy.Point(e, n + TILE_M), arcpy.Point(e + TILE_M, n + TILE_M),
                   arcpy.Point(e + TILE_M, n), arcpy.Point(e, n)]
            poly = arcpy.Polygon(arcpy.Array(pts), srs[r.zone]).projectAs(sr10)
            cur.insertRow([poly, r.tile, r.unit, r.zone, r.url, 0])

    # 3. AOI = TRPA boundary union lake polygon, buffered; flag intersecting tiles
    b = cfg["boundaries"]
    aoi_parts = [arcpy.management.Project(b["trpa_boundary"], str(gdb / "trpa_boundary"), sr10),
                 arcpy.management.Project(b["lake_high_water"], str(gdb / "lake"), sr10)]
    merged = arcpy.management.Merge(aoi_parts, str(gdb / "aoi_merge"))
    aoi = arcpy.management.Dissolve(merged, str(gdb / "aoi"))
    aoi_buf = arcpy.analysis.Buffer(aoi, str(gdb / "aoi_buf"), f"{args.buffer} Meters", dissolve_option="ALL")
    lyr = arcpy.management.MakeFeatureLayer(fc, "tiles_lyr")
    arcpy.management.SelectLayerByLocation(lyr, "INTERSECT", aoi_buf)
    arcpy.management.CalculateField(lyr, "in_aoi", "1", "PYTHON3")
    arcpy.management.Delete(lyr)

    hit = pd.DataFrame(arcpy.da.TableToNumPyArray(fc, ["tile", "unit", "zone", "url", "in_aoi"]))
    hit["url"] = hit["url"].astype(str)
    inside = hit[hit["in_aoi"] == 1].sort_values(["unit", "tile"])
    inside.drop(columns="in_aoi").to_csv(out_dir / "usgs_2022_tiles_in_aoi.csv", index=False)
    print("\nTiles intersecting the AOI, by work unit and zone:")
    print(inside.groupby(["unit", "zone"]).size().to_string())
    print(f"\ntotal {len(inside)} tiles -> {out_dir / 'usgs_2022_tiles_in_aoi.csv'}")

    # 4. Map
    fig, ax = plt.subplots(figsize=(9, 11), dpi=120)
    colors = {u: c for u, c in zip(sorted(hit["unit"].unique()), plt.cm.tab20.colors)}
    with arcpy.da.SearchCursor(fc, ["SHAPE@", "unit", "in_aoi", "zone"]) as cur:
        for shp, unit, flag, zone in cur:
            ext = shp.extent
            if not (700_000 < ext.XMin < 820_000 and 4_250_000 < ext.YMin < 4_400_000):
                continue
            ax.add_patch(plt.Rectangle((ext.XMin, ext.YMin), ext.width, ext.height,
                                       facecolor=colors[unit] if flag else "none",
                                       edgecolor=colors[unit], linewidth=0.3, alpha=0.7 if flag else 0.4))
    for name, lw in (("aoi", 1.5), ("lake", 1.0)):
        with arcpy.da.SearchCursor(str(gdb / name), ["SHAPE@"]) as cur:
            for (shp,) in cur:
                for part in shp:
                    xs = [p.X for p in part if p]
                    ys = [p.Y for p in part if p]
                    ax.plot(xs, ys, color="black", linewidth=lw)
    for unit, c in colors.items():
        if unit in inside["unit"].values:
            ax.plot([], [], color=c, linewidth=6, label=f"{unit} ({(inside['unit'] == unit).sum()})")
    ax.legend(loc="lower left", fontsize=8, title="tiles in AOI")
    ax.set_xlim(725_000, 785_000)
    ax.set_ylim(4_280_000, 4_365_000)
    ax.set_aspect("equal")
    ax.set_title("USGS CA_SierraNevada_B22 OPR bare-earth tiles vs TRPA boundary + lake\n"
                 "filled = intersects AOI; outline = available but outside")
    fig.tight_layout()
    fig.savefig(out_dir / "usgs_2022_tiles_in_aoi.png")
    print(f"map -> {out_dir / 'usgs_2022_tiles_in_aoi.png'}")


if __name__ == "__main__":
    main()
