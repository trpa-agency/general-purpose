"""
c2t/cloud.py — point cloud to DTM, CHM, and normalized LAZ. Port of cloud2raster(), lasr_pipeline(),
lasr_dtm_norm(), lasr_chm(), and the helpers the trees_*() functions use to read points.

Pipeline per tile (lasr_pipeline.R order):
  read (drop duplicates, drop classes 7, 18, 20, 22) -> denoise (IVF or SOR by noise_level)
  -> ground (existing class 2, or CSF when asked) -> decimate ground by density -> TIN
  -> DTM raster (dtm_res_m) -> normalize (TIN at every point for accuracy 2 and 3, DTM raster for 1)
  -> write normalized LAZ (drop noise, drop z < 0) -> CHM (max z, classes not in 2, 9, 18,
  z between min_height and max_height, chm_res_m) -> pit fill.

cloud2raster() runs tiles in a pool, mosaics DTM (mean) and CHM (max, then 3x3 mean fill of empty
cells), and writes the delivery files. Everything here is numpy, scipy, laspy, and rasterio.
"""
from __future__ import annotations

import glob
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import ndimage
from scipy.spatial import Delaunay, cKDTree
from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator

DROP_CLASSES = (7, 18, 20, 22)          # USGS noise, high noise, ignored ground, low point... per lasR read filter
CHM_EXCLUDE_CLASSES = (2, 9, 18)        # ground, water, high noise never enter the CHM


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def list_las(input_las: str | Path | list) -> list[Path]:
    """A folder (walked recursively), a glob, a file, or a list of any of those."""
    if isinstance(input_las, (list, tuple)):
        out: list[Path] = []
        for i in input_las:
            out += list_las(i)
        return sorted(set(out))
    p = Path(str(input_las))
    if p.is_dir():
        return sorted(q for q in p.rglob("*") if q.suffix.lower() in (".las", ".laz"))
    if any(ch in str(input_las) for ch in "*?["):
        return sorted(Path(q) for q in glob.glob(str(input_las), recursive=True))
    return [p] if p.exists() else []


def las_header(path: Path) -> dict:
    import laspy
    with laspy.open(path) as f:
        h = f.header
        try:
            crs = h.parse_crs()
        except Exception:
            crs = None
        return {"path": str(path), "xmin": float(h.mins[0]), "ymin": float(h.mins[1]), "xmax": float(h.maxs[0]),
                "ymax": float(h.maxs[1]), "zmin": float(h.mins[2]), "zmax": float(h.maxs[2]),
                "n_points": int(h.point_count), "crs": crs, "point_format": int(h.point_format.id)}


def crs_is_feet(crs) -> bool:
    """check_horizontal_crs_is_feet(): True when the horizontal axis unit is a foot (US survey or international)."""
    if crs is None:
        return False
    try:
        for ax in crs.axis_info[:2]:
            if "foot" in (ax.unit_name or "").lower() or "feet" in (ax.unit_name or "").lower():
                return True
    except Exception:
        pass
    return False


def read_las(path: Path, drop_classes=DROP_CLASSES, drop_duplicates: bool = True, bounds=None) -> dict:
    """Read a whole file with laspy and return numpy arrays plus the header dict."""
    import laspy
    las = laspy.read(path)
    x = np.asarray(las.x, dtype=np.float64); y = np.asarray(las.y, dtype=np.float64); z = np.asarray(las.z, dtype=np.float64)
    cls = np.asarray(las.classification, dtype=np.uint8)
    rn = np.asarray(las.return_number, dtype=np.uint8)
    nr = np.asarray(las.number_of_returns, dtype=np.uint8)
    inten = np.asarray(las.intensity, dtype=np.uint16) if hasattr(las, "intensity") else np.zeros(len(x), np.uint16)
    keep = ~np.isin(cls, drop_classes) if drop_classes else np.ones(len(x), bool)
    if bounds is not None:
        keep &= (x >= bounds[0]) & (x <= bounds[2]) & (y >= bounds[1]) & (y <= bounds[3])
    if drop_duplicates:
        # lasR::drop_duplicates(): identical x, y, z
        key = np.round(np.column_stack([x, y, z]) * 1000).astype(np.int64)
        _, first = np.unique(key, axis=0, return_index=True)
        dup = np.ones(len(x), bool); dup[first] = False
        keep &= ~dup
    hdr = las_header(path)
    return {"x": x[keep], "y": y[keep], "z": z[keep], "classification": cls[keep], "return_number": rn[keep],
            "number_of_returns": nr[keep], "intensity": inten[keep], "header": hdr, "las": las, "keep": keep}


