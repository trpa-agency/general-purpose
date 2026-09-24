"""
c2t/biomass.py: tree crown biomass from stand-level canopy bulk density (CBD). Port of cloud2trees
R/trees_biomass.R, R/trees_biomass_landfire.R, R/trees_biomass_cruz.R, R/trees_landfire_cbd.R,
R/utils_biomass.R, and the LANDFIRE branch of R/utils_rast_points.R (commit 8ab10b8).

Public functions
----------------
trees_landfire_cbd(tree_list, ...)     adds `landfire_cell_kg_per_m3` (LANDFIRE CBD at the tree top)
trees_biomass_landfire(tree_list, ...) adds crown geometry and `landfire_*` biomass columns
trees_biomass_cruz(tree_list, ...)     adds crown geometry and `cruz_*` biomass columns
trees_biomass(tree_list, method, ...)  dispatcher: method "landfire", "cruz", or "all" (cruz runs first, as in R)

Ported faithfully (same arithmetic, same column names, same defaults)
--------------------------------------------------------------------
* calc_tree_level_cols():
    basal_area_m2   = pi * ((dbh_cm / 100) / 2)^2, or pi * (dbh_m / 2)^2, or the column as given
    crown_dia_m     = 2 * sqrt(crown_area_m2 / pi)
    crown_length_m  = tree_height_m - tree_cbh_m, or 0.5 * tree_height_m when tree_height_m < tree_cbh_m
    crown_volume_m3 = (4/3) * pi * (crown_length_m / 2) * (crown_dia_m / 2)^2
  The crown is one full ellipsoid with semi-axes crown_length_m / 2, crown_dia_m / 2, crown_dia_m / 2.
  cloud2trees has the two-half-ellipsoid version (using height to max crown diameter) commented out, so
  `max_crown_diam_height_m` is not used here either.
* calc_rast_cell_overlap() and calc_rast_cell_trees(): the stand is the 30 m raster cell (LANDFIRE CBD grid
  for the landfire method, Wilson 2023 forest type group grid for the cruz method). Per cell with trees:
    trees, basal_area_m2 (sum), mean_crown_length_m, mean_crown_dia_m, sum_crown_volume_m3,
    overlap_area_m2 = cell area * fraction of the cell covered by the study boundary (or the bounding box of
    the tree tops when no boundary is given) buffered by round(0.5 * cell size) = 15 m,
    overlap_area_ha, basal_area_m2_per_ha = basal_area_m2 / overlap_area_ha, trees_per_ha = trees / overlap_area_ha
* get_cruz_stand_kg_per_m3(): Cruz, Alexander, and Wakimoto (2003) Int. J. Wildland Fire 12(1):39-50, Table 4,
  page 46, as transcribed by cloud2trees utils_biomass.R. Stand CBD in kg/m3 from stand basal area BA (m2/ha)
  and stand density N (trees/ha):
    CBD = exp(b0 + b1 * ln(BA) + b2 * ln(N))
    FIA forest type group 200 Douglas-fir:        b0 = -7.380, b1 = 0.479, b2 = 0.625
    FIA forest type group 220 ponderosa pine:     b0 = -6.649, b1 = 0.435, b2 = 0.579
    FIA forest type group 280 lodgepole pine:     b0 = -7.852, b1 = 0.349, b2 = 0.711
    FIA forest type groups 120, 260, 320 (spruce/fir, fir/spruce/mountain hemlock, western larch)
      mapped to the Cruz mixed conifer model:     b0 = -8.445, b1 = 0.319, b2 = 0.859
  Any other code (including 370 California mixed conifer, which cloud2trees does not map) gives NA. The mapping
  lives in CRUZ_COEFFICIENTS so a caller can extend it deliberately; the default is exactly the R mapping.
  The cell's forest type is the most common `forest_type_group_code` among the trees in the cell, non-missing
  codes first, ties to the smallest code.
* distribute_stand_fuel_load(): stand CBD to trees, both methods:
    landfire_stand_kg_per_m3 = median of landfire_cell_kg_per_m3 over the trees in the cell
    cruz_stand_kg_per_m3     = Cruz equation on the cell's basal_area_m2_per_ha and trees_per_ha
    kg_per_m2                = mean_crown_length_m * stand_kg_per_m3            (canopy fuel load)
    biomass_kg               = kg_per_m2 * overlap_area_m2                     (stand crown biomass)
    tree_kg_per_m3           = biomass_kg / sum_crown_volume_m3                 (constant within a cell)
    crown_biomass_kg         = tree_kg_per_m3 * crown_volume_m3
  So within a cell sum(crown_biomass_kg) = stand_kg_per_m3 * mean_crown_length_m * overlap_area_m2.
  max_crown_kg_per_m3 (default 2, after Mell et al. 2009): when any cell's tree_kg_per_m3 exceeds it and at
  least one cell is below it, the cells above are set to the median tree_kg_per_m3 of the cells below.
* Required columns and error text: `treeID` (unique), `crown_area_m2`, `tree_height_m`, `tree_cbh_m`, and one
  of `dbh_cm`, `dbh_m`, `basal_area_m2` with at least one non-missing value; cruz also needs
  `forest_type_group_code` (trees_type() is attempted when it is absent, as in R). Missing columns raise
  ValueError with the R messages. clean_biomass_cols(): columns this module creates are dropped from the input
  first so re-running overwrites them.
* check_biomass_method(): "cruz" and "landfire" tokens are extracted from the argument (a string or a list),
  so "cruz,landfire" works; "all" is accepted as both. Anything else raises the R error.
* trees_biomass(): each method runs in a try block; a failure becomes a warning ("Could not get `cruz` biomass
  estimates: ...") and the tree list is returned without that method's columns, as purrr::safely does in R.

Assumptions and substitutions (read before trusting a number)
-------------------------------------------------------------
1. LANDFIRE decoding. cloud2trees v0.8.3 downloads a numeric re-packaging of lc23_cbd_240.tif (Zenodo record
   19684623) whose values are already kg/m3 with non-forest and nodata as NA; its reclass_landfire_rast() only
   rescales when the raster is categorical (the original LANDFIRE product, integer VALUE = CBD x 100 with 0 =
   non-forested and -9999 = Fill-NoData). This port keys on the raster data type: integer rasters are treated
   as the original product (value / 100, values <= 0 become NA), floating point rasters as kg/m3 as-is (values
   <= 0 become NA). Both paths are tested.
2. Missing-cell fill. R fills every NA cell of the cropped raster with the value of the nearest valid cell
   centre (a Voronoi diagram of the valid cell centres, rasterized), after aggregating the raster by the modal
   value when the search box exceeds 100k ha. This port fills only the NA cells that contain trees, with the
   nearest valid cell centre to the NA cell's centre (scipy cKDTree), within `max_search_dist_m`, and never
   aggregates. R's crop box is the bounding box of the tree tops (or the study boundary if its box is larger)
   buffered by 0.6 x its largest side; R's `max_search_dist_m` is never applied because of a dropped expression
   in crop_raster_match_points(). Here the box is buffered by max(0.6 x largest side, max_search_dist_m) so the
   search radius is always inside the window.
3. Overlap fraction. terra::rasterize(cover = TRUE) estimates the fraction of a cell covered by the boundary;
   this port computes it exactly with shapely (intersection area / cell area).
4. Cells. Only cells that contain trees are built (R builds every cell in the crop). `landfire_stand_id` and
   `cruz_stand_id` are the 1-based row-major cell numbers of the full raster instead of R's crop-relative cell
   numbers. Trees outside the study boundary buffered by 115 m (R crops the raster to boundary + 15 m + 100 m)
   get no cell and NA biomass, as in R. The per-cell table is attached to the result as
   `result.attrs["stand_cell_data"]` (and `stand_cell_data_landfire` / `stand_cell_data_cruz` from the
   dispatcher) rather than returned in a list.
5. Cell size. Cell area is xres * yres (terra::cellSize(transform = FALSE) on a projected raster). The rasters
   must be north-up (no rotation terms in the affine).
6. External data lookup. R's find_ext_data() also checks the package directory and a stored location file.
   Here the file is looked for in `input_landfire_dir` / `input_foresttype_dir` (or a `landfire` /
   `foresttype` subfolder), then the working directory; a file path is accepted too.
7. R warnings that only announce a fallback ("Attempting to determine CBD using trees_landfire_cbd()...")
   go to `log` instead of warnings.warn; warnings that change the result are kept. R's message when no tree
   gets a CBD value ("Unable to determine LANDFIRE CBD ...") goes to `log`, or to warnings.warn without one.
   A bare shapely geometry passed as `study_boundary` is taken to be in the tree list CRS.
8. `trees_type()` for the cruz fallback is imported lazily from c2t.foresttype and called with
   (tree_list, study_boundary=, input_foresttype_dir=, max_search_dist_m=); if the module is absent or the call
   fails, the R error "failed to get forest type group using trees_type()" is raised.
"""
from __future__ import annotations

