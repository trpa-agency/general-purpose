"""Build the 2022 bare-earth sources for the mosaic pipeline from USGS OPR tiles on disk.

Usage (arcgispro-py3, on the GIS server):
    python build_2022_source.py --tiles \\\\vcenter2\\GIS_DATA\\LiDAR\\2022\\DEM\\DEM_downloads
    python build_2022_source.py --tiles <folder> --inventory-only     # just report present / missing
    python build_2022_source.py --tiles <folder> --gdb C:\\GIS\\lidar2022.gdb

What it does
    1. Walks --tiles recursively for USGS_OPR_*_<zone>S<sq><eeee>.tif files and keys them by tile ID.
    2. Compares with dem-mosaic/data/usgs_2022_tiles_in_aoi.csv (the 1,460 tiles that cover the
       TRPA boundary + lake). Writes data/usgs_2022_tiles_missing_urls.txt for the download notebook
       if anything is absent, and reports tiles present that are outside the AOI (ignored).
    3. Reads the real projection of each present AOI tile (the name's zone is not trusted) and
       samples a few for pixel type, vertical CRS, and value range (ground elevations at Tahoe are
       1,880 to 3,320 m; anything else means the wrong product).
    4. Creates one mosaic dataset per UTM zone in --gdb (a file gdb, never SDE) and adds the AOI
       tiles to it, at native 0.5 m, no resampling. These are the paths for lidar_2022_west /
       lidar_2022_east in config.yaml; the pipeline does the single resample to 1 m UTM 10N.

Idempotent: an existing mosaic dataset is dropped and rebuilt only with --force; otherwise the
script reports it and leaves it alone.
"""

import argparse
import re
import random
from collections import defaultdict
from pathlib import Path

import arcpy
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
AOI_CSV = REPO / "data" / "usgs_2022_tiles_in_aoi.csv"
TILE_RE = re.compile(r"_(\d{2}S[A-Z]{2}\d{4})\.tif$", re.IGNORECASE)
Z_OK = (1880.0, 3320.0)


def find_tiles(root):
    found = {}
    dupes = 0
    for p in Path(root).rglob("*.tif"):
        m = TILE_RE.search(p.name)
        if not m:
            continue
        tid = m.group(1).upper()
        if tid in found:
            dupes += 1
            continue
        found[tid] = p
    return found, dupes


def tile_zone(path):
    sr = arcpy.Describe(str(path)).spatialReference
    m = re.search(r"Zone_(\d{2})N", sr.name or "")
    return (int(m.group(1)) if m else None), sr