def load_points(norm_las, bounds=None, keep_classes=None) -> dict:
    """
    Contract helper for cbh, hmd, stems: read one normalized file, a folder, or a list, optionally
    clipped to bounds (xmin, ymin, xmax, ymax). Files whose header extent misses bounds are skipped.
    Returns x, y, z (height above ground), classification, return_number, number_of_returns, intensity.
    """
    files = list_las(norm_las)
    parts = []
    for f in files:
        h = las_header(f)
        if bounds is not None and (h["xmax"] < bounds[0] or h["xmin"] > bounds[2] or h["ymax"] < bounds[1] or h["ymin"] > bounds[3]):
            continue
        d = read_las(f, drop_classes=(), drop_duplicates=False, bounds=bounds)
        if keep_classes is not None:
            m = np.isin(d["classification"], keep_classes)
            d = {k: (v[m] if isinstance(v, np.ndarray) and v.shape[:1] == m.shape else v) for k, v in d.items()}
        parts.append(d)
    if not parts:
        return {k: np.zeros(0) for k in ("x", "y", "z", "classification", "return_number", "number_of_returns", "intensity")}
    keys = ("x", "y", "z", "classification", "return_number", "number_of_returns", "intensity")
    return {k: np.concatenate([p[k] for p in parts]) for k in keys}


def points_to_crowns(pts: dict, crowns, res: float = 0.25) -> np.ndarray:
    """
    Row index into `crowns` (Polygon GeoDataFrame) for every point, -1 outside every crown. Crowns are
    burned to a grid at `res` (later rows win where crowns overlap), then points are looked up.
    """
    import rasterio.features
    from rasterio.transform import from_origin
    if len(crowns) == 0 or len(pts["x"]) == 0:
        return np.full(len(pts["x"]), -1, dtype=np.int64)
    b = crowns.total_bounds
    x0, y1 = np.floor(b[0] / res) * res, np.ceil(b[3] / res) * res
    ncol = int(np.ceil((b[2] - x0) / res)) + 1; nrow = int(np.ceil((y1 - b[1]) / res)) + 1
    tr = from_origin(x0, y1, res, res)
    shapes = ((geom, i + 1) for i, geom in enumerate(crowns.geometry.values) if geom is not None and not geom.is_empty)
    lab = rasterio.features.rasterize(shapes, out_shape=(nrow, ncol), transform=tr, fill=0, dtype="int32", all_touched=False)
    c = np.floor((pts["x"] - x0) / res).astype(int); r = np.floor((y1 - pts["y"]) / res).astype(int)
    ok = (r >= 0) & (r < nrow) & (c >= 0) & (c < ncol)
    out = np.full(len(c), -1, dtype=np.int64)
    out[ok] = lab[r[ok], c[ok]] - 1
    return out


def sample_crowns(crowns, n=None, prop=None, seed: int = 21):
    """The tree sample trees_cbh() and trees_hmd() draw: n trees, or a proportion, without replacement."""
    rng = np.random.default_rng(seed)
    N = len(crowns)
    if prop is not None and not (isinstance(prop, float) and np.isnan(prop)):
        k = int(round(float(prop) * N))
    else:
        k = int(n if n is not None else 333)
    k = max(0, min(k, N))
    idx = np.sort(rng.choice(N, size=k, replace=False)) if k else np.zeros(0, int)
    return crowns.iloc[idx]


# ---------------------------------------------------------------------------
# Denoise and ground
# ---------------------------------------------------------------------------

