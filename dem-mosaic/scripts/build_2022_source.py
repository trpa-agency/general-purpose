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
import numpy as np
import pandas as pd
import yaml
from arcpy.sa import ExtractMultiValuesToPoints

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
    """Per-tile facts read from pixels, not metadata: OPR tiles often ship without statistics."""
    out = []
    for p in random.sample(paths, min(n, len(paths))):
        r = arcpy.Raster(str(p))
        sr = r.spatialReference
        a = arcpy.RasterToNumPyArray(r, nodata_to_value=np.nan).astype(float)
        v = a[np.isfinite(a)]
        row = dict(tile=p.name, crs_zone=re.search(r"Zone_(\d+[NS])", sr.name or "").group(1) if sr.name else "?",
                   vcs=(sr.VCS.name if sr.VCS else None), cell=r.meanCellWidth, pixel=r.pixelType,
                   nodata_pct=round(100 * (1 - v.size / a.size), 1))
        if v.size:
            # A hydro-flattened tile holds one value over a large share of its area
            vals, counts = np.unique(np.round(v, 3), return_counts=True)
            modal, modal_share = float(vals[counts.argmax()]), counts.max() / v.size
            row.update(zmin=round(float(v.min()), 2), zmax=round(float(v.max()), 2),
                       modal_z=modal if modal_share > 0.02 else None,
                       modal_pct=round(100 * modal_share, 1),
                       plausible=bool(Z_OK[0] <= v.min() and v.max() <= Z_OK[1]))
        else:
            row.update(zmin=None, zmax=None, modal_z=None, modal_pct=None, plausible=False)
        out.append(row)
    return pd.DataFrame(out)


def verify_bare_earth(paths, ref_dem, n_tiles=3, n_pts=300):
    """Compare sample tiles against the known bare-earth 2010 DEM.

    Bare earth vs bare earth differs by centimetres to a metre or two (real change since 2010).
    A first-return or surface model sits metres above it under forest, with a long positive tail.
    """
    rows = []
    scratch = arcpy.env.scratchGDB
    for i, p in enumerate(random.sample(paths, min(n_tiles, len(paths)))):
        r = arcpy.Raster(str(p))
        pts = arcpy.management.CreateRandomPoints(scratch, f"bechk_{i}", "", r.extent, n_pts)
        arcpy.management.DefineProjection(pts, r.spatialReference)
        ExtractMultiValuesToPoints(pts, [[str(p), "ztile"], [ref_dem, "zref"]])
        with arcpy.da.SearchCursor(pts, ["ztile", "zref"]) as cur:
            d = pd.DataFrame([x for x in cur if None not in x], columns=["ztile", "zref"]).astype(float)
        arcpy.management.Delete(pts)
        if len(d) < 20:
            rows.append(dict(tile=p.name, n=len(d), note="too few overlapping points"))
            continue
        dz = d["ztile"] - d["zref"]
        rows.append(dict(tile=p.name, n=len(d), median_dz=round(float(dz.median()), 2),
                         p90_dz=round(float(dz.quantile(0.9)), 2), max_dz=round(float(dz.max()), 2),
                         pct_over_3m=round(100 * float((dz > 3).mean()), 1)))
    return pd.DataFrame(rows)


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

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    ref_dem = cfg["sources"]["lidar_2010"]["path"]
    have_ref = arcpy.Exists(ref_dem)
    if not have_ref:
        print(f"\nnote: {ref_dem} unreachable; skipping the bare-earth comparison")
    for z, paths in sorted(by_zone.items()):
        print(f"\nsample check, zone {z} (statistics read from pixels):")
        df = sample_check(paths)
        print(df.to_string(index=False))
        if not df["plausible"].all():
            print("  WARNING: values outside Tahoe ground elevations (1,880-3,320 m); check product type / units")
        if df["modal_z"].notna().any():
            print("  one value covers a large share of some tiles: hydro-flattened water surface, "
                  "which step 5 of the pipeline strips")
        if have_ref:
            print(f"  bare-earth check against {cfg['sources']['lidar_2010']['label']}:")
            print("  " + verify_bare_earth(paths, ref_dem).to_string(index=False).replace("\n", "\n  "))
            print("  (bare earth: median near 0, few points over 3 m. A surface model sits metres higher "
                  "under forest.)")

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
        # A new mosaic dataset resamples NEAREST by default. That is the artifact this rebuild
        # exists to remove, so set it to the method this source uses in the pipeline.
        key = f"lidar_2022_{'west' if z == 10 else 'east'}"
        method = str(cfg["sources"].get(key, {}).get("resampling", "BILINEAR")).upper()
        arcpy.management.SetMosaicDatasetProperties(md, resampling_type=method)
        # First-source-wins ordering does not matter within one zone (tiles do not overlap); keep default
        n = int(arcpy.management.GetCount(md)[0])
        r = arcpy.Raster(md)
        print(f"  {name}: {n} items, cell {r.meanCellWidth} m, resampling {method}, "
              f"extent {r.extent.XMin:.0f} {r.extent.YMin:.0f} {r.extent.XMax:.0f} {r.extent.YMax:.0f}")

    print("\nconfig.yaml source paths:")
    for z in sorted(by_zone):
        print(f"  lidar_2022_{'west' if z == 10 else 'east'}: path: {(gdb / f'opr_2022_utm{z}').as_posix()}")


if __name__ == "__main__":
    main()