import math
import os
import re
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.windows import Window
from scipy.spatial import cKDTree
import shapely
from shapely.geometry import box

from . import schema

LANDFIRE_FILE = "lc23_cbd_240.tif"
FORESTTYPE_FILE = "foresttype.tif"
LANDFIRE_INTEGER_SCALE = 100.0   # original LANDFIRE product stores CBD x 100

# Cruz et al. (2003) Table 4 coefficients keyed by FIA forest type group code, as mapped by cloud2trees.
CRUZ_COEFFICIENTS = {
    200: (-7.380, 0.479, 0.625),   # Douglas-fir group
    220: (-6.649, 0.435, 0.579),   # ponderosa pine group
    280: (-7.852, 0.349, 0.711),   # lodgepole pine group
    120: (-8.445, 0.319, 0.859),   # spruce/fir group -> Cruz mixed conifer
    260: (-8.445, 0.319, 0.859),   # fir/spruce/mountain hemlock group -> Cruz mixed conifer
    320: (-8.445, 0.319, 0.859),   # western larch group -> Cruz mixed conifer
}

BIOMASS_METHODS = ("cruz", "landfire")

CRUZ_COLS = ("cruz_stand_id", "crown_dia_m", "crown_length_m", "crown_volume_m3",
             "cruz_tree_kg_per_m3", "cruz_stand_kg_per_m3", "cruz_crown_biomass_kg")
LANDFIRE_COLS = ("landfire_stand_id", "crown_dia_m", "crown_length_m", "crown_volume_m3",
                 "landfire_tree_kg_per_m3", "landfire_stand_kg_per_m3", "landfire_crown_biomass_kg")


