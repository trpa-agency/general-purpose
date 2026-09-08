# dem-mosaic

Build a single bare-earth DEM (land + lake bottom) for the Tahoe Basin from four SDE rasters:
2022 and 2010 terrestrial LiDAR, 2020 green (topobathy) LiDAR, 1999 USGS multibeam sonar.
Per-zone priority with NoData fall-through; a source-ID raster records provenance.

## Layout

- `config.yaml` - all paths, per-source metadata, offset chain, priority lists, QA thresholds
- `scripts/build_dem_mosaic.py` - the pipeline. Steps 1-8, idempotent, resumable, `--test`, `--force`
- `notebooks/dem_mosaic.ipynb` - thin front end that imports the script; for looking at tables,
  not for the full-basin run
- `data/` - local copies of reference polygons if not pulling from SDE

## Runtime

- Runs on the GIS server. Scratch is `C:\GIS\Scratch.gdb` (shared); all intermediates carry
  the `dm_` prefix (`dm_test_` under `--test`). Final COGs go to `C:\GIS\dem-mosaic`.
- SDE connection files live in `F:\GIS\DB_CONNECT` (`Raster.sde` for DEMs, `Vector.sde` for
  boundaries). SDE is READ ONLY: Describe, list, and read. Never write, create, or delete
  anything through an .sde connection.
- Env: `arcgispro-py3`, needs Spatial Analyst.
- Full basin is ~1.3e9 cells per raster and takes hours. Always run `--steps 1-3 --test` first.

## Run record

First full-basin build completed 2026-09-08 (5.6 h: step 2 77 min, step 3 40 min, step 5 26 min,
step 6 191 min with 300 m water feather, step 7 38 min, step 8 41 min). Extent 737568 4288093
771053 4357403, 2.32e9 cells. Outputs in `C:\GIS\dem-mosaic`.

Solved offsets relative to 2022 lidar: 2010 -0.02 m, green -0.05 m, USGS bathy +1.17 m (from its
topo skin on land; consistent with NGVD29). Area by source: 2022 802 km2, 2010 51, green 33,
sonar 466. 15 NoData cells in the AOI.

Known weakness: the 1999 USGS grid is interpolated fill in the 0-13 m band (sonar minus green
grows ~2.9 m per 10 m of depth basin-wide, flat at Kings Beach). The green-to-sonar handoff
near 13 m depth is a 2-3 m step in places, ramped over 300 m. Proper fix needs the original
multibeam coverage polygon so the sonar can be restricted to surveyed cells.

## Design decisions (do not silently undo)

- Vertical datums are reconciled empirically in step 3: 2022 lidar is the reference, other
  sources are shifted by the negated median difference over stable ground along
  `qa.offset_chain`. Output datum = the 2022 product's datum. `vertical_offset_m: auto` in the
  config means "use the solved value"; a number overrides.
- Offsets are applied in step 5, not step 2, so changing one never forces a re-projection.
- Water surface in terrestrial lidar is detected from the data (median over the open lake), not
  from gauge readings. 2010 is hydro-flattened, 2022 is not; tolerances differ accordingly.
- The high-water lake polygon is a zone boundary and extent, not a hard mask, for terrestrial lidar.
- Bilinear resampling only. Never nearest neighbor on elevation.
- Logic lives in the script only. Notebook cells call `Pipeline.stepN_*`; do not fork code into them.