def ivf_filter(x, y, z, res: float = 5.0, n: int = 9) -> np.ndarray:
    """Isolated voxel filter (lasR::classify_with_ivf): keep points whose 3x3x3 voxel neighbourhood holds at least n points."""
    if len(x) == 0:
        return np.zeros(0, bool)
    vx = np.floor((x - x.min()) / res).astype(np.int64); vy = np.floor((y - y.min()) / res).astype(np.int64); vz = np.floor((z - z.min()) / res).astype(np.int64)
    shape = (vx.max() + 3, vy.max() + 3, vz.max() + 3)
    counts = np.zeros(shape, dtype=np.int32)
    np.add.at(counts, (vx + 1, vy + 1, vz + 1), 1)
    nb = ndimage.uniform_filter(counts.astype(np.float32), size=3, mode="constant") * 27.0
    return nb[vx + 1, vy + 1, vz + 1] >= n


def sor_filter(x, y, z, k: int = 15, m: float = 3.0, chunk: int = 2_000_000) -> np.ndarray:
    """Statistical outlier removal (lasR::classify_with_sor): drop points whose mean distance to k neighbours exceeds mean + m sd."""
    npts = len(x)
    if npts <= k + 1:
        return np.ones(npts, bool)
    pts = np.column_stack([x, y, z])
    tree = cKDTree(pts)
    md = np.empty(npts, dtype=np.float32)
    for s in range(0, npts, chunk):
        d, _ = tree.query(pts[s:s + chunk], k=k + 1, workers=-1)
        md[s:s + chunk] = d[:, 1:].mean(axis=1)
    thr = md.mean() + m * md.std()
    return md <= thr


def denoise(x, y, z, noise_level: int = 2) -> np.ndarray:
    """noise_level 1 = IVF(5 m, 9); 2 = SOR(15, 3); 3 = SOR(50, 4) then SOR(15, 3). Returns a keep mask."""
    lvl = int(noise_level)
    if lvl == 1:
        return ivf_filter(x, y, z, 5.0, 9)
    keep = np.ones(len(x), bool)
    if lvl == 3:
        k1 = sor_filter(x, y, z, 50, 4.0)
        keep[~k1] = False
        idx = np.nonzero(keep)[0]
        k2 = sor_filter(x[idx], y[idx], z[idx], 15, 3.0)
        keep[idx[~k2]] = False
        return keep
    return sor_filter(x, y, z, 15, 3.0)


def ground_mask(x, y, z, cls, method: str = "existing", ground_class: int = 2) -> np.ndarray:
    """
    Ground points. 'existing' uses the vendor classification (class 2). 'csf' runs the cloth simulation
    filter with cloud2trees' settings (class_threshold 0.5, cloth_resolution 0.5, rigidness 1, 500
    iterations, time_step 0.65); needs `pip install cloth-simulation-filter`.
    """
    if method == "csf":
        try:
            import CSF  # type: ignore
        except ImportError as e:
            raise ImportError("ground: csf needs the CSF package: pip install cloth-simulation-filter") from e
        csf = CSF.CSF()
        csf.params.bSloopSmooth = False
        csf.params.class_threshold = 0.5
        csf.params.cloth_resolution = 0.5
        csf.params.rigidness = 1
        csf.params.interations = 500
        csf.params.time_step = 0.65
        csf.setPointCloud(np.column_stack([x, y, z]).astype(np.float64))
        g = CSF.VecInt(); ng = CSF.VecInt()
        csf.do_filtering(g, ng)
        m = np.zeros(len(x), bool); m[np.asarray(list(g), dtype=int)] = True
        return m
    return cls == ground_class


def decimate_ground(gx, gy, gz, pts_m2: float, max_pts_m2: float) -> np.ndarray:
    """
    lasr_dtm_norm(): when the tile is denser than max_pts_m2 (20 for accuracy 1 and 2, 100 for 3) keep
    the lowest ground point per cell at 0.4 m (fraction <= 0.01), 0.2 m (<= 0.3), else 0.1 m.
    """
    frac = 1.0 if pts_m2 <= 0 else min(1.0, max_pts_m2 / pts_m2)
    if frac >= 1.0 or len(gx) == 0:
        return np.ones(len(gx), bool)
    res = 0.4 if frac <= 0.01 else (0.2 if frac <= 0.3 else 0.1)
    cx = np.floor((gx - gx.min()) / res).astype(np.int64); cy = np.floor((gy - gy.min()) / res).astype(np.int64)
    key = cx * (cy.max() + 1) + cy
    order = np.lexsort((gz, key))
    ks = key[order]
    first = np.r_[True, ks[1:] != ks[:-1]]
    keep = np.zeros(len(gx), bool); keep[order[first]] = True
    return keep