# ----------------------------------------------------------------------------------------------------
# checks and small helpers (utils_biomass.R, check_spatial_points.R, find_ext_data.R)
# ----------------------------------------------------------------------------------------------------
def check_biomass_method(method) -> list[str]:
    """check_biomass_method() in R: which of cruz, landfire were asked for. 'all' means both. R order: cruz, landfire."""
    if method is None:
        text = ""
    elif isinstance(method, str):
        text = method
    else:
        text = " ".join(str(m) for m in method if m is not None)
    text = text.lower().strip()
    if text == "all":
        text = " ".join(BIOMASS_METHODS)
    found = set(re.findall("|".join(BIOMASS_METHODS), text))
    if not found:
        raise ValueError("`method` parameter must be one or multiple of:\n    " + ", ".join(BIOMASS_METHODS))
    return [m for m in BIOMASS_METHODS if m in found]


def clean_biomass_cols(df, method):
    """clean_biomass_cols() in R: drop the columns a trees_biomass_*() run creates so they are overwritten."""
    cols = []
    for m in check_biomass_method(method):
        cols += list(CRUZ_COLS if m == "cruz" else LANDFIRE_COLS)
    return schema.drop_cols(df, cols)


def _check_cols_all_missing(df, col_names) -> None:
    """check_df_cols_all_missing(all_numeric = TRUE) in R."""
    missing = [c for c in col_names if c not in df.columns]
    if missing:
        raise ValueError("the data does not contain the columns: " + ", ".join(missing) + "\n this data must exist")
    all_missing = [c for c in col_names if pd.to_numeric(df[c], errors="coerce").isna().all()]
    if all_missing:
        raise ValueError("the columns listed below have all missing data:\n   " + ", ".join(all_missing))


def _check_biomass_inputs(df) -> None:
    """The column checks at the top of trees_biomass_landfire() and trees_biomass_cruz()."""
    _check_cols_all_missing(df, ("crown_area_m2", "tree_height_m", "tree_cbh_m"))
    has_ba = False
    for c in ("dbh_cm", "dbh_m", "basal_area_m2"):
        try:
            _check_cols_all_missing(df, (c,))
            has_ba = True
        except ValueError:
            pass
    if not has_ba:
        raise ValueError("the data does not contain the columns `basal_area_m2`, `dbh_cm`, or `dbh_m`"
                         "\n .... at least one of these columns must be present and have data")


def _spatial_points(tree_list, crs=None) -> gpd.GeoDataFrame:
    """check_spatial_points() in R: treeID present and unique, tree tops as a Point GeoDataFrame with a CRS."""
    if "treeID" not in tree_list.columns:
        raise ValueError("`tree_list` data must contain `treeID` column."
                         "\nProvide the `treeID` as a unique identifier of individual trees.")
    if tree_list["treeID"].duplicated().any():
        raise ValueError("Duplicates found in the treeID column. Please remove duplicates and try again.")
    pts = schema.as_points(tree_list, crs)
    pts = schema.ensure_treeid(pts)
    return pts.reset_index(drop=True)


def _find_ext_file(input_dir, file_name: str, sub_dir: str, not_found_msg: str) -> Path:
    """find_ext_data() in R, reduced to the given directory (or its `sub_dir` subfolder) and the working directory."""
    candidates = []
    if input_dir is not None:
        p = Path(input_dir)
        if p.is_file():
            return p
        candidates += [p / file_name, p / sub_dir / file_name]
    cwd = Path(os.getcwd())
    candidates += [cwd / file_name, cwd / sub_dir / file_name]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(not_found_msg)


def _find_landfire_file(input_landfire_dir) -> Path:
    return _find_ext_file(
        input_landfire_dir, LANDFIRE_FILE, "landfire",
        "LANDFIRE CBD data has not been downloaded to package contents. Use `get_landfire()` first."
        "\nIf you supplied a value to the `input_landfire_dir` parameter check that directory for data.")


def _find_foresttype_file(input_foresttype_dir) -> Path:
    return _find_ext_file(
        input_foresttype_dir, FORESTTYPE_FILE, "foresttype",
        "Forest Type Group data has not been downloaded to package contents. Use `get_foresttype()` first."
        "\nIf you supplied a value to the `input_foresttype_dir` parameter check that directory for data.")


def _raster_crs_wkt(src) -> str:
    if src.crs is None:
        raise ValueError(f"raster {src.name} has no CRS")
    return src.crs.to_wkt()


def _check_north_up(transform) -> None:
    if transform.b != 0 or transform.d != 0:
        raise ValueError("rotated rasters are not supported")


def _rowcol(transform, xs, ys):
    """Row and column of points, floor of the direct affine division (terra::cellFromXY)."""
    cols = np.floor((np.asarray(xs, dtype="float64") - transform.c) / transform.a).astype("int64")
    rows = np.floor((np.asarray(ys, dtype="float64") - transform.f) / transform.e).astype("int64")
    return rows, cols


