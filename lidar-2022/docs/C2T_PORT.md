# cloud2trees port: `src/c2t`

Python port of the cloud2trees R package (George Woolsey and Wade Tinkham, USFS RMRS, v0.8.3, GPL-3) for `lidar-2022`. Same processing logic and output columns as the R package where the method is deterministic; documented substitutions where R relied on Bayesian fitting (brms) or a package with no Python equivalent (lasR, ForestTools, LadderFuelsR, TreeLS). Nothing here calls R.

Source read for the port: `github.com/georgewoolsey/cloud2trees` at commit `8ab10b8` (Sept 14, 2026).

## Why it exists next to 00a and 00b

00a and 00b already produce the DTM, CHM, normalized LAZ, and a TAO table for the Basin. The port adds the back half of cloud2trees that TRPA did not have: per-tree DBH from FIA allometry (TreeMap 2022), forest type group, competition, height of maximum crown diameter, crown base height, crown biomass, and the LANL TREES export for QUIC-Fire. Its raster and detection functions are a faithful second implementation of cloud2raster and raster2trees with the cloud2trees defaults (0.25 m CHM, variable window, watershed with no crown floor) so the two pipelines can be compared on the same tiles. `c2t.dbh.trees_dbh()` and the other `trees_*` functions accept the 00b tree table directly.

## Package layout

```
src/c2t/
  __init__.py       public API re-exports
  schema.py         tree list contract, checks, GeoDataFrame helpers
  ws.py             itd_ws_functions: lin_fn, exp_fn, log_fn, constant, custom
  cloud.py          LAS reading, class filters, denoise (IVF, SOR), CSF ground (optional), TIN DTM,
                    normalization, CHM with pit fill, normalized LAZ writer, cloud2raster()
  itd.py            locate_trees (variable-window local maximum), mcws (marker-controlled watershed),
                    raster2trees(), itd_tuning()
  rf.py             rf_tune_subsample, rf_subsample_and_model_n_times, rf_model_avg_predictions (sklearn)
  extdata.py        get_treemap, get_foresttype, get_landfire, get_data, find_ext_data
  dbh.py            trees_dbh(): regional Chapman-Richards or power model from TreeMap FIA plots,
                    optional local model from stem detections
  stems.py          treels_stem_dbh(): stem circle detection on the 1.12 to 1.62 m slice (UAS, MLS, TLS density)
  foresttype.py     trees_type(): FIA forest type group with nearest-cell fill
  competition.py    trees_competition(): trees per ha, relative height, distance to nearest
  hmd.py            trees_hmd(): height of maximum crown diameter, sample then RF impute
  cbh.py            trees_cbh(): LadderFuelsR-style crown base height from LAD profiles, sample then RF impute
  biomass.py        trees_biomass(): LANDFIRE CBD distribution and Cruz et al. 2003 crown biomass
  lanl.py           cloud2trees_to_lanl_trees(): LANL TREES inputs (treelist, topo, fuellist)
  pipeline.py       cloud2trees(): the all-in-one runner writing point_cloud_processing_delivery
```

Notebook `00c_cloud2trees` (built by `scripts/build_c2t_notebook.py`) runs the pipeline on the configured tiles and then runs the `trees_*` steps on the 00b tree table. Config lives under `c2t:` in `config.yaml`.

## Tree list contract (every `trees_*` function)

Input and output is a `geopandas.GeoDataFrame` in the working CRS (EPSG:26910). Required columns:

| column | type | meaning |
|---|---|---|
| `treeID` | str | unique id; `raster2trees` writes `<n>_<x>_<y>`; the 00b table's `tree_id` is renamed on the way in |
| `tree_x`, `tree_y` | float | top location in CRS units (m) |
| `tree_height_m` | float | top height |
| `crown_area_m2` | float | crown polygon area; `raster2trees` writes it, the 00b table has it |
| `geometry` | Polygon (crowns) or Point (tree tops) | functions that need the crown polygon say so |

`schema.check_tree_list(gdf, need=("tree_height_m",), geometry="any"|"polygon"|"point")` raises with the R package's message when something is missing. `schema.from_taos(taos_gdf)` converts the 00b tree table (`tree_id, x, y, height_m, crown_area_m2, crown_diam_m, tile`) to the contract. Column names added by each step match the R package exactly:

