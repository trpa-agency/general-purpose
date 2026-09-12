"""Standalone 2022 bare-earth DEM from the USGS OPR mosaic datasets (both UTM zones merged).

Usage (arcgispro-py3, on the GIS server, after build_2022_source.py):
    python build_2022_dem.py                    # 1 m, NAD83 UTM 10N, basin AOI, lake left hydro-flattened
    python build_2022_dem.py --cell 0.5         # native resolution for the zone-10 half
    python build_2022_dem.py --strip-water      # null the flattened lake surface instead of keeping it
    python build_2022_dem.py --test             # on target.test_extent, outputs suffixed _test
    python build_2022_dem.py --force            # rebuild the projected halves even if present

Outputs (in outputs.dir from config.yaml)
    tahoe_dem_2022_<cell>m.tif        bare-earth DEM, float32 COG, NoData -9999
    tahoe_dem_2022_<cell>m_zone.tif   1 = zone-10 tiles (native grid), 2 = zone-11 tiles (projected)
    tahoe_dem_2022_<cell>m_hs.tif     hillshade for QA
    tahoe_dem_2022_<cell>m.txt        provenance: sources, offset applied, water handling, extent

How it is built
    Reuses the mosaic pipeline (build_dem_mosaic.Pipeline) for the AOI, projection, snapping,
    plausibility filter, and water-surface detection, so this product and the multi-source
    mosaic agree cell for cell where both use the 2022 lidar. The zone-10 mosaic dataset defines
    the grid (registered at integer meters); the zone-11 half is projected once, bilinear, snapped
    to it, and shifted by the east-vs-west offset solved in the pipeline's step 3 if that file
    exists (expected to be within a few centimetres of zero). Where both halves have data the
    zone-10 half wins, so the seam sits at the zone-10 tiles' eastern edge.

Intermediates go to the scratch gdb under the pipeline prefix plus "dem22_" (or "dem22_<cell>m_"
for a non-default cell) so they never collide with the mosaic run.
"""

import argparse
import datetime as dt
from pathlib import Path

import arcpy
from arcpy.sa import Con, IsNull, SetNull, Hillshade, Int

from build_dem_mosaic import Pipeline, log

