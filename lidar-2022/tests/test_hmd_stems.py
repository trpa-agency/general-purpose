"""Synthetic checks for c2t.hmd (trees_hmd) and c2t.stems (treels_stem_dbh)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from c2t import hmd, stems  # noqa: E402

CRS = "EPSG:26910"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def write_las(path, x, y, z, cls=None):
    import laspy
    hdr = laspy.LasHeader(point_format=6, version="1.4")
    hdr.offsets = [0.0, 0.0, 0.0]
    hdr.scales = [0.001, 0.001, 0.001]
    las = laspy.LasData(hdr)
    las.x = np.asarray(x, float); las.y = np.asarray(y, float); las.z = np.asarray(z, float)
    las.classification = np.ones(len(x), dtype=np.uint8) if cls is None else np.asarray(cls, dtype=np.uint8)
    las.write(path)
    return path


def crown_points(cx, cy, height, hmd_height, max_radius, rng, jitter=0.03):
    """A crown whose horizontal extent peaks at hmd_height: rings of points at every 0.25 m of height."""
    xs, ys, zs = [], [], []
    for h in np.arange(0.5, height, 0.25):
        r = max_radius * (1.0 - abs(h - hmd_height) / max(hmd_height, height - hmd_height))
        r = max(r, 0.05)
        for frac in (0.3, 0.6, 1.0):
            ang = np.linspace(0, 2 * np.pi, 16, endpoint=False) + rng.uniform(0, 0.3)
            xs.append(cx + frac * r * np.cos(ang) + rng.normal(0, jitter, ang.size))
            ys.append(cy + frac * r * np.sin(ang) + rng.normal(0, jitter, ang.size))
            zs.append(np.full(ang.size, h))
    xs.append(np.array([cx])); ys.append(np.array([cy])); zs.append(np.array([height]))   # the top
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)


def stem_points(cx, cy, radius, rng, top=6.0, noise=0.005, n_per_layer=80):
    """Points on a vertical cylinder surface between 0.2 m and top, plus noise in xy."""
    xs, ys, zs = [], [], []
    for h in np.arange(0.2, top, 0.05):
        ang = rng.uniform(0, 2 * np.pi, n_per_layer)
        xs.append(cx + radius * np.cos(ang) + rng.normal(0, noise, n_per_layer))
        ys.append(cy + radius * np.sin(ang) + rng.normal(0, noise, n_per_layer))
        zs.append(np.full(n_per_layer, h) + rng.uniform(0, 0.05, n_per_layer))
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)


# ---------------------------------------------------------------------------
# (a) HMD of one synthetic crown
# ---------------------------------------------------------------------------

def test_calc_tree_hmd_widest_at_6m():
    rng = np.random.default_rng(1)
    x, y, z = crown_points(500.0, 500.0, height=12.0, hmd_height=6.0, max_radius=3.0, rng=rng)
    h, max_z, n = hmd.calc_tree_hmd(x, y, z)
    assert n == len(x)
    assert max_z == 12.0
    assert abs(h - 6.0) <= 1.0


def test_trees_hmd_single_crown_from_las(tmp_path):
    rng = np.random.default_rng(2)
    x, y, z = crown_points(500.0, 500.0, height=12.0, hmd_height=6.0, max_radius=3.0, rng=rng)
    # ground returns (class 2) and a water return (class 9) inside the crown must be ignored
    x = np.r_[x, 501.0, 499.0]; y = np.r_[y, 501.0, 499.0]; z = np.r_[z, 0.0, 0.0]
    cls = np.r_[np.ones(len(x) - 2, np.uint8), 2, 9]
    las = write_las(tmp_path / "tile_normalize.las", x, y, z, cls)
    crowns = gpd.GeoDataFrame({"treeID": ["1_500_500"], "tree_height_m": [12.0], "tree_x": [500.0], "tree_y": [500.0]},
                              geometry=[Point(500, 500).buffer(3.6)], crs=CRS)
    out = hmd.trees_hmd(crowns, las, tree_sample_prop=1, estimate_missing_hmd=False)
    assert list(out.columns[-2:]) == ["max_crown_diam_height_m", "is_training_hmd"]
    assert bool(out["is_training_hmd"].iloc[0]) is True
    assert abs(float(out["max_crown_diam_height_m"].iloc[0]) - 6.0) <= 1.0
    # HMD never exceeds height
    assert float(out["max_crown_diam_height_m"].iloc[0]) <= 12.0


# ---------------------------------------------------------------------------
# (b) trees_hmd with imputation
# ---------------------------------------------------------------------------

def _crown_grid(n, rng, start=0):
    """n non-overlapping crowns on a 10 m grid with heights between 8 and 20 m. Returns GeoDataFrame and centers."""
    rows = []
    for i in range(start, start + n):
        cx, cy = 500.0 + 10.0 * (i % 5), 500.0 + 10.0 * (i // 5)
        height = float(rng.uniform(8, 20))
        rows.append({"treeID": f"{i + 1}_{cx:.0f}_{cy:.0f}", "tree_height_m": height, "tree_x": cx, "tree_y": cy,
                     "geometry": Point(cx, cy).buffer(3.5)})
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def test_trees_hmd_imputes_five_crowns(tmp_path):
    rng = np.random.default_rng(3)
    with_pts = _crown_grid(12, rng)                        # training trees: R needs more than 10
    no_pts = _crown_grid(5, rng, start=12)                 # the five crowns to impute
    crowns = gpd.GeoDataFrame(pd.concat([with_pts, no_pts], ignore_index=True), crs=CRS)
    xs, ys, zs = [], [], []
    for r in with_pts.itertuples():
        x, y, z = crown_points(r.tree_x, r.tree_y, height=r.tree_height_m, hmd_height=0.5 * r.tree_height_m, max_radius=3.0, rng=rng)
        xs.append(x); ys.append(y); zs.append(z)
    # a crown with only 3 points does not count (calc_tree_hmd_n_pts < 5)
    r = no_pts.iloc[0]
    xs.append(np.array([r.tree_x, r.tree_x + 1, r.tree_x - 1])); ys.append(np.full(3, r.tree_y)); zs.append(np.array([r.tree_height_m, 3.0, 4.0]))
    las_dir = tmp_path / "norm"; las_dir.mkdir()
    write_las(las_dir / "a_normalize.las", np.concatenate(xs), np.concatenate(ys), np.concatenate(zs))
    out = hmd.trees_hmd(crowns, las_dir, tree_sample_prop=1, estimate_missing_hmd=True, outfolder=tmp_path / "models")
    assert len(out) == 17
    assert out["is_training_hmd"].sum() == 12
    train = out[out["is_training_hmd"]]
    imputed = out[~out["is_training_hmd"]]
    assert np.allclose(train["max_crown_diam_height_m"], 0.5 * train["tree_height_m"], atol=1.0)
    assert imputed["max_crown_diam_height_m"].notna().all()
    assert (imputed["max_crown_diam_height_m"] > 0).all()
    assert (imputed["max_crown_diam_height_m"] <= imputed["tree_height_m"]).all()
    assert (tmp_path / "models" / "hmd_height_model_estimates.joblib").exists()
    # the original columns and geometry survive
    assert out.geometry.geom_type.eq("Polygon").all() and "treeID" in out.columns


def test_trees_hmd_five_crowns_without_imputation(tmp_path):
    """Five crowns only: too few for a model, so the R code returns the extracted values and NA for the rest."""
    rng = np.random.default_rng(4)
    crowns = _crown_grid(5, rng)
    xs, ys, zs = [], [], []
    for r in crowns.iloc[:3].itertuples():
        x, y, z = crown_points(r.tree_x, r.tree_y, height=r.tree_height_m, hmd_height=0.4 * r.tree_height_m, max_radius=3.0, rng=rng)
        xs.append(x); ys.append(y); zs.append(z)
    las = write_las(tmp_path / "b_normalize.las", np.concatenate(xs), np.concatenate(ys), np.concatenate(zs))
    out = hmd.trees_hmd(crowns, las, tree_sample_n=10, estimate_missing_hmd=True)
    assert out["is_training_hmd"].tolist() == [True, True, True, False, False]
    assert out["max_crown_diam_height_m"].iloc[3:].isna().all()


def test_check_sample_vals_defaults():
    assert hmd.check_sample_vals(None, None, 777) == (777.0, None)
    assert hmd.check_sample_vals(50, 0.2, 777) == (50.0, None)
    assert hmd.check_sample_vals(None, 0.2, 777) == (None, 0.2)
    assert hmd.check_sample_vals(None, 5, 777) == (None, 1.0)
    assert hmd.check_sample_vals(None, -1, 777) == (None, 0.5)
    assert hmd.check_sample_vals(0, None, 777) == (777.0, None)


# ---------------------------------------------------------------------------
# (c) stem DBH from a circle of points
# ---------------------------------------------------------------------------

def test_circle_fit_recovers_radius():
    rng = np.random.default_rng(5)
    ang = rng.uniform(0, 2 * np.pi, 300)
    x = 10.0 + 0.2 * np.cos(ang) + rng.normal(0, 0.005, 300)
    y = 20.0 + 0.2 * np.sin(ang) + rng.normal(0, 0.005, 300)
    # a few outliers well off the circle
    x = np.r_[x, 10.35, 9.6, 10.0]; y = np.r_[y, 20.0, 20.1, 20.5]
    fit = stems.fit_circle_ransac(x, y, rng=rng)
    assert abs(fit["r"] - 0.2) < 0.02 and abs(fit["cx"] - 10.0) < 0.02 and abs(fit["cy"] - 20.0) < 0.02
    assert fit["n_inliers"] >= 290


def test_treels_stem_dbh_synthetic_stem(tmp_path):
    rng = np.random.default_rng(6)
    x1, y1, z1 = stem_points(500.0, 500.0, 0.20, rng)
    x2, y2, z2 = stem_points(504.0, 503.0, 0.12, rng)
    # ground, low shrub blob, and a canopy blob above the first stem
    n_g = 2000
    gx = rng.uniform(495, 510, n_g); gy = rng.uniform(495, 510, n_g); gz = np.zeros(n_g)
    n_s = 300
    shx = rng.uniform(507, 508, n_s); shy = rng.uniform(497, 498, n_s); shz = rng.uniform(0.3, 1.5, n_s)
    n_c = 3000
    cx = rng.uniform(497, 503, n_c); cy = rng.uniform(497, 503, n_c); cz = rng.uniform(6, 12, n_c)
    x = np.concatenate([x1, x2, gx, shx, cx]); y = np.concatenate([y1, y2, gy, shy, cy]); z = np.concatenate([z1, z2, gz, shz, cz])
    cls = np.concatenate([np.ones(len(x1) + len(x2), np.uint8), np.full(n_g, 2, np.uint8), np.ones(n_s + n_c, np.uint8)])
    las = write_las(tmp_path / "stems_normalize.las", x, y, z, cls)
    out = stems.treels_stem_dbh(las, crs=CRS, outfolder=tmp_path / "stems")
    assert isinstance(out, gpd.GeoDataFrame) and out.crs is not None
    assert out.geometry.geom_type.eq("Point").all()
    for c in ("stem_x", "stem_y", "dbh_cm", "radius_m", "n_points", "fit_rmse_m"):
        assert c in out.columns
    assert len(out) == 2
    big = out.iloc[(out["stem_x"] - 500.0).abs().argmin()]
    small = out.iloc[(out["stem_x"] - 504.0).abs().argmin()]
    assert abs(big["dbh_cm"] - 40.0) / 40.0 <= 0.10
    assert abs(small["dbh_cm"] - 24.0) / 24.0 <= 0.10
    assert abs(big["stem_x"] - 500.0) < 0.05 and abs(big["stem_y"] - 500.0) < 0.05
    assert big["fit_rmse_m"] < 0.02
    assert big["tree_height_m"] > 11.0                       # max z within 3 m of the stem (the canopy blob)
    assert (tmp_path / "stems" / "stems_normalize.gpkg").exists()


def test_treels_stem_dbh_skips_empty_tile():
    pts = {"x": np.array([0.0, 1.0]), "y": np.array([0.0, 1.0]), "z": np.array([0.5, 1.0]), "classification": np.array([1, 1])}
    out = stems.treels_stem_dbh(pts, crs=CRS)
    assert len(out) == 0 and "dbh_cm" in out.columns