def _boundary_geometry(study_boundary, tree_tops: gpd.GeoDataFrame, target_crs) -> shapely.Geometry:
    """Union of the study boundary (or the bounding box of the tree tops) in target_crs."""
    if study_boundary is None or (isinstance(study_boundary, float) and math.isnan(study_boundary)):
        geom = gpd.GeoSeries([box(*tree_tops.total_bounds)], crs=tree_tops.crs)
    elif isinstance(study_boundary, gpd.GeoDataFrame):
        geom = study_boundary.geometry
    elif isinstance(study_boundary, gpd.GeoSeries):
        geom = study_boundary
    else:
        geom = gpd.GeoSeries([study_boundary], crs=tree_tops.crs)
    if geom.crs is None:
        geom = geom.set_crs(tree_tops.crs)
    return geom.to_crs(target_crs).union_all()


# ----------------------------------------------------------------------------------------------------
# raster sampling with nearest-cell fill (crop_raster_match_points, fill_rast_na, reclass_landfire_rast)
# ----------------------------------------------------------------------------------------------------
def _decode_landfire(values: np.ndarray, integer_raster: bool) -> np.ndarray:
    """reclass_landfire_rast(): CBD x 100 integers to kg/m3, anything <= 0 (non-forested, fill) is NA."""
    out = values.astype("float64")
    with np.errstate(invalid="ignore"):
        out[out <= 0] = np.nan
    if integer_raster:
        out = out / LANDFIRE_INTEGER_SCALE
    return out


def _read_window(src, bounds):
    """Band 1 of src over bounds (xmin, ymin, xmax, ymax in the raster CRS) clipped to the raster, nodata as NaN."""
    xmin, ymin, xmax, ymax = bounds
    r0, c0 = _rowcol(src.transform, [xmin], [ymax])
    r1, c1 = _rowcol(src.transform, [xmax], [ymin])
    r0, c0 = max(int(r0[0]), 0), max(int(c0[0]), 0)
    r1, c1 = min(int(r1[0]), src.height - 1), min(int(c1[0]), src.width - 1)
    if r1 < r0 or c1 < c0:
        return None, None
    win = Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1)
    arr = src.read(1, window=win, masked=True)
    values = np.ma.filled(arr.astype("float64"), np.nan)
    return values, src.window_transform(win)


def sample_raster_fill(src, points: gpd.GeoDataFrame, study_boundary=None, max_search_dist_m: float = 1000,
                       decode=None, log=None) -> np.ndarray:
    """
    crop_raster_match_points() followed by agg_fill_rast_match_points() for one raster band.

    Returns one value per point: the (decoded) cell value at the point, or for points on NA cells the value of
    the nearest non-NA cell centre within max_search_dist_m of the NA cell's centre, else NaN.
    """
    _check_north_up(src.transform)
    pts = points.to_crs(_raster_crs_wkt(src))
    xs, ys = pts.geometry.x.to_numpy(), pts.geometry.y.to_numpy()
    n = len(pts)
    out = np.full(n, np.nan)
    if n == 0:
        return out

    # search box: tree bbox, or the study boundary bbox when its largest side is longer, buffered like R
    xmin, ymin, xmax, ymax = pts.total_bounds
    side = max(xmax - xmin, ymax - ymin)
    if study_boundary is not None:
        bxmin, bymin, bxmax, bymax = shapely.bounds(_boundary_geometry(study_boundary, points, pts.crs))
        if max(bxmax - bxmin, bymax - bymin) > side:
            xmin, ymin, xmax, ymax = bxmin, bymin, bxmax, bymax
            side = max(bxmax - bxmin, bymax - bymin)
    buffer = max(0.6 * side, float(max_search_dist_m))
    values, wt = _read_window(src, (xmin - buffer, ymin - buffer, xmax + buffer, ymax + buffer))
    if values is None:
        if log:
            log.warning(f"{Path(src.name).name}: tree list does not overlap the raster")
        return out
    integer_raster = np.issubdtype(src.dtypes[0], np.integer)
    if decode is not None:
        values = decode(values, integer_raster)

    rows, cols = _rowcol(wt, xs, ys)
    inside = (rows >= 0) & (rows < values.shape[0]) & (cols >= 0) & (cols < values.shape[1])
    out[inside] = values[rows[inside], cols[inside]]

    need = inside & np.isnan(out)
    if not need.any():
        return out
    valid = np.isfinite(values)
    if not valid.any():
        if log:
            log.warning(f"{Path(src.name).name}: all raster values are NA in the search window; cannot fill")
        return out
    vr, vc = np.nonzero(valid)
    vx = wt.c + (vc + 0.5) * wt.a
    vy = wt.f + (vr + 0.5) * wt.e
    tree = cKDTree(np.column_stack([vx, vy]))
    na_cells, inv = np.unique(np.column_stack([rows[need], cols[need]]), axis=0, return_inverse=True)
    qx = wt.c + (na_cells[:, 1] + 0.5) * wt.a
    qy = wt.f + (na_cells[:, 0] + 0.5) * wt.e
    dist, idx = tree.query(np.column_stack([qx, qy]), k=1, distance_upper_bound=float(max_search_dist_m))
    found = np.isfinite(dist)
    filled = np.full(len(na_cells), np.nan)
    filled[found] = values[vr[idx[found]], vc[idx[found]]]
    out[need] = filled[inv.ravel()]
    if log:
        log.info(f"{Path(src.name).name}: filled {int(np.isfinite(out[need]).sum())} of {int(need.sum())} trees on NA cells")
    return out


