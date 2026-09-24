"""
Tests for c2t.lanl: the LANL TREES export. Synthetic trees only, seconds to run.
  cd <repo> && python -m pytest tests/test_lanl.py -q
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
from shapely.geometry import Point, box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.c2t import lanl  # noqa: E402

CRS = "EPSG:26910"
TREELIST_COLS = ["sp", "x_coord", "y_coord", "tree_height_m", "tree_cbh_m", "crown_dia_m",
                 "max_crown_diam_height_m", "cbd", "moist", "ss"]


def make_trees(n: int = 8, seed: int = 21) -> gpd.GeoDataFrame:
    """Eight synthetic trees with every column the cloud2trees chain would add."""
    rng = np.random.default_rng(seed)
    x = 740000.7 + rng.uniform(0, 50, n)
    y = 4320000.3 + rng.uniform(0, 40, n)
    h = rng.uniform(5, 30, n)
    crown_area = rng.uniform(5, 40, n)
    df = pd.DataFrame({
        "treeID": [f"{i}_{xi:.1f}_{yi:.1f}" for i, (xi, yi) in enumerate(zip(x, y), start=1)],
        "tree_x": x, "tree_y": y, "tree_height_m": h, "crown_area_m2": crown_area,
        "dbh_cm": h * 1.6, "dbh_m": h * 0.016, "basal_area_m2": np.pi * (h * 0.008) ** 2,
        "forest_type_group_code": 221, "forest_type_group": "Ponderosa pine group", "hardwood_softwood": "Softwood",
        "tree_cbh_m": h * 0.3, "max_crown_diam_height_m": h * 0.55,
        "crown_dia_m": 2 * np.sqrt(crown_area / np.pi), "crown_length_m": h * 0.7,
        "landfire_tree_kg_per_m3": rng.uniform(0.05, 0.35, n), "cruz_tree_kg_per_m3": rng.uniform(0.05, 0.35, n),
    })
    return gpd.GeoDataFrame(df, geometry=[Point(a, b) for a, b in zip(x, y)], crs=CRS)


def read_treelist(path) -> pd.DataFrame:
    rows = [line.split(" ") for line in Path(path).read_text().splitlines()]
    return pd.DataFrame(rows, columns=TREELIST_COLS).astype(float)


def write_plane_dtm(path, west=739990.0, north=4320050.0, res=1.0, rows=60, cols=70, hole=None, nodata=None):
    """A 1 m DTM whose value is a plane: 1500 + 1.0 * dy_from_south + 0.01 * dx_from_west."""
    ii, jj = np.mgrid[0:rows, 0:cols]
    northing = north - (ii + 0.5) * res
    easting = west + (jj + 0.5) * res
    z = (1500 + 1.0 * (northing - 4319990) + 0.01 * (easting - 739990)).astype("float32")
    if hole is not None:
        r0, r1, c0, c1 = hole
        z[r0:r1, c0:c1] = np.nan
    profile = dict(driver="GTiff", dtype="float32", count=1, height=rows, width=cols, crs=CRS,
                   transform=from_origin(west, north, res, res))
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(z, 1)
    return z


# ---------------------------------------------------------------------------------------------
# flat export
# ---------------------------------------------------------------------------------------------
def test_flat_export_files_and_treelist(tmp_path):
    trees = make_trees()
    ans = lanl.cloud2trees_to_lanl_trees(trees, tmp_path, topofile="flat")

    out = tmp_path / "lanl_trees_delivery"
    assert Path(ans["output_dir"]) == out
    assert Path(ans["treelist_path"]) == out / "Cloud2Trees_TreeList.txt"
    assert Path(ans["fuellist_path"]) == out / "fuellist"
    assert Path(ans["domain_path"]) == out / "Lidar_Bounds.geojson"
    assert ans["topofile_path"] is None and ans["dtm_path"] is None and ans["dtm"] is None
    assert sorted(p.name for p in out.iterdir()) == ["Cloud2Trees_TreeList.txt", "Lidar_Bounds.geojson", "fuellist"]

    # domain: multiples of 2 m, nx and ny integers, trees inside on the west and south edges
    d = ans["domain"]
    for k in ("xmin", "ymin", "xmax", "ymax"):
        assert d[k] % 2 == 0
    assert d["width"] == d["nx"] * 2 and d["length"] == d["ny"] * 2
    assert d["xmin"] <= trees.tree_x.min() and d["ymin"] <= trees.tree_y.min()
    assert d["nx"] == round(d["nx"]) and d["ny"] == round(d["ny"])

    # tree list text file: 8 rows, 10 space separated columns, no header
    raw = Path(ans["treelist_path"]).read_text()
    assert raw.endswith("\n") and "\r" not in raw
    tl = read_treelist(ans["treelist_path"])
    assert tl.shape == (8, 10)
    assert (tl["x_coord"] >= 0).all() and (tl["y_coord"] >= 0).all()
    np.testing.assert_allclose(tl["x_coord"], np.round(trees.tree_x.values - d["xmin"], 4), atol=1e-4)
    np.testing.assert_allclose(tl["y_coord"], np.round(trees.tree_y.values - d["ymin"], 4), atol=1e-4)
    np.testing.assert_allclose(tl["tree_height_m"], trees.tree_height_m.values, atol=1e-4)
    np.testing.assert_allclose(tl["tree_cbh_m"], trees.tree_cbh_m.values, atol=1e-4)
    np.testing.assert_allclose(tl["crown_dia_m"], trees.crown_dia_m.values, atol=1e-4)
    np.testing.assert_allclose(tl["max_crown_diam_height_m"], trees.max_crown_diam_height_m.values, atol=1e-4)
    np.testing.assert_allclose(tl["cbd"], trees.landfire_tree_kg_per_m3.values, atol=1e-4)
    assert (tl["sp"] == 1).all() and (tl["moist"] == 1).all() and (tl["ss"] == 0.0005).all()
    # printed like R: integer columns without decimals, ss as 0.0005
    first = raw.splitlines()[0].split(" ")
    assert first[0] == "1" and first[-2] == "1" and first[-1] == "0.0005"

    # the returned tree list is the clipped input, the formatted table has the same rows
    assert len(ans["tree_list"]) == 8 and len(ans["treelist"]) == 8

    # Lidar_Bounds.geojson is the domain in WGS84
    bounds = gpd.read_file(ans["domain_path"])
    assert len(bounds) == 1 and bounds.crs.to_epsg() == 4326
    back = bounds.to_crs(CRS).total_bounds
    np.testing.assert_allclose(back, [d["xmin"], d["ymin"], d["xmax"], d["ymax"]], atol=1e-3)


def test_fuellist_substitutions(tmp_path):
    trees = make_trees()
    ans = lanl.cloud2trees_to_lanl_trees(trees, tmp_path, topofile="flat")
    d = ans["domain"]
    text = Path(ans["fuellist_path"]).read_text()
    assert "\r" not in text and text.endswith("\n")
    lines = text.splitlines()
    assert len(lines) == 83
    assert lines[0] == "&fuellist"
    nz = int(np.ceil(trees.tree_height_m.max())) + 1
    assert lines[4] == f"      nx  = {int(d['nx'])}"
    assert lines[5] == f"      ny  = {int(d['ny'])}"
    assert lines[6] == f"      nz  = {nz}"
    assert lines[7] == "      dx  = 2"
    assert lines[8] == "      dy  = 2"
    assert lines[13] == "      topofile = flat"
    assert lines[36] == "      treefile = '" + ans["treelist_path"] + "'"
    assert lines[37] == f"      ndatax = {int(d['width'])}"
    assert lines[38] == f"      ndatay = {int(d['length'])}"
    assert lines[44] == "      ilitter = 0"
    assert lines[47] == "      lrho = 4.667"
    assert lines[48] == "      lmoisture = 0.06"
    assert lines[49] == "      lss = 0.0005"
    assert lines[50] == "      ldepth = 0.06"
    assert lines[71] == "      igrass = 0"
    assert lines[75] == "      grho = 1.17"
    assert lines[76] == "      gmoisture = 0.06"
    assert lines[77] == "      gss = 0.0005"
    assert lines[78] == "      gdepth = 0.27"
    # untouched template lines survive
    template = lanl.quicfire_get_fuellist()
    for i in (9, 10, 11, 12, 17, 26, 34, 35, 39, 40, 46, 73, 74, 82):
        assert lines[i] == template[i]
    assert lanl.quicfire_check_fuellist(lines)


def test_custom_fuel_lists_and_cruz(tmp_path):
    trees = make_trees()
    ans = lanl.cloud2trees_to_lanl_trees(
        trees, tmp_path, topofile="flat", cbd_method="cruz",
        fuel_litter=[1, 3.14159, 0.123, 0.00123, 0.055],  # unnamed sequence in R order
        fuel_grass={"igrass": 1, "grho": 0.9876, "gmoisture": 0.5, "gss": 0.00099, "gdepth": 0.3},
    )
    lines = Path(ans["fuellist_path"]).read_text().splitlines()
    assert lines[44] == "      ilitter = 1"
    assert lines[47] == "      lrho = 3.142"       # round 3
    assert lines[48] == "      lmoisture = 0.12"   # round 2
    assert lines[49] == "      lss = 0.00123"      # round 5
    assert lines[50] == "      ldepth = 0.06"      # round 2 (0.055 -> 0.06 in floating point)
    assert lines[71] == "      igrass = 1"
    assert lines[75] == "      grho = 0.988"
    assert lines[76] == "      gmoisture = 0.5"
    assert lines[77] == "      gss = 0.00099"
    assert lines[78] == "      gdepth = 0.3"
    tl = read_treelist(ans["treelist_path"])
    np.testing.assert_allclose(tl["cbd"], trees.cruz_tree_kg_per_m3.values, atol=1e-4)


# ---------------------------------------------------------------------------------------------
# topo.dat
# ---------------------------------------------------------------------------------------------
def test_topo_dat_from_dtm(tmp_path):
    trees = make_trees()
    dtm = tmp_path / "dtm_1m.tif"
    write_plane_dtm(dtm)
    ans = lanl.cloud2trees_to_lanl_trees(trees, tmp_path, dtm_path=dtm, topofile="dtm")
    d = ans["domain"]
    nx, ny = int(d["nx"]), int(d["ny"])
    out = tmp_path / "lanl_trees_delivery"
    assert Path(ans["topofile_path"]) == out / "topo.dat"
    assert Path(ans["dtm_path"]) == out / "dtm_Clipped.tif"

    # FORTRAN unformatted sequential: int32 record length, float32 data, same int32 again
    raw = np.fromfile(ans["topofile_path"], dtype=np.uint8)
    assert raw.size == nx * ny * 4 + 8
    assert int(raw[:4].view("<i4")[0]) == nx * ny * 4
    assert int(raw[-4:].view("<i4")[0]) == nx * ny * 4
    values = raw[4:-4].view("<f4")
    assert values.size == nx * ny

    # values are the clipped DTM, south row first, west to east
    with rasterio.open(ans["dtm_path"]) as src:
        clipped = src.read(1)
        assert src.crs.to_epsg() == 26910
        assert src.transform.a == 2 and src.transform.e == -2
        assert (src.transform.c, src.transform.f) == (d["xmin"], d["ymax"])
    assert clipped.shape == (ny, nx)
    assert np.array_equal(values, np.flipud(clipped).ravel())
    assert np.array_equal(ans["dtm"], clipped)
    # the plane is z = 1500 + dy + 0.01 dx, so the first value is the SW cell centre and the values
    # step 0.02 along a row (2 m in x) and 2 along columns (2 m in y)
    sw = 1500 + (d["ymin"] + 1 - 4319990) + 0.01 * (d["xmin"] + 1 - 739990)
    assert abs(values[0] - sw) < 1e-3
    assert abs(values[1] - values[0] - 0.02) < 1e-3
    assert abs(values[nx] - values[0] - 2.0) < 1e-3
    assert values[-1] > values[0]
    np.testing.assert_array_equal(lanl.read_topo_dat(ans["topofile_path"], nx, ny), clipped)

    # the fuellist points at the topo file, single quoted
    lines = Path(ans["fuellist_path"]).read_text().splitlines()
    assert lines[13] == "      topofile = '" + ans["topofile_path"] + "'"


def test_topo_written_even_when_flat(tmp_path):
    """R writes dtm_Clipped.tif and topo.dat whenever a DTM is found, but the fuellist stays flat."""
    trees = make_trees()
    dtm = tmp_path / "dtm_1m.tif"
    write_plane_dtm(dtm)
    ans = lanl.cloud2trees_to_lanl_trees(trees, tmp_path, dtm_path=dtm, topofile="flat")
    assert Path(ans["topofile_path"]).exists() and Path(ans["dtm_path"]).exists()
    assert Path(ans["fuellist_path"]).read_text().splitlines()[13] == "      topofile = flat"


def test_topo_missing_values(tmp_path):
    trees = make_trees()
    dtm = tmp_path / "dtm_1m.tif"
    write_plane_dtm(dtm, hole=(28, 34, 28, 34), nodata=np.nan)  # a 6 m hole inside the domain
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ans = lanl.cloud2trees_to_lanl_trees(trees, tmp_path, dtm_path=dtm, topofile="flat")
    assert any("missing values in DTM bounding box extent" in str(w.message) for w in caught)
    assert ans["topofile_path"] is None
    assert Path(ans["dtm_path"]).exists()
    assert np.isnan(ans["dtm"]).any()
    with pytest.raises(ValueError, match="`topofile` set to dtm but the topo.dat file could not be generated"):
        lanl.cloud2trees_to_lanl_trees(trees, tmp_path, dtm_path=dtm, topofile="dtm")


def test_write_read_topo_roundtrip(tmp_path):
    z = np.arange(12, dtype="float32").reshape(3, 4) * 1.5  # 3 rows (north to south), 4 columns
    p = lanl.write_topo_dat(tmp_path / "topo.dat", z)
    raw = np.fromfile(p, dtype=np.uint8)
    assert raw.size == 12 * 4 + 8
    assert raw[:4].tobytes() == np.int32(48).astype("<i4").tobytes() == raw[-4:].tobytes()
    assert np.array_equal(raw[4:-4].view("<f4"), np.concatenate([z[2], z[1], z[0]]))
    assert np.array_equal(lanl.read_topo_dat(p, nx=4, ny=3), z)


# ---------------------------------------------------------------------------------------------
# AOI, directory input, checks
# ---------------------------------------------------------------------------------------------
def test_study_boundary_clip_and_buffer(tmp_path):
    trees = make_trees()
    xmin, ymin, xmax, ymax = trees.total_bounds
    # a polygon holding only the western half of the trees
    half = gpd.GeoDataFrame(geometry=[box(xmin - 1, ymin - 1, (xmin + xmax) / 2, ymax + 1)], crs=CRS)
    n_in = int(trees.intersects(half.geometry.iloc[0]).sum())
    assert 0 < n_in < 8
    ans = lanl.cloud2trees_to_lanl_trees(trees, tmp_path, study_boundary=half, bbox_aoi=False, topofile="flat")
    assert len(ans["tree_list"]) == n_in
    assert len(read_treelist(ans["treelist_path"])) == n_in
    # a large buffer brings every tree back in and grows the domain
    ans2 = lanl.cloud2trees_to_lanl_trees(trees, tmp_path, study_boundary=half, bbox_aoi=True, buffer=100, topofile="flat")
    assert len(ans2["tree_list"]) == 8
    assert ans2["domain"]["width"] > ans["domain"]["width"]
    # the boundary may be given in another CRS
    ans3 = lanl.cloud2trees_to_lanl_trees(trees, tmp_path, study_boundary=half.to_crs(4326), bbox_aoi=False, topofile="flat")
    assert len(ans3["tree_list"]) == n_in
    assert ans3["aoi"].crs == trees.crs


def test_directory_input_like_r(tmp_path):
    trees = make_trees()
    delivery = tmp_path / "point_cloud_processing_delivery"
    delivery.mkdir()
    trees.to_file(delivery / "final_detected_tree_tops.gpkg", driver="GPKG")
    write_plane_dtm(delivery / "dtm_1m.tif")
    ans = lanl.cloud2trees_to_lanl_trees(delivery, tmp_path, topofile="dtm")
    assert Path(ans["topofile_path"]).exists()
    assert len(read_treelist(ans["treelist_path"])) == 8
    # rerun empties the delivery folder first and writes the same set of files
    before = sorted(p.name for p in Path(ans["output_dir"]).iterdir())
    ans = lanl.cloud2trees_to_lanl_trees(delivery, tmp_path, topofile="flat")
    assert sorted(p.name for p in Path(ans["output_dir"]).iterdir()) == before


def test_polygon_tree_list(tmp_path):
    trees = make_trees()
    crowns = trees.copy()
    crowns["geometry"] = crowns.buffer(1.5)
    ans = lanl.cloud2trees_to_lanl_trees(crowns, tmp_path, topofile="flat")
    tl = read_treelist(ans["treelist_path"])
    np.testing.assert_allclose(tl["x_coord"], np.round(trees.tree_x.values - ans["domain"]["xmin"], 4), atol=1e-4)
    assert ans["tree_list"].geom_type.eq("Polygon").all()


def test_r_error_messages(tmp_path):
    trees = make_trees()
    with pytest.raises(ValueError, match="the data does not contain the columns: tree_cbh_m"):
        lanl.cloud2trees_to_lanl_trees(trees.drop(columns=["tree_cbh_m"]), tmp_path)
    with pytest.raises(ValueError, match="the data does not contain the columns: max_crown_diam_height_m, cruz_tree_kg_per_m3"):
        lanl.cloud2trees_to_lanl_trees(trees.drop(columns=["max_crown_diam_height_m", "cruz_tree_kg_per_m3"]), tmp_path, cbd_method="cruz")
    allna = trees.copy()
    allna["landfire_tree_kg_per_m3"] = np.nan
    with pytest.raises(ValueError, match="the columns listed below have all missing data:\n   landfire_tree_kg_per_m3"):
        lanl.cloud2trees_to_lanl_trees(allna, tmp_path)
    dup = trees.copy()
    dup.loc[dup.index[1], "treeID"] = dup["treeID"].iloc[0]
    with pytest.raises(ValueError, match="Duplicates found in the treeID column"):
        lanl.cloud2trees_to_lanl_trees(dup, tmp_path)
    with pytest.raises(ValueError, match="`tree_list` data must contain `treeID` column"):
        lanl.cloud2trees_to_lanl_trees(trees.drop(columns=["treeID"]), tmp_path)
    with pytest.raises(ValueError, match="`topofile` parameter must be one of:\n    flat, dtm"):
        lanl.cloud2trees_to_lanl_trees(trees, tmp_path, topofile="bumpy")
    with pytest.raises(ValueError, match="could not locate DTM raster"):
        lanl.cloud2trees_to_lanl_trees(trees, tmp_path, topofile="dtm")
    with pytest.raises(ValueError, match="`method` parameter must be one or multiple of:\n    cruz, landfire"):
        lanl.cloud2trees_to_lanl_trees(trees, tmp_path, cbd_method="fia")
    with pytest.raises(ValueError, match="incorrect list length in litter fuel list. expecting: 5"):
        lanl.cloud2trees_to_lanl_trees(trees, tmp_path, fuel_litter=[0, 1, 2])
    with pytest.raises(ValueError, match="incorrect list names in grass fuel list. need to add/rename:\n   gdepth"):
        lanl.cloud2trees_to_lanl_trees(trees, tmp_path, fuel_grass={"igrass": 0, "grho": 1, "gmoisture": 0.1, "gss": 0.001, "depth": 0.3})
    with pytest.raises(ValueError, match="could not locate the directory `output_dir` at:"):
        lanl.cloud2trees_to_lanl_trees(trees, tmp_path / "nope")
    with pytest.raises(ValueError, match="study_boundary does not have a CRS"):
        lanl.cloud2trees_to_lanl_trees(trees, tmp_path, study_boundary=gpd.GeoSeries([box(0, 0, 1, 1)]))
    with pytest.raises(ValueError, match="study_boundary must only have a single record geometry"):
        lanl.cloud2trees_to_lanl_trees(trees, tmp_path, study_boundary=gpd.GeoSeries([box(0, 0, 1, 1), box(2, 2, 3, 3)], crs=CRS))
    with pytest.raises(ValueError, match="study_boundary must contain POLYGON type geometry only"):
        lanl.cloud2trees_to_lanl_trees(trees, tmp_path, study_boundary=gpd.GeoSeries([Point(0, 0)], crs=CRS))
    with pytest.raises(ValueError, match="tree_list does not have a CRS"):
        lanl.cloud2trees_to_lanl_trees(trees.set_crs(None, allow_override=True), tmp_path)
    holes = trees.copy()
    holes.loc[holes.index[:2], "tree_cbh_m"] = np.nan  # 2 of 8 dropped is more than 10 percent
    with pytest.raises(ValueError, match="more than 10% of data dropped due to missing values"):
        lanl.cloud2trees_to_lanl_trees(holes, tmp_path)


def test_fuellist_template_check():
    lines = lanl.quicfire_get_fuellist()
    assert len(lines) == 83
    assert lanl.quicfire_check_fuellist(lines)
    broken = lines[:4] + lines[5:]  # drop the nx line so every later parameter shifts up
    with pytest.raises(ValueError, match="fuellist has missing or unordered parameters .*\n   5:nx, 6:ny"):
        lanl.quicfire_check_fuellist(broken)
    with pytest.raises(ValueError, match="this fuellist isn't even character"):
        lanl.quicfire_check_fuellist([1, 2, 3])


def test_as_character_safe_matches_r_format():
    """R format(x, scientific = FALSE, trim = TRUE): 7 significant digits, common decimals per vector."""
    assert list(lanl.as_character_safe([1234.5678, 0.5])) == ["1234.568", "0.500"]
    assert list(lanl.as_character_safe([0.0005])) == ["0.0005"]
    assert list(lanl.as_character_safe([64.0])) == ["64"]
    assert list(lanl.as_character_safe([100000.0])) == ["100000"]
    assert list(lanl.as_character_safe([12.3456, 7.0])) == ["12.3456", "7.0000"]
    assert list(lanl.as_character_safe([1.0, np.nan, 2.5])) == ["1.0", None, "2.5"]
    assert list(lanl.as_character_safe(["a", "b"])) == ["a", "b"]


def test_domain_rounding_rule(tmp_path):
    """R quicfire_define_domain: an AOI whose sides are already multiples of 2 m is kept as is."""
    aoi = gpd.GeoSeries([box(740001, 4320001, 740041, 4320021)], crs=CRS)  # 40 by 20 m, odd origin
    d = lanl.quicfire_define_domain(aoi, outdir=tmp_path)["quicfire_domain_df"]
    assert (d["xmin"], d["ymin"], d["xmax"], d["ymax"], d["nx"], d["ny"]) == (740001, 4320001, 740041, 4320021, 20, 10)
    aoi = gpd.GeoSeries([box(740000.6, 4320000.4, 740041.3, 4320021.7)], crs=CRS)  # not multiples of 2
    d = lanl.quicfire_define_domain(aoi, outdir=tmp_path)["quicfire_domain_df"]
    # xmax = 2*round(370020.65) = 740042, xmin = 2*round(370000.3) - 2 = 739998, and the same for y
    assert (d["xmin"], d["xmax"], d["ymin"], d["ymax"]) == (739998, 740042, 4319998, 4320022)
    assert (d["nx"], d["ny"], d["width"], d["length"]) == (22, 12, 44, 24)