- dbh: `fia_est_dbh_cm`, `fia_est_dbh_cm_lower`, `fia_est_dbh_cm_upper`, `dbh_cm`, `is_training_data`, `dbh_m`, `radius_m`, `basal_area_m2`, `basal_area_ft2`, `ptcld_extracted_dbh_cm`, `ptcld_predicted_dbh_cm`
- type: `forest_type_group_code`, `forest_type_group`, `hardwood_softwood`
- competition: `comp_trees_per_ha`, `comp_relative_tree_height`, `comp_dist_to_nearest_m`
- hmd: `max_crown_diam_height_m`, `is_training_hmd`
- cbh: `tree_cbh_m`, `is_training_cbh`
- biomass: `crown_dia_m`, `crown_length_m`, `crown_volume_m3`, `landfire_crown_biomass_kg`, `landfire_tree_kg_per_m3`, `cruz_crown_biomass_kg`, `cruz_stand_kg_per_m3`, `cruz_tree_kg_per_m3` (see biomass.py docstring for the full list)

## Point cloud access contract (used by cbh, hmd, stems)

`c2t.cloud.load_points(norm_las, bounds=None, keep_classes=None)` reads one file, a directory of files, or a list, returns a dict of numpy arrays `x, y, z, classification, return_number, number_of_returns, intensity`. `z` is height above ground in a normalized file. `bounds` is `(xmin, ymin, xmax, ymax)`; files whose header extent misses the bounds are skipped.

`c2t.cloud.points_to_crowns(pts, crowns, res=0.25)` returns an int array, one per point, with the row index into `crowns` (a Polygon GeoDataFrame) that the point falls in, or -1. It rasterizes crown ids at `res` so it is fast for millions of points.

`c2t.cloud.sample_crowns(crowns, n=None, prop=None, seed=21)` draws the tree sample the same way `trees_cbh` and `trees_hmd` do.

## Substitutions (read before trusting a number)

| R | Python | why |
|---|---|---|
| `brms` Chapman-Richards, lognormal family, weighted, 4 chains | `scipy.optimize.curve_fit` on `log(dbh)` with weights = TreeMap cell counts; 5 and 95 percent bounds from the residual sd on the log scale | same functional form and weighting, point estimates match to fitting error; the interval is a frequentist prediction band, not a posterior predictive interval |
| `brms` power model, Gamma family | `curve_fit` on `dbh = b1*h + h^b2`, bounds from log-scale residual sd | as above |
| `brms` local linear (Gamma, log link) | `sklearn.linear_model.GammaRegressor`-free implementation: least squares on `log(dbh) ~ h` | same mean structure |
| `randomForest::tuneRF` + `randomForest` | `sklearn.ensemble.RandomForestRegressor` with `oob_score=True`, `max_features` tuned by OOB over the R step sequence | same estimator family; mtry semantics kept |
| `lasR::classify_with_csf` (always on) | optional `CSF` package when `c2t.ground: csf`; default `existing` uses the vendor class 2 | 2022 tiles are ground-classified; CSF is a pip extra |
| `lasR::classify_with_sor(k, m)` | scipy cKDTree mean k-neighbour distance, drop points above mean + m sd | same definition |
| `lasR::classify_with_ivf(res, n)` | voxel occupancy count, drop points in voxels with fewer than n neighbours in the 27-voxel neighbourhood | same definition |
| `lasR::triangulate` + `transform_with` (accuracy 2, 3) | Delaunay TIN of ground points, barycentric interpolation at every point with the queries sorted along a coarse grid so the simplex walk stays local (`cloud.tin_interpolate`, about 20x faster than `LinearNDInterpolator` on unsorted points), nearest fill outside the hull | same |
| `lasR::sampling_pixel(min)` ground decimation | grid minimum at 0.1, 0.2, or 0.4 m by the same density rule | same |
| `lasR::pit_fill` (St-Onge 2008) | Laplacian pit detection at `lap_size`, median fill, plus a median fill of empty cells that have at least 5 finite neighbours (interior gaps in a sparse CHM), two passes; the mosaic step's 3x3 mean fill handles canopy edges | approximation; documented in `cloud.pit_fill`. Without the empty-cell fill a 0.25 to 0.5 m CHM at ALS density produces hundreds of spurious local maxima |
| `lidR::lmf(ws = fn)` on a raster | vectorised circular local maximum with a per-pixel window from `ws(height)` over height bands | same rule; bands at 0.25 m of height so the window is exact to the rounding |
| `ForestTools::mcws(minHeight)` | `skimage.segmentation.watershed(-chm, markers, mask=chm >= minHeight)` | same algorithm |
| tiled `raster2trees` with 10 m buffer and largest-crown overlap resolution | tiles with a pad; a crown is kept by the tile that owns its top | deterministic and no duplicates; crown shapes at tile seams can differ from R by a few pixels |
| `TreeLS::treeMap` + `tlsInventory` (Hough circles) | DBSCAN clusters of the 1.12 to 1.62 m slice (`tlsInventory(dh = 1.37, dw = 0.5)`), algebraic circle fit with a RANSAC inlier search using the TreeLS sample size and iteration count, then refit on inliers; a 3/4-of-layers vertical continuity check stands in for the Hough vote map | same circle model and filters (max_dbh 2 m, trp.crop 3 m height); the Hough transform itself is not ported, see `stems.py` docstring |
| `LadderFuelsR` chain | ported in `cbh.py` from the LadderFuelsR calls cloud2trees makes (`get_gaps_fbhs`, `get_depths`, `get_real_fbh`, `get_real_depths`, `get_effective_gap`, `get_layers_lad`, `get_cbh_metrics`) with the same thresholds | see `cbh.py` docstring for what was kept |