# ----------------------------------------------------------------------------------------------------
# trees_landfire_cbd
# ----------------------------------------------------------------------------------------------------
def trees_landfire_cbd(tree_list, crs=None, study_boundary=None, input_landfire_dir=None,
                       max_search_dist_m: float = 1000, log=None) -> gpd.GeoDataFrame:
    """
    trees_landfire_cbd() in R: attach `landfire_cell_kg_per_m3`, the LANDFIRE canopy bulk density (kg/m3) of
    the cell under each tree top, with the nearest forested cell used for trees on non-forest or nodata cells.
    """
    path = _find_landfire_file(input_landfire_dir)
    tree_tops = _spatial_points(tree_list, crs)
    if tree_tops.crs is None:
        raise ValueError("Cannot get LANDFIRE data with blank CRS.\n  ensure that the `tree_list` data has a CRS")
    tree_tops = schema.drop_cols(tree_tops, ["landfire_cell_kg_per_m3"])
    with rasterio.open(path) as src:
        values = sample_raster_fill(src, tree_tops, study_boundary, max_search_dist_m, decode=_decode_landfire, log=log)
    tree_tops["landfire_cell_kg_per_m3"] = values
    if np.isnan(values).all() and len(values):
        msg = ("Unable to determine LANDFIRE CBD for this tree list and study boundary (if provided)."
               "\nTry expanding the study boundary area or increasing the max_search_dist_m parameter"
               "\nand ensure that your tree data is in the continental US.")
        if log:
            log.warning(msg)
        else:
            warnings.warn(msg)
    return tree_tops


# ----------------------------------------------------------------------------------------------------
# tree level crown columns and stand (raster cell) aggregation (calc_tree_level_cols, calc_rast_cell_trees)
# ----------------------------------------------------------------------------------------------------
def calc_tree_level_cols(df):
    """calc_tree_level_cols() in R: basal_area_m2, crown_dia_m, crown_length_m, crown_volume_m3."""
    _check_cols_all_missing(df, ("crown_area_m2", "tree_height_m", "tree_cbh_m"))
    out = df.copy()
    if "basal_area_m2" not in out.columns and "dbh_cm" in out.columns:
        out["dbh_cm"] = pd.to_numeric(out["dbh_cm"], errors="coerce")
        out["basal_area_m2"] = np.pi * ((out["dbh_cm"] / 100.0) / 2.0) ** 2
    elif "basal_area_m2" not in out.columns and "dbh_m" in out.columns:
        out["dbh_m"] = pd.to_numeric(out["dbh_m"], errors="coerce")
        out["basal_area_m2"] = np.pi * (out["dbh_m"] / 2.0) ** 2
    elif "basal_area_m2" in out.columns:
        out["basal_area_m2"] = pd.to_numeric(out["basal_area_m2"], errors="coerce")
    else:
        raise ValueError("the data does not contain the columns `basal_area_m2`, `dbh_cm`, or `dbh_m`"
                         "\n .... at least one of these columns must be present")
    _check_cols_all_missing(out, ("basal_area_m2",))

    area = pd.to_numeric(out["crown_area_m2"], errors="coerce").astype("float64")
    ht = pd.to_numeric(out["tree_height_m"], errors="coerce").astype("float64")
    cbh = pd.to_numeric(out["tree_cbh_m"], errors="coerce").astype("float64")
    out["crown_area_m2"], out["tree_height_m"], out["tree_cbh_m"] = area, ht, cbh
    out["crown_dia_m"] = np.sqrt(area / np.pi) * 2.0
    out["crown_length_m"] = np.where(ht < cbh, ht * 0.5, ht - cbh)
    out["crown_volume_m3"] = (4.0 / 3.0) * np.pi * (out["crown_length_m"] / 2.0) * (out["crown_dia_m"] / 2.0) ** 2
    return out


