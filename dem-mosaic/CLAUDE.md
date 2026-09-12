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

## 2022 source rebuild (2026-09-09)

The first basin build used `SDE.DEM_BareEarth_LiDAR_2022`, a cross-zone resample at
0.728 x 0.676 m whose row/column moire hillshades as terracing. Replaced by the USGS OPR
tiles it came from, fed as two sources so each is resampled exactly once by this pipeline.

- Tiles live on the server at `\\vcenter2\GIS_DATA\LiDAR\2022\DEM\DEM_downloads`
  (16,762 tiles; 1,460 cover the basin, all present, none missing).
- Project CA_SierraNevada_B22: 903 tiles in NAD83(2011) UTM 10N (work units TahoeWest and
  ca_sierranevada_6, named `_bh_`), 557 in UTM 11N (TahoeEast, no `_bh_` infix).
- Both zones verified bare earth against the 2010 DEM: median difference -0.14 to +0.12 m,
  p90 under 0.61 m, essentially no points over 3 m. The naming difference means nothing.
- Both zones hydro-flattened at exactly 1897.89 m over the lake, within 5 cm of the water
  surface the 2010 lidar shows. Step 5 strips it.
- Tiles ship without raster statistics, so `Raster.minimum` is None; read pixels instead.
- The two work units are clipped at the 120th meridian with no overlap (394 usable points in
  500,000 for the east-vs-west check). Bilinear resampling loses ~half a cell per data edge, so
  a 1-2 cell strip along the meridian had no 2022 data. `edge_fill_cells: 2` on both halves
  fills NoData within two cells of data from the mean of valid neighbours, before the water strip.
- `scripts/build_2022_source.py` inventories the tiles and builds one mosaic dataset per zone
  in `C:\GIS\lidar2022.gdb`, referencing tiles in place. It sets each mosaic dataset's
  `resampling_type` from the config, because a new mosaic dataset defaults to NEAREST and that
  is the artifact this rebuild exists to remove.

## Run record: second basin build, OPR sources (2026-09-12)

`--force` from step 1, 21:24 to 05:55, 8.5 h: step 2 88 min, step 3 103, step 4 38, step 5 49
(with edge fill), step 6 170 (materialized feather chain; was 550), step 7 32, step 8 31.
Offsets vs zone-10 2022: 2010 -0.002 m, green -0.061 m, sonar +0.848 m (skin, IQR 4.7, TILT).
East-vs-west diagnostic starved again (361 usable of 500,000): the meridian overlap is a
sliver, and step 3 samples the step 2 rasters, before the edge fill. Area by source: zone 10
477 km2, zone 11 333, 2010 43 (was 51 with the old raster), green 33, sonar 466. 15 NoData
cells. Seam cells: median 3x3 range 0.12 m, p99 2.36 m, max 2.40 m (old raster: 3.86 / 4.94).
Results reproduced the lost 2026-09-11 build to within 0.2 km2 per source and 1 cm per offset.
Products exported 05:23 with run markers; scratch cleaned to 54.6 GB free.

## Incident 2026-09-11: mosaic dropped before export

The first basin run with the OPR sources (steps 5-8 resumed without --force) built the new
mosaic in scratch, but step 7 skipped the export because COGs from the 2026-09-08 build
already existed, and step 8 then dropped the scratch mosaic because "COGs exist". Nine hours
lost; the products on disk stayed the old ones. Fixes: run markers (`<prefix>mosaic_built.txt`,
`<prefix>mosaic_exported.txt` in the outputs folder) decide whether COGs came from the mosaic
in scratch; step 7 exports whenever they did not; step 8 and finalize refuse to drop an
unexported mosaic. Existence of a COG alone must never justify a drop.

Same run showed step 6 at 9.2 h with five sources against 3.2 h with four: the feather chain
referenced the accumulated raster three times per level and the lazy expression tripled per
source. Each level is now materialized to scratch (`dm_acc_<zone>_<level>`) and dropped after
the mosaic saves.

## Scratch hygiene (2026-09-10)

Basin intermediates are ~9 GB each and a run makes ~15 of them; the old basin run, two test
boxes, and the standalone 2022 DEM together filled the scratch drive and ProjectRaster died
with "Unspecified error". Now:

- Each step drops what the next step consumed once its own output is saved: `*_std` after
  `*_clean`, `*_clean` after the mosaic, the mosaic after its COG exists, QA temporaries after
  their CSV, and masks/polygons after step 8. Nothing is dropped until its consumer is on disk,
  so crashes still resume from the last completed step.
- Consequence: re-solving offsets (step 3) after step 5 means rerunning step 2.
- `--keep` retains everything. `--purge [PREFIX]` deletes by prefix and compacts; it refuses
  prefixes that do not start with `dm_`. Bare `dm_` clears every run including tests.
- Step 2 checks free space first and refuses to start if one raster cannot fit.
- `build_2022_dem.py` purges its own `dm_dem22_` prefix after exporting.
- Nothing in Scratch.gdb needs to survive between runs. The mosaic datasets live in
  `C:\GIS\lidar2022.gdb`, products and offsets in `C:\GIS\dem-mosaic`.

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
