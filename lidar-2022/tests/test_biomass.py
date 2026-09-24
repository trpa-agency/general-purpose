"""
Synthetic checks for c2t.biomass: a 3 x 3 grid of 30 m cells written as the LANDFIRE product encodes it
(Int16, CBD x 100, 0 = non-forested, -9999 = nodata) in EPSG:26910, a matching forest type group grid, and a
handful of trees. Runs in seconds: python -m pytest tests/test_biomass.py -q
"""
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from c2t import biomass  # noqa: E402

CRS = "EPSG:26910"
X0, Y0, RES = 760000.0, 4330090.0, 30.0   # top-left corner; cells cover x 760000..760090, y 4330000..4330090

# row 0: nodata, nodata, non-forest; row 1: 0.10, 0.12, non-forest; row 2: 0.15, 0.09, non-forest
CBD_X100 = np.array([[-9999, -9999, 0],
                     [10, 12, 0],
                     [15, 9, 0]], dtype="int16")


def write_raster(path, values, dtype, nodata):
    with rasterio.open(path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1], count=1,
                       dtype=dtype, crs=CRS, transform=from_origin(X0, Y0, RES, RES), nodata=nodata) as dst:
        dst.write(values.astype(dtype), 1)
    return path


@pytest.fixture
def ext_dir(tmp_path):
    """LANDFIRE CBD and forest type group rasters on the same 3 x 3 grid."""
    write_raster(tmp_path / "lc23_cbd_240.tif", CBD_X100, "int16", -9999)
    write_raster(tmp_path / "foresttype.tif", np.full((3, 3), 261, dtype="int16"), "int16", -9999)
    return tmp_path


def make_trees(xs, ys, heights, cbhs, areas, dbhs, ftg="260"):
    df = pd.DataFrame({
        "treeID": [str(i + 1) for i in range(len(xs))],
        "tree_x": xs, "tree_y": ys,
        "tree_height_m": heights, "tree_cbh_m": cbhs, "crown_area_m2": areas, "dbh_cm": dbhs,
        "forest_type_group_code": ftg,
    })
    return gpd.GeoDataFrame(df, geometry=[Point(x, y) for x, y in zip(xs, ys)], crs=CRS)


@pytest.fixture
def trees():
    """Six trees inside the centre cell (x 760030..760060, y 4330030..4330060), spanning enough that the tree
    bounding box buffered by 15 m covers the whole cell, so the stand overlap area is exactly 900 m2."""
    return make_trees(
        xs=[760035, 760055, 760035, 760055, 760045, 760050],
        ys=[4330035, 4330035, 4330055, 4330055, 4330045, 4330040],
        heights=[12, 15, 18, 20, 22, 16],
        cbhs=[4, 5, 6, 8, 9, 5],
        areas=[12, 18, 25, 30, 35, 20],
        dbhs=[45, 50, 55, 60, 65, 52],
    )


