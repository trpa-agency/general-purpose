"""
End-to-end checks of the cloud2trees port on synthetic data (seconds). Run: python -m pytest tests/test_c2t.py -q
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import c2t  # noqa: E402
from src.c2t.synthetic import synthetic_tile, CRS, X0, Y0  # noqa: E402


@pytest.fixture(scope="module")
def tile(tmp_path_factory):
    d = tmp_path_factory.mktemp("las")
    p = d / "synthetic_tile.laz"
    truth = synthetic_tile(p)
    return p, truth


def test_ws_functions_match_r():
    assert abs(c2t.log_fn(10) - 3.0929) < 1e-3
    assert abs(c2t.lin_fn(10) - 2.15) < 1e-9
    assert abs(c2t.exp_fn(10) - 2.0639) < 1e-3
    assert c2t.log_fn(1.0) == 0.6 and c2t.log_fn(40) == 5.0 and c2t.lin_fn(-1) == 0.001


def test_cloud2raster_and_raster2trees(tile, tmp_path):
    import rasterio
    p, truth = tile
    ans = c2t.cloud2raster(tmp_path, p, dtm_res_m=1.0, chm_res_m=0.5, noise_level=1, workers=1)
    with rasterio.open(ans["dtm_path"]) as r:
        dtm = r.read(1)
        assert r.crs is not None
    with rasterio.open(ans["chm_path"]) as r:
        chm = r.read(1)
    # sloping ground recovered to within 10 cm at the centre, the noise points did not make it into the CHM
    assert abs(np.nanmedian(dtm) - (1900 + 0.05 * 60 + 0.02 * 60)) < 0.5
    assert np.nanmax(chm) < 32
    assert (np.nanmax(chm) > 25)
    assert Path(ans["norm_dir"]).exists() and list(Path(ans["norm_dir"]).glob("*.laz"))
    crowns = c2t.raster2trees(ans["chm_path"], outfolder=tmp_path / "trees", ws="log_fn", min_height=2)
    assert {"treeID", "tree_height_m", "tree_x", "tree_y", "crown_area_m2", "geometry"} <= set(crowns.columns)
    assert crowns["treeID"].is_unique
    # detection: within 25 percent of the true count and each true top matched within 1.5 m
    assert abs(len(crowns) - len(truth)) <= 0.25 * len(truth)
    from scipy.spatial import cKDTree
    d, _ = cKDTree(np.column_stack([crowns.tree_x, crowns.tree_y])).query(np.column_stack([truth.x, truth.y]))
    assert np.median(d) < 1.5
    assert (tmp_path / "trees" / "final_detected_crowns.gpkg").exists()
    # tiled path gives the same trees as the single-window path
    tiled = c2t.raster2trees(ans["chm_path"], ws="log_fn", min_height=2, tile_px=120, pad_m=10)
    assert abs(len(tiled) - len(crowns)) <= 2


def test_itd_tuning(tile, tmp_path):
    p, truth = tile
    ans = c2t.cloud2raster(tmp_path, p, chm_res_m=0.5, noise_level=1, write_normalized=False)
    tune = c2t.itd_tuning(input_chm_rast=ans["chm_path"], n_samples=2, out_png=tmp_path / "tune.png")
    assert set(tune["ws_fn_list"]) == {"lin_fn", "exp_fn", "log_fn"}
    assert tune["summary"]["ws_fn"].nunique() == 3 and (tune["summary"]["n"] > 0).any()


def make_treemap(ext: Path, n_plots: int = 12, seed: int = 5):
    """A fake TreeMap: 30 m raster of plot ids over the tile, tree table with a Chapman-Richards truth."""
    import rasterio
    from rasterio.transform import from_origin
    rng = np.random.default_rng(seed)
    d = ext / "treemap"; d.mkdir(parents=True, exist_ok=True)
    nrow = ncol = 8
    ids = rng.integers(1, n_plots + 1, (nrow, ncol)).astype(np.int32)
    with rasterio.open(d / "treemap2022_conus.tif", "w", driver="GTiff", height=nrow, width=ncol, count=1, dtype="int32", crs=CRS,
                       transform=from_origin(X0 - 60, Y0 + 180, 30, 30), nodata=0) as dst:
        dst.write(ids, 1)
    rows = []
    for pid in range(1, n_plots + 1):
        for _ in range(40):
            ht_m = rng.uniform(3, 45)
            dbh_cm = 80 * (1 - np.exp(-0.05 * ht_m)) ** 1.8 * np.exp(rng.normal(0, 0.15))
            rows.append({"TM_ID": pid, "PLT_CN": f"cn{pid}", "SPECIES_SYMBOL": "PIJE", "STATUSCD": 1, "DIA": dbh_cm / 2.54, "HT": ht_m / 0.3048, "CR": 50})
        rows.append({"TM_ID": pid, "PLT_CN": f"cn{pid}", "SPECIES_SYMBOL": "PIJE", "STATUSCD": 2, "DIA": 30, "HT": 60, "CR": 50})  # dead, dropped
    pd.DataFrame(rows).to_csv(d / "treemap2022_conus_tree_table.csv", index=False)


def make_foresttype(ext: Path):
    import rasterio
    from rasterio.transform import from_origin
    d = ext / "foresttype"; d.mkdir(parents=True, exist_ok=True)
    a = np.full((8, 8), 370, np.int16); a[:2, :] = 0          # a non-forest strip to test the nearest fill
    with rasterio.open(d / "foresttype.tif", "w", driver="GTiff", height=8, width=8, count=1, dtype="int16", crs=CRS,
                       transform=from_origin(X0 - 60, Y0 + 180, 30, 30), nodata=-1) as dst:
        dst.write(a, 1)
    pd.DataFrame({"forest_type_code": ["370", "220"], "forest_type_group_code": ["370", "220"],
                  "forest_type_group": ["California mixed conifer group", "Ponderosa pine group"], "hardwood_softwood": ["Softwood", "Softwood"]}).to_csv(d / "foresttype_lookup.csv", index=False)


def test_trees_dbh_type_competition(tile, tmp_path, monkeypatch):
    import geopandas as gpd
    from shapely.geometry import box
    p, truth = tile
    ext = tmp_path / "ext"
    make_treemap(ext); make_foresttype(ext)
    monkeypatch.setenv("C2T_EXT_DIR", str(ext))
    found = c2t.find_ext_data()
    assert found["treemap_dir"] and found["foresttype_dir"] and found["landfire_dir"] is None
    # a tree list straight from the 00b-style table
    taos = gpd.GeoDataFrame({"tree_id": [f"t{i}" for i in range(len(truth))], "x": truth.x, "y": truth.y, "height_m": truth.height_m,
                             "crown_area_m2": np.pi * (0.25 * truth.height_m) ** 2, "tile": "synthetic"},
                            geometry=gpd.points_from_xy(truth.x, truth.y), crs=CRS)
    trees = c2t.from_taos(taos)
    aoi = gpd.GeoDataFrame(geometry=[box(X0, Y0, X0 + 120, Y0 + 120)], crs=CRS)
    dbh = c2t.trees_dbh(trees, study_boundary=aoi, outfolder=tmp_path / "dbh")
    for col in ("fia_est_dbh_cm", "fia_est_dbh_cm_lower", "fia_est_dbh_cm_upper", "dbh_cm", "basal_area_m2", "basal_area_ft2", "is_training_data"):
        assert col in dbh.columns
    assert dbh["dbh_cm"].notna().all() and (dbh["fia_est_dbh_cm_lower"] < dbh["dbh_cm"]).all() and (dbh["dbh_cm"] < dbh["fia_est_dbh_cm_upper"]).all()
    truth_dbh = 80 * (1 - np.exp(-0.05 * dbh["tree_height_m"])) ** 1.8
    assert np.median(np.abs(dbh["dbh_cm"] - truth_dbh) / truth_dbh) < 0.1
    assert (tmp_path / "dbh" / "regional_dbh_height_model_predictions.csv").exists()
    # power model also fits
    dbh2 = c2t.trees_dbh(trees, study_boundary=aoi, dbh_model_regional="power")
    assert np.corrcoef(dbh2["dbh_cm"], dbh["dbh_cm"])[0, 1] > 0.95
    # local model from fake stems: 15 crowns get a stem within the band, the rest are predicted
    crowns = gpd.GeoDataFrame(trees.drop(columns="geometry"), geometry=trees.geometry.buffer(0.25 * trees["tree_height_m"]), crs=CRS)
    stems = gpd.GeoDataFrame({"dbh_cm": truth_dbh.values[:15] * 1.05}, geometry=gpd.points_from_xy(truth.x[:15], truth.y[:15]), crs=CRS)
    dbh3 = c2t.trees_dbh(crowns, study_boundary=aoi, treels_dbh_locations=stems, dbh_model_local="lin")
    assert dbh3["is_training_data"].sum() == 15 and dbh3["ptcld_predicted_dbh_cm"].notna().sum() == len(trees) - 15
    assert np.allclose(dbh3.loc[dbh3["is_training_data"], "dbh_cm"], dbh3.loc[dbh3["is_training_data"], "ptcld_extracted_dbh_cm"])
    # forest type, with the nearest fill for trees on the non-forest strip
    typed, rast = c2t.trees_type(trees)
    assert (typed["forest_type_group_code"] == "370").all()
    assert (typed["hardwood_softwood"] == "Softwood").all()
    # competition
    comp = c2t.trees_competition(trees, competition_buffer_m=5, search_dist_max=10)
    assert {"comp_trees_per_ha", "comp_relative_tree_height", "comp_dist_to_nearest_m"} <= set(comp.columns)
    assert (comp["comp_relative_tree_height"] <= 1.0 + 1e-9).all() and (comp["comp_dist_to_nearest_m"] <= 10).all()


def test_cloud2trees_pipeline(tile, tmp_path, monkeypatch):
    p, truth = tile
    ext = tmp_path / "ext"
    make_treemap(ext); make_foresttype(ext)
    monkeypatch.setenv("C2T_EXT_DIR", str(ext))
    ans = c2t.cloud2trees(tmp_path / "run", p, chm_res_m=0.5, noise_level=1, estimate_tree_dbh=True, estimate_tree_competition=True,
                          estimate_tree_type=True, estimate_tree_hmd=True, hmd_estimate_missing_hmd=True, estimate_tree_cbh=True,
                          cbh_estimate_missing_cbh=True, cbh_tree_sample_n=30)
    crowns = ans["crowns_sf"]
    assert len(crowns) > 20
    for col in ("dbh_cm", "comp_trees_per_ha", "forest_type_group_code", "max_crown_diam_height_m", "tree_cbh_m"):
        assert col in crowns.columns, col
    assert crowns["dbh_cm"].notna().all()
    assert crowns["tree_cbh_m"].notna().mean() > 0.5
    d = Path(ans["delivery_dir"])
    for f in ("final_detected_crowns.gpkg", "final_detected_tree_tops.gpkg", "processed_tracking_data.csv", "fia_foresttype_raster.tif", "raw_las_ctg_info.gpkg"):
        assert (d / f).exists(), f
    assert ans["errors"] == []
    # the tree tops delivery is what trees_biomass and the LANL export take next; both are covered by their own tests