# ---------------------------------------------------------------------------
# DTM, normalization, CHM
# ---------------------------------------------------------------------------

def _grid(bounds, res):
    xmin, ymin, xmax, ymax = bounds
    x0 = np.floor(xmin / res) * res; y1 = np.ceil(ymax / res) * res
    ncol = int(np.ceil((xmax - x0) / res)); nrow = int(np.ceil((y1 - ymin) / res))
    return x0, y1, max(ncol, 1), max(nrow, 1)


def tin_interpolate(tri, gz, x, y, cell: float = 2.0) -> np.ndarray:
    """
    Linear interpolation on a Delaunay TIN at (x, y). scipy's LinearNDInterpolator walks from the last
    simplex, so randomly ordered queries are very slow; sorting the queries along a coarse row-major grid
    keeps the walk local (about 20x faster on a LAS tile) and the barycentric weights are then computed
    directly from tri.transform. NaN outside the hull.
    """
    x = np.asarray(x, dtype=np.float64); y = np.asarray(y, dtype=np.float64)
    out = np.full(len(x), np.nan)
    if tri is None or len(x) == 0:
        return out
    pts = np.column_stack([x, y])
    kx = np.floor((x - x.min()) / cell).astype(np.int64); ky = np.floor((y - y.min()) / cell).astype(np.int64)
    order = np.lexsort((kx, ky))
    s = tri.find_simplex(pts[order])
    ok = s >= 0
    if ok.any():
        T = tri.transform[s[ok]]
        r = pts[order][ok] - T[:, 2]
        b = np.einsum("nij,nj->ni", T[:, :2], r)
        w = np.column_stack([b, 1.0 - b.sum(axis=1)])
        vals = (gz[tri.simplices[s[ok]]] * w).sum(axis=1)
        bad = ~np.isfinite(w).all(axis=1) | (w.min(axis=1) < -1e-6) | (w.max(axis=1) > 1 + 1e-6)
        if bad.any():                                   # ill-conditioned simplices: let scipy decide
            vals[bad] = LinearNDInterpolator(tri, gz)(pts[order][ok][bad])
        out[order[ok]] = vals
    return out


def tin_dtm(gx, gy, gz, bounds, res: float):
    """Delaunay TIN of ground points rasterized at res (lasR::triangulate + rasterize). Returns (array, transform, tri)."""
    from rasterio.transform import from_origin
    x0, y1, ncol, nrow = _grid(bounds, res)
    tr = from_origin(x0, y1, res, res)
    if len(gx) < 3:
        return np.full((nrow, ncol), np.nan, np.float32), tr, None
    pts = np.column_stack([gx, gy])
    tri = Delaunay(pts)
    cx = x0 + (np.arange(ncol) + 0.5) * res; cy = y1 - (np.arange(nrow) + 0.5) * res
    X, Y = np.meshgrid(cx, cy)
    dtm = tin_interpolate(tri, gz, X.ravel(), Y.ravel()).reshape(nrow, ncol).astype(np.float32)
    nan = ~np.isfinite(dtm)
    if nan.any():                                   # outside the hull: nearest ground point
        near = NearestNDInterpolator(pts, gz)
        dtm[nan] = near(X[nan], Y[nan])
    return dtm, tr, tri


def normalize_heights(x, y, z, gx, gy, gz, tri, dtm, transform, accuracy_level: int = 2) -> np.ndarray:
    """Height above ground: TIN at every point (accuracy 2, 3) or DTM raster lookup (accuracy 1)."""
    if int(accuracy_level) >= 2 and tri is not None:
        g = tin_interpolate(tri, gz, x, y)
        nan = ~np.isfinite(g)
        if nan.any():
            g[nan] = NearestNDInterpolator(np.column_stack([gx, gy]), gz)(x[nan], y[nan])
        return (z - g).astype(np.float32)
    res = abs(transform.a)
    c = np.clip(((x - transform.c) / res).astype(int), 0, dtm.shape[1] - 1)
    r = np.clip(((transform.f - y) / res).astype(int), 0, dtm.shape[0] - 1)
    return (z - dtm[r, c]).astype(np.float32)