def run_dispatcher(*args, **kwargs):
    """trees_biomass() swallows method failures into warnings like R; surface them as test failures."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = biomass.trees_biomass(*args, **kwargs)
    failures = [str(w.message) for w in caught if "Could not get" in str(w.message)]
    assert not failures, failures
    return out


def test_landfire_biomass_positive_and_conserved(ext_dir, trees):
    out = run_dispatcher(trees, method="landfire", input_landfire_dir=ext_dir)
    for c in ("crown_dia_m", "crown_length_m", "crown_volume_m3", "basal_area_m2", "landfire_stand_id",
              "landfire_tree_kg_per_m3", "landfire_stand_kg_per_m3", "landfire_crown_biomass_kg"):
        assert c in out.columns, c
    assert "landfire_cell_kg_per_m3" not in out.columns     # dropped at the end, as in R
    assert len(out) == 6 and list(out["treeID"]) == list(trees["treeID"])

    # crown geometry: ellipsoid volume reduces to (2/3) * crown length * crown area
    L = trees["tree_height_m"] - trees["tree_cbh_m"]
    np.testing.assert_allclose(out["crown_length_m"], L)
    np.testing.assert_allclose(out["crown_dia_m"], 2 * np.sqrt(trees["crown_area_m2"] / np.pi))
    np.testing.assert_allclose(out["crown_volume_m3"], (2.0 / 3.0) * L * trees["crown_area_m2"])
    np.testing.assert_allclose(out["basal_area_m2"], np.pi * (trees["dbh_cm"] / 200.0) ** 2)

    # centre cell CBD is 12 -> 0.12 kg/m3, same for every tree
    np.testing.assert_allclose(out["landfire_stand_kg_per_m3"], 0.12)
    assert out["landfire_stand_id"].nunique() == 1
    assert (out["landfire_crown_biomass_kg"] > 0).all()
    assert (out["landfire_tree_kg_per_m3"] < 2).all()      # the max_crown_kg_per_m3 cap must not have fired

    # conservation: sum of tree crown biomass = cell CBD * mean crown length * overlap area (900 m2 here)
    expected = 0.12 * L.mean() * 900.0
    assert expected == pytest.approx(0.12 * 11.0 * 900.0)
    np.testing.assert_allclose(out["landfire_crown_biomass_kg"].sum(), expected)
    np.testing.assert_allclose(out["landfire_tree_kg_per_m3"], expected / out["crown_volume_m3"].sum())

    cells = out.attrs["stand_cell_data_landfire"]
    assert len(cells) == 1
    assert cells["overlap_area_m2"].iloc[0] == pytest.approx(900.0)
    assert cells["trees"].iloc[0] == 6
    assert cells["biomass_kg"].iloc[0] == pytest.approx(expected)


def test_landfire_cbd_nearest_fill_for_nonforest_and_nodata(ext_dir):
    # tree 1 on the non-forest cell (row 1, col 2): nearest valid cell centre to that cell's centre is the centre
    # cell (0.12) at 30 m; tree 2 on the nodata cell (row 0, col 0): nearest valid is (row 1, col 0) = 0.10 at 30 m
    tl = make_trees(xs=[760062, 760005], ys=[4330045, 4330085], heights=[10, 10], cbhs=[3, 3], areas=[10, 10],
                    dbhs=[30, 30])
    out = biomass.trees_landfire_cbd(tl, input_landfire_dir=ext_dir)
    np.testing.assert_allclose(out["landfire_cell_kg_per_m3"], [0.12, 0.10])
    # a search distance shorter than one cell leaves them missing
    out2 = biomass.trees_landfire_cbd(tl, input_landfire_dir=ext_dir, max_search_dist_m=10)
    assert out2["landfire_cell_kg_per_m3"].isna().all()


def test_landfire_cbd_reads_the_numeric_product_as_is(tmp_path):
    # the Zenodo re-packaging cloud2trees downloads: float kg/m3 with NaN for non-forest and nodata
    vals = np.where(CBD_X100 > 0, CBD_X100 / 100.0, np.nan).astype("float32")
    write_raster(tmp_path / "lc23_cbd_240.tif", vals, "float32", np.nan)
    tl = make_trees(xs=[760045, 760015], ys=[4330045, 4330015], heights=[10, 10], cbhs=[3, 3], areas=[10, 10],
                    dbhs=[30, 30])
    out = biomass.trees_landfire_cbd(tl, input_landfire_dir=tmp_path)
    np.testing.assert_allclose(out["landfire_cell_kg_per_m3"], [0.12, 0.15], rtol=1e-6)


def test_cruz_biomass_positive_and_plausible(ext_dir, trees):
    out = run_dispatcher(trees, method="cruz", input_foresttype_dir=ext_dir)
    for c in ("crown_dia_m", "crown_length_m", "crown_volume_m3", "cruz_stand_id",
              "cruz_tree_kg_per_m3", "cruz_stand_kg_per_m3", "cruz_crown_biomass_kg"):
        assert c in out.columns, c
    assert (out["cruz_crown_biomass_kg"] > 0).all()
    assert (out["cruz_tree_kg_per_m3"] > 0).all()
    cbd = out["cruz_stand_kg_per_m3"]
    assert cbd.nunique() == 1
    assert 0.01 <= cbd.iloc[0] <= 0.5

    # Cruz et al. (2003) mixed conifer: CBD = exp(-8.445 + 0.319 ln(BA m2/ha) + 0.859 ln(trees/ha)), stand = 900 m2
    ba_ha = (np.pi * (trees["dbh_cm"] / 200.0) ** 2).sum() / 0.09
    tph = 6 / 0.09
    expected = np.exp(-8.445 + 0.319 * np.log(ba_ha) + 0.859 * np.log(tph))
    assert cbd.iloc[0] == pytest.approx(expected)

    L = trees["tree_height_m"] - trees["tree_cbh_m"]
    np.testing.assert_allclose(out["cruz_crown_biomass_kg"].sum(), expected * L.mean() * 900.0)
    cells = out.attrs["stand_cell_data_cruz"]
    assert cells["forest_type_group_code"].iloc[0] == 260
    assert cells["trees_per_ha"].iloc[0] == pytest.approx(tph)


def test_cruz_coefficients_by_forest_type_group():
    ba, n = 30.0, 500.0
    for code, (b0, b1, b2) in {200: (-7.380, 0.479, 0.625), 220: (-6.649, 0.435, 0.579),
                               280: (-7.852, 0.349, 0.711), 120: (-8.445, 0.319, 0.859),
                               260: (-8.445, 0.319, 0.859), 320: (-8.445, 0.319, 0.859)}.items():
        got = biomass.get_cruz_stand_kg_per_m3([code], [ba], [n])[0]
        assert got == pytest.approx(np.exp(b0 + b1 * np.log(ba) + b2 * np.log(n)))
    assert np.isnan(biomass.get_cruz_stand_kg_per_m3(["370"], [ba], [n])[0])   # not mapped by cloud2trees
    assert np.isnan(biomass.get_cruz_stand_kg_per_m3([None], [ba], [n])[0])


def test_cruz_without_a_cruz_forest_type_returns_no_cruz_columns(ext_dir, trees):
    tl = trees.assign(forest_type_group_code="370")
    with pytest.warns(UserWarning, match="None of the forest types"):
        out = biomass.trees_biomass_cruz(tl, input_foresttype_dir=ext_dir)
    assert "cruz_crown_biomass_kg" not in out.columns
    assert len(out) == 6


def test_all_methods(ext_dir, trees):
    out = run_dispatcher(trees, method="all", input_landfire_dir=ext_dir, input_foresttype_dir=ext_dir)
    assert {"cruz_crown_biomass_kg", "landfire_crown_biomass_kg", "crown_volume_m3"} <= set(out.columns)
    assert out.attrs["stand_cell_data_cruz"] is not None
    assert out.attrs["stand_cell_data_landfire"] is not None
    assert (out["landfire_crown_biomass_kg"] > 0).all() and (out["cruz_crown_biomass_kg"] > 0).all()
    # the same crown volume feeds both methods
    out2 = run_dispatcher(trees, method=["cruz", "landfire"], input_landfire_dir=ext_dir, input_foresttype_dir=ext_dir)
    pd.testing.assert_frame_equal(out.drop(columns="geometry"), out2.drop(columns="geometry"))


def test_max_crown_kg_per_m3_cap(ext_dir):
    # three normal trees in the centre cell (0.12) and three tiny-crowned trees in the cell to the left (0.10);
    # the tiny crowns give an implausible 90 kg/m3 that the cap replaces with the median of the cells under 2
    tl = make_trees(
        xs=[760035, 760055, 760045, 760005, 760025, 760015],
        ys=[4330035, 4330055, 4330045, 4330035, 4330055, 4330045],
        heights=[12, 15, 18, 4, 4, 4], cbhs=[4, 5, 6, 2, 2, 2], areas=[30, 40, 50, 0.5, 0.5, 0.5],
        dbhs=[45, 50, 55, 10, 10, 10])
    out = biomass.trees_biomass_landfire(tl, input_landfire_dir=ext_dir)
    normal = out.iloc[:3]["landfire_tree_kg_per_m3"]
    tiny = out.iloc[3:]["landfire_tree_kg_per_m3"]
    expected_normal = 0.12 * 10.0 * 900.0 / ((2.0 / 3.0) * np.array([8 * 30, 10 * 40, 12 * 50])).sum()
    np.testing.assert_allclose(normal, expected_normal)
    np.testing.assert_allclose(tiny, expected_normal)          # capped cell takes the median of the others
    uncapped = biomass.trees_biomass_landfire(tl, input_landfire_dir=ext_dir, max_crown_kg_per_m3=None)
    np.testing.assert_allclose(uncapped.iloc[3:]["landfire_tree_kg_per_m3"], 0.10 * 2.0 * 900.0 / 2.0)


def test_required_columns_raise_r_messages(ext_dir, trees):
    with pytest.raises(ValueError, match="does not contain the columns: tree_cbh_m"):
        biomass.trees_biomass_cruz(trees.drop(columns="tree_cbh_m"), input_foresttype_dir=ext_dir)
    with pytest.raises(ValueError, match="`basal_area_m2`, `dbh_cm`, or `dbh_m`"):
        biomass.trees_biomass_landfire(trees.drop(columns="dbh_cm"), input_landfire_dir=ext_dir)
    with pytest.raises(ValueError, match="all missing data"):
        biomass.trees_biomass_landfire(trees.assign(tree_cbh_m=np.nan), input_landfire_dir=ext_dir)
    with pytest.raises(ValueError, match="must contain `treeID`"):
        biomass.trees_biomass_landfire(trees.drop(columns="treeID"), input_landfire_dir=ext_dir)
    with pytest.raises(ValueError, match="`method` parameter must be one or multiple of"):
        biomass.trees_biomass(trees, method="bogus", input_landfire_dir=ext_dir)
    with pytest.raises(FileNotFoundError, match="get_landfire"):
        biomass.trees_biomass_landfire(trees, input_landfire_dir=ext_dir / "nowhere")
    # the dispatcher turns a method failure into a warning and returns the trees, as R's purrr::safely does
    with pytest.warns(UserWarning, match="Could not get `landfire` biomass estimates"):
        out = biomass.trees_biomass(trees.drop(columns="dbh_cm"), method="landfire", input_landfire_dir=ext_dir)
    assert len(out) == 6 and "landfire_crown_biomass_kg" not in out.columns


def test_plain_dataframe_with_crs_and_basal_area(ext_dir, trees):
    df = pd.DataFrame(trees.drop(columns=["geometry", "dbh_cm"]))
    df["basal_area_m2"] = np.pi * (trees["dbh_cm"] / 200.0) ** 2
    out = biomass.trees_biomass_landfire(df, crs=CRS, input_landfire_dir=ext_dir)
    ref = biomass.trees_biomass_landfire(trees, input_landfire_dir=ext_dir)
    np.testing.assert_allclose(out["landfire_crown_biomass_kg"], ref["landfire_crown_biomass_kg"])