def _rast_cell_trees(src, tree_tops: gpd.GeoDataFrame, study_boundary, buffer_m: float = 100):
    """
    calc_rast_cell_overlap() + calc_rast_cell_trees() in R for the cells that contain trees.

    Returns (tree_list with `cell` and the crown columns, cell_df). Cell numbers are 1-based row-major numbers
    of the full raster. Trees outside the boundary (buffered by half a cell plus buffer_m) get NaN cell.
    """
    _check_north_up(src.transform)
    t = src.transform
    crs_wkt = _raster_crs_wkt(src)
    cell_w, cell_h = abs(t.a), abs(t.e)
    cell_area = cell_w * cell_h

    boundary = _boundary_geometry(study_boundary, tree_tops, crs_wkt).buffer(round(cell_w * 0.5))
    exmin, eymin, exmax, eymax = shapely.bounds(boundary.buffer(buffer_m))

    pts = tree_tops.to_crs(crs_wkt)
    xs, ys = pts.geometry.x.to_numpy(), pts.geometry.y.to_numpy()
    rows, cols = _rowcol(t, xs, ys)
    inside = ((rows >= 0) & (rows < src.height) & (cols >= 0) & (cols < src.width)
              & (xs >= exmin) & (xs <= exmax) & (ys >= eymin) & (ys <= eymax))
    cell = np.full(len(pts), np.nan)
    cell[inside] = rows[inside] * src.width + cols[inside] + 1

    tl = tree_tops.copy()
    tl["cell"] = cell
    tl = calc_tree_level_cols(tl)

    # one row per cell with trees: x, y, area, overlap with the boundary (calc_rast_cell_overlap)
    cells = np.unique(cell[np.isfinite(cell)]).astype("int64")
    crow = (cells - 1) // src.width
    ccol = (cells - 1) % src.width
    x0 = t.c + ccol * t.a
    y1 = t.f + crow * t.e
    boxes = shapely.box(x0, y1 + t.e, x0 + t.a, y1)
    pct = shapely.area(shapely.intersection(boxes, boundary)) / cell_area
    epsg = src.crs.to_epsg()
    cell_df = pd.DataFrame({
        "cell": cells,
        "x": x0 + 0.5 * t.a,
        "y": y1 + 0.5 * t.e,
        "area": cell_area,
        "pct_overlap": pct,
    })
    cell_df["overlap_area_m2"] = cell_df["area"] * cell_df["pct_overlap"].fillna(0)
    cell_df["overlap_area_ha"] = cell_df["overlap_area_m2"] / 10000.0
    cell_df["rast_epsg_code"] = str(epsg) if epsg is not None else None

    agg = (tl.drop(columns=tl.geometry.name)
           .dropna(subset=["cell"])
           .groupby("cell")
           .agg(trees=("treeID", "size"),
                basal_area_m2=("basal_area_m2", "sum"),
                mean_crown_length_m=("crown_length_m", "mean"),
                mean_crown_dia_m=("crown_dia_m", "mean"),
                sum_crown_volume_m3=("crown_volume_m3", "sum"))
           .reset_index())
    agg["cell"] = agg["cell"].astype("int64")
    cell_df = cell_df.merge(agg, on="cell", how="left")
    with np.errstate(divide="ignore", invalid="ignore"):
        cell_df["basal_area_m2_per_ha"] = cell_df["basal_area_m2"] / cell_df["overlap_area_ha"]
        cell_df["trees_per_ha"] = cell_df["trees"] / cell_df["overlap_area_ha"]
    return tl, cell_df


# ----------------------------------------------------------------------------------------------------
# stand CBD to trees (get_cruz_stand_kg_per_m3, distribute_stand_fuel_load)
# ----------------------------------------------------------------------------------------------------
def get_cruz_stand_kg_per_m3(forest_type_group_code, basal_area_m2_per_ha, trees_per_ha) -> np.ndarray:
    """Cruz et al. (2003) Table 4: CBD = exp(b0 + b1 ln(BA) + b2 ln(N)); NaN for codes without a model."""
    code = pd.to_numeric(pd.Series(np.atleast_1d(forest_type_group_code)), errors="coerce").fillna(0).to_numpy()
    ba = np.asarray(basal_area_m2_per_ha, dtype="float64")
    n = np.asarray(trees_per_ha, dtype="float64")
    b0 = np.full(code.shape, np.nan)
    b1 = np.full(code.shape, np.nan)
    b2 = np.full(code.shape, np.nan)
    for k, (c0, c1, c2) in CRUZ_COEFFICIENTS.items():
        m = code == k
        b0[m], b1[m], b2[m] = c0, c1, c2
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        return np.exp(b0 + b1 * np.log(ba) + b2 * np.log(n))


def _cap_tree_kg_per_m3(values: pd.Series, max_crown_kg_per_m3) -> pd.Series:
    """The max_crown_kg_per_m3 rule: cells above the cap take the median of the cells below it."""
    cap = 1e10 if max_crown_kg_per_m3 is None else float(max_crown_kg_per_m3)
    if np.isnan(cap):
        cap = 1e10
    v = values.astype("float64")
    if v.notna().any() and v.max(skipna=True) > cap:
        below = v[(v < cap) & v.notna()]
        if len(below) > 0:
            v = v.where(~(v.notna() & (v > cap)), below.median())
    return v


def _cell_forest_type(tl: pd.DataFrame) -> pd.Series:
    """Most common forest_type_group_code per cell, non-missing codes first, ties to the smallest code."""
    ft = pd.DataFrame({
        "cell": tl["cell"].to_numpy(),
        "code": pd.to_numeric(tl["forest_type_group_code"], errors="coerce").to_numpy(),
    }).dropna(subset=["cell"])
    ft["is_na_ft"] = ft["code"].isna().astype(int)
    counts = ft.groupby(["cell", "is_na_ft", "code"], dropna=False).size().reset_index(name="trees")
    counts = counts.sort_values(["cell", "is_na_ft", "trees", "code"], ascending=[True, True, False, True],
                                na_position="last")
    first = counts.drop_duplicates("cell")
    return pd.Series(first["code"].to_numpy(), index=first["cell"].to_numpy().astype("int64"))


