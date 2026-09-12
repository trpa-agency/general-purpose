# Running the 2022 LiDAR processing on the server

Source: `\\vcenter2\GIS_DATA\LiDAR\2022\LAZ` (about 2 TB of LAZ). Machine: the 32 GB server with Python and access to the share. Products land in `\\vcenter2\GIS_DATA\LiDAR\2022\Derived`.

## Where things go and why

| What | Where | Why |
|---|---|---|
| Source LAZ | `\\vcenter2\GIS_DATA\LiDAR\2022\LAZ` | Read once per tile, never written |
| Staged tile (one per worker, temporary) | `C:\lidar_scratch` on the server | One network read per tile; decompress and process from local disk |
| Repo, partials, CHM tiles, logs | `general-purpose\lidar-2022` in the server's clone | Partials are the resume state; keep them local and fast |
| Final products | `\\vcenter2\GIS_DATA\LiDAR\2022\Derived` | 30 m metric rasters, 1 m CHM tiles plus VRT, 1 m bare-earth DTM tiles plus VRT, tile index, processing log, config snapshot; readable from any workstation and from Pro |
| TAOs | `\\vcenter2\GIS_DATA\LiDAR\2022\Derived\taos` | Tree table (GeoParquet), crown polygons per tile, TAO density raster; the LITIDA input for Shengli |
| Height-normalized LAZ (AOI tiles only) | `\\vcenter2\GIS_DATA\LiDAR\2022\LAZ_Basin_HAG` | Written once per tile during the pass (`lidar.normalized_dir`). Every future metric (other percentiles, strata by height band, terrestrial scan calibration, a 2026 change layer) starts from these without redoing the DTM. Expect roughly the AOI share of the archive in size; the drive has about 1 TB free |

Local disk needed on the server: about 20 GB for scratch (worst case 3 workers times the largest tile) plus about 5 GB for CHM tiles and partials. The 30 m rasters are a few hundred MB. Scratch stays on the server's own disk, not the external drive: staging to the same drive you are reading from doubles the traffic on the slowest link. The external drive's free space is for products, above all the normalized LAZ, which is the one output worth the write.

Normalized LAZ format: in the in-memory path the original z is kept and a `HeightAboveGround` float32 extra dimension is added (`normalized_write_extra_dim: true`). Tiles too large for memory go through the chunked path, where z itself becomes height above ground; the processing log says which path each tile took. Readers that want one convention can set `normalized_write_extra_dim: false` so every tile has z = HAG.

## Memory on a 32 GB box

Each worker holds one decompressed tile plus working arrays. At `in_memory_max_points: 40000000` that is roughly 2.5 to 3 GB per worker in the in-memory path; larger tiles fall back to the chunked two-pass path automatically. `workers: 3` leaves headroom for the OS, the kernel, and the merge. Watch Task Manager during the first ten tiles; if the machine stays under 20 GB, `workers: 4` is safe. The merge histogram needs about `rows x cols x 160 x 4` bytes, which the notebook logs before it starts (a Basin-wide 30 m grid is about 1 to 2 GB).

## Steps

1. Clone `trpa-agency/general-purpose` on the server, check out your branch, and `cd lidar-2022`. Copy the CWHR or Basin boundary layer to `data\raw\cwhr_veg.gpkg`, or set `lidar.aoi` to a TRPA REST URL.
2. Install into the Pro environment once:
   ```
   conda install -n arcgispro-py3 -c conda-forge laspy lazrs-python pyyaml python-dotenv
   ```
3. In `config.yaml`: set `run.synthetic: false`; confirm `lidar.las_dir`, `lidar.local_scratch`, `lidar.publish_dir`; set `lidar.grid_origin` to Shengli's raster origin once he confirms it (default is a placeholder and can be changed later only by rerunning the merge, not the tiles, because partials are stored with grid offsets computed from it: change it before the heavy pass).
4. **Pre-flight (minutes).** Open `notebooks/00a_las_to_chm.ipynb` in Jupyter on the server and run sections 1 and 2 only. Read the log: total GB and points after the AOI filter, median density, ground-class share, CRS. Two TB of LAZ is far more than the Basin at 8 to 30 points per square meter, so expect the AOI filter to cut the job substantially. Stop here if ground is unclassified (switch `lidar.engine` to `arcpy` and classify first) or if tiles are in a different CRS.
5. **Heavy pass (hours to days).** Either keep running the notebook, or close it and run headless so it survives a logoff:
   ```
   cd <clone>\general-purpose\lidar-2022
   "C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" scripts\run_lidar.py --with-chm-metrics --with-taos
   ```
   Use Task Scheduler ("run whether user is logged on or not") or a `start /b` from an RDP session that stays open. The log in `logs\` reports each tile and a projected total after 5, 20, and 50 tiles.
6. **Interrupting and resuming.** Stop the process at any time. Rerun the same command; tiles with both a partial and a CHM tile are skipped. To reprocess one tile, delete its `.npz` in `data\processed\point_metrics_tiles`.
7. **Publish.** The last cell copies products to `Derived`. Register `Derived\chm_1m_tiles\chm_2022_1m.vrt` and `Derived\dtm_1m_tiles\dtm_2022_1m.vrt` (or build mosaic datasets) in Pro for viewing. The DTM is the one used for normalization: mean class 2 elevation per 1 m cell, nearest-fill for holes, 3x3 smooth; it is a working bare-earth surface, not a hydro-flattened engineering DEM.
8. Back on a workstation, in `trpa-agency/ForestHealth/plot-network-design`, run `01_frame` onward; its `config.yaml` `sources:` already point at `\\vcenter2\GIS_DATA\LiDAR\2022\Derived`.

## Throughput expectations

The share is the limit. At 60 MB/s sustained, 200 GB of Basin tiles is about one hour of reading; at 2 TB it is nine to ten hours per pass. Processing per tile is under a minute of CPU for a 40 M point tile, so with three workers the pipeline stays read-bound and the projection in the log is the number to trust. If the projection is unacceptable, copy the AOI-filtered tiles to the server's local disk first (`robocopy /MT`) and point `las_dir` there; the code path is identical.

## Things that go wrong

- `PermissionError` on the share: run as a user with read on `LAZ` and write on `Derived`.
- `MemoryError` in a worker: lower `in_memory_max_points` to 25 M or `workers` to 2.
- Tiles reporting EPSG other than 26910: reproject with `las2las` or Pro's Extract LAS before processing; do not mix CRS in one run.
- Tile bounds far outside the Basin surviving the filter: the header bounds are wrong or the AOI CRS differs; check `outputs\las_tile_index.gpkg` in Pro.
- No ground class: the arcpy engine path, then rerun the laspy pass.