def sample_check(paths, n=4):
    out = []
    for p in random.sample(paths, min(n, len(paths))):
        r = arcpy.Raster(str(p))
        sr = r.spatialReference
        lo, hi = r.minimum, r.maximum
        ok = lo is not None and Z_OK[0] <= lo and hi <= Z_OK[1]
        out.append(dict(tile=p.name, crs=sr.name, vcs=(sr.VCS.name if sr.VCS else None), cell=r.meanCellWidth,
                        pixel=r.pixelType, zmin=lo, zmax=hi, nodata=r.noDataValue, plausible=ok))
    return pd.DataFrame(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tiles", required=True, help="folder holding the downloaded OPR tiles (searched recursively)")
    ap.add_argument("--gdb", default=r"C:\GIS\lidar2022.gdb", help="file gdb for the mosaic datasets")
    ap.add_argument("--inventory-only", action="store_true")
    ap.add_argument("--force", action="store_true", help="rebuild mosaic datasets that already exist")
    ap.add_argument("--limit", type=int, default=0, help="testing: add at most N tiles per zone")
    ap.add_argument("--aoi-csv", default=str(AOI_CSV), help="testing: alternate tile list")
    args = ap.parse_args(argv)

    aoi = pd.read_csv(args.aoi_csv)
    aoi["tile"] = aoi["tile"].str.upper()
    need = dict(zip(aoi["tile"], aoi["url"]))
    print(f"AOI needs {len(need)} tiles ({(aoi['zone'] == 10).sum()} named zone 10, {(aoi['zone'] == 11).sum()} zone 11)")

    found, dupes = find_tiles(args.tiles)
    print(f"found {len(found)} unique tiles under {args.tiles} ({dupes} duplicate names skipped)")
    present = {t: p for t, p in found.items() if t in need}
    missing = sorted(t for t in need if t not in found)
    extra = len(found) - len(present)
    print(f"present in AOI: {len(present)} | missing: {len(missing)} | present but outside AOI (ignored): {extra}")
    if missing:
        out = REPO / "data" / "usgs_2022_tiles_missing_urls.txt"
        out.write_text("\n".join(need[t] for t in missing) + "\n", encoding="ascii")
        by_unit = aoi[aoi["tile"].isin(missing)].groupby("unit").size()
        print("missing by work unit:\n" + by_unit.to_string())
        print(f"missing URLs -> {out}  (feed to lidar-download/usgs_lidar_download.ipynb, then rerun)")
    if not present:
        raise SystemExit("no AOI tiles present; nothing to build")

    # Real zone per tile, from the file, not the name
    by_zone = defaultdict(list)
    zone_sr = {}
    print("reading projections of present tiles...")
    for tid, p in present.items():
        z, sr = tile_zone(p)
        if z is None:
            print(f"  {p.name}: cannot read UTM zone from CRS '{sr.name}', skipped")
            continue
        by_zone[z].append(p)
        zone_sr.setdefault(z, sr)
    for z, paths in sorted(by_zone.items()):
        named = (aoi.set_index("tile").loc[[TILE_RE.search(p.name).group(1).upper() for p in paths], "zone"] == z).mean()
        print(f"  zone {z}: {len(paths)} tiles ({named*100:.0f}% also named in zone {z}), CRS {zone_sr[z].name}")

    for z, paths in sorted(by_zone.items()):
        print(f"\nsample check, zone {z}:")
        df = sample_check(paths)
        print(df.to_string(index=False))
        if not df["plausible"].all():
            print("  WARNING: value range outside Tahoe ground elevations on some tiles; check product type / units")
        if df["vcs"].isna().any():
            print("  note: some tiles carry no vertical CRS; datum is NAVD88 per the USGS project")

    if args.inventory_only:
        return

    gdb = Path(args.gdb)
    if not arcpy.Exists(str(gdb)):
        arcpy.management.CreateFileGDB(str(gdb.parent), gdb.name)
    arcpy.env.overwriteOutput = True
    for z, paths in sorted(by_zone.items()):
        name = f"opr_2022_utm{z}"
        md = str(gdb / name)
        if arcpy.Exists(md):
            if not args.force:
                print(f"\n{md} exists ({int(arcpy.management.GetCount(md)[0])} items); use --force to rebuild")
                continue
            arcpy.management.Delete(md)
        sr = arcpy.SpatialReference(6339 if z == 10 else 6340)   # NAD83(2011) UTM 10N / 11N
        if zone_sr[z].factoryCode and zone_sr[z].factoryCode != sr.factoryCode:
            sr = zone_sr[z]   # trust the tiles' own realization if it differs
        print(f"\ncreating {md} in {sr.name} with {len(paths) if not args.limit else min(args.limit, len(paths))} tiles")
        arcpy.management.CreateMosaicDataset(str(gdb), name, sr, num_bands=1, pixel_type="32_BIT_FLOAT")
        inputs = [str(p) for p in (paths[:args.limit] if args.limit else paths)]
        arcpy.management.AddRastersToMosaicDataset(
            md, "Raster Dataset", inputs,
            update_cellsize_ranges="UPDATE_CELL_SIZES", update_boundary="UPDATE_BOUNDARY",
            update_overviews="NO_OVERVIEWS", maximum_pyramid_levels=0,
            build_pyramids="NO_PYRAMIDS", calculate_statistics="NO_STATISTICS",
            duplicate_items_action="EXCLUDE_DUPLICATES", build_thumbnails="NO_THUMBNAILS",
            estimate_statistics="NO_STATISTICS")
        # First-source-wins ordering does not matter within one zone (tiles do not overlap); keep default
        n = int(arcpy.management.GetCount(md)[0])
        r = arcpy.Raster(md)
        print(f"  {name}: {n} items, cell {r.meanCellWidth} m, extent {r.extent.XMin:.0f} {r.extent.YMin:.0f} "
              f"{r.extent.XMax:.0f} {r.extent.YMax:.0f}")

    print("\nconfig.yaml source paths:")
    for z in sorted(by_zone):
        print(f"  lidar_2022_{'west' if z == 10 else 'east'}: path: {(gdb / f'opr_2022_utm{z}').as_posix()}")


if __name__ == "__main__":
    main()
