"""
c2t/stems.py: stem detection and DBH from a high density normalized cloud. Reimplementation of
treels_stem_dbh() from cloud2trees R/treels_stem_dbh.R, which wraps the TreeLS package
(treeMap + map.hough, treeMap.merge, treePoints + trp.crop, stemPoints + stm.hough, tlsInventory +
shapeFit). TreeLS has no Python equivalent, so the Hough transform stem search is replaced by
horizontal clustering of the DBH slice plus a circle fit. Meant for UAS, MLS, or TLS density; a
20 to 30 pts/m2 ALS tile has too few stem returns for this to find much.

What follows the R code (and the TreeLS defaults it relies on)
- Points with z < 0 are dropped; a tile whose highest point is below min_height (R default 2 m) is
  skipped. Ground points (class 2) are excluded from the stem search as stm.hough does; noise classes
  7 and 18 are excluded too.
- DBH slice: TreeLS::tlsInventory(dh = 1.37, dw = 0.5) keeps stem points with dh - dw/2 < z < dh + dw/2,
  so the slice is 1.12 to 1.62 m, not 1.2 to 1.4 m. slice_m defaults to that and is exposed.
- Circle fit: TreeLS::shapeFit(shape = "circle", algorithm = "ransac", n = 20) with TreeLS defaults
  conf = 0.95, inliers = 0.9, n_best = 10. The circle model is the algebraic (Kasa) fit that TreeLS
  solves by QR, and the RANSAC iteration count is TreeLS's (n_best + 5) * ceil(log(1 - conf) /
  log(1 - inliers^n)) = 360 for these values. TreeLS falls back to the plain algebraic fit when a
  stem has n or fewer points; so does this.
- Filters from the R post-processing: radius must be finite, dbh_m <= max_dbh (R default 2 m, here
  max_dbh_cm = 200), valid non-empty point geometry. R keeps only stems inside the tile extent plus
  1 m, which is automatic here because the slice points come from the tile.
- Tree height per stem: TreeLS::trp.crop(l = 3) assigns every point within 3 m of the stem position
  to that stem, and tlsInventory(hp = 1) takes the maximum z of those points as H. Same here
  (crop_radius_m = 3, nearest stem wins where circles overlap).
- Output columns match the R return (treeID = "x_y", tree_height_m, stem_x, stem_y, radius_m,
  radius_error_m, dbh_m, dbh_cm, basal_area_m2, basal_area_ft2, condition = "detected_stem") plus
  n_points, n_inliers, inlier_frac, fit_rmse_m requested by the port contract. radius_error_m is the
  same value as fit_rmse_m.
- Per tile results are written to outfolder/<tile>.gpkg like the R function when outfolder is given.

Assumptions (not in the R code or TreeLS)
- Candidate stems come from sklearn DBSCAN on the xy of the slice points (cluster_eps_m = 0.10,
  cluster_min_samples = 5) rather than the Hough transform vote map (pixel_size 0.025 m, min_votes 3,
  min_density 0.0001 in map.hough). Clusters with fewer than min_points points (default 20 = the
  RANSAC sample size) or a bounding box wider than 1.5 * max_dbh are dropped before fitting.
- RANSAC scoring: TreeLS scores each sample by the residual of the sampled points and returns the
  median of the n_best + 1 best circles. Here the best sample is the one with most points within
  tolerance_m (default 0.025 m = the TreeLS Hough pixel size) of its circle, tie broken by rmse; the
  circle is then refit on those inliers twice, and fit_rmse_m is the rmse over the final inliers.
- Vertical continuity: map.hough keeps a stem candidate only when it is found in at least 3/4 of the
  0.5 m layers between min_h = 1 and max_h = 5 m. That is approximated by requiring points within
  radius_m + layer_tolerance_m of the fitted center in at least min_layer_frac of the h_step_m layers
  between layer_range_m. Set min_layer_frac = 0 to disable.
- treeMap.merge(d = 0.2) merges stem positions closer than the neighbour gap rule; here two fitted
  circles whose centers are closer than merge_dist_m (0.2 m) keep the one with more inliers.
- Stems are not deduplicated across overlapping tiles (R does not either).
- The output CRS is the `crs` argument, else the LAS header CRS when one is present.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.spatial import cKDTree
from shapely.geometry import Point

from . import cloud

DROP_CLASSES = (2, 7, 18)
OUT_COLS = ["treeID", "tree_height_m", "stem_x", "stem_y", "radius_m", "radius_error_m", "dbh_m", "dbh_cm",
            "basal_area_m2", "basal_area_ft2", "n_points", "n_inliers", "inlier_frac", "fit_rmse_m", "condition"]


def _say(log, msg: str) -> None:
    if log is not None:
        log.info(msg)


# ---------------------------------------------------------------------------
# Circle fitting
# ---------------------------------------------------------------------------

def fit_circle_algebraic(x: np.ndarray, y: np.ndarray):
    """
    Kasa fit, the TreeLS 'qr' circle: least squares on x^2 + y^2 = a x + b y + c, center (a/2, b/2),
    radius sqrt(a^2/4 + b^2/4 + c). Returns (cx, cy, r) or None when degenerate.
    """
    if len(x) < 3:
        return None
    A = np.column_stack([x, y, np.ones(len(x))])
    rhs = x * x + y * y
    sol, _, rank, _ = np.linalg.lstsq(A, rhs, rcond=None)
    if rank < 3:
        return None
    cx, cy = sol[0] / 2.0, sol[1] / 2.0
    r2 = cx * cx + cy * cy + sol[2]
    if not np.isfinite(r2) or r2 <= 0:
        return None
    return float(cx), float(cy), float(math.sqrt(r2))


def circle_residuals(x, y, cx, cy, r) -> np.ndarray:
    """Signed radial distance of each point from the circle."""
    return np.hypot(x - cx, y - cy) - r


def fit_circle_ransac(x: np.ndarray, y: np.ndarray, n: int = 20, conf: float = 0.95, inliers: float = 0.9,
                      n_best: int = 10, tolerance: float = 0.025, rng=None):
    """
    RANSAC circle fit. Samples n points per iteration for TreeLS's iteration count, keeps the sample
    circle with the most points within `tolerance`, then refits on the inliers. Returns a dict with
    cx, cy, r, rmse, n_inliers, inlier (mask), or None. With n or fewer points the algebraic fit is
    used directly (TreeLS does the same).
    """
    rng = np.random.default_rng() if rng is None else rng
    npts = len(x)
    if npts < 3:
        return None
    if npts <= n:
        fit = fit_circle_algebraic(x, y)
        if fit is None:
            return None
        res = circle_residuals(x, y, *fit)
        inl = np.abs(res) <= tolerance
        rmse = float(np.sqrt(np.mean(res ** 2)))
        return {"cx": fit[0], "cy": fit[1], "r": fit[2], "rmse": rmse, "n_inliers": int(inl.sum()), "inlier": inl}
    k = int((n_best + 5) * math.ceil(math.log(1 - conf) / math.log(1 - inliers ** n)))
    best_fit, best_score = None, None
    for _ in range(k):
        idx = rng.choice(npts, size=n, replace=False)
        fit = fit_circle_algebraic(x[idx], y[idx])
        if fit is None:
            continue
        res = np.abs(circle_residuals(x, y, *fit))
        inl = res <= tolerance
        n_in = int(inl.sum())
        if n_in < 3:
            continue
        score = (n_in, -float(np.sqrt(np.mean(res[inl] ** 2))))
        if best_score is None or score > best_score:
            best_fit, best_score = fit, score
    if best_fit is None:
        return None
    # refine: refit on the inliers, discard points outside the tolerance, and repeat once
    fit = best_fit
    for _ in range(2):
        inl = np.abs(circle_residuals(x, y, *fit)) <= tolerance
        if inl.sum() < 3:
            break
        refit = fit_circle_algebraic(x[inl], y[inl])
        if refit is None:
            break
        fit = refit
    res = circle_residuals(x, y, *fit)
    inl = np.abs(res) <= tolerance
    if inl.sum() < 3:
        return None
    rmse = float(np.sqrt(np.mean(res[inl] ** 2)))
    return {"cx": fit[0], "cy": fit[1], "r": fit[2], "rmse": rmse, "n_inliers": int(inl.sum()), "inlier": inl}


# ---------------------------------------------------------------------------
# One tile
# ---------------------------------------------------------------------------

def detect_stems(pts: dict, min_points: int = 20, max_dbh_cm: float = 200.0, slice_m=(1.12, 1.62), min_height: float = 2.0,
                 cluster_eps_m: float = 0.10, cluster_min_samples: int = 5, ransac_n: int = 20, ransac_conf: float = 0.95,
                 ransac_inliers: float = 0.9, ransac_n_best: int = 10, tolerance_m: float = 0.025,
                 layer_range_m=(1.0, 5.0), h_step_m: float = 0.5, min_layer_frac: float = 0.75, layer_tolerance_m: float = 0.15,
                 merge_dist_m: float = 0.2, crop_radius_m: float = 3.0, seed: int = 21, log=None) -> pd.DataFrame:
    """
    Stems for one tile given the arrays from cloud.load_points (x, y, z height above ground,
    classification). Returns a DataFrame with OUT_COLS (no geometry).
    """
    from sklearn.cluster import DBSCAN
    empty = pd.DataFrame({c: pd.Series(dtype=("str" if c in ("treeID", "condition") else float)) for c in OUT_COLS})
    x = np.asarray(pts["x"], float); y = np.asarray(pts["y"], float); z = np.asarray(pts["z"], float)
    cls = np.asarray(pts.get("classification", np.zeros(len(x))), dtype=np.int64)
    keep = (z >= 0) & ~np.isin(cls, DROP_CLASSES) & np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
    x, y, z = x[keep], y[keep], z[keep]
    if len(z) == 0 or z.max() < min_height:
        _say(log, "treels_stem_dbh: tile skipped (no points above min_height)")
        return empty
    max_d = max_dbh_cm / 100.0

    # ---- DBH slice and horizontal clustering (replaces map.hough + stm.hough) ----
    in_slice = (z > slice_m[0]) & (z < slice_m[1])
    sx, sy = x[in_slice], y[in_slice]
    if len(sx) < max(3, cluster_min_samples):
        return empty
    labels = DBSCAN(eps=cluster_eps_m, min_samples=cluster_min_samples).fit(np.column_stack([sx, sy])).labels_
    rng = np.random.default_rng(seed)
    rows = []
    for lab in np.unique(labels[labels >= 0]):
        m = labels == lab
        if m.sum() < min_points:
            continue
        cx_, cy_ = sx[m], sy[m]
        if max(cx_.max() - cx_.min(), cy_.max() - cy_.min()) > 1.5 * max_d:
            continue                                            # a wall, a log, or a shrub band, not a stem
        fit = fit_circle_ransac(cx_, cy_, n=ransac_n, conf=ransac_conf, inliers=ransac_inliers, n_best=ransac_n_best,
                                tolerance=tolerance_m, rng=rng)
        if fit is None or not np.isfinite(fit["r"]) or fit["r"] <= 0 or 2 * fit["r"] > max_d:
            continue
        rows.append({"stem_x": fit["cx"], "stem_y": fit["cy"], "radius_m": fit["r"], "fit_rmse_m": fit["rmse"],
                     "n_points": int(m.sum()), "n_inliers": fit["n_inliers"], "inlier_frac": fit["n_inliers"] / m.sum()})
    if not rows:
        return empty
    df = pd.DataFrame(rows)

    # ---- merge near duplicate centers (treeMap.merge d = 0.2): keep the fit with more inliers ----
    df = df.sort_values("n_inliers", ascending=False).reset_index(drop=True)
    centers = df[["stem_x", "stem_y"]].to_numpy()
    drop = np.zeros(len(df), bool)
    pairs = cKDTree(centers).query_pairs(merge_dist_m)
    for i, j in sorted(pairs):
        if not drop[i]:
            drop[max(i, j)] = True                              # rows are ordered by n_inliers, the later one goes
    df = df.loc[~drop].reset_index(drop=True)
    centers = df[["stem_x", "stem_y"]].to_numpy()

    # ---- assign every point to the nearest stem within crop_radius_m (trp.crop l = 3) ----
    dist, near = cKDTree(centers).query(np.column_stack([x, y]), k=1, distance_upper_bound=crop_radius_m)
    found = np.isfinite(dist)
    height = np.full(len(df), -np.inf)
    np.maximum.at(height, near[found], z[found])                 # tlsInventory hp = 1: top height per stem
    df["tree_height_m"] = np.where(np.isfinite(height), height, np.nan)

    # ---- vertical continuity (approximates the 3/4 of stacked layers rule in map.hough) ----
    if min_layer_frac > 0:
        n_layers = int(math.ceil((layer_range_m[1] - layer_range_m[0]) / h_step_m))
        radii = df["radius_m"].to_numpy()
        col = found & (dist <= radii[np.minimum(near, len(df) - 1)] + layer_tolerance_m) & (z >= layer_range_m[0]) & (z < layer_range_m[1])
        layer = np.floor((z[col] - layer_range_m[0]) / h_step_m).astype(int)
        occ = np.zeros((len(df), n_layers), dtype=np.int64)
        np.add.at(occ, (near[col], layer), 1)
        frac = (occ > 0).sum(axis=1) / n_layers
        df = df.loc[frac >= min_layer_frac].reset_index(drop=True)
        if df.empty:
            return empty

    # ---- the R post-processing columns ----
    df["radius_error_m"] = df["fit_rmse_m"]
    df["dbh_m"] = df["radius_m"] * 2
    df["dbh_cm"] = df["dbh_m"] * 100
    df["basal_area_m2"] = np.pi * df["radius_m"] ** 2
    df["basal_area_ft2"] = df["basal_area_m2"] * 10.764
    df["treeID"] = [f"{a}_{b}" for a, b in zip(df["stem_x"], df["stem_y"])]
    df["condition"] = "detected_stem"
    df = df[np.isfinite(df["radius_m"]) & (df["dbh_m"] <= max_d)].reset_index(drop=True)
    return df[OUT_COLS]


# ---------------------------------------------------------------------------
# treels_stem_dbh
# ---------------------------------------------------------------------------

def treels_stem_dbh(norm_las, min_points: int = 20, max_dbh_cm: float = 200.0, slice_m=(1.12, 1.62), min_height: float = 2.0,
                    cluster_eps_m: float = 0.10, cluster_min_samples: int = 5, ransac_n: int = 20, ransac_conf: float = 0.95,
                    ransac_inliers: float = 0.9, ransac_n_best: int = 10, tolerance_m: float = 0.025,
                    layer_range_m=(1.0, 5.0), h_step_m: float = 0.5, min_layer_frac: float = 0.75, layer_tolerance_m: float = 0.15,
                    merge_dist_m: float = 0.2, crop_radius_m: float = 3.0, crs=None, seed: int = 21, outfolder=None,
                    log=None) -> gpd.GeoDataFrame:
    """
    treels_stem_dbh(): detect stems in the DBH slice of every normalized tile and estimate DBH from a
    circle fit. norm_las is a file, a directory, a list, or a dict of arrays from cloud.load_points.
    Returns a Point GeoDataFrame (stem_x, stem_y, dbh_cm, radius_m, n_points, fit_rmse_m, ...) in `crs`
    (or the LAS header CRS); this is what dbh.trees_dbh(treels_dbh_locations=...) consumes.
    """
    kw = dict(min_points=min_points, max_dbh_cm=max_dbh_cm, slice_m=slice_m, min_height=min_height, cluster_eps_m=cluster_eps_m,
              cluster_min_samples=cluster_min_samples, ransac_n=ransac_n, ransac_conf=ransac_conf, ransac_inliers=ransac_inliers,
              ransac_n_best=ransac_n_best, tolerance_m=tolerance_m, layer_range_m=layer_range_m, h_step_m=h_step_m,
              min_layer_frac=min_layer_frac, layer_tolerance_m=layer_tolerance_m, merge_dist_m=merge_dist_m,
              crop_radius_m=crop_radius_m, seed=seed, log=log)
    if isinstance(norm_las, dict):
        tiles = [("points", norm_las)]
    else:
        files = cloud.list_las(norm_las)
        if not files:
            raise FileNotFoundError(f"could not detect .las|.laz files at {norm_las}")
        tiles = [(f.stem, f) for f in files]
    out_crs = crs
    parts = []
    for name, t in tiles:
        if isinstance(t, dict):
            pts = t
        else:
            pts = cloud.load_points(t)
            if out_crs is None:
                hdr_crs = cloud.las_header(t)["crs"]
                out_crs = hdr_crs if hdr_crs is not None else None
        df = detect_stems(pts, **kw)
        _say(log, f"treels_stem_dbh: {name}: {len(df)} stems")
        if df.empty:
            continue
        gdf = gpd.GeoDataFrame(df, geometry=[Point(a, b) for a, b in zip(df["stem_x"], df["stem_y"])], crs=out_crs)
        if outfolder is not None:
            Path(outfolder).mkdir(parents=True, exist_ok=True)
            gdf.to_file(Path(outfolder) / f"{name}.gpkg", driver="GPKG")
        parts.append(gdf)
    if not parts:
        empty = pd.DataFrame({c: pd.Series(dtype=("str" if c in ("treeID", "condition") else float)) for c in OUT_COLS})
        return gpd.GeoDataFrame(empty, geometry=gpd.GeoSeries([], dtype="geometry"), crs=out_crs)
    res = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=out_crs)
    return res[res.geometry.notna() & ~res.geometry.is_empty].reset_index(drop=True)
