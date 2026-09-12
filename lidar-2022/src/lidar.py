"""
src/lidar.py — per-tile LAS/LAZ processing for the 2022 acquisition.

Built for a multi-terabyte LAZ archive on a network-attached drive:
  * one network read per tile (copy to local scratch, process locally, delete)
  * one decompression pass per tile when the tile fits in memory, chunked two-pass otherwise
  * every tile writes its own partial (.npz) so a run can be stopped and resumed, and
    tiles can be processed in parallel with a multiprocessing pool
  * p95 is carried as a per-cell height histogram, so partials from overlapping tiles merge exactly

Public pieces used by notebooks/00a_las_to_chm.ipynb:
    drive_inventory(root)                     what is on the drive, by folder and extension (no file opens)
    header_index(paths)                       extent, count, format per tile from headers only
    sample_classification(path, n)            share of ground / unclassified in a point sample
    process_tile(args)                        DTM -> HAG -> CHM tile + 30 m partial; safe for Pool.map
    merge_partials(partial_paths, grid)       sums partials into the 30 m metric arrays
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import ndimage

HIST_BIN_M = 0.5
HIST_MAX_M = 80.0
N_BINS = int(HIST_MAX_M / HIST_BIN_M)
NOISE_CLASSES = (7, 18)
NONVEG_CLASSES = (6, 9, 17)          # building, water, bridge deck: dropped from canopy metrics when the vendor classified them


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def drive_inventory(root: str | Path) -> pd.DataFrame:
    """Walk the drive once with os.scandir; return size and count by (folder, extension). Opens no files."""
    rows = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            ext = Path(f).suffix.lower()
            try:
                size = os.stat(os.path.join(dirpath, f)).st_size
            except OSError:
                continue
            k = (dirpath, ext)
            r = rows.setdefault(k, {"folder": dirpath, "ext": ext, "files": 0, "gb": 0.0})
            r["files"] += 1
            r["gb"] += size / 1e9
    return pd.DataFrame(rows.values()).sort_values("gb", ascending=False) if rows else pd.DataFrame(columns=["folder", "ext", "files", "gb"])


def header_index(paths: list[Path]) -> pd.DataFrame:
    """Header-only read per tile (fast even over the network)."""
    import laspy
    rows = []
    for p in paths:
        try:
            with laspy.open(p) as f:
                h = f.header
                try:
                    epsg = h.parse_crs().to_epsg()
                except Exception:
                    epsg = None
                rows.append({"tile": p.name, "path": str(p), "points": h.point_count, "gb": p.stat().st_size / 1e9,
                             "xmin": h.mins[0], "ymin": h.mins[1], "xmax": h.maxs[0], "ymax": h.maxs[1],
                             "pts_per_m2": h.point_count / max((h.maxs[0] - h.mins[0]) * (h.maxs[1] - h.mins[1]), 1),
                             "point_format": h.point_format.id, "version": str(h.version), "epsg": epsg})
        except Exception as e:
            rows.append({"tile": p.name, "path": str(p), "error": str(e)})
    return pd.DataFrame(rows)


def sample_classification(path: Path, n: int = 1_000_000, ground_class: int = 2) -> dict:
    """Share of ground, unclassified, and first returns in the first n points."""
    import laspy
    with laspy.open(path) as f:
        pts = next(iter(f.chunk_iterator(n)))
    cls = np.asarray(pts.classification)
    rn = np.asarray(pts.return_number)
    return {"tile": path.name, "pct_ground": float((cls == ground_class).mean() * 100),
            "pct_unclassified": float(np.isin(cls, [0, 1]).mean() * 100), "pct_first": float((rn == 1).mean() * 100)}


# ---------------------------------------------------------------------------
# Per-tile processing
# ---------------------------------------------------------------------------

def _build_dtm(xg, yg, zg, bounds, res):
    xmin, ymin, xmax, ymax = bounds
    nc = int(np.ceil((xmax - xmin) / res)) + 1
    nr = int(np.ceil((ymax - ymin) / res)) + 1
    ci = np.clip(((xg - xmin) / res).astype(int), 0, nc - 1)
    ri = np.clip(((ymax - yg) / res).astype(int), 0, nr - 1)
    s = np.zeros((nr, nc)); n = np.zeros((nr, nc))
    np.add.at(s, (ri, ci), zg); np.add.at(n, (ri, ci), 1)
    dtm = np.where(n > 0, s / np.maximum(n, 1), np.nan)
    if np.isnan(dtm).any():
        _, (ir, ic) = ndimage.distance_transform_edt(np.isnan(dtm), return_indices=True)
        dtm = dtm[ir, ic]
    return ndimage.uniform_filter(dtm, 3, mode="nearest")


def _accumulate(acc, hist, x, y, hag, first, cls, nret, grid, band, ground_class, use_first):
    x0, y1, g, nrow, ncol = grid
    ci = np.floor((x - x0) / g).astype(int); ri = np.floor((y1 - y) / g).astype(int)
    ok = (ri >= 0) & (ri < nrow) & (ci >= 0) & (ci < ncol)
    ri, ci, h, fr, cl, nr = ri[ok], ci[ok], hag[ok], first[ok], cls[ok], nret[ok]
    # penetrability: above 2 m, a single-return pulse means a solid surface (roof, rock); canopy yields several returns
    m = fr & (h > 2); np.add.at(acc["n_gt2_first"], (ri[m], ci[m]), 1)
    m = m & (nr == 1); np.add.at(acc["n_gt2_single"], (ri[m], ci[m]), 1)
    base = fr if use_first else np.ones_like(fr, dtype=bool)
    np.add.at(acc["n_first"], (ri[base], ci[base]), 1)
    m = base & (h > 2); np.add.at(acc["n_first_gt2"], (ri[m], ci[m]), 1)
    m = base & (h > 5); np.add.at(acc["n_first_gt5"], (ri[m], ci[m]), 1)
    np.add.at(acc["n_all"], (ri, ci), 1)
    m = h > 0.5; np.add.at(acc["n_all_gt05"], (ri[m], ci[m]), 1)
    m = (h >= band[0]) & (h < band[1]); np.add.at(acc["n_mid"], (ri[m], ci[m]), 1)
    m = cl == ground_class; np.add.at(acc["n_ground"], (ri[m], ci[m]), 1)
    m = h > 2
    b = np.minimum((h[m] / HIST_BIN_M).astype(int), N_BINS - 1)
    np.add.at(hist, (ri[m], ci[m], b), 1)


def _write_normalized_inmemory(las, x, y, z, dtm, bounds, res, src, args, L):
    """AOI-filtered, height-normalized copy of the tile. Keeps z and adds HeightAboveGround (float32),
    or replaces z with HAG when normalized_write_extra_dim is false. Written straight to the share."""
    import laspy
    out = Path(args["normalized_dir"]) / (src.stem + "_hag.laz")
    if out.exists():
        return
    dr = np.clip(((bounds[3] - y) / res).astype(int), 0, dtm.shape[0] - 1)
    dc = np.clip(((x - bounds[0]) / res).astype(int), 0, dtm.shape[1] - 1)
    hag = (z - dtm[dr, dc]).astype("float32")
    if L.get("normalized_write_extra_dim", True):
        las.add_extra_dim(laspy.ExtraBytesParams(name="HeightAboveGround", type=np.float32, description="z minus DTM (class 2 mean, 1 m)"))
        las.HeightAboveGround = hag
    else:
        las.z = hag
    tmp = out.with_suffix(".tmp.laz")
    las.write(tmp)
    tmp.replace(out)


def process_tile(args: dict) -> dict:
    """
    One tile: stage locally (optional), DTM from ground class, HAG, 1 m CHM tile, 30 m partial.
    args keys: path, grid (x0, y1, g, nrow, ncol), cfg_lidar, wcrs, chm_dir, partial_dir,
               scratch (or None), in_memory_max_points, normalized_dir (or None), dtm_dir (or None)
    Returns a small dict of stats; writes <partial_dir>/<tile>.npz and <chm_dir>/<tile>_chm1m.tif.
    """
    import laspy
    import rasterio
    from rasterio.transform import from_origin

    t0 = time.time()
    src = Path(args["path"]); L = args["cfg_lidar"]; grid = args["grid"]
    x0, y1, g, nrow, ncol = grid
    partial = Path(args["partial_dir"]) / (src.stem + ".npz")
    chm_out = Path(args["chm_dir"]) / (src.stem + "_chm1m.tif")
    dtm_dir = Path(args["dtm_dir"]) if args.get("dtm_dir") else None
    dtm_out = dtm_dir / (src.stem + "_dtm1m.tif") if dtm_dir else None
    if partial.exists() and chm_out.exists() and (dtm_out is None or dtm_out.exists()):
        return {"tile": src.name, "status": "skipped (done)", "seconds": 0}

    # stage locally: one network read instead of two decompression passes over the wire
    local = src
    if args.get("scratch"):
        local = Path(args["scratch"]) / src.name
        if not local.exists():
            shutil.copy2(src, local)
    read_s = time.time() - t0

    with laspy.open(local) as f:
        h = f.header
        n_pts = h.point_count
        bounds = (np.floor(h.mins[0]), np.floor(h.mins[1]), np.ceil(h.maxs[0]), np.ceil(h.maxs[1]))
    res = L["dtm_res_m"]; gc = L["ground_class"]; band = tuple(L["mid_canopy_band_m"])
    use_first = L["point_metrics_use_first_returns_for_cover"]

    # local 30 m sub-grid covering this tile (+1 cell), accumulated then placed in the partial with an offset
    r_off = max(int(np.floor((y1 - bounds[3]) / g)), 0); c_off = max(int(np.floor((bounds[0] - x0) / g)), 0)
    r_end = min(int(np.ceil((y1 - bounds[1]) / g)), nrow); c_end = min(int(np.ceil((bounds[2] - x0) / g)), ncol)
    sub_rows, sub_cols = r_end - r_off, c_end - c_off
    if sub_rows <= 0 or sub_cols <= 0:
        return {"tile": src.name, "status": "outside grid", "seconds": time.time() - t0}
    sub_grid = (x0 + c_off * g, y1 - r_off * g, g, sub_rows, sub_cols)
    keys = ["n_first", "n_first_gt2", "n_first_gt5", "n_all", "n_all_gt05", "n_mid", "n_ground", "n_gt2_first", "n_gt2_single"]
    acc = {k: np.zeros((sub_rows, sub_cols), dtype=np.float64) for k in keys}
    hist = np.zeros((sub_rows, sub_cols, N_BINS), dtype=np.uint32)
    cw = int(bounds[2] - bounds[0]); ch = int(bounds[3] - bounds[1])
    chm = np.full((ch, cw), -1.0, dtype="float32")

    def handle_points(x, y, z, rn, cls, nret, dtm):
        dr = np.clip(((bounds[3] - y) / res).astype(int), 0, dtm.shape[0] - 1)
        dc = np.clip(((x - bounds[0]) / res).astype(int), 0, dtm.shape[1] - 1)
        hag = z - dtm[dr, dc]
        keep = (hag > -1.0) & (hag <= L["max_hag_m"]) & ~np.isin(cls, NOISE_CLASSES) & ~np.isin(cls, NONVEG_CLASSES)
        x, y, hag, rn, cls, nret = x[keep], y[keep], np.clip(hag[keep], 0, None), rn[keep], cls[keep], nret[keep]
        first = rn == 1
        cr = np.clip((bounds[3] - y[first]).astype(int), 0, ch - 1); cc = np.clip((x[first] - bounds[0]).astype(int), 0, cw - 1)
        np.maximum.at(chm, (cr, cc), hag[first].astype("float32"))
        _accumulate(acc, hist, x, y, hag, first, cls, nret, sub_grid, band, gc, use_first)

    n_ground = 0
    if n_pts <= args.get("in_memory_max_points", 60_000_000):
        las = laspy.read(local)                       # single decompression
        x = np.asarray(las.x); y = np.asarray(las.y); z = np.asarray(las.z)
        cls = np.asarray(las.classification); rn = np.asarray(las.return_number); nret = np.asarray(las.number_of_returns)
        gm = cls == gc; n_ground = int(gm.sum())
        if n_ground < L["min_ground_points_per_tile"]:
            return {"tile": src.name, "status": f"no ground ({n_ground})", "seconds": time.time() - t0}
        dtm = _build_dtm(x[gm], y[gm], z[gm], bounds, res)
        for s in range(0, len(x), 5_000_000):
            e = s + 5_000_000
            handle_points(x[s:e], y[s:e], z[s:e], rn[s:e], cls[s:e], nret[s:e], dtm)
        if args.get("normalized_dir"):
            _write_normalized_inmemory(las, x, y, z, dtm, bounds, res, src, args, L)
        del las, x, y, z, cls, rn, nret
    else:                                              # chunked two-pass for very large tiles
        gx, gy, gz = [], [], []
        with laspy.open(local) as f:
            for pts in f.chunk_iterator(5_000_000):
                m = np.asarray(pts.classification) == gc
                gx.append(np.asarray(pts.x)[m]); gy.append(np.asarray(pts.y)[m]); gz.append(np.asarray(pts.z)[m])
        gx, gy, gz = map(np.concatenate, (gx, gy, gz)); n_ground = len(gz)
        if n_ground < L["min_ground_points_per_tile"]:
            return {"tile": src.name, "status": f"no ground ({n_ground})", "seconds": time.time() - t0}
        dtm = _build_dtm(gx, gy, gz, bounds, res)
        writer = None
        if args.get("normalized_dir"):
            out_laz = Path(args["normalized_dir"]) / (src.stem + "_hag.laz")
            with laspy.open(local) as f:
                hdr = f.header
            writer = laspy.open(out_laz, mode="w", header=hdr)
        with laspy.open(local) as f:
            for pts in f.chunk_iterator(5_000_000):
                handle_points(np.asarray(pts.x), np.asarray(pts.y), np.asarray(pts.z),
                              np.asarray(pts.return_number), np.asarray(pts.classification), np.asarray(pts.number_of_returns), dtm)
                if writer is not None:                 # chunked path: z becomes height above ground
                    dr = np.clip(((bounds[3] - np.asarray(pts.y)) / res).astype(int), 0, dtm.shape[0] - 1)
                    dc = np.clip(((np.asarray(pts.x) - bounds[0]) / res).astype(int), 0, dtm.shape[1] - 1)
                    pts.z = np.asarray(pts.z) - dtm[dr, dc]
                    writer.write_points(pts)
        if writer is not None:
            writer.close()

    # bare earth: the same DTM used for normalization, cropped to the tile bounds
    if dtm_out is not None:
        dtm_dir.mkdir(parents=True, exist_ok=True)
        d = dtm[:ch, :cw].astype("float32")
        with rasterio.open(dtm_out, "w", driver="GTiff", height=d.shape[0], width=d.shape[1], count=1, dtype="float32", crs=args["wcrs"],
                           transform=from_origin(bounds[0], bounds[3], res, res), nodata=L["nodata"], tiled=True, compress="deflate") as dst:
            dst.write(d, 1)

    # CHM pit fill and write
    nod = chm < 0
    med = ndimage.median_filter(np.where(nod, 0, chm), 3)
    chm = np.where(nod & (med > 2), med, chm); chm[chm < 0] = L["nodata"]
    with rasterio.open(chm_out, "w", driver="GTiff", height=ch, width=cw, count=1, dtype="float32", crs=args["wcrs"],
                       transform=from_origin(bounds[0], bounds[3], 1, 1), nodata=L["nodata"], tiled=True, compress="deflate") as dst:
        dst.write(chm, 1)
    np.savez_compressed(partial, r_off=r_off, c_off=c_off, hist=hist, **acc)

    if args.get("scratch") and local != src:
        try: local.unlink()
        except OSError: pass
    return {"tile": src.name, "status": "ok", "points": n_pts, "ground": n_ground, "gb": src.stat().st_size / 1e9,
            "copy_seconds": round(read_s, 1), "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_partials(partial_paths: list[Path], grid, percentile: float = 95) -> dict:
    x0, y1, g, nrow, ncol = grid
    keys = ["n_first", "n_first_gt2", "n_first_gt5", "n_all", "n_all_gt05", "n_mid", "n_ground", "n_gt2_first", "n_gt2_single"]
    acc = {k: np.zeros((nrow, ncol)) for k in keys}
    hist = np.zeros((nrow, ncol, N_BINS), dtype=np.uint32)
    for p in partial_paths:
        d = np.load(p)
        r, c = int(d["r_off"]), int(d["c_off"])
        h = d["hist"]; rr, cc = h.shape[:2]
        hist[r:r + rr, c:c + cc] += h
        for k in keys:
            if k in d.files:
                acc[k][r:r + rr, c:c + cc] += d[k]
    cum = np.cumsum(hist, axis=2)
    tot = cum[:, :, -1]
    target = np.ceil(tot * percentile / 100.0)
    idx = (cum >= target[:, :, None]).argmax(axis=2)
    p95 = np.where(tot > 0, (idx + 0.5) * HIST_BIN_M, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = {
            "cover": np.where(acc["n_first"] > 0, acc["n_first_gt2"] / acc["n_first"] * 100, np.nan),
            "cover5": np.where(acc["n_first"] > 0, acc["n_first_gt5"] / acc["n_first"] * 100, np.nan),
            "mid_canopy_frac": np.where(acc["n_all_gt05"] > 0, acc["n_mid"] / acc["n_all_gt05"], np.nan),
            "return_density": np.where(acc["n_all"] > 0, acc["n_all"] / (g * g), np.nan),
            "ground_density": np.where(acc["n_all"] > 0, acc["n_ground"] / (g * g), np.nan),
            "p95": p95,
            # solid fraction: share of above-2 m first returns that were single-return pulses. ~1 = roof or rock, forest well below.
            "solid_frac": np.where(acc["n_gt2_first"] >= 20, acc["n_gt2_single"] / np.maximum(acc["n_gt2_first"], 1), np.nan),
        }
    return out
