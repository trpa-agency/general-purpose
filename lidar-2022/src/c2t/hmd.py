"""
c2t/hmd.py: height of the maximum crown diameter (HMD). Port of trees_hmd() from cloud2trees
R/trees_hmd.R and R/utils_hmd.R (trees_hmd_sf, trees_hmd_flist, calc_tree_hmd, ctg_calc_tree_hmd)
plus the helpers they call in R/utils_cbh.R (check_sample_vals, make_spatial_predictors,
search_dir_final_detected, check_trees_poly) and R/simplify_multipolygon_crowns.R.

Ported faithfully
- calc_tree_hmd(): per tree, from the normalized points that fall inside the crown polygon after
  dropping classes 2, 9, 18 and duplicate xyz (the R LAScatalog filter). The tree center is the
  highest point; ties are broken by the point closest to the xy mean of the crown points, then by
  point order. The crown "diameter" is the horizontal distance of each point from that center and
  HMD is the minimum z among the points at the maximum distance. There are no height bins and no
  per-bin diameter in the R code; the task brief expected bins but the package does not use them.
- Minimum point count: a tree with fewer than 5 points gets NA. HMD above tree_height_m gets NA.
- Tree sample (check_sample_vals): both missing gives n = 777 (333 when trees_poly is a directory or
  list holding more than one crowns file, because R samples through sample_trees_flist there);
  n <= 0 gives the default; prop <= 0 gives 0.5; prop > 1 gives 1; when both are given n wins.
  prop < 1 draws that proportion, n below the tree count draws n trees, otherwise every tree.
- Trees split across tiles: R keeps the tile row with the most points, then the lowest HMD. Here the
  points of a sampled crown are pooled across tiles before calc_tree_hmd, which equals the R result
  whenever the crown fits inside a tile plus its 30 m catalog buffer (always for a crown under 30 m).
- Random forest imputation: predictors tree_height_m, crown_area_zzz (polygon area), tree_x_zzz and
  tree_y_zzz (polygon centroid); response max_crown_diam_height_m; needs more than 10 training trees;
  rf_subsample_and_model_n_times with mod_n_subsample = 25000 and mod_n_times =
  clamp(ceil(0.5 * n_training / 25000), 3, 50); predictions are the average over the models; the first
  model is saved to outfolder/hmd_height_model_estimates.joblib (R saves an .rds).
- force_hmd_lte_ht: training trees with HMD above height leave the training set; imputed trees whose
  HMD / height ratio is above the 95th percentile of the training ratio are capped at that ratio
  times height (R quantile type 7 is numpy's linear default).
- Output columns max_crown_diam_height_m and is_training_hmd; both are removed from the input first.
  With no extracted HMD at all the R function returns NA for both columns and says so.

Assumptions
- No CRS check between crowns and points (R force_same_crs); TRPA normalized LAZ can carry no CRS.
- Point to crown assignment uses cloud.points_to_crowns (crown ids burned to a 0.25 m grid) instead of
  lidR::merge_spatial; MultiPolygon crowns are reduced to their largest part first as
  simplify_multipolygon_crowns() does.
- dplyr::slice_sample is replaced by cloud.sample_crowns with numpy's generator and `seed`.
- The random forest is sklearn (see rf.py) rather than randomForest; mtry tuning is kept.
- Trees whose predictors are not finite are left NaN rather than raising inside predict.
- HMD is bounded below by 0 only because normalized z is >= 0 and a forest predicts averages of
  training values; the R code has no explicit lower clamp either.
- norm_las may also be a dict of arrays as returned by cloud.load_points (one in-memory tile).
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd

from . import cloud
from .rf import rf_subsample_and_model_n_times, rf_model_avg_predictions
from .schema import drop_cols

DROP_CLASSES = (2, 9, 18)              # ground, water, high noise: the R opt_filter on the catalog
MIN_POINTS = 5                         # calc_tree_hmd_n_pts < 5 gives NA
MAX_N_TRAINING = 25000                 # estimate_missing_max_n_training
DEFAULT_SAMPLE_N_SF = 777              # trees_hmd_sf default
DEFAULT_SAMPLE_N_FLIST = 333           # sample_trees_flist default (directory or list of files)
MIN_TRAINING_FOR_MODEL = 10            # n_hmd > 10 before a model is built
PREDICTORS = ("tree_height_m", "crown_area_zzz", "tree_x_zzz", "tree_y_zzz")
OUT_COLS = ("max_crown_diam_height_m", "is_training_hmd")


def _say(log, msg: str) -> None:
    if log is not None:
        log.info(msg)


# ---------------------------------------------------------------------------
# Input handling (search_dir_final_detected, check_trees_poly, simplify_multipolygon_crowns)
# ---------------------------------------------------------------------------

def read_trees_poly(trees_poly):
    """
    Return (GeoDataFrame, number of files read). Accepts a GeoDataFrame, one spatial file, a list
    of files, or a directory holding final_detected_crowns*.gpkg written by raster2trees.
    """
    if isinstance(trees_poly, gpd.GeoDataFrame):
        return trees_poly.copy(), 0
    if isinstance(trees_poly, (str, Path)):
        p = Path(trees_poly)
        if p.is_dir():
            files = sorted(p.glob("final_detected_crowns*.gpkg"))
            if not files:
                raise ValueError(
                    "If attempting to pass a list of files, the file list must:"
                    "\n   * be a directory that has final_detected_crowns* files from cloud2trees() or raster2trees()"
                    "\n   * -OR- be a list of spatial files that geopandas can read")
        elif not p.exists():
            raise FileNotFoundError(f"could not find the file:\n    {p}")
        else:
            files = [p]
    elif isinstance(trees_poly, (list, tuple)):
        files = [Path(f) for f in dict.fromkeys(str(f) for f in trees_poly)]
        missing = [f for f in files if not f.exists()]
        if missing:
            raise FileNotFoundError(f"could not find the file:\n    {missing[0]}")
    else:
        raise ValueError(
            "`trees_poly` data must be: "
            "\n   * a GeoDataFrame with only POLYGON type"
            "\n   * -OR- a directory that has final_detected_crowns* files from cloud2trees() or raster2trees()"
            "\n   * -OR- a list of spatial files that geopandas can read")
    parts = [gpd.read_file(f) for f in files]
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
    return gdf, len(files)


def check_trees_poly(gdf: gpd.GeoDataFrame) -> None:
    """check_trees_poly() in R: polygons only, treeID present, unique, character or numeric, tree_height_m present."""
    if not isinstance(gdf, gpd.GeoDataFrame):
        raise ValueError("`trees_poly` data must be an object of class `sf` with only POLYGON type.")
    if len(gdf) and not gdf.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise ValueError("non-POLYGON found in `trees_poly`; only POLYGON type allowed for `trees_poly`")
    if "treeID" not in gdf.columns:
        raise ValueError("`trees_poly` data must contain `treeID` column to estimate missing values."
                         "\nProvide the `treeID` as a unique identifier of individual trees.")
    if gdf["treeID"].nunique(dropna=False) != len(gdf):
        raise ValueError("Duplicates found in the treeID column. Please remove duplicates and try again.")
    if not (pd.api.types.is_numeric_dtype(gdf["treeID"]) or pd.api.types.is_string_dtype(gdf["treeID"])
            or pd.api.types.is_object_dtype(gdf["treeID"])):
        raise ValueError("`trees_poly` data must contain `treeID` column of class numeric or character.")
    if "tree_height_m" not in gdf.columns:
        raise ValueError("`trees_poly` data must contain `tree_height_m` column to estimate missing values."
                         "\nRename the height column if it exists and ensure it is in meters.")


def simplify_multipolygon_crowns(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Replace each MultiPolygon crown by its largest part (R keeps the largest piece per treeID)."""
    out = gdf.copy()
    is_multi = out.geom_type == "MultiPolygon"
    if not is_multi.any():
        return out
    biggest = [max(g.geoms, key=lambda p: p.area) for g in out.geometry[is_multi]]
    out.loc[is_multi, out.geometry.name] = gpd.GeoSeries(biggest, index=out.index[is_multi], crs=out.crs)
    return out


