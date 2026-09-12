"""
src/taos.py — tree approximate objects (TAOs) from the 1 m canopy height model.

Per CHM tile: smooth, detect tree tops with a height-dependent local-maximum window
(same rule as 00_lidar), grow a crown around each top by marker-controlled watershed,
and write a tree table (points) plus crown polygons. Tiles are read with a pad so
crowns that straddle a tile edge are segmented whole; a crown is kept by the tile
that owns its top, so no tree is written twice.

    segment_tile(args)          one tile; safe for Pool.map; writes <stem>_trees.parquet and <stem>_crowns.gpkg
    merge_trees(paths)          concatenates per-tile tree tables into one GeoDataFrame
    tao_density_raster(...)     detected trees per acre on the 30 m grid, from the merged tree table

Second-pass segmentation from the normalized point cloud lives in scripts/segment_trees_lidr.R.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import ndimage


def detect_tops(chm: np.ndarray, sigma: float, hmin: float, win_map: list[tuple[float, int]]) -> tuple[np.ndarray, np.ndarray]:
    """Return (smoothed CHM, boolean tops mask). Window grows with height so big pines are not split."""
    sm = ndimage.gaussian_filter(chm, sigma)
    tops = np.zeros(chm.shape, dtype=bool)
    lower = 0.0
    for upper, w in win_map:
        band = (sm >= max(lower, hmin)) & (sm < upper)
        if band.any():
            mx = ndimage.maximum_filter(sm, size=w, mode="nearest")
            tops |= band & (sm == mx)
        lower = upper
    return sm, tops


def segment_crowns(sm: np.ndarray, tops: np.ndarray, hmin: float, crown_floor_frac: float) -> np.ndarray:
    """
    Marker-controlled watershed on the inverted smoothed CHM. A crown stops where the
    CHM falls below hmin or below crown_floor_frac of its own top height (the latter is
    enforced afterwards by trimming), which keeps crowns from bleeding across gaps.
    Returns a label array (0 = no crown).
    """
    from skimage.segmentation import watershed
    markers, n = ndimage.label(tops)
    if n == 0:
        return np.zeros(sm.shape, dtype=np.int32)
    labels = watershed(-sm, markers=markers, mask=sm >= hmin, compactness=0.0)
    # trim: pixels below crown_floor_frac * top height are not part of that crown
    top_h = ndimage.maximum(sm, labels, index=np.arange(1, n + 1))
    top_h = np.concatenate([[0.0], top_h])
    keep = sm >= crown_floor_frac * top_h[labels]
    labels = np.where(keep, labels, 0)
    return labels.astype(np.int32)


def crown_table(labels: np.ndarray, sm: np.ndarray, chm: np.ndarray, tops: np.ndarray, transform, pad: int, interior_shape: tuple[int, int]) -> pd.DataFrame:
    """Per-crown stats. Keeps only crowns whose top lies in the interior (unpadded) window."""
    n = int(labels.max())
    if n == 0:
        return pd.DataFrame()
    idx = np.arange(1, n + 1)
    area_px = ndimage.sum(np.ones_like(labels), labels, index=idx)
    h_max = ndimage.maximum(chm, labels, index=idx)
    h_mean = ndimage.mean(chm, labels, index=idx)
    # top location = position of the marker pixel for each label
    top_rows, top_cols = np.nonzero(tops & (labels > 0))
    top_lab = labels[top_rows, top_cols]
    order = np.argsort(top_lab)
    top_rows, top_cols, top_lab = top_rows[order], top_cols[order], top_lab[order]
    # one top per label (first occurrence)
    first = np.r_[True, np.diff(top_lab) > 0]
    top_rows, top_cols, top_lab = top_rows[first], top_cols[first], top_lab[first]
    inside = (top_rows >= pad) & (top_rows < pad + interior_shape[0]) & (top_cols >= pad) & (top_cols < pad + interior_shape[1])
    top_rows, top_cols, top_lab = top_rows[inside], top_cols[inside], top_lab[inside]
    xs, ys = (transform * (top_cols + 0.5, top_rows + 0.5))
    res = abs(transform.a)
    df = pd.DataFrame({
        "label": top_lab,
        "x": np.asarray(xs), "y": np.asarray(ys),
        "height_m": h_max[top_lab - 1].astype("float32"),
        "crown_area_m2": (area_px[top_lab - 1] * res * res).astype("float32"),
        "crown_mean_h_m": h_mean[top_lab - 1].astype("float32"),
    })
    df["crown_diam_m"] = (2 * np.sqrt(df["crown_area_m2"] / np.pi)).astype("float32")
    return df


def segment_tile(args: dict) -> dict:
    """
    args: chm_path, out_dir, cfg_taos (dict), nodata, wcrs, write_polygons (bool), pad_px
    """
    import rasterio
    from rasterio.windows import Window

    t0 = time.time()
    src = Path(args["chm_path"]); out_dir = Path(args["out_dir"]); T = args["cfg_taos"]
    stem = src.stem.replace("_chm1m", "")
    trees_out = out_dir / f"{stem}_trees.parquet"
    crowns_out = out_dir / f"{stem}_crowns.gpkg"
    if trees_out.exists() and (crowns_out.exists() or not args.get("write_polygons", True)):
        return {"tile": stem, "status": "skipped (done)", "trees": 0, "seconds": 0}

    pad = int(args.get("pad_px", 25))
    win_map = sorted((float(k), int(v)) for k, v in T["window_m_by_height"].items())
    with rasterio.open(src) as r:
        nod = r.nodata if r.nodata is not None else args["nodata"]
        # read the tile plus a pad from neighbours is not possible from a single tile file, so
        # neighbouring tiles are read through a VRT when args["vrt"] is given; otherwise pad within the tile
        if args.get("vrt"):
            with rasterio.open(args["vrt"]) as v:
                b = r.bounds
                win = rasterio.windows.from_bounds(b.left - pad, b.bottom - pad, b.right + pad, b.top + pad, transform=v.transform)
                chm = v.read(1, window=win, boundless=True, fill_value=nod).astype("float32")
                transform = v.window_transform(win)
        else:
            chm = r.read(1).astype("float32"); transform = r.transform; pad = 0
        interior = (r.height, r.width)
    chm[(chm == nod) | ~np.isfinite(chm)] = 0.0
    chm = np.clip(chm, 0, None)

    sm, tops = detect_tops(chm, T["smooth_sigma_m"], T["min_tree_height_m"], win_map)
    labels = segment_crowns(sm, tops, T["min_tree_height_m"], T["crown_floor_frac"])
    df = crown_table(labels, sm, chm, tops, transform, pad, interior)
    if df.empty:
        pd.DataFrame(columns=["tree_id", "x", "y", "height_m", "crown_area_m2", "crown_mean_h_m", "crown_diam_m", "tile"]).to_parquet(trees_out, index=False)
        return {"tile": stem, "status": "ok (no trees)", "trees": 0, "seconds": round(time.time() - t0, 1)}
    df = df[df["crown_area_m2"] >= T["min_crown_area_m2"]]
    df["tile"] = stem
    df["tree_id"] = stem + "-" + df["label"].astype(str)

    import geopandas as gpd
    from shapely.geometry import Point
    gdf = gpd.GeoDataFrame(df.drop(columns=["label"]), geometry=[Point(xy) for xy in zip(df.x, df.y)], crs=args["wcrs"])
    gdf.to_parquet(trees_out, index=False)

    if args.get("write_polygons", True):
        from rasterio import features
        from shapely.geometry import shape
        keep = np.isin(labels, df["label"].values)
        lab_keep = np.where(keep, labels, 0)
        polys = []; ids = []
        for geom, val in features.shapes(lab_keep.astype("int32"), mask=lab_keep > 0, transform=transform):
            polys.append(shape(geom)); ids.append(int(val))
        crowns = gpd.GeoDataFrame({"tree_id": [stem + "-" + str(i) for i in ids]}, geometry=polys, crs=args["wcrs"])
        crowns = crowns.dissolve(by="tree_id", as_index=False)      # multi-part crowns from shapes() become one feature
        crowns = crowns.merge(df[["tree_id", "height_m", "crown_area_m2"]], on="tree_id", how="left")
        crowns.to_file(crowns_out, driver="GPKG")

    return {"tile": stem, "status": "ok", "trees": int(len(df)), "seconds": round(time.time() - t0, 1)}


def merge_trees(paths: list[Path]):
    import geopandas as gpd
    parts = [gpd.read_parquet(p) for p in paths if p.stat().st_size > 0]
    parts = [p for p in parts if len(p)]
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs) if parts else gpd.GeoDataFrame()


def tao_density_raster(trees, grid, out_path, wcrs, nodata=-9999.0, min_height_m: float = 0.0):
    """Detected trees per acre on the 30 m grid (x0, y1, g, nrow, ncol)."""
    import rasterio
    from rasterio.transform import from_origin
    x0, y1, g, nrow, ncol = grid
    t = trees[trees["height_m"] >= min_height_m]
    ci = np.floor((t.geometry.x.values - x0) / g).astype(int); ri = np.floor((y1 - t.geometry.y.values) / g).astype(int)
    ok = (ri >= 0) & (ri < nrow) & (ci >= 0) & (ci < ncol)
    cnt = np.zeros((nrow, ncol)); np.add.at(cnt, (ri[ok], ci[ok]), 1)
    per_ac = (cnt / (g * g / 4046.86)).astype("float32")
    with rasterio.open(out_path, "w", driver="GTiff", height=nrow, width=ncol, count=1, dtype="float32", crs=wcrs,
                       transform=from_origin(x0, y1, g, g), nodata=nodata, tiled=True, compress="deflate") as dst:
        dst.write(per_ac, 1)
    return per_ac