def distribute_stand_fuel_load(cell_df: pd.DataFrame, tree_list, cbd_method: str = "cruz", max_crown_kg_per_m3=2):
    """distribute_stand_fuel_load() in R. Returns (cell_df, tree_list)."""
    cbd_method = (cbd_method or "").strip().lower()
    if cbd_method not in BIOMASS_METHODS:
        raise ValueError('`cbd_method` parameter must be one of "cruz" or "landfire"')
    if "cell" not in tree_list.columns:
        raise ValueError("tree_list data must have the column `cell`...should be in the output of calc_rast_cell_trees()?")
    if cbd_method == "cruz" and "forest_type_group_code" not in tree_list.columns:
        raise ValueError("cbd_method set to `cruz` but the `forest_type_group_code` does not exist in the tree list data"
                         "\n try running cloud2trees::trees_type() first")
    if cbd_method == "landfire" and "landfire_cell_kg_per_m3" not in tree_list.columns:
        raise ValueError("cbd_method set to `landfire` but the `landfire_cell_kg_per_m3` does not exist in the tree list data"
                         "\n try running cloud2trees::trees_landfire_cbd() first")

    cell_df = cell_df.copy()
    tl = tree_list.copy()
    stand_col = f"{cbd_method}_stand_kg_per_m3"
    tree_col = f"{cbd_method}_tree_kg_per_m3"
    stand_id = f"{cbd_method}_stand_id"
    biomass_col = f"{cbd_method}_crown_biomass_kg"

    if cbd_method == "cruz":
        cell_df["forest_type_group_code"] = cell_df["cell"].map(_cell_forest_type(tl))
        cell_df[stand_col] = get_cruz_stand_kg_per_m3(
            cell_df["forest_type_group_code"], cell_df["basal_area_m2_per_ha"], cell_df["trees_per_ha"])
    else:
        med = (pd.DataFrame({"cell": tl["cell"], "v": pd.to_numeric(tl["landfire_cell_kg_per_m3"], errors="coerce")})
               .dropna(subset=["cell"]).groupby("cell")["v"].median())
        med.index = med.index.astype("int64")
        cell_df[stand_col] = cell_df["cell"].map(med)

    with np.errstate(divide="ignore", invalid="ignore"):
        cell_df["kg_per_m2"] = cell_df["mean_crown_length_m"] * cell_df[stand_col]
        cell_df["biomass_kg"] = cell_df["kg_per_m2"] * cell_df["overlap_area_m2"]
        cell_df[tree_col] = cell_df["biomass_kg"] / cell_df["sum_crown_volume_m3"]
    cell_df[tree_col] = _cap_tree_kg_per_m3(cell_df[tree_col], max_crown_kg_per_m3)

    lookup = cell_df.set_index("cell")
    tl[tree_col] = tl["cell"].map(lookup[tree_col])
    tl[stand_col] = tl["cell"].map(lookup[stand_col])
    tl[biomass_col] = tl[tree_col] * tl["crown_volume_m3"]
    tl = tl.rename(columns={"cell": stand_id})
    if cbd_method == "landfire":
        tl = schema.drop_cols(tl, ["landfire_cell_kg_per_m3"])
    cell_df = cell_df.rename(columns={"cell": stand_id})
    return cell_df, tl


# ----------------------------------------------------------------------------------------------------
# trees_biomass_landfire
# ----------------------------------------------------------------------------------------------------
def trees_biomass_landfire(tree_list, crs=None, study_boundary=None, input_landfire_dir=None,
                           max_crown_kg_per_m3=2, max_search_dist_m: float = 1000, log=None) -> gpd.GeoDataFrame:
    """
    trees_biomass_landfire() in R: crown biomass (kg) per tree from LANDFIRE CBD distributed over the crown
    volumes of the trees in each 30 m cell. Adds basal_area_m2 (if derived), crown_dia_m, crown_length_m,
    crown_volume_m3, landfire_stand_id, landfire_tree_kg_per_m3, landfire_stand_kg_per_m3,
    landfire_crown_biomass_kg. The cell table is in result.attrs["stand_cell_data"].
    """
    path = _find_landfire_file(input_landfire_dir)
    tree_tops = _spatial_points(tree_list, crs)
    _check_biomass_inputs(tree_tops)
    tree_tops = clean_biomass_cols(tree_tops, "landfire")

    if "landfire_cell_kg_per_m3" not in tree_tops.columns:
        if log:
            log.info("`tree_list` data must contain `landfire_cell_kg_per_m3` column."
                     " Attempting to determine CBD using trees_landfire_cbd()...")
        try:
            tree_tops = trees_landfire_cbd(tree_tops, study_boundary=study_boundary, input_landfire_dir=path,
                                           max_search_dist_m=max_search_dist_m, log=log)
        except Exception as e:
            raise ValueError(f"Error: failed to get LANDFIRE CBD using trees_landfire_cbd(). Message:\n{e}") from e
    try:
        _check_cols_all_missing(tree_tops, ("landfire_cell_kg_per_m3",))
    except ValueError as e:
        raise ValueError("Error: failed to get LANDFIRE CBD using trees_landfire_cbd()."
                         "\n Is this area in the continental United States? Try to expand search area in trees_landfire_cbd()?") from e

    with rasterio.open(path) as src:
        tl, cell_df = _rast_cell_trees(src, tree_tops, study_boundary)
    cell_df, tl = distribute_stand_fuel_load(cell_df, tl, cbd_method="landfire", max_crown_kg_per_m3=max_crown_kg_per_m3)
    if log:
        log.info(f"landfire biomass: {int(tl['landfire_crown_biomass_kg'].notna().sum())} of {len(tl)} trees, "
                 f"{len(cell_df)} cells")
    tl.attrs["stand_cell_data"] = cell_df
    return tl


# ----------------------------------------------------------------------------------------------------
# trees_biomass_cruz
# ----------------------------------------------------------------------------------------------------
def has_cruz_forest_type_group_code(x) -> bool:
    """has_cruz_forest_type_group_code() in R: any code with a Cruz model present."""
    if isinstance(x, pd.DataFrame):
        if "forest_type_group_code" not in x.columns:
            raise ValueError("data must contain `forest_type_group_code` column")
        x = x["forest_type_group_code"]
    codes = pd.to_numeric(pd.Series(np.atleast_1d(np.asarray(x, dtype=object))), errors="coerce")
    return bool(codes.isin(list(CRUZ_COEFFICIENTS)).sum() > 0)