WEST, EAST = "lidar_2022_west", "lidar_2022_east"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config.yaml"))
    ap.add_argument("--cell", type=float, default=None, help="output cell size in m (default: target.cell_size_m)")
    ap.add_argument("--strip-water", action="store_true",
                    help="null the hydro-flattened lake surface inside the high-water polygon")
    ap.add_argument("--test", action="store_true", help="run on target.test_extent, outputs suffixed _test")
    ap.add_argument("--force", action="store_true", help="rebuild projected halves and outputs")
    ap.add_argument("--keep", action="store_true", help="keep the projected halves and merge in the scratch gdb")
    args = ap.parse_args(argv)

    p = Pipeline(args.config, test=args.test, force=args.force, keep=args.keep)
    cell = float(args.cell or p.cell)
    default_cell = abs(cell - p.cell) < 1e-9
    p.pfx = p.pfx + ("dem22_" if default_cell else f"dem22_{cell:g}m_".replace(".", "p"))
    p.cell = cell
    arcpy.env.cellSize = cell
    p.snap_key = WEST                       # zone-10 half defines the grid
    for k in (WEST, EAST):
        if k not in p.active:
            raise SystemExit(f"{k} is not an active source in the config; run build_2022_source.py first")
    p.ensure_extent()                        # sets arcpy.env.extent, prunes sources outside it
    halves = [k for k in (WEST, EAST) if k in p.active]
    log.info("standalone 2022 DEM: cell %g m, halves %s, prefix %s", cell, halves, p.pfx)

    # 1. Project each half once onto the shared grid (reuses the pipeline's step-2 code path)
    for key in halves:
        name = f"{key}_std"
        if p.stale(name):
            snap = None if key == WEST else p.snap
            p._standardize_one(key, p.active[key], snap)
        else:
            log.info("%s: %s exists, reusing", key, p.n(name))
    arcpy.env.snapRaster = p.snap

    # 2. East-vs-west offset resolved exactly as the mosaic pipeline resolves it: a number in the
    #    config wins, 'auto' reads the pipeline's step-3 file. Keeps the two products consistent.
    offset = 0.0
    if EAST in halves:
        try:
            resolved = p.offsets()
            offset = float(resolved[EAST]) - float(resolved[WEST])
            log.info("east-vs-west offset %+.3f m (per config / resolved offsets)", offset)
        except SystemExit as e:
            log.warning("%s; east half used unshifted (expected offset is ~0)", e)
        if abs(offset) > 0.2:
            log.warning("east-vs-west offset %+.3f m is large for one product in two zones; "
                        "check overlap_qa before trusting this DEM", offset)

    # 3. Merge: zone-10 wins where both have data. Each half is first grown a couple of cells into
    #    NoData so the strip along the meridian that bilinear resampling leaves uncovered closes.
    west = p.fill_edges(arcpy.Raster(p.path(f"{WEST}_std")), p.active[WEST].get("edge_fill_cells", 0), WEST)
    if EAST in halves:
        east = p.fill_edges(arcpy.Raster(p.path(f"{EAST}_std")), p.active[EAST].get("edge_fill_cells", 0), EAST)
        if offset:
            east = east + offset
        dem = Con(IsNull(west), east, west)
        zone = Con(IsNull(west), Con(IsNull(east), 0, 2), 1)
    else:
        dem = west
        zone = Con(IsNull(west), 0, 1)
    zone = SetNull(zone == 0, Int(zone))

    # 4. Optional: remove the hydro-flattened lake surface
    water_note = "lake left hydro-flattened as delivered by USGS"
    if args.strip_water:
        ws, mad, n = p.water_surface_elev(WEST)
        if ws is None:
            log.warning("no open-lake samples in this extent; nothing stripped")
            water_note = "strip requested but no open-lake samples in extent"
        else:
            tol = float(p.active[WEST].get("water_strip_tol_m", 0.15))
            dem = SetNull((p.lake_r == 1) & (dem <= ws + tol), dem)
            water_note = (f"hydro-flattened surface stripped inside the lake high-water polygon at "
                          f"<= {ws + tol:.3f} m (detected {ws:.3f} m, MAD {mad:.3f} m, n={n})")
            log.info(water_note)

    dem_name, zone_name = p.path("dem2022"), p.path("dem2022_zone")
    dem.save(dem_name)
    zone.save(zone_name)

    # 5. Export
    sfx = "_test" if p.test else ""
    stem = f"tahoe_dem_2022_{cell:g}m{sfx}"
    out = p.out_dir
    dem_tif, zone_tif, hs_tif = (str(out / f"{stem}{e}.tif") for e in ("", "_zone", "_hs"))
    arcpy.management.CopyRaster(dem_name, dem_tif, nodata_value=p.nodata,
                                pixel_type=p.cfg["target"]["pixel_type"], format="COG")
    arcpy.management.CopyRaster(zone_name, zone_tif, nodata_value=0, pixel_type="8_BIT_UNSIGNED", format="COG")
    arcpy.management.BuildRasterAttributeTable(zone_tif, "Overwrite")
    Hillshade(dem_name, z_factor=1).save(hs_tif)

    r = arcpy.Raster(dem_tif)
    ext = p.ensure_extent()
    prov = "\n".join([
        f"{stem}: standalone 2022 bare-earth DEM, Lake Tahoe Basin",
        f"built {dt.datetime.now():%Y-%m-%d %H:%M} by build_2022_dem.py",
        f"grid: NAD83 UTM 10N, {cell:g} m cells registered at integer meters, "
        f"extent {ext.XMin:.0f} {ext.YMin:.0f} {ext.XMax:.0f} {ext.YMax:.0f}, {r.width} x {r.height} cells",
        f"vertical: {p.cfg['metadata']['vertical_datum']}",
        "sources: " + "; ".join(f"{p.active[k]['label']} -> {p.active[k]['path']}" for k in halves),
        f"zone raster: 1 = zone-10 tiles on their native grid (bilinear 0.5 m -> {cell:g} m), "
        f"2 = zone-11 tiles projected to UTM 10N (bilinear), zone 1 wins where both exist",
        f"edge fill: NoData within {p.active[WEST].get('edge_fill_cells', 0)} cell(s) of data filled from "
        f"the mean of valid neighbours (closes the resampling strip along the 120th meridian)",
        f"east-vs-west shift applied to zone-11 half: {offset:+.3f} m",
        f"water: {water_note}",
        f"plausible elevation filter: {p.z_lo} to {p.z_hi} m",
        f"NoData {p.nodata}; float32 COG, {p.cfg['target']['compression']}",
    ])
    (out / f"{stem}.txt").write_text(prov + "\n", encoding="utf-8")
    log.info("wrote %s, %s, %s and %s", dem_tif, zone_tif, hs_tif, out / f"{stem}.txt")
    # The COGs are the product; ~30 GB of scratch intermediates are now spent.
    if not p.keep:
        p.purge(p.pfx)


if __name__ == "__main__":
    main()
