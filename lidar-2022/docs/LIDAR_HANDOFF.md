# 2022 LiDAR: what we have, what the code does, and what comes next

Handoff note for working in `plot-network-design` from Claude Code. Written 2026-09-09. Everything below was verified on synthetic data only; the real archive has not been processed yet.

## Where the code lives

Two repos, split 2026-09-09:

- `trpa-agency/general-purpose/lidar-2022/` (this folder): all base LiDAR processing. 00a, 00_lidar, 00b, `src/lidar.py`, `src/taos.py`, `scripts/run_lidar.py`, `scripts/segment_trees_lidr.R`, the runbook, and this note. Work on a branch (existing: `main`, `Mason's-edits`, `Amy's-edits`, `mtb-edits-arc10`), merge to `main` once the real pass has run clean.
- `trpa-agency/ForestHealth/plot-network-design/`: everything downstream. 01_frame to 04_evaluate, `src/strata.py`, `src/qa.py`, `scripts/grts_draw.R`, `PLAN.md`, `docs/METHODS.md`, `docs/INPUTS_OUTPUTS.md`. Its `config.yaml` `sources:` point at `\\vcenter2\GIS_DATA\LiDAR\2022\Derived`, so the contract between the two repos is the product names listed under "Products and who uses them" below. Rename a product only in both places.
- `ForestHealth` today holds the threshold update analysis (RRK notebooks, GitHub Pages visualizations at trpa-agency.github.io/ForestHealth) at its root; `plot-network-design/` sits beside them as a subfolder with its own `CLAUDE.md` and `config.yaml`.
- Both folders carry their own copy of `src/io.py` and `src/layers.py` (small, stable helpers); `run.synthetic` exists in both and each has its own synthetic mode.
- `general-purpose` root `.gitignore` already blocks `*.tif`, `*.laz`, `*.gdb/`, `*.sde`, `.env`, `outputs/`, `*.log`, and `.claude/`. `ForestHealth` root `.gitignore` only covers `__pycache__`; the folder-level `.gitignore` in `plot-network-design/` blocks `data/`, `outputs/`, `logs/`, and `.env`, so keep it.
- `dem-mosaic/` in `general-purpose` is the model for how a server pipeline is laid out: `CLAUDE.md` at the folder root, `config.yaml` with every path, logic in scripts, notebooks as thin front ends. Both folders follow it.
- Facts from `dem-mosaic` that matter here: SDE connection files live in `F:\GIS\DB_CONNECT` (`Raster.sde`, `Vector.sde`) and are read only. `Raster.sde` holds `SDE.DEM_BareEarth_LiDAR_2022`, the vendor 2022 bare earth at 0.73 x 0.68 m with the vertical datum not recorded (likely NAVD88 GEOID18, unconfirmed). `C:\GIS\dem-mosaic` holds the finished 1 m land plus lakebed DEM built 2026-09-08. The server's shared scratch is `C:\GIS\Scratch.gdb`, prefixed per project.
- Implication for 00a: the vendor bare earth already exists. 00a still builds its own DTM per tile so height normalization is internally consistent with the points, but the SDE raster is the cross-check: difference the two over stable ground and log the median and spread. If they agree within a few cm, `Derived\dtm_1m_tiles` is redundant for downstream use and the ForestHealth `sources.dem` can point at the SDE product instead.
- `lidar-download/` in `general-purpose` is the USGS National Map LAZ downloader, and its link lists confirm the archive is the USGS 3DEP delivery `CA_SierraNevada_B22` (work units `_8_2022`, 5,887 tiles, and `_5_2022`). Tile names are USNG 1 km cells and split across UTM zones 10 and 11 (251 vs 5,636), so the delivery CRS is probably UTM 11N rather than the 26910 assumed here. See "The archive" in `CLAUDE.md`; the pre-flight header check settles it.

## The archive

- Location: `\\vcenter2\GIS_DATA\LiDAR\2022\LAZ`, about 2 TB of LAZ on an external drive attached to the network, about 1 TB free on that drive.
- 2 TB is far more than Basin coverage at 8 to 30 pts/m2 implies. We do not yet know what the drive actually holds (extra flightlines, duplicate deliverables, tiles outside the Basin, or a denser acquisition than expected). The 00a pre-flight (drive inventory plus header index, minutes, no point reads) answers that before any heavy pass.
- Unknowns to resolve in the pre-flight: tile count and naming, point density, CRS (everything downstream assumes EPSG:26910), whether ground (class 2) is classified, and what share of tiles fall inside the AOI.
- Expected CRS is NAD83 UTM 10N. Mixed CRS in one run is not supported; reproject first with `las2las` or Pro's Extract LAS.
- A 2026 acquisition may happen but is not confirmed. The design has to work with one or two epochs. If 2026 lands, 2022 vs 2026 differencing gives a Basin-wide structural change layer and tree-level change from TAOs.