def trees_biomass_cruz(tree_list, crs=None, study_boundary=None, input_foresttype_dir=None,
                       max_crown_kg_per_m3=2, max_search_dist_m: float = 1000, log=None) -> gpd.GeoDataFrame:
    """
    trees_biomass_cruz() in R: crown biomass (kg) per tree from Cruz et al. (2003) stand CBD by FIA forest type
    group, on the 30 m forest type group grid. Adds basal_area_m2 (if derived), crown_dia_m, crown_length_m,
    crown_volume_m3, cruz_stand_id, cruz_tree_kg_per_m3, cruz_stand_kg_per_m3, cruz_crown_biomass_kg (and the
    trees_type() columns when forest_type_group_code was absent). When no tree has a forest type group with a
    Cruz model, warns and returns the tree list without cruz columns, as R does.
    """
    path = _find_foresttype_file(input_foresttype_dir)
    tree_tops = _spatial_points(tree_list, crs)
    _check_biomass_inputs(tree_tops)
    tree_tops = clean_biomass_cols(tree_tops, "cruz")

    if "forest_type_group_code" not in tree_tops.columns:
        if log:
            log.info("`tree_list` data must contain `forest_type_group_code` column."
                     " Attempting to determine forest type group using trees_type()...")
        try:
            from .foresttype import trees_type
            ans = trees_type(tree_tops, study_boundary=study_boundary, input_foresttype_dir=path.parent,
                             max_search_dist_m=max_search_dist_m)
            if isinstance(ans, dict):
                ans = ans["tree_list"]
            elif isinstance(ans, tuple):
                ans = ans[0]
            tree_tops = ans
        except Exception as e:
            raise ValueError(f"Error: failed to get forest type group using trees_type(). Message:\n{e}") from e

    if not has_cruz_forest_type_group_code(tree_tops):
        warnings.warn("None of the forest types present match with the Cruz equations available..."
                      "\n returning original data with forest type group if it was attached via trees_type()")
        return tree_tops

    with rasterio.open(path) as src:
        tl, cell_df = _rast_cell_trees(src, tree_tops, study_boundary)
    cell_df, tl = distribute_stand_fuel_load(cell_df, tl, cbd_method="cruz", max_crown_kg_per_m3=max_crown_kg_per_m3)
    if log:
        log.info(f"cruz biomass: {int(tl['cruz_crown_biomass_kg'].notna().sum())} of {len(tl)} trees, "
                 f"{len(cell_df)} cells")
    tl.attrs["stand_cell_data"] = cell_df
    return tl


# ----------------------------------------------------------------------------------------------------
# trees_biomass dispatcher
# ----------------------------------------------------------------------------------------------------
def trees_biomass(tree_list, method="landfire", crs=None, study_boundary=None, input_landfire_dir=None,
                  max_search_dist_m: float = 1000, log=None, input_foresttype_dir=None,
                  max_crown_kg_per_m3=2) -> gpd.GeoDataFrame:
    """
    trees_biomass() in R. method: "landfire", "cruz", "all" (or a list). Cruz runs first, then landfire, as in
    R. A method that fails raises a warning and leaves its columns out (R wraps each in purrr::safely).
    Cell tables: result.attrs["stand_cell_data_landfire"], result.attrs["stand_cell_data_cruz"] (None if not run).
    """
    methods = check_biomass_method(method)
    tree_tops = _spatial_points(tree_list, crs)
    stand_cruz = None
    stand_landfire = None

    if "cruz" in methods:
        if log:
            log.info("attempting to estimate biomass using the `cruz` method")
        try:
            ans = trees_biomass_cruz(tree_tops, crs=crs, study_boundary=study_boundary,
                                     input_foresttype_dir=input_foresttype_dir, max_crown_kg_per_m3=max_crown_kg_per_m3,
                                     max_search_dist_m=max_search_dist_m, log=log)
            stand_cruz = ans.attrs.get("stand_cell_data")
            tree_tops = ans
        except Exception as e:
            warnings.warn(f"Could not get `cruz` biomass estimates:\n{e}")
            if log:
                log.warning(f"Could not get `cruz` biomass estimates: {e}")

    if "landfire" in methods:
        if log:
            log.info("attempting to estimate biomass using the `landfire` method")
        try:
            ans = trees_biomass_landfire(tree_tops, crs=crs, study_boundary=study_boundary,
                                         input_landfire_dir=input_landfire_dir, max_crown_kg_per_m3=max_crown_kg_per_m3,
                                         max_search_dist_m=max_search_dist_m, log=log)
            stand_landfire = ans.attrs.get("stand_cell_data")
            tree_tops = ans
        except Exception as e:
            warnings.warn(f"Could not get `landfire` biomass estimates:\n{e}")
            if log:
                log.warning(f"Could not get `landfire` biomass estimates: {e}")

    tree_tops.attrs = {}
    tree_tops.attrs["stand_cell_data_landfire"] = stand_landfire
    tree_tops.attrs["stand_cell_data_cruz"] = stand_cruz
    return tree_tops