def chm_max(x, y, hag, cls, bounds, res: float, min_height: float = 2.0, max_height: float = 70.0,
            exclude_classes=CHM_EXCLUDE_CLASSES):
    """Max height per cell from non-ground returns between min_height and max_height. Empty cells are NaN."""
    from rasterio.transform import from_origin
    x0, y1, ncol, nrow = _grid(bounds, res)
    tr = from_origin(x0, y1, res, res)
    m = ~np.isin(cls, exclude_classes) & (hag >= min_height) & (hag <= max_height)
    chm = np.full((nrow, ncol), -np.inf, dtype=np.float32)
    if m.any():
        c = np.floor((x[m] - x0) / res).astype(int); r = np.floor((y1 - y[m]) / res).astype(int)
        ok = (r >= 0) & (r < nrow) & (c >= 0) & (c < ncol)
        np.maximum.at(chm, (r[ok], c[ok]), hag[m][ok].astype(np.float32))
    chm[~np.isfinite(chm)] = np.nan
    return chm, tr


def _nanmedian_filter(a: np.ndarray, size: int) -> np.ndarray:
    """Median of the finite values in a size x size window (vectorised: stack the shifted views)."""
    r = size // 2
    pad = np.pad(a, r, mode="edge")
    stack = np.stack([pad[dr:dr + a.shape[0], dc:dc + a.shape[1]] for dr in range(size) for dc in range(size)])
    with np.errstate(all="ignore"):
        return np.nanmedian(stack, axis=0)


def pit_fill(chm: np.ndarray, lap_size: int = 3, thr_lap: float = 0.1, thr_spk: float = -0.1, med_size: int = 3,
             fill_empty_min_neighbors: int = 5, n_iter: int = 2) -> np.ndarray:
    """
    Pit filling after St-Onge (2008) as in lasR::pit_fill: cells whose Laplacian (window lap_size) exceeds
    thr_lap are pits and get the median of the med_size window; cells below thr_spk are spikes and get the
    same treatment. Two additions for sparse CHMs (8 to 30 pts per m2 at 0.25 m leaves many empty cells):
    an empty cell with at least fill_empty_min_neighbors finite neighbours in the med_size window is filled
    with their median (an interior gap, not the crown edge), and the pass runs n_iter times. Empty cells at
    the canopy edge stay empty; the mosaic step's 3x3 mean fill handles those.
    """
    out = chm.astype(np.float32).copy()
    for _ in range(int(n_iter)):
        valid = np.isfinite(out)
        if valid.sum() < 9:
            return out
        filled = np.where(valid, out, 0.0)
        mean = ndimage.uniform_filter(filled, size=lap_size, mode="nearest")
        cnt = ndimage.uniform_filter(valid.astype(np.float32), size=lap_size, mode="nearest")
        local_mean = np.where(cnt > 0, mean / np.maximum(cnt, 1e-6), np.nan)
        lap = local_mean - out                                   # positive where the cell sits below its neighbours
        pits = valid & np.isfinite(lap) & ((lap > thr_lap) | (lap < thr_spk))
        n_nb = ndimage.uniform_filter(valid.astype(np.float32), size=med_size, mode="constant") * (med_size ** 2)
        empty = ~valid & (n_nb >= fill_empty_min_neighbors)
        if not (pits.any() or empty.any()):
            break
        med = _nanmedian_filter(out, med_size)
        out[pits] = med[pits]
        out[empty] = med[empty]
    return out


def fill_na_mean3(a: np.ndarray) -> np.ndarray:
    """terra::focal(w=3, fun=mean, na.policy='only'): fill NaN cells that have a finite neighbour."""
    valid = np.isfinite(a)
    s = ndimage.uniform_filter(np.where(valid, a, 0.0), size=3, mode="constant") * 9.0
    c = ndimage.uniform_filter(valid.astype(np.float32), size=3, mode="constant") * 9.0
    out = a.copy()
    fill = ~valid & (c > 0)
    out[fill] = s[fill] / c[fill]
    return out


def write_raster(path, arr, transform, crs, nodata=np.nan):
    import rasterio
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype="float32",
                       crs=crs, transform=transform, nodata=nodata, tiled=True, compress="deflate") as dst:
        dst.write(arr.astype(np.float32), 1)


