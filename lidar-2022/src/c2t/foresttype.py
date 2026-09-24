"""
c2t/foresttype.py — FIA forest type group at each tree. Port of trees_type() from cloud2trees.

R: crop the Forest Type Groups raster (Wilson 2023, 30 m) to the tree extent (buffer 0.6 x the larger side
of the bounding box, or the study boundary when larger), sample it at the trees, and when any tree lands
on a non-forest or NA cell, fill the raster from a Voronoi of the valid cells (aggregated first when the
area is huge) and sample again. Here the fill is a nearest-valid-cell lookup (cKDTree on valid cell
centres) limited to max_search_dist_m, which is what the Voronoi fill amounts to at the tree locations.

Adds forest_type_group_code, forest_type_group, hardwood_softwood from foresttype_lookup.csv. Returns
(tree_list, cropped raster dict) like the R list(tree_list, foresttype_rast).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from . import schema
from .extdata import find_ext_data

TYPE_COLS = ("forest_type_group_code", "forest_type_group", "hardwood_softwood")


def read_lookup(foresttype_dir) -> pd.DataFrame:
    lk = pd.read_csv(Path(foresttype_dir) / "foresttype_lookup.csv", dtype=str)
    lk.columns = [c.lower() for c in lk.columns]
    return lk


def sample_raster_with_fill(points_gdf, raster_path, max_search_dist_m: float = 1000.0, valid_codes=None, study_boundary=None):
    """
    Values of a categorical raster at points, filling non-valid cells from the nearest valid cell within
    max_search_dist_m. Returns (values array of object/str with None where unfilled, cropped raster dict).
    """
    import rasterio
    from rasterio.windows import from_bounds
    with rasterio.open(raster_path) as r:
        pts = points_gdf.to_crs(r.crs)
        b = pts.total_bounds
        side = max(b[2] - b[0], b[3] - b[1]) * 0.6
        if study_boundary is not None:
            sb = study_boundary.to_crs(r.crs).total_bounds
            sside = max(sb[2] - sb[0], sb[3] - sb[1]) * 0.6
            if sside > side:
                side, b = sside, sb
        buf = max(side, max_search_dist_m)
        win = from_bounds(b[0] - buf, b[1] - buf, b[2] + buf, b[3] + buf, transform=r.transform)
        win = win.round_offsets().round_lengths()
        arr = r.read(1, window=win, boundless=True, fill_value=r.nodata if r.nodata is not None else 0)
        tr = r.window_transform(win)
        nod = r.nodata
        res = abs(tr.a)
    valid = np.isfinite(arr.astype(float))
    if nod is not None:
        valid &= arr != nod
    if valid_codes is not None:
        valid &= np.isin(arr.astype(str), np.asarray(list(valid_codes), dtype=str)) | np.isin(arr, [int(c) for c in valid_codes if str(c).lstrip("-").isdigit()])
    x = pts.geometry.x.to_numpy(); y = pts.geometry.y.to_numpy()
    c = np.floor((x - tr.c) / res).astype(int); rr = np.floor((tr.f - y) / res).astype(int)
    inside = (rr >= 0) & (rr < arr.shape[0]) & (c >= 0) & (c < arr.shape[1])
    vals = np.full(len(x), None, dtype=object)
    got = np.zeros(len(x), bool)
    got[inside] = valid[rr[inside], c[inside]]
    vals[got] = arr[rr[got], c[got]]
    need = ~got
    if need.any() and valid.any():
        vr, vc = np.nonzero(valid)
        vx = tr.c + (vc + 0.5) * res; vy = tr.f - (vr + 0.5) * res
        tree = cKDTree(np.column_stack([vx, vy]))
        d, i = tree.query(np.column_stack([x[need], y[need]]), k=1, distance_upper_bound=max_search_dist_m)
        ok = np.isfinite(d)
        idx = np.nonzero(need)[0][ok]
        vals[idx] = arr[vr[i[ok]], vc[i[ok]]]
    rast = {"array": arr, "transform": tr, "crs": r.crs, "nodata": nod}
    return vals, rast


def trees_type(tree_list, crs=None, study_boundary=None, input_foresttype_dir=None, max_search_dist_m: float = 1000.0, log=None):
    """FIA forest type group for each tree. Returns (tree_list with the three type columns, cropped raster dict)."""
    ext = find_ext_data(input_foresttype_dir=input_foresttype_dir)
    if ext.get("foresttype_dir") is None:
        raise FileNotFoundError("Forest Type Group data has not been downloaded to package contents. Use `get_foresttype()` first.\nIf you supplied a value to the `input_foresttype_dir` parameter check that directory for data.")
    tops = schema.as_points(schema.ensure_treeid(tree_list), crs)
    if tops.crs is None:
        raise ValueError("Cannot get forest type with blank CRS.\n  ensure that the `tree_list` data has a CRS")
    lk = read_lookup(ext["foresttype_dir"])
    valid_codes = set(lk["forest_type_code"].astype(str))
    vals, rast = sample_raster_with_fill(tops, Path(ext["foresttype_dir"]) / "foresttype.tif", max_search_dist_m, valid_codes, study_boundary)
    codes = pd.Series([None if v is None else str(int(v)) if str(v).replace(".", "").lstrip("-").isdigit() else str(v) for v in vals], index=tops.index)
    out = tree_list.copy()
    out = schema.drop_cols(out, TYPE_COLS)
    m = lk.set_index("forest_type_code")
    out["forest_type_group_code"] = codes.map(m["forest_type_group_code"]).values
    out["forest_type_group"] = codes.map(m["forest_type_group"]).values
    out["hardwood_softwood"] = codes.map(m["hardwood_softwood"]).values
    n_na = int(out["forest_type_group_code"].isna().sum())
    if n_na == len(out) and log:
        log.warning("Unable to determine forest type for this tree list and study boundary (if provided).\nTry expanding the study boundary area or increasing the max_search_dist_m parameter\nand ensure that your tree data is in the continental US.")
    elif n_na and log:
        log.info(f"trees_type: {n_na:,} trees without a forest type within {max_search_dist_m} m")
    return out, rast


def write_foresttype_raster(rast: dict, path) -> None:
    import rasterio
    a = rast["array"]
    with rasterio.open(path, "w", driver="GTiff", height=a.shape[0], width=a.shape[1], count=1, dtype=a.dtype, crs=rast["crs"],
                       transform=rast["transform"], nodata=rast["nodata"], compress="deflate") as dst:
        dst.write(a, 1)
