"""
c2t/itd.py — individual tree detection and crown delineation from a CHM. Port of raster2trees() and
itd_tuning() from cloud2trees.

raster2trees (R): lidR::locate_trees(chm, lmf(ws = fn, hmin)) then ForestTools::mcws(treetops, chm,
minHeight) then polygons (makeValid, simplify, fillHoles), crown_area_m2 > min_crown_area, join tops,
treeID = "<n>_<x>_<y>". Large rasters were tiled with a 10 m buffer and overlapping crowns resolved by
keeping the larger one. Here large rasters are processed in windows with a pad, and a crown belongs to
the window that owns its top, which gives the same trees without the overlap bookkeeping.

itd_tuning (R): up to five 0.1 ha sample plots (the first at the CHM centre), each run through every
window function; returns crown plots and a summary table (trees per ha, height and crown stats).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy import ndimage

from .ws import as_ws_function, itd_ws_functions


# ---------------------------------------------------------------------------
# Local maxima with a variable circular window (lidR::lmf on a raster)
# ---------------------------------------------------------------------------

def _disk(radius_px: float) -> np.ndarray:
    r = int(np.ceil(radius_px))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    return (xx ** 2 + yy ** 2) <= radius_px ** 2 + 1e-9


def locate_trees(chm: np.ndarray, res: float, ws=None, hmin: float = 2.0, band_m: float = 0.25) -> np.ndarray:
    """
    Boolean array of tree tops. A pixel is a top when its height is >= hmin and it is the maximum inside a
    circular window whose diameter (m) is ws(height). Heights are grouped into bands of band_m so one
    maximum filter serves each distinct window size.
    """
    ws = as_ws_function(ws if ws is not None else itd_ws_functions()["log_fn"])
    a = np.where(np.isfinite(chm), chm, -np.inf).astype(np.float32)
    cand = a >= hmin
    tops = np.zeros(chm.shape, dtype=bool)
    if not cand.any():
        return tops
    band = np.floor(a[cand] / band_m) * band_m
    for b in np.unique(band):
        d_m = float(ws(b + band_m / 2))
        r_px = max(d_m / 2.0 / res, 0.5)
        sel = np.zeros(chm.shape, dtype=bool); sel[cand] = band == b
        mx = ndimage.maximum_filter(a, footprint=_disk(r_px), mode="constant", cval=-np.inf)
        tops |= sel & (a == mx)
    # ties on a plateau: keep one pixel per connected plateau of equal-height tops
    lab, n = ndimage.label(tops)
    if n:
        first = np.zeros(n + 1, dtype=bool)
        idx = np.nonzero(tops)
        out = np.zeros(chm.shape, dtype=bool)
        for r, c in zip(*idx):
            l = lab[r, c]
            if not first[l]:
                first[l] = True; out[r, c] = True
        tops = out
    return tops


def mcws(chm: np.ndarray, tops: np.ndarray, min_height: float = 2.0) -> np.ndarray:
    """Marker-controlled watershed on the inverted CHM (ForestTools::mcws). Returns int32 labels, 0 = no crown."""
    from skimage.segmentation import watershed
    markers, n = ndimage.label(tops)
    if n == 0:
        return np.zeros(chm.shape, dtype=np.int32)
    a = np.where(np.isfinite(chm), chm, -np.inf).astype(np.float32)
    lab = watershed(-a, markers=markers, mask=a >= min_height)
    return lab.astype(np.int32)


def _crown_polygons(labels: np.ndarray, transform, crs, min_crown_area: float):
    """Label array to a GeoDataFrame of crowns (layer = label id) with valid, simplified, hole-free polygons."""
    import geopandas as gpd
    from rasterio import features
    from shapely.geometry import shape, Polygon, MultiPolygon
    polys, ids = [], []
    for geom, val in features.shapes(labels, mask=labels > 0, transform=transform):
        polys.append(shape(geom)); ids.append(int(val))
    if not polys:
        return gpd.GeoDataFrame({"layer": pd.Series([], dtype=int)}, geometry=[], crs=crs)
    g = gpd.GeoDataFrame({"layer": ids}, geometry=polys, crs=crs)
    g = g.dissolve(by="layer", as_index=False)
    def fix(geom):
        geom = geom.buffer(0).simplify(0.01)
        if geom.geom_type == "Polygon":
            return Polygon(geom.exterior)
        if geom.geom_type == "MultiPolygon":
            return MultiPolygon([Polygon(p.exterior) for p in geom.geoms])
        return geom
    g["geometry"] = g.geometry.map(fix)
    g["crown_area_m2"] = g.geometry.area
    return g[g["crown_area_m2"] > min_crown_area].reset_index(drop=True)


def detect_window(chm: np.ndarray, transform, crs, ws, min_height: float, min_crown_area: float,
                  interior: tuple[int, int, int, int] | None = None):
    """
    Tops and crowns for one array. interior = (row0, row1, col0, col1) keeps only trees whose top pixel
    is inside that window (used with a pad). Returns a crowns GeoDataFrame with treeID, tree_height_m,
    tree_x, tree_y, crown_area_m2 and Polygon geometry, plus the labels array.
    """
    res = abs(transform.a)
    tops = locate_trees(chm, res, ws, min_height)
    # watershed with every top (pad tops included) so interior crowns stop at their neighbours,
    # then keep only the crowns whose top lies in the interior window
    labels = mcws(chm, tops, min_height)
    if interior is not None:
        r0, r1, c0, c1 = interior
        keep = np.zeros(chm.shape, dtype=bool); keep[r0:r1, c0:c1] = True
        tops &= keep
        keep_lab = np.zeros(int(labels.max()) + 1, dtype=bool)
        keep_lab[np.unique(labels[tops])] = True
        keep_lab[0] = False
        labels = np.where(keep_lab[labels], labels, 0).astype(np.int32)
    rr, cc = np.nonzero(tops)
    if len(rr) == 0:
        import geopandas as gpd
        return gpd.GeoDataFrame(columns=["treeID", "tree_height_m", "tree_x", "tree_y", "crown_area_m2", "geometry"], geometry="geometry", crs=crs), labels
    top_lab = labels[rr, cc]
    xs, ys = transform * (cc + 0.5, rr + 0.5)
    tops_df = pd.DataFrame({"layer": top_lab, "tree_x": np.asarray(xs), "tree_y": np.asarray(ys), "tree_height_m": chm[rr, cc].astype(float)})
    tops_df = tops_df[tops_df["layer"] > 0]
    crowns = _crown_polygons(labels, transform, crs, min_crown_area)
    crowns = crowns.merge(tops_df, on="layer", how="inner")
    return crowns, labels


def _read_chm(chm):
    """chm: path (GeoTIFF or VRT) or (array, transform, crs). Returns (array or None, transform, crs, dataset or None)."""
    import rasterio
    if isinstance(chm, (str, Path)):
        ds = rasterio.open(chm)
        return None, ds.transform, ds.crs, ds
    arr, tr, crs = chm
    return np.asarray(arr, dtype=np.float32), tr, crs, None


def raster2trees(chm, outfolder=None, ws=None, min_height: float = 2.0, min_crown_area: float = 0.1,
                 tile_px: int = 4000, pad_m: float = 10.0, log=None):
    """
    Tree list from a CHM. `chm` is a GeoTIFF or VRT path, or (array, transform, crs). Rasters larger than
    tile_px in either dimension are processed in windows with a pad of pad_m; a crown is kept by the window
    that holds its top. Writes final_detected_crowns.gpkg and final_detected_tree_tops.gpkg to outfolder
    (split into _<n> files above 250,000 trees, as R does) and returns the crowns GeoDataFrame.
    """
    import geopandas as gpd
    from rasterio.windows import Window
    ws = as_ws_function(ws if ws is not None else itd_ws_functions()["log_fn"])
    arr, tr, crs, ds = _read_chm(chm)
    res = abs(tr.a)
    if ds is not None:
        nrow, ncol = ds.height, ds.width
        nodata = ds.nodata
    else:
        nrow, ncol = arr.shape; nodata = None
    pad = int(np.ceil(pad_m / res))
    parts = []
    if nrow <= tile_px and ncol <= tile_px:
        a = ds.read(1).astype(np.float32) if ds is not None else arr
        if nodata is not None and not (isinstance(nodata, float) and np.isnan(nodata)):
            a = np.where(a == nodata, np.nan, a)
        crowns, _ = detect_window(a, tr, crs, ws, min_height, min_crown_area)
        parts.append(crowns)
    else:
        for r0 in range(0, nrow, tile_px):
            for c0 in range(0, ncol, tile_px):
                r1 = min(r0 + tile_px, nrow); c1 = min(c0 + tile_px, ncol)
                pr0, pc0 = max(r0 - pad, 0), max(c0 - pad, 0)
                pr1, pc1 = min(r1 + pad, nrow), min(c1 + pad, ncol)
                if ds is not None:
                    a = ds.read(1, window=Window(pc0, pr0, pc1 - pc0, pr1 - pr0)).astype(np.float32)
                    if nodata is not None and not (isinstance(nodata, float) and np.isnan(nodata)):
                        a = np.where(a == nodata, np.nan, a)
                    wtr = ds.window_transform(Window(pc0, pr0, pc1 - pc0, pr1 - pr0))
                else:
                    a = arr[pr0:pr1, pc0:pc1]
                    wtr = tr * tr.__class__.translation(pc0, pr0)
                if not np.isfinite(a).any():
                    continue
                interior = (r0 - pr0, r1 - pr0, c0 - pc0, c1 - pc0)
                crowns, _ = detect_window(a, wtr, crs, ws, min_height, min_crown_area, interior)
                parts.append(crowns)
                if log: log.info(f"raster2trees window r{r0} c{c0}: {len(crowns):,} trees")
    if ds is not None:
        ds.close()
    parts = [p for p in parts if len(p)]
    if not parts:
        raise RuntimeError("Could not locate any trees using the CHM raster\n and window size settings...try different settings or data?")
    crowns = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=crs)
    crowns = crowns.drop(columns=["layer"])
    crowns.insert(0, "treeID", [f"{i + 1}_{round(x, 1)}_{round(y, 1)}" for i, (x, y) in enumerate(zip(crowns["tree_x"], crowns["tree_y"]))])
    crowns = crowns[["treeID", "tree_height_m", "tree_x", "tree_y", "crown_area_m2", "geometry"]]
    if outfolder is not None:
        write_raster2trees_ans(crowns, outfolder)
    return crowns


def write_raster2trees_ans(crowns, outfolder, chunk: int = 250_000) -> list[tuple[Path, Path]]:
    """final_detected_crowns.gpkg and final_detected_tree_tops.gpkg, split into _<n> files above 250k trees."""
    import geopandas as gpd
    out = Path(outfolder); out.mkdir(parents=True, exist_ok=True)
    files = []
    if len(crowns) > chunk:
        c = crowns.sort_values(["tree_x", "tree_y"]).reset_index(drop=True)
        groups = np.ceil((np.arange(len(c)) + 1) / chunk).astype(int)
        for g in np.unique(groups):
            sub = c[groups == g]
            cf = out / f"final_detected_crowns_{g}.gpkg"; tf = out / f"final_detected_tree_tops_{g}.gpkg"
            sub.to_file(cf, driver="GPKG")
            gpd.GeoDataFrame(sub.drop(columns="geometry"), geometry=gpd.points_from_xy(sub["tree_x"], sub["tree_y"]), crs=crowns.crs).to_file(tf, driver="GPKG")
            files.append((cf, tf))
    else:
        cf = out / "final_detected_crowns.gpkg"; tf = out / "final_detected_tree_tops.gpkg"
        crowns.to_file(cf, driver="GPKG")
        gpd.GeoDataFrame(crowns.drop(columns="geometry"), geometry=gpd.points_from_xy(crowns["tree_x"], crowns["tree_y"]), crs=crowns.crs).to_file(tf, driver="GPKG")
        files.append((cf, tf))
    return files


# ---------------------------------------------------------------------------
# itd_tuning
# ---------------------------------------------------------------------------

def itd_tuning(input_las_dir=None, n_samples: int = 3, ws_fn_list: dict | None = None, min_height: float = 2.0,
               chm_res_m: float = 0.25, input_chm_rast=None, plot_area_m2: float = 1000.0, seed: int = 21,
               out_png=None, log=None) -> dict:
    """
    Test window functions on up to five 0.1 ha square sample plots. input_chm_rast is a GeoTIFF path or
    (array, transform, crs); input_las_dir is used to build a CHM at chm_res_m with cloud2raster when no
    CHM is given. Returns {"samples": DataFrame of trees per sample and function, "summary": DataFrame,
    "ws_fn_list": dict, "plot_samples": matplotlib Figure or None, "plot_sample_summary": Figure or None}.
    """
    import rasterio
    from rasterio.windows import from_bounds
    n_samples = int(max(1, min(5, n_samples)))
    fns = ws_fn_list or itd_ws_functions()
    fns = {k: as_ws_function(v) for k, v in fns.items()}
    if input_chm_rast is None:
        if input_las_dir is None:
            raise ValueError("`input_las_dir` or `input_chm_rast` must be provided")
        import tempfile
        from .cloud import cloud2raster
        tmp = tempfile.mkdtemp(prefix="itd_tuning_")
        ans = cloud2raster(tmp, input_las_dir, chm_res_m=chm_res_m, min_height=min_height, write_normalized=False, log=log)
        input_chm_rast = ans["chm_path"]
    arr, tr, crs, ds = _read_chm(input_chm_rast)
    if ds is not None:
        arr = ds.read(1).astype(np.float32)
        if ds.nodata is not None and not np.isnan(ds.nodata):
            arr = np.where(arr == ds.nodata, np.nan, arr)
        ds.close()
    res = abs(tr.a)
    side = np.sqrt(plot_area_m2)
    side_px = int(round(side / res))
    nrow, ncol = arr.shape
    # sample plot centres: the CHM centre first, then random centres where the plot holds trees
    rng = np.random.default_rng(seed)
    centres = [(nrow // 2, ncol // 2)]
    tries = 0
    while len(centres) < n_samples and tries < 200:
        tries += 1
        r = int(rng.integers(side_px // 2, max(nrow - side_px // 2, side_px // 2 + 1)))
        c = int(rng.integers(side_px // 2, max(ncol - side_px // 2, side_px // 2 + 1)))
        win = arr[r - side_px // 2:r + side_px // 2, c - side_px // 2:c + side_px // 2]
        if np.isfinite(win).any() and np.nanmax(win) >= min_height and (r, c) not in centres:
            centres.append((r, c))
    rows = []
    sample_windows = []
    for si, (r, c) in enumerate(centres, 1):
        r0, c0 = max(r - side_px // 2, 0), max(c - side_px // 2, 0)
        r1, c1 = min(r0 + side_px, nrow), min(c0 + side_px, ncol)
        win = arr[r0:r1, c0:c1]
        wtr = tr * tr.__class__.translation(c0, r0)
        sample_windows.append((si, win, wtr))
        for name, fn in fns.items():
            crowns, labels = detect_window(win, wtr, crs, fn, min_height, 0.1)
            for t in crowns.itertuples():
                rows.append({"sample_number": si, "ws_fn": name, "tree_height_m": t.tree_height_m, "crown_area_m2": t.crown_area_m2, "tree_x": t.tree_x, "tree_y": t.tree_y})
    samples = pd.DataFrame(rows)
    if samples.empty:
        raise RuntimeError("itd_tuning: no trees detected in any sample plot")
    summary = (samples.groupby(["sample_number", "ws_fn"])
               .agg(n=("tree_height_m", "size"), height_mean_m=("tree_height_m", "mean"), height_max_m=("tree_height_m", "max"),
                    crown_area_mean_m2=("crown_area_m2", "mean"), crown_area_max_m2=("crown_area_m2", "max"))
               .reset_index())
    summary["tpha"] = summary["n"] / (plot_area_m2 / 10000.0)
    fig = fig2 = None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        nf = len(fns); ns = len(sample_windows)
        fig, axes = plt.subplots(ns, nf, figsize=(3.2 * nf, 3.2 * ns), squeeze=False)
        for i, (si, win, wtr) in enumerate(sample_windows):
            for j, (name, fn) in enumerate(fns.items()):
                ax = axes[i][j]
                ax.imshow(np.where(np.isfinite(win), win, 0), cmap="viridis")
                crowns, labels = detect_window(win, wtr, crs, fn, min_height, 0.1)
                ax.contour(labels > 0, levels=[0.5], colors="white", linewidths=0.5)
                ax.contour(labels, levels=np.arange(0.5, labels.max() + 1), colors="orange", linewidths=0.3)
                ax.set_title(f"sample {si}: {name} ({len(crowns)} trees)", fontsize=8); ax.axis("off")
        plt.tight_layout()
        fig2, ax2 = plt.subplots(1, 3, figsize=(11, 3.2))
        for name, sub in samples.groupby("ws_fn"):
            ax2[0].hist(sub["tree_height_m"], bins=20, histtype="step", label=name)
            ax2[1].hist(sub["crown_area_m2"], bins=20, histtype="step", label=name)
        ax2[0].set_xlabel("tree height (m)"); ax2[1].set_xlabel("crown area (m2)"); ax2[0].legend(fontsize=7)
        s = summary.groupby("ws_fn")["tpha"].mean()
        ax2[2].bar(s.index, s.values); ax2[2].set_ylabel("trees per ha (mean over samples)")
        plt.tight_layout()
        if out_png:
            fig.savefig(str(out_png), dpi=110); fig2.savefig(str(Path(out_png).with_name(Path(out_png).stem + "_summary.png")), dpi=110)
    except Exception as e:  # plotting is optional
        if log: log.info(f"itd_tuning plots skipped: {e}")
    return {"samples": samples, "summary": summary, "ws_fn_list": fns, "plot_samples": fig, "plot_sample_summary": fig2}