def write_normalized_laz(src_las, keep: np.ndarray, hag: np.ndarray, out_path: Path) -> None:
    """Copy of the tile with z replaced by height above ground; points with hag < 0 dropped (lasR drop_z_below 0)."""
    import laspy
    m = keep.copy()
    idx = np.nonzero(m)[0]
    ok = hag >= 0
    m[idx[~ok]] = False
    sub = src_las[m]
    sub.z = hag[ok]
    sub.header.mins[2] = float(np.min(hag[ok])) if ok.any() else 0.0
    sub.header.maxs[2] = float(np.max(hag[ok])) if ok.any() else 0.0
    sub.write(out_path)


# ---------------------------------------------------------------------------
# Per-tile and per-folder runners
# ---------------------------------------------------------------------------

def cloud2raster_tile(args: dict) -> dict:
    """
    One tile, safe for Pool.map. args: path, out_dtm_dir, out_chm_dir, out_norm_dir (or None),
    dtm_res_m, chm_res_m, min_height, max_height, accuracy_level, noise_level, ground ('existing'|'csf'),
    ground_class, crs (string, optional override), overwrite.
    """
    t0 = time.time()
    src = Path(args["path"]); stem = src.stem
    dtm_out = Path(args["out_dtm_dir"]) / f"{stem}_dtm_{args['dtm_res_m']}m.tif"
    chm_out = Path(args["out_chm_dir"]) / f"{stem}_chm_{args['chm_res_m']}m.tif"
    norm_dir = Path(args["out_norm_dir"]) if args.get("out_norm_dir") else None
    norm_out = (norm_dir / f"{stem}_normalize.laz") if norm_dir else None
    if not args.get("overwrite", True) and dtm_out.exists() and chm_out.exists() and (norm_out is None or norm_out.exists()):
        return {"tile": stem, "status": "skipped (done)", "seconds": 0.0, "n_points": 0, "n_ground": 0, "pts_m2": np.nan}
    d = read_las(src)
    x, y, z, cls = d["x"], d["y"], d["z"], d["classification"]
    hdr = d["header"]
    crs = args.get("crs") or (hdr["crs"].to_wkt() if hdr["crs"] is not None else None)
    if crs_is_feet(hdr["crs"]):
        return {"tile": stem, "status": "error: horizontal CRS in feet; reproject to meters first (c2t.cloud.reproject_las)", "seconds": time.time() - t0}
    if len(x) == 0:
        return {"tile": stem, "status": "error: no points after class filter", "seconds": time.time() - t0}
    bounds = (hdr["xmin"], hdr["ymin"], hdr["xmax"], hdr["ymax"])
    area = max((bounds[2] - bounds[0]) * (bounds[3] - bounds[1]), 1.0)
    pts_m2 = len(x) / area
    keep = denoise(x, y, z, args.get("noise_level", 2))
    x, y, z, cls = x[keep], y[keep], z[keep], cls[keep]
    keep_all = d["keep"].copy(); keep_all[np.nonzero(keep_all)[0][~keep]] = False
    gm = ground_mask(x, y, z, cls, args.get("ground", "existing"), args.get("ground_class", 2))
    n_ground = int(gm.sum())
    if n_ground < 3:
        return {"tile": stem, "status": f"error: {n_ground} ground points", "seconds": time.time() - t0}
    acc = int(args.get("accuracy_level", 2))
    dec = decimate_ground(x[gm], y[gm], z[gm], pts_m2, 100.0 if acc == 3 else 20.0)
    gx, gy, gz = x[gm][dec], y[gm][dec], z[gm][dec]
    dtm, dtm_tr, tri = tin_dtm(gx, gy, gz, bounds, float(args["dtm_res_m"]))
    hag = normalize_heights(x, y, z, gx, gy, gz, tri, dtm, dtm_tr, acc)
    chm, chm_tr = chm_max(x, y, hag, cls, bounds, float(args["chm_res_m"]), float(args.get("min_height", 2)), float(args.get("max_height", 70)))
    chm = pit_fill(chm, lap_size=2 if pts_m2 < 20 else 3)
    Path(args["out_dtm_dir"]).mkdir(parents=True, exist_ok=True); Path(args["out_chm_dir"]).mkdir(parents=True, exist_ok=True)
    write_raster(dtm_out, dtm, dtm_tr, crs)
    write_raster(chm_out, chm, chm_tr, crs)
    if norm_out is not None:
        norm_dir.mkdir(parents=True, exist_ok=True)
        write_normalized_laz(d["las"], keep_all, hag, norm_out)
    return {"tile": stem, "status": "ok", "seconds": round(time.time() - t0, 1), "n_points": int(len(x)), "n_ground": n_ground,
            "pts_m2": round(float(pts_m2), 2), "dtm": str(dtm_out), "chm": str(chm_out), "norm": str(norm_out) if norm_out else None,
            "xmin": bounds[0], "ymin": bounds[1], "xmax": bounds[2], "ymax": bounds[3], "crs": crs}


