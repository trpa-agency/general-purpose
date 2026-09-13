"""Standalone 2022 bare-earth DEM from the USGS OPR mosaic datasets (both UTM zones merged).

Usage (arcgispro-py3, on the GIS server, after build_2022_source.py):
    python build_2022_dem.py                    # 1 m, NAD83 UTM 10N, basin AOI, lake left hydro-flattened
    python build_2022_dem.py --cell 0.5         # native resolution for the zone-10 half
    python build_2022_dem.py --strip-water      # null the flattened lake surface instead of keeping it
    python build_2022_dem.py --test             # on target.test_extent, outputs suffixed _test
    python build_2022_dem.py --force            # rebuild the projected halves even if present

Outputs (in outputs.dir from config.yaml)
    tahoe_dem_2022_<cell>m.tif        bare-earth DEM, float32 COG, NoData -9999
    tahoe_dem_2022_<cell>m_zone.tif   1 = zone-10 OPR tiles, 2 = zone-11 OPR tiles (projected), 3 = USGS 1 m patch
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

WEST, EAST, PATCH = "lidar_2022_west", "lidar_2022_east", "lidar_2022_1m"
ORDER = [WEST, EAST, PATCH]   # first valid wins; the 1 m patch only fills the meridian wedges


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
    if WEST not in p.active:
        raise SystemExit(f"{WEST} is not an active source in the config; run build_2022_source.py first")
    for k in (EAST, PATCH):
        if k not in p.active:
            log.warning("%s is not active (path missing?); the DEM will lack what only it covers", k)
    p.ensure_extent()                        # sets arcpy.env.extent, prunes sources outside it
    halves = [k for k in ORDER if k in p.active]
    log.info("standalone 2022 DEM: cell %g m, sources in priority order %s, prefix %s", cell, halves, p.pfx)

    # 1. Project each half once onto the shared grid (reuses the pipeline's step-2 code path)
    for key in halves:
        name = f"{key}_std"
        if p.stale(name):
            snap = None if key == WEST else p.snap
            p._standardize_one(key, p.active[key], snap)
        else:
            log.info("%s: %s exists, reusing", key, p.n(name))
    arcpy.env.snapRaster = p.snap

    # 2. Shifts relative to the zone-10 half, resolved exactly as the mosaic pipeline resolves
    #    them: a number in the config wins, 'auto' reads the pipeline's step-3 file.
    shifts = {k: 0.0 for k in halves}
    if len(halves) > 1:
        try:
            resolved = p.offsets()
            for k in halves[1:]:
                shifts[k] = float(resolved[k]) - float(resolved[WEST])
                log.info("%s: shift %+.3f m relative to %s (per config / resolved offsets)", k, shifts[k], WEST)
                if abs(shifts[k]) > 0.2:
                    log.warning("%s: shift %+.3f m is large for one project; check overlap_qa", k, shifts[k])
        except SystemExit as e:
            log.warning("%s; other sources used unshifted (expected shifts are ~0)", e)

    # 3. Merge in priority order: first valid wins. Each source is first grown into NoData by its
    #    edge_fill_cells so the strips bilinear resampling leaves along tile-set edges close.
    layers = []
    for k in halves:
        r = p.fill_edges(arcpy.Raster(p.path(f"{k}_std")), p.active[k].get("edge_fill_cells", 0), k)
        layers.append(r + shifts[k] if shifts[k] else r)
    dem = layers[-1]
    zone = Con(IsNull(layers[-1]), 0, len(layers))
    for i in range(len(layers) - 2, -1, -1):
        dem = Con(IsNull(layers[i]), dem, layers[i])
        zone = Con(IsNull(layers[i]), zone, i + 1)
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
        "zone raster: " + "; ".join(f"{i + 1} = {p.active[k]['label']}" for i, k in enumerate(halves))
        + " (lower number wins where several have data)",
        "edge fill (cells of NoData filled from the mean of valid neighbours, closes resampling strips "
        "along tile-set edges): " + ", ".join(f"{k} {p.active[k].get('edge_fill_cells', 0)}" for k in halves),
        "shifts applied relative to the zone-10 half: "
        + (", ".join(f"{k} {shifts[k]:+.3f} m" for k in halves[1:]) or "none"),
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