## Compute and layout

- Processing runs on a 32 GB RAM server that can reach the share, not on the workstation. Python is `arcgispro-py3`.
- Code runs from `lidar-2022\` inside the server's clone of `general-purpose`.
- Staging scratch is `C:\lidar_scratch` on the server's own disk. Never stage on the external drive; reading and writing the same slow link doubles traffic.
- Partials, CHM tiles, DTM tiles, and TAO tiles stay local under `data/processed` and are the resume state.
- Final products publish to `\\vcenter2\GIS_DATA\LiDAR\2022\Derived`.
- Height-normalized LAZ copies of every AOI tile go to `\\vcenter2\GIS_DATA\LiDAR\2022\LAZ_Basin_HAG`. This is the one output worth the drive space: every future metric, scan calibration, and a 2026 change layer start from these without redoing the DTM.
- Local disk needed: about 20 GB scratch (3 workers times the largest tile) plus about 5 GB for CHM tiles and partials, plus TAO tiles. The 30 m rasters are a few hundred MB.

## Sizing

- `lidar.workers: 3`, `lidar.in_memory_max_points: 40000000`. About 2.5 to 3 GB per worker in the in-memory path. Raise to 4 workers only if RAM stays under 20 GB during the first ten tiles.
- Tiles over 40 M points fall back automatically to a chunked two-pass path.
- Merge histogram is about `rows x cols x 160 x 4` bytes, roughly 1 to 2 GB Basin-wide. The notebook logs it before starting.
- Throughput is read-bound at roughly 60 MB/s off the share. 200 GB of AOI tiles is about an hour per pass; 2 TB is nine to ten hours. Per-tile CPU is under a minute for a 40 M point tile. If the log's projection is unacceptable, `robocopy /MT` the AOI tiles to local disk and point `lidar.las_dir` there; the code path is identical.
- `taos.write_polygons: false` is the fast path if only the tree table is needed. Expect 10 to 35 M trees Basin-wide.

## Processing chain

Order: `00a_las_to_chm` -> `00_lidar` -> `00b_taos` -> `01_frame` -> `02_strata` -> `03_allocate_draw` -> `04_evaluate`. Notebooks are generated from `scripts/build_*_notebook.py`; edit the builders, not the `.ipynb` files.

### 00a_las_to_chm (`src/lidar.py`)

Pre-flight (sections 1 and 2, minutes): `drive_inventory` by folder and extension with no file opens; `header_index` giving extent, count, density, CRS per tile; AOI filter by header bounds then polygon; `sample_classification` on 25 random tiles for ground share; CRS check. Writes `outputs/drive_inventory.csv` and `outputs/las_tile_index.gpkg`.

Heavy pass, per tile in a multiprocessing pool (`process_tile`):

1. Copy tile to local scratch (one network read), delete after.
2. Decompress once in memory if under `in_memory_max_points`, else chunked two-pass.
3. Drop noise classes 7 and 18, drop returns above `max_hag_m` (80 m). Drop classes 6, 9, and 17 (building, water, bridge) from canopy metrics.
4. DTM from class 2: mean elevation per 1 m cell, nearest-fill for holes, 3x3 smooth. Written as a 1 m bare-earth tile. Working surface, not a hydro-flattened engineering DEM. Needs `min_ground_points_per_tile: 1000`.
5. Height above ground per return.
6. 1 m CHM tile: max first-return HAG, pits filled.
7. Optional height-normalized LAZ copy. In-memory path keeps z and adds a `HeightAboveGround` float32 extra dim (`normalized_write_extra_dim: true`); chunked path writes z = HAG. The log records which path each tile took. Set the flag false if every tile must have z = HAG.
8. Partial `.npz` with 30 m counts plus a 0.5 m height histogram (0 to 80 m), stored as offsets from `lidar.grid_origin`.

Finished tiles (partial plus CHM tile plus DTM tile present) are skipped on rerun. Delete a tile's `.npz` to redo it.

`merge_partials` sums partials exactly (histograms make p95 exact to 0.5 m and merge across tile overlaps) and writes the 30 m rasters: cover above 2 m (first returns above 2 m over all first returns, the standard point-based cover), cover above 5 m, p95 height, mid-canopy fraction 2 to 8 m, return density, ground density, and solid fraction (single-return share above 2 m, used to screen roofs and rock; cells above `frame.max_solid_fraction: 0.85` are dropped in 01_frame).

Publish step copies metrics, CHM tiles plus VRT, DTM tiles plus VRT, tile index, log, and config snapshot to `Derived`.

### 00_lidar

From the 1 m CHM tiles (via VRT), in grid-aligned blocks: CHM-based cover 2 m and 5 m, p95 (written under `_chm_` names for comparison with the point-based versions), rumple, and individual-tree-detection count per acre (Gaussian smooth sigma 1 m, local maxima with height-dependent window `{5: 3, 15: 5, 25: 7, 999: 9}`, min tree height 3 m). Writes `outputs/lidar_checks.png`. Point-based cover from 00a is what 01_frame reads; the `_chm_` versions stand in only if 00a did not run.

### 00b_taos (`src/taos.py`)

Per CHM tile, read with a 25 px pad from neighbours through the VRT (without GDAL, no padding and edge crowns may clip; arcgispro-py3 has GDAL). `detect_tops` uses the same smoothing and window rule as 00_lidar so the two agree. `segment_crowns` runs marker-controlled watershed on the inverted smoothed CHM masked at 3 m, trims crown pixels below half the top height, drops crowns under 2 m2. A crown is kept by the tile owning its top. `crown_table` writes per-tile GeoParquet points (`tree_id, x, y, height_m, crown_area_m2, crown_mean_h_m, crown_diam_m, tile`) and crown polygons (GeoPackage). `merge_trees` builds `taos_trees_2022.parquet`; `tao_density_raster` writes `lidar2022_tao_per_ac_30m.tif` on the shared grid. Parallel and resumable like 00a (delete `_trees.parquet` to redo a tile). Publishes to `Derived\taos`.

Known limits: CHM watershed merges dense fir crowns and splits big pines; suppressed stems under closed canopy are not detected. TAO count is detected-tree density, not the standard's TPA. Synthetic check: TAO vs ITD correlation 0.79.

### scripts/segment_trees_lidr.R

Second pass on `LAZ_Basin_HAG` with lidR (`lmf` with the same height-dependent window, `dalponte2016` with crown floor 0.5, 25 m chunk buffer). Run on plot footprints and a validation sample first; go Basin-wide only if it validates better than the CHM pass. Usage: `Rscript scripts/segment_trees_lidr.R <laz_dir> <out_dir> [aoi.gpkg] [workers]`.

### scripts/run_lidar.py

Headless runner: executes 00a, then 00_lidar with `--with-chm-metrics`, then 00b_taos with `--with-taos`. Default timeout 7 days. Run under Task Scheduler ("run whether user is logged on or not") or `start /b` in an RDP session that stays open.

## Config keys that must be set before the heavy pass

- `run.synthetic: false` (currently true).
- `lidar.grid_origin`: set to Shengli's covariate raster origin. Partials store offsets from it, so changing it later means rerunning the tiles, not just the merge. Current value `[730000, 4290000]` is a placeholder.
- `lidar.aoi`: Basin boundary or CWHR layer, file or TRPA REST URL. Tiles whose header bounds miss it are skipped.
- `lidar.las_dir`, `lidar.local_scratch`, `lidar.publish_dir`, `lidar.normalized_dir` (null to skip the normalized copy).
- `lidar.engine`: `laspy` (default) or `arcpy` (LAS dataset tools; needs 3D and Spatial Analyst). Switch to arcpy only if ground is unclassified and needs classifying first.

## Environment

Install once into arcgispro-py3:

```
conda install -n arcgispro-py3 -c conda-forge laspy lazrs-python pyyaml python-dotenv
```

scikit-image and pyarrow are normally present. R needs `lidR, sf, future, terra` for the second pass and `spsurvey, sf` for the frozen GRTS draw.

## Run sequence on the server

1. Clone `general-purpose` on the server, check out your branch, and work in `lidar-2022\`. The earlier plan said `C:\repos\plot-network-design`; the folder inside the clone replaces that, and the relative paths in `config.yaml` (`data/processed`, `outputs`, `logs`) resolve from the folder, so nothing else changes. Copy the AOI layer to `data\raw\cwhr_veg.gpkg` or set a REST URL (or read `SDE.Jurisdictions\SDE.TRPA_bdy` from `Vector.sde`).
2. Install packages; set config keys above.
3. Pre-flight: open `notebooks/00a_las_to_chm.ipynb` in Jupyter, run sections 1 and 2, read the log (total GB and points after AOI filter, median density, ground share, CRS). Stop if ground is unclassified or CRS differs.
4. Heavy pass: `"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" scripts\run_lidar.py --with-chm-metrics --with-taos`. Log projects total runtime after 5, 20, and 50 tiles.
5. Interrupt and rerun freely.
6. Publish; register `Derived\chm_1m_tiles\chm_2022_1m.vrt` and `Derived\dtm_1m_tiles\dtm_2022_1m.vrt` in Pro.
7. Back on the workstation, in `ForestHealth\plot-network-design`, confirm `sources:` in its config.yaml resolve against `Derived` (already wired) and run 01_frame onward.

## Products and who uses them

| Product | Where | Use |
|---|---|---|
| `drive_inventory.csv`, `las_tile_index.gpkg` | outputs, Derived | What the 2 TB is; QA map |
| `<tile>_hag.laz` | `LAZ_Basin_HAG` | Base for any future metric, scan calibration, 2026 change |
| DTM 1 m tiles plus VRT | `Derived\dtm_1m_tiles` | Slope, aspect, TPI, heat load, frame cutoff, navigation |
| CHM 1 m tiles plus VRT | `Derived\chm_1m_tiles` | ITD, rumple, TAOs, plot navigation packets |
| `lidar2022_cover_30m.tif` | `Derived\metrics_30m` | Cover class (sparse under 15, open/closed at 40 JP or 50 SMC/RF percent) |
| `lidar2022_p95_30m.tif` | `Derived\metrics_30m` | Seral proxy, calibrated in 02_strata to QMD 5 and 25 in |
| `_midcanopy_`, `_itd_per_ac_`, `_tao_per_ac_` | `Derived\metrics_30m`, `Derived\taos` | Candidate density proxies for the TPA classes |
| `_solid_frac_` | `Derived\metrics_30m` | Roof and rock screen |
| `_rumple_`, `_cover5m_`, `_chm_*` | `Derived\metrics_30m` | Heterogeneity, Becky's 5 m break, comparison |
| `taos_trees_2022.parquet` plus crowns | `Derived\taos` | LITIDA input for Shengli; clump and gap metrics; tree-level change |

## How this feeds the plot design

- LiDAR is the covariate most strongly tied to VP9 and VP10 structure (TNF demonstration hit LOOCV R2 0.84 with cover, mean height, and percentiles at 7 to 10 pts/m2).
- Strata are LiDAR proxies for the QMD (seral) and TPA (density) definitions, crossed with forest type. RRK rasters are evaluation only, never strata. Disturbance is a sub-allocation by inclusion weight, not a stratum.
- 2027 plots are five years off 2022. Plot measurements get reconciled to the LiDAR epoch (FVS projection); protocol records measurement date.
- Co-registration dominates noise at 30 m, so plot positions need survey-grade accuracy (`plot.positional_tolerance_m: 1.0`, pixel window per Shengli). New survey-grade plots are worth more per plot than legacy plots.
- Stem mapping (per-tree azimuth and distance, or terrestrial scans) is needed to match measured trees to TAO IDs for LITIDA training. Unmatched share is recorded as omission rate. Cannot be added after the fact.

## LITIDA plan

1. 00b_taos produces the first-pass TAO layer (done, synthetic-verified).
2. lidR pass on `LAZ_Basin_HAG` at plot footprints and a validation sample; Basin-wide if it validates better.
3. Match measured trees to TAO IDs after the field season; record omission rate per plot.
4. Shengli runs LITIDA; TRPA scores DBH, species, TPA, and BA against the blind remeasure subset and a plot holdout.
5. TAOs also give detected-tree density, crown cover cross-check, clump and gap metrics for VP9, and 2022 to 2026 tree-level change if the second epoch lands.

## Open decisions

- Shengli: grid origin, LITIDA input format, which detection (CHM watershed or lidR) to train on, DBH floor for TPA (`threshold.tpa_dbh_floor_in` is null).
- Becky: per-tree position in the base protocol or only on a calibration subset.
- Whether 2026 LiDAR is confirmed. If yes, tell the consultant immediately and plan the change layer.
- Height and density break placeholders in `strata:` are calibrated in 02_strata once plots with both QMD and LiDAR exist.

## Things that go wrong

- `PermissionError` on the share: need read on `LAZ`, write on `Derived`.
- `MemoryError` in a worker: lower `in_memory_max_points` to 25 M or `workers` to 2.
- Tiles reporting EPSG other than 26910: reproject before running; do not mix CRS.
- Tile bounds far outside the Basin surviving the filter: bad headers or AOI CRS mismatch; check `outputs\las_tile_index.gpkg` in Pro.
- No ground class: arcpy engine to classify, then rerun the laspy pass.

## Related docs

Here: `docs/SERVER_RUN.md` (runbook), `CLAUDE.md`, `environment.md`. In `ForestHealth/plot-network-design`: `PLAN.md` (design rationale), `docs/METHODS.md`, `docs/INPUTS_OUTPUTS.md`, `docs/CHANGELOG.md`.
