"""Synthetic checks for c2t.cbh: one cone tree through the LadderFuelsR chain, and trees_cbh() end to end."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from c2t import cbh  # noqa: E402


def cone_points(rng, cx, cy, height, base, radius, n=1500, n_low=30, n_ground=30):
    """A cone of foliage points from `base` to `height` (widest at the base), a few low class 1 points
    below 0.3 m, and class 2 ground points that the pipeline must drop."""
    h = rng.uniform(base, height, n)
    r = radius * (height - h) / (height - base) * np.sqrt(rng.uniform(0, 1, n))
    a = rng.uniform(0, 2 * np.pi, n)
    lx = cx + rng.uniform(-radius, radius, n_low); ly = cy + rng.uniform(-radius, radius, n_low)
    gx = cx + rng.uniform(-radius, radius, n_ground); gy = cy + rng.uniform(-radius, radius, n_ground)
    x = np.r_[cx + r * np.cos(a), lx, gx]; y = np.r_[cy + r * np.sin(a), ly, gy]
    z = np.r_[h, rng.uniform(0, 0.3, n_low), rng.uniform(0, 0.05, n_ground)]
    cls = np.r_[np.full(n, 1), np.full(n_low, 1), np.full(n_ground, 2)].astype(np.uint8)
    return x, y, z, cls


def write_las(path, x, y, z, cls):
    import laspy
    hdr = laspy.LasHeader(point_format=3, version="1.2")
    hdr.offsets = [float(x.min()), float(y.min()), 0.0]
    hdr.scales = [0.001, 0.001, 0.001]
    las = laspy.LasData(hdr)
    las.x = x; las.y = y; las.z = z
    las.classification = cls
    las.return_number = np.ones(len(x), dtype=np.uint8)
    las.number_of_returns = np.ones(len(x), dtype=np.uint8)
    las.write(path)


def forest(tmp_path, n_trees, seed=7):
    """n_trees cones on a grid, one LAS file, and the crown polygons with treeID and tree_height_m."""
    rng = np.random.default_rng(seed)
    xs, ys, zs, cs, rows = [], [], [], [], []
    for i in range(n_trees):
        cx = 500000.0 + 14.0 * (i % 4) + rng.uniform(-0.4, 0.4)
        cy = 4300000.0 + 14.0 * (i // 4) + rng.uniform(-0.4, 0.4)
        height = rng.uniform(9.0, 20.0); base = rng.uniform(2.0, 0.4 * height); radius = rng.uniform(2.0, 3.5)
        x, y, z, c = cone_points(rng, cx, cy, height, base, radius)
        xs.append(x); ys.append(y); zs.append(z); cs.append(c)
        rows.append({"treeID": f"{i + 1}_{cx:.1f}_{cy:.1f}", "tree_x": cx, "tree_y": cy, "tree_height_m": height,
                     "true_cbh_m": base, "crown_area_m2": np.pi * radius ** 2, "geometry": Point(cx, cy).buffer(radius + 0.5)})
    las_path = tmp_path / "tile_normalize.las"
    write_las(las_path, np.concatenate(xs), np.concatenate(ys), np.concatenate(zs), np.concatenate(cs))
    crowns = gpd.GeoDataFrame(pd.DataFrame(rows), geometry="geometry", crs="EPSG:26910")
    return crowns, las_path


def test_lowest_cbh_single_cone():
    rng = np.random.default_rng(1)
    x, y, z, cls = cone_points(rng, 100.3, 200.7, 12.0, 3.0, 3.0)
    keep = cls != 2                                   # the pipeline drops ground before the profile
    pts = {"x": x[keep], "y": y[keep], "z": z[keep]}
    prof = cbh.lad_profile(pts)
    assert list(prof.columns) == ["height", "lad", "pulses", "total_pulses"]
    assert prof["height"].iloc[0] == 1.5 and (prof["lad"].iloc[:2] == 0).all()   # bins 1 and 2 are the gap
    res = cbh.ladderfuelsr_cbh(points_dict=pts, tree_height_m=12.0)
    assert res["ok"], res["reason"]
    assert 2.0 <= res["cbh_last_height_m"] <= 4.0
    assert res["nlayers"] == 1
    assert res["cbh_last_height_m"] == res["cbh_maxlad_height_m"] == res["cbh_max_height_m"] == res["Hcbh1"]
    assert list(res["layers"].columns) == ["Hcbh", "Hdptf", "dptf", "Hdist", "effdist", "lad_pct"]


def test_fuel_layers_two_layers_and_merge():
    h = np.arange(1, 16) + 0.5
    lad = np.array([0.3, 0.25, 0.01, 0.01, 0.01, 0.4, 0.8, 1.2, 0.01, 0.7, 0.5, 0.3, 0.2, 0.01, 0.01])
    layers, why = cbh.fuel_layers(h, lad)
    assert why == "ok" and len(layers) == 2
    assert layers["Hcbh"].tolist() == [1.0, 6.0]           # low layer at the bottom, crown base at bin 6
    assert layers["effdist"].tolist() == [0.0, 3.0]        # bins 3, 4, 5 are the gap
    assert layers["Hdptf"].iloc[1] == 13.0                 # the one bin dip at bin 9 is merged (num_jump_steps 1)
    m = cbh.cbh_metrics(layers)
    assert m["last_Hcbh"] == 6.0 and m["max_Hcbh"] == 6.0 and m["maxlad_Hcbh"] == 6.0 and m["Hcbh1"] == 1.0
    assert cbh.fuel_layers(np.arange(1, 4) + 0.5, np.array([0.1, 0.2, 0.3]))[0] is None
    assert cbh.fuel_layers(h, np.full(15, 0.01))[0] is None


def test_trees_cbh_five_crowns(tmp_path):
    crowns, las_path = forest(tmp_path, 5)
    out = cbh.trees_cbh(crowns, las_path, estimate_missing_cbh=True, outfolder=tmp_path / "cbh")
    assert isinstance(out, gpd.GeoDataFrame) and len(out) == 5 and out.crs == crowns.crs
    assert {"tree_cbh_m", "is_training_cbh"} <= set(out.columns)
    assert out["is_training_cbh"].dtype == bool
    got = out[out["is_training_cbh"]]
    assert len(got) >= 4                                    # every cone is sampled; extraction should succeed
    assert (got["tree_cbh_m"] < got["tree_height_m"]).all()
    assert (np.abs(got["tree_cbh_m"] - got["true_cbh_m"]) <= 1.5).all()
    assert (tmp_path / "cbh" / "cbh_training_data.csv").exists()
    # five trees is not enough for the model (needs more than 10), so nothing is imputed
    assert out.loc[~out["is_training_cbh"], "tree_cbh_m"].isna().all()


def test_trees_cbh_imputation(tmp_path):
    crowns, las_path = forest(tmp_path, 16, seed=11)
    out = cbh.trees_cbh(crowns, las_path, tree_sample_n=12, estimate_missing_cbh=True, outfolder=tmp_path / "cbh", seed=3)
    assert len(out) == 16
    train = out[out["is_training_cbh"]]; imputed = out[~out["is_training_cbh"]]
    assert len(train) > 10 and len(imputed) >= 4
    assert np.isfinite(imputed["tree_cbh_m"]).all()
    assert (imputed["tree_cbh_m"] < imputed["tree_height_m"]).all()
    assert (tmp_path / "cbh" / "cbh_model_1.joblib").exists()
    # which_cbh alternatives run and the LadderFuelsR mapping holds (max_Hcbh <= last_Hcbh)
    hi = cbh.trees_cbh(crowns, las_path, tree_sample_n=12, which_cbh="highest", estimate_missing_cbh=False, seed=3)
    lo = out.set_index("treeID").loc[hi.loc[hi["is_training_cbh"], "treeID"], "tree_cbh_m"]
    assert (hi.loc[hi["is_training_cbh"], "tree_cbh_m"].values <= lo.values + 1e-9).all()