def make_spatial_predictors(gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    """make_spatial_predictors() in R: crown_area_zzz, tree_x_zzz, tree_y_zzz from the polygon; geometry dropped."""
    df = pd.DataFrame(gdf.drop(columns=[gdf.geometry.name]))
    df["crown_area_zzz"] = gdf.geometry.area.to_numpy(dtype=float)
    cen = gdf.geometry.centroid
    df["tree_x_zzz"] = cen.x.to_numpy(dtype=float)
    df["tree_y_zzz"] = cen.y.to_numpy(dtype=float)
    return df


def check_sample_vals(tree_sample_n, tree_sample_prop, def_tree_sample_n: int = 333, def_tree_sample_prop: float = 0.5):
    """check_sample_vals() in R. Returns (n, prop) with exactly one of them set."""
    def num(v):
        if v is None:
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(v) else v
    n, prop = num(tree_sample_n), num(tree_sample_prop)
    if n is None and prop is None:
        return float(def_tree_sample_n), None
    if n is not None:                       # n given (alone or with prop): n wins
        return (float(def_tree_sample_n) if n <= 0 else n), None
    if prop <= 0:                           # prop only
        prop = def_tree_sample_prop
    elif prop > 1:
        prop = 1.0
    return None, prop


# ---------------------------------------------------------------------------
# HMD from the points (calc_tree_hmd, ctg_calc_tree_hmd)
# ---------------------------------------------------------------------------

def calc_tree_hmd(x: np.ndarray, y: np.ndarray, z: np.ndarray):
    """
    calc_tree_hmd() for one tree. Returns (max_crown_diam_height_m, max_z, n_pts).
    Center = highest point (tie: closest to the xy mean, then first in order). HMD = min z among the
    points at the maximum horizontal distance from the center.
    """
    ok = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
    x, y, z = x[ok], y[ok], z[ok]
    n = len(z)
    if n == 0:
        return np.nan, np.nan, 0
    dist_mean = np.hypot(x - x.mean(), y - y.mean())
    max_z = z.max()
    top = np.flatnonzero(z == max_z)
    center = top[np.argmin(dist_mean[top])]         # argmin returns the first of equal values
    dist = np.hypot(x - x[center], y - y[center])
    farthest = dist == dist.max()
    return float(z[farthest].min()), float(max_z), int(n)


def extract_hmd(crowns: gpd.GeoDataFrame, norm_las, log=None) -> pd.DataFrame:
    """
    Read every tile that touches the crowns, keep the points inside a crown (classes 2, 9, 18 and
    duplicate xyz dropped), pool them per crown, and run calc_tree_hmd. Returns a DataFrame aligned
    with `crowns` rows: max_crown_diam_height_m, calc_tree_hmd_max_z, calc_tree_hmd_n_pts.
    """
    n = len(crowns)
    bounds = tuple(float(v) for v in crowns.total_bounds)
    tiles = [norm_las] if isinstance(norm_las, dict) else cloud.list_las(norm_las)
    if not tiles:
        raise FileNotFoundError(f"`norm_las` must contain a directory with las files or the path of a .laz|.las file: {norm_las}")
    xs, ys, zs, ids = [], [], [], []
    for t in tiles:
        pts = t if isinstance(t, dict) else cloud.load_points(t, bounds=bounds)
        if len(pts["x"]) == 0:
            continue
        keep = ~np.isin(pts["classification"], DROP_CLASSES) if len(pts["classification"]) == len(pts["x"]) else np.ones(len(pts["x"]), bool)
        sub = {"x": np.asarray(pts["x"], float)[keep], "y": np.asarray(pts["y"], float)[keep], "z": np.asarray(pts["z"], float)[keep]}
        tid = cloud.points_to_crowns(sub, crowns)
        inside = tid >= 0
        if not inside.any():
            continue
        xs.append(sub["x"][inside]); ys.append(sub["y"][inside]); zs.append(sub["z"][inside]); ids.append(tid[inside])
        _say(log, f"trees_hmd: {getattr(t, 'name', 'tile')}: {int(inside.sum())} points in {len(np.unique(tid[inside]))} crowns")
    hmd = np.full(n, np.nan); maxz = np.full(n, np.nan); npts = np.zeros(n, dtype=int)
    if xs:
        x, y, z, tid = (np.concatenate(a) for a in (xs, ys, zs, ids))
        key = np.column_stack([np.round(x * 1000), np.round(y * 1000), np.round(z * 1000)]).astype(np.int64)
        _, first = np.unique(key, axis=0, return_index=True)          # -drop_duplicates (identical xyz)
        first = np.sort(first)
        x, y, z, tid = x[first], y[first], z[first], tid[first]
        order = np.argsort(tid, kind="stable")
        x, y, z, tid = x[order], y[order], z[order], tid[order]
        starts = np.r_[0, np.flatnonzero(np.diff(tid)) + 1]
        ends = np.r_[starts[1:], len(tid)]
        for s, e in zip(starts, ends):
            row = tid[s]
            hmd[row], maxz[row], npts[row] = calc_tree_hmd(x[s:e], y[s:e], z[s:e])
    return pd.DataFrame({"max_crown_diam_height_m": hmd, "calc_tree_hmd_max_z": maxz, "calc_tree_hmd_n_pts": npts}, index=crowns.index)


# ---------------------------------------------------------------------------
# trees_hmd
# ---------------------------------------------------------------------------

def trees_hmd(trees_poly, norm_las, tree_sample_n=None, tree_sample_prop=None, estimate_missing_hmd: bool = True,
              seed: int = 21, outfolder=None, log=None) -> gpd.GeoDataFrame:
    """
    trees_hmd(): extract HMD from the normalized cloud for a sample of crowns, then impute the rest with
    a random forest on height, crown area, and location. Returns the full crown table with
    max_crown_diam_height_m and is_training_hmd added.
    """
    trees, n_files = read_trees_poly(trees_poly)
    check_trees_poly(trees)
    trees = drop_cols(trees, OUT_COLS).reset_index(drop=True)
    n_trees = len(trees)
    height_all = pd.to_numeric(trees["tree_height_m"], errors="coerce").to_numpy(dtype=float)

    # ---- sample (trees_hmd_sf / sample_trees_flist) ----
    default_n = DEFAULT_SAMPLE_N_FLIST if n_files > 1 else DEFAULT_SAMPLE_N_SF
    sample_n, sample_prop = check_sample_vals(tree_sample_n, tree_sample_prop, def_tree_sample_n=default_n)
    if sample_prop is not None and sample_prop < 1:
        samp = cloud.sample_crowns(trees, prop=sample_prop, seed=seed)
    elif sample_n is not None and sample_n < n_trees:
        samp = cloud.sample_crowns(trees, n=int(sample_n), seed=seed)
    else:
        samp = trees
    samp = simplify_multipolygon_crowns(samp)
    _say(log, f"trees_hmd: extracting HMD for {len(samp)} of {n_trees} crowns")

    # ---- extract (ctg_calc_tree_hmd + calc_tree_hmd) ----
    ext = extract_hmd(samp, norm_las, log=log)
    hmd = ext["max_crown_diam_height_m"].to_numpy(dtype=float).copy()
    npts = ext["calc_tree_hmd_n_pts"].to_numpy()
    hmd[npts < MIN_POINTS] = np.nan
    samp_height = height_all[samp.index.to_numpy()]
    with np.errstate(invalid="ignore"):
        hmd[hmd > samp_height] = np.nan                    # force_hmd_lte_ht inside trees_hmd_sf
    training = np.isfinite(hmd)
    n_hmd = int(training.sum())
    _say(log, f"trees_hmd: HMD extracted for {n_hmd} trees")

    # ---- full table with the extracted values ----
    out = trees.copy()
    out["max_crown_diam_height_m"] = np.nan
    out["is_training_hmd"] = False
    train_rows = samp.index.to_numpy()[training]
    out.loc[train_rows, "max_crown_diam_height_m"] = hmd[training]
    out.loc[train_rows, "is_training_hmd"] = True

    # ---- model on the training trees ----
    mods = None
    if estimate_missing_hmd and n_hmd > MIN_TRAINING_FOR_MODEL:
        train_df = make_spatial_predictors(out.loc[train_rows])
        X = train_df[list(PREDICTORS)].astype(float)
        y = out.loc[train_rows, "max_crown_diam_height_m"].to_numpy(dtype=float)
        ok = np.isfinite(X.to_numpy()).all(axis=1) & np.isfinite(y)
        if ok.sum() > MIN_TRAINING_FOR_MODEL:
            ntimes = int(min(max(math.ceil((ok.sum() * 0.5) / MAX_N_TRAINING), 3), 50))
            mods = rf_subsample_and_model_n_times(X[ok], y[ok], mod_n_subsample=MAX_N_TRAINING, mod_n_times=ntimes, seed=seed)
            if outfolder is not None and mods:
                import joblib
                Path(outfolder).mkdir(parents=True, exist_ok=True)
                joblib.dump(mods[0], Path(outfolder) / "hmd_height_model_estimates.joblib")

    # ---- fill missing values ----
    if mods is not None:
        need = ~out["is_training_hmd"].to_numpy(dtype=bool)
        if need.any():
            pred_df = make_spatial_predictors(out.loc[need])
            Xp = pred_df[list(PREDICTORS)].astype(float)
            okp = np.isfinite(Xp.to_numpy()).all(axis=1)
            if okp.any():
                pred = rf_model_avg_predictions(mods, Xp[okp])
                out.loc[out.index[need][okp], "max_crown_diam_height_m"] = pred
        _say(log, f"trees_hmd: imputed HMD for {int(need.sum())} trees with {len(mods)} model(s)")
    elif n_hmd == 0:
        _say(log, "No HMD values extracted")
        out["is_training_hmd"] = pd.array([pd.NA] * n_trees, dtype="boolean")
        return out
    elif estimate_missing_hmd:
        _say(log, "Insufficient data available to estimate missing HMD values.\nReturning HMD values extracted from cloud only.")

    # ---- force_hmd_lte_ht on the full table ----
    v = out["max_crown_diam_height_m"].to_numpy(dtype=float).copy()
    tr = out["is_training_hmd"].to_numpy(dtype=bool).copy()
    with np.errstate(invalid="ignore", divide="ignore"):
        pool = tr & (v < height_all)
        if pool.any():
            max_ratio = float(np.quantile(v[pool] / height_all[pool], 0.95))
            tr[tr & (v > height_all)] = False
            ratio = v / height_all
            cap = (~tr) & np.isfinite(ratio) & (ratio > max_ratio)
            v[cap] = max_ratio * height_all[cap]
    out["max_crown_diam_height_m"] = v
    out["is_training_hmd"] = tr
    return out