def mosaic(paths: list[str], out_path: Path, method: str, fill3: bool = False) -> None:
    """Mosaic tiles that share a grid. method 'mean' (DTM) or 'max' (CHM); fill3 applies the 3x3 mean fill."""
    import rasterio
    from rasterio.transform import from_origin
    metas = []
    for p in paths:
        with rasterio.open(p) as r:
            metas.append((p, r.bounds, r.res[0], r.crs))
    res = metas[0][2]; crs = metas[0][3]
    xmin = min(m[1].left for m in metas); ymin = min(m[1].bottom for m in metas)
    xmax = max(m[1].right for m in metas); ymax = max(m[1].top for m in metas)
    ncol = int(round((xmax - xmin) / res)); nrow = int(round((ymax - ymin) / res))
    tr = from_origin(xmin, ymax, res, res)
    if method == "max":
        acc = np.full((nrow, ncol), -np.inf, np.float32)
    else:
        acc = np.zeros((nrow, ncol), np.float32); cnt = np.zeros((nrow, ncol), np.float32)
    for p, b, _, _ in metas:
        with rasterio.open(p) as r:
            a = r.read(1).astype(np.float32)
            if r.nodata is not None and not np.isnan(r.nodata):
                a[a == r.nodata] = np.nan
        r0 = int(round((ymax - b.top) / res)); c0 = int(round((b.left - xmin) / res))
        h, w = a.shape
        h = min(h, nrow - r0); w = min(w, ncol - c0)
        a = a[:h, :w]
        win = (slice(r0, r0 + h), slice(c0, c0 + w))
        v = np.isfinite(a)
        if method == "max":
            acc[win] = np.where(v, np.maximum(acc[win], np.where(v, a, -np.inf)), acc[win])
        else:
            acc[win][v] += a[v]; cnt[win][v] += 1
    if method == "max":
        out = np.where(np.isfinite(acc), acc, np.nan).astype(np.float32)
    else:
        out = np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan).astype(np.float32)
    if fill3:
        out = fill_na_mean3(out)
    write_raster(out_path, out, tr, crs)