## Two things to know about the CBH and HMD ports

`which_cbh="lowest"` in cloud2trees maps to LadderFuelsR's `last_Hcbh`, the base of the topmost effective fuel layer, not the literal lowest base; `"highest"` maps to `max_Hcbh`. The port keeps that mapping so numbers match R and also returns `Hcbh1` (the lowest layer base) from `ladderfuelsr_cbh()`. Single-layer conifers give the same value either way.

HMD in R is not bin based: the highest crown point is the centre and HMD is the lowest z among the points at the maximum horizontal distance from it. Ported as coded.

## Tests and the notebook

`python -m pytest tests -q` runs 41 tests in about a minute: `test_c2t.py` (window functions against R values, cloud2raster and raster2trees on a synthetic tile with 40 cones, tiled versus single-window detection, itd_tuning, trees_dbh regional and local models against a fake TreeMap with a known Chapman-Richards truth, trees_type with the nearest fill, trees_competition, and the full cloud2trees pipeline), `test_cbh.py`, `test_hmd_stems.py`, `test_biomass.py`, `test_lanl.py`. The synthetic tile lives in `src/c2t/synthetic.py` and is what `00c_cloud2trees` uses when `run.synthetic` is true.

`notebooks/00c_cloud2trees.ipynb` (from `scripts/build_c2t_notebook.py`): external data check, tile list, `itd_tuning` on the first tiles, the full `cloud2trees()` run, a comparison against the 00b TAO table on the same extent, attribution of the 00b table (`trees_dbh`, `trees_type`, `trees_competition`, and `trees_hmd`, `trees_cbh`, `trees_biomass` when crown polygons and `LAZ_Basin_HAG` exist), per-cell rollups (TPA, BA, QMD on the 30 m grid), optional LANL export, publish. `scripts/run_c2t.py` runs it headless; `--test` runs the suite.

## Runtime

One 100k-point synthetic tile takes about 3 s through cloud2raster in the container. The costs that scale with points are SOR denoising (cKDTree, k = 15) and the TIN walk; a 40 M point tile is on the order of 15 to 25 minutes per worker. This port is for tile subsets, plot footprints, and the TAO attribution; the Basin-wide rasters stay with 00a and 00b.

## External data

`c2t.extdata.get_data(dest)` downloads the three datasets the R package uses to `data/raw/ext/`: TreeMap 2022 (`treemap2022_conus.tif`, `treemap2022_conus_tree_table.csv`; USFS Box link as in the R source), Forest Type Groups of CONUS at 30 m (Wilson 2023, Zenodo record 14630199: `foresttype.tif`, `foresttype_lookup.csv`), and LANDFIRE CBD 2024 (`lc23_cbd_240.tif`, Zenodo record 19684623). About 7 GB. `config.yaml: c2t.ext_dir` points at them; `find_ext_data()` reports what is present.

## Running

Synthetic check (seconds): `python -m pytest tests/test_c2t.py -q`. Real tiles: set `c2t.input_las_dir` (a folder, a glob, or a list of tiles), `c2t.output_dir`, and `c2t.ext_dir`, then run `notebooks/00c_cloud2trees.ipynb`, or `python scripts/run_c2t.py`.