def cloud2raster(output_dir, input_las, dtm_res_m: float = 1.0, chm_res_m: float = 0.25, min_height: float = 2.0,
                 max_height: float = 70.0, accuracy_level: int = 2, noise_level: int = 2, ground: str = "existing",
                 ground_class: int = 2, keep_intrmdt: bool = False, write_normalized: bool = True, overwrite: bool = True,
                 workers: int = 1, crs: str | None = None, log=None) -> dict:
    """
    cloud2raster(): every tile to DTM and CHM, normalized LAZ per tile, then mosaics into
    <output_dir>/point_cloud_processing_delivery/dtm_<res>m.tif and chm_<res>m.tif plus raw_las_ctg_info.gpkg.
    Returns dict with dtm_path, chm_path, norm_dir, ctg (DataFrame of tile headers and results), delivery_dir, temp_dir.
    """
    import geopandas as gpd
    from shapely.geometry import box
    out = Path(output_dir)
    delivery = out / "point_cloud_processing_delivery"; temp = out / "point_cloud_processing_temp"
    dtm_dir = temp / "01_dtm"; chm_dir = temp / "02_chm"; norm_dir = temp / "03_normalize"
    for p in (delivery, dtm_dir, chm_dir, norm_dir):
        p.mkdir(parents=True, exist_ok=True)
    files = list_las(input_las)
    if not files:
        raise FileNotFoundError(f"could not detect .las|.laz files at {input_las}")
    jobs = [{"path": str(f), "out_dtm_dir": str(dtm_dir), "out_chm_dir": str(chm_dir), "out_norm_dir": str(norm_dir) if write_normalized else None,
             "dtm_res_m": dtm_res_m, "chm_res_m": chm_res_m, "min_height": min_height, "max_height": max_height,
             "accuracy_level": accuracy_level, "noise_level": noise_level, "ground": ground, "ground_class": ground_class,
             "crs": crs, "overwrite": overwrite} for f in files]
    results = []
    if workers > 1 and len(jobs) > 1:
        from multiprocessing import get_context
        with get_context("spawn").Pool(workers) as pool:
            for r in pool.imap_unordered(cloud2raster_tile, jobs):
                results.append(r)
                if log: log.info(f"{r['tile']}: {r['status']} ({r.get('seconds', 0)}s)")
    else:
        for j in jobs:
            r = cloud2raster_tile(j); results.append(r)
            if log: log.info(f"{r['tile']}: {r['status']} ({r.get('seconds', 0)}s)")
    res = pd.DataFrame(results)
    ok = res[res["status"].str.startswith("ok") | res["status"].str.startswith("skipped")]
    if ok.empty:
        raise RuntimeError("cloud2raster: no tile succeeded; first status: " + str(res["status"].iloc[0]))
    dtm_tiles = sorted(str(p) for p in dtm_dir.glob(f"*_dtm_{dtm_res_m}m.tif"))
    chm_tiles = sorted(str(p) for p in chm_dir.glob(f"*_chm_{chm_res_m}m.tif"))
    dtm_path = delivery / f"dtm_{dtm_res_m}m.tif"; chm_path = delivery / f"chm_{chm_res_m}m.tif"
    mosaic(dtm_tiles, dtm_path, "mean")
    mosaic(chm_tiles, chm_path, "max", fill3=True)
    hdrs = pd.DataFrame([las_header(f) for f in files])
    ctg = gpd.GeoDataFrame(hdrs.drop(columns=["crs"]), geometry=[box(r.xmin, r.ymin, r.xmax, r.ymax) for r in hdrs.itertuples()],
                           crs=hdrs["crs"].iloc[0] if hdrs["crs"].iloc[0] is not None else crs)
    ctg = ctg.merge(res[["tile", "status", "seconds", "n_points", "n_ground", "pts_m2"]].rename(columns={"n_points": "n_points_kept"}),
                    left_on=hdrs["path"].map(lambda p: Path(p).stem), right_on="tile", how="left")
    ctg.to_file(delivery / "raw_las_ctg_info.gpkg", driver="GPKG")
    res.to_csv(delivery / "cloud2raster_processing_log.csv", index=False)
    if not keep_intrmdt:
        for p in list(dtm_dir.glob("*.tif")) + list(chm_dir.glob("*.tif")):
            p.unlink()
    return {"dtm_path": str(dtm_path), "chm_path": str(chm_path), "norm_dir": str(norm_dir) if write_normalized else None,
            "ctg": ctg, "delivery_dir": str(delivery), "temp_dir": str(temp), "results": res}


def reproject_las(path: Path, out_path: Path, dst_epsg: int = 26910) -> Path:
    """apply_st_transform_las(): rewrite a tile in a metric CRS (cloud2trees used EPSG:5070; TRPA uses 26910)."""
    import laspy
    from pyproj import Transformer, CRS
    las = laspy.read(path)
    src = las.header.parse_crs()
    tr = Transformer.from_crs(src, CRS.from_epsg(dst_epsg), always_xy=True)
    x, y = tr.transform(np.asarray(las.x), np.asarray(las.y))
    z = np.asarray(las.z)
    if crs_is_feet(src):
        z = z * (0.3048006096 if "survey" in " ".join(a.unit_name.lower() for a in src.axis_info[:2]) else 0.3048)
    hdr = laspy.LasHeader(point_format=las.header.point_format, version=las.header.version)
    hdr.offsets = [float(x.min()), float(y.min()), float(z.min())]; hdr.scales = [0.001, 0.001, 0.001]
    new = laspy.LasData(hdr)
    new.points = las.points.copy()
    new.x = x; new.y = y; new.z = z
    new.header.add_crs(CRS.from_epsg(dst_epsg))
    new.write(out_path)
    return out_path
