"""Generate notebooks/00_lidar.ipynb. Run: python scripts/build_lidar_notebook.py"""
import nbformat as nbf
from pathlib import Path

NB = Path(__file__).resolve().parent.parent / "notebooks"

HEADER = '''import sys, os
print(sys.executable)
from pathlib import Path
os.chdir(Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd())
sys.path.insert(0, str(Path.cwd()))
import numpy as np, pandas as pd
import rasterio
from rasterio.windows import Window
from rasterio.transform import from_origin
from scipy import ndimage
from src.io import load_config, get_logger
cfg = load_config()
log = get_logger("00_lidar")
L = cfg["lidar"]; g = cfg["crs"]["lidar_grid_m"]; wcrs = cfg["crs"]["working"]
RAW = Path(cfg["paths"]["raw"]); RAW.mkdir(parents=True, exist_ok=True)
log.info(f"Project: {cfg['project']['name']} | synthetic={cfg['run']['synthetic']} | grid={g} m")'''

cells = [
("md", """# 00 LiDAR metrics

Derive the CHM-based 30 m rasters from the 2022 LiDAR canopy height model. When the raw LAS tiles are the starting point, run `00a_las_to_chm.ipynb` first: it writes the 1 m CHM tiles this notebook reads and the point-based cover, p95, and mid-canopy rasters that `01_frame` prefers. This notebook adds individual tree detection and rumple, and its CHM-based cover and p95 are written under `_chm_` names for comparison (they stand in for the point-based versions only when 00a did not run).

| Output raster (`sources:` key) | Metric | Used for |
|---|---|---|
| `canopy_cover_30m` | fraction of 1 m CHM cells above 2 m (vertical projection) | cover class, open/closed at 40/50 percent |
| `canopy_cover5_30m` | fraction above 5 m | Becky's second cover break; evaluation |
| `p95_height_30m` | 95th percentile CHM height | seral proxy, calibrated to QMD 5 and 25 in |
| `stem_density_30m` | individual-tree-detection count per acre from CHM local maxima | density proxy for the TPA classes |
| `rumple_30m` | surface area / ground area of the CHM | heterogeneity attribute (optional) |
| `mid_canopy_frac_30m` | fraction of returns 2 to 8 m above ground (point cloud only) | ladder-fuel proxy; alternative density proxy |

Everything runs in blocks so a Basin-wide 1 m CHM fits in memory. The 30 m grid is snapped to `lidar.grid_origin` so it aligns with Shengli's model grid; confirm his origin and cell size before the real run. With `run.synthetic: true` a small fake CHM is generated so the notebook can be exercised."""),
("code", HEADER),
("md", "## Input CHM\n\nA single GeoTIFF, a folder of tiles (mosaicked with a VRT), or a TRPA ImageServer URL (exported once at 1 m to `data/raw/`)."),
("code", '''src = L["chm_source"]
if cfg["run"]["synthetic"]:
    # fake 1 m CHM: 3 km x 3 km, blobs of tall and short forest, some openings, random trees
    rng = np.random.default_rng(7)
    n = 3000
    yy, xx = np.mgrid[0:n, 0:n]
    base = 6 + 14 * np.sin(xx / 600) * np.cos(yy / 500)           # stand-level height field
    base = np.clip(base + rng.normal(0, 1.5, (n, n)), 0, None)
    density = np.clip(0.004 + 0.02 * (base / base.max()), 0, 0.03)  # denser where taller
    trees = rng.random((n, n)) < density
    tops = np.where(trees, np.clip(base * rng.uniform(0.8, 1.6, (n, n)), 3, 50), 0)
    crown_px = 7
    chm = ndimage.maximum_filter(tops, size=crown_px)               # flat-topped crowns
    chm = ndimage.gaussian_filter(chm, 1.2)                          # soften edges
    chm[base < 4] *= 0.15                                            # openings
    chm = np.clip(chm + rng.normal(0, 0.3, (n, n)), 0, 55).astype("float32")
    transform = from_origin(L["grid_origin"][0], L["grid_origin"][1] + n, 1, 1)
    chm_path = RAW / "synthetic_chm_1m.tif"
    with rasterio.open(chm_path, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32",
                       crs=wcrs, transform=transform, nodata=L["nodata"], tiled=True, compress="deflate") as dst:
        dst.write(chm, 1)
    src = str(chm_path)
    log.info(f"Synthetic CHM written: {chm_path} ({n} x {n} m)")
elif str(src).lower().startswith("http"):
    from src.layers import read_raster
    raise SystemExit("For an ImageServer CHM, export it once at 1 m with src.layers.read_raster(bbox=...) and point lidar.chm_source at the cached GeoTIFF")
elif Path(src).is_dir():
    tiles = sorted(str(p) for p in Path(src).glob("*.tif"))
    assert tiles, f"no .tif tiles in {src}; run 00a_las_to_chm first"
    vrt = RAW / "chm_2022_1m.vrt"
    try:
        from osgeo import gdal                       # present in arcgispro-py3
        gdal.BuildVRT(str(vrt), tiles); src = str(vrt)
        log.info(f"Built VRT from {len(tiles)} tiles: {vrt}")
    except ImportError:
        import subprocess, shutil
        if shutil.which("gdalbuildvrt"):
            subprocess.run(["gdalbuildvrt", str(vrt)] + tiles, check=True); src = str(vrt)
            log.info(f"Built VRT with gdalbuildvrt from {len(tiles)} tiles")
        else:                                        # last resort: merge to one GeoTIFF (memory heavy Basin-wide)
            from rasterio.merge import merge
            srcs = [rasterio.open(t) for t in tiles]
            mosaic, tr = merge(srcs, nodata=L["nodata"])
            prof = srcs[0].profile; prof.update(height=mosaic.shape[1], width=mosaic.shape[2], transform=tr, tiled=True, compress="deflate")
            mos = RAW / "chm_2022_1m_mosaic.tif"
            with rasterio.open(mos, "w", **prof) as dst: dst.write(mosaic)
            for s_ in srcs: s_.close()
            src = str(mos); log.warning(f"No GDAL VRT available; merged {len(tiles)} tiles to {mos}")

chm_ds = rasterio.open(src)
log.info(f"CHM: {chm_ds.width} x {chm_ds.height} px, res {chm_ds.res}, CRS {chm_ds.crs}, nodata {chm_ds.nodata}")
assert abs(chm_ds.res[0] - 1.0) < 0.05, "expected a 1 m CHM; resample first if not"
if str(chm_ds.crs) != wcrs:
    log.warning(f"CHM CRS {chm_ds.crs} != working CRS {wcrs}; reproject the CHM (gdalwarp) before running")'''),
("md", "## Output grid\n\nA 30 m grid snapped to `lidar.grid_origin` that covers the CHM extent. Every output raster shares this transform so they stack pixel for pixel with Shengli's covariates."),
("code", '''ox, oy = L["grid_origin"]
b = chm_ds.bounds
x0 = ox + np.floor((b.left - ox) / g) * g
y1 = oy + np.ceil((b.top - oy) / g) * g
ncol = int(np.ceil((b.right - x0) / g)); nrow = int(np.ceil((y1 - b.bottom) / g))
out_transform = from_origin(x0, y1, g, g)
log.info(f"Output grid: {ncol} x {nrow} cells of {g} m, origin ({x0:.0f}, {y1:.0f})")

metrics = {k: np.full((nrow, ncol), np.nan, dtype="float32")
           for k in ["cover", "cover5", "p95", "itd_per_ac", "rumple", "n_valid"]}'''),
("md", "## Block processing: cover, p95 height, rumple, and individual tree detection\n\nITD uses a Gaussian-smoothed CHM and a local-maximum filter whose window grows with height (`lidar.itd.window_m_by_height`). Detected tops are counted per 30 m cell and converted to trees per acre. This is a proxy for stems, not a stem count: it under-detects suppressed trees under closed canopy, which is exactly why the plots exist. Blocks overlap by one window so edge trees are not double counted."),
("code", '''thr = L["canopy_height_threshold_m"]; thr5 = L["cover_secondary_threshold_m"]; pct = L["height_percentile"]
itd = L["itd"]; sigma = itd["smooth_sigma_m"]; hmin = itd["min_tree_height_m"]
win_map = sorted((float(k), int(v)) for k, v in itd["window_m_by_height"].items())
pad = max(v for _, v in win_map)
nod = chm_ds.nodata if chm_ds.nodata is not None else L["nodata"]
B = max(int(L["tile_px"] // g), 1)           # block size in output cells
cell_ac = g * g / 4046.86

def local_maxima(arr):
    """Variable-window local maxima: a cell is a tree top if it equals the max in the window for its height class."""
    sm = ndimage.gaussian_filter(arr, sigma)
    tops = np.zeros(arr.shape, dtype=bool)
    lower = 0.0
    for upper, w in win_map:
        band = (sm >= max(lower, hmin)) & (sm < upper)
        if band.any():
            mx = ndimage.maximum_filter(sm, size=w, mode="nearest")
            tops |= band & (sm == mx) & (sm >= hmin)
        lower = upper
    return tops

def read_block(xb0, yb1, ncell_x, ncell_y, padpx):
    """Read the CHM for a block of output cells (+ pad) in map coordinates; nodata -> NaN."""
    from rasterio.windows import from_bounds
    win = from_bounds(xb0 - padpx, yb1 - ncell_y * g - padpx, xb0 + ncell_x * g + padpx, yb1 + padpx, transform=chm_ds.transform)
    a = chm_ds.read(1, window=win, boundless=True, fill_value=nod,
                    out_shape=(ncell_y * g + 2 * padpx, ncell_x * g + 2 * padpx)).astype("float32")
    a[(a == nod) | ~np.isfinite(a)] = np.nan
    return a

for rb in range(0, nrow, B):
    ny = min(B, nrow - rb)
    for cb in range(0, ncol, B):
        nx = min(B, ncol - cb)
        xb0 = x0 + cb * g; yb1 = y1 - rb * g
        a = read_block(xb0, yb1, nx, ny, pad)
        core = a[pad:pad + ny * g, pad:pad + nx * g]
        blocks = core.reshape(ny, g, nx, g)                         # (cell_row, py, cell_col, px)
        valid = np.isfinite(blocks)
        nv = valid.sum(axis=(1, 3))
        if nv.sum() == 0:
            continue
        with np.errstate(invalid="ignore", divide="ignore"):
            metrics["n_valid"][rb:rb+ny, cb:cb+nx] = nv
            metrics["cover"][rb:rb+ny, cb:cb+nx] = np.where(nv > 0, (blocks > thr).sum(axis=(1, 3)) / nv * 100, np.nan)
            metrics["cover5"][rb:rb+ny, cb:cb+nx] = np.where(nv > 0, (blocks > thr5).sum(axis=(1, 3)) / nv * 100, np.nan)
            metrics["p95"][rb:rb+ny, cb:cb+nx] = np.nanpercentile(blocks, pct, axis=(1, 3))
            filled = np.nan_to_num(a, nan=0.0)
            gy, gx = np.gradient(filled)
            sa = np.sqrt(1 + gx**2 + gy**2)[pad:pad + ny * g, pad:pad + nx * g].reshape(ny, g, nx, g)
            metrics["rumple"][rb:rb+ny, cb:cb+nx] = np.where(nv > 0, np.where(valid, sa, 0).sum(axis=(1, 3)) / nv, np.nan)
            tops = local_maxima(filled) & np.isfinite(a)
            nt = tops[pad:pad + ny * g, pad:pad + nx * g].reshape(ny, g, nx, g).sum(axis=(1, 3))
            metrics["itd_per_ac"][rb:rb+ny, cb:cb+nx] = np.where(nv > 0, nt / (nv / (g * g)) / cell_ac, np.nan)
    log.info(f"Cell rows {rb}-{min(rb+B, nrow)} of {nrow} done")

log.info(f"Cells with data: {int((metrics['n_valid'] > 0).sum()):,} of {nrow*ncol:,}")'''),
("md", "## Point-cloud metric (optional): mid-canopy return fraction\n\nRuns only when `lidar.laz_dir` is set. Uses `laspy` (in arcgispro-py3) on height-normalized or classified tiles; returns between 2 and 8 m above ground divided by all returns above 0.5 m. This is the ladder-fuel proxy and a candidate density proxy for stems under closed canopy that ITD cannot see."),
("code", '''mid = None
if L["laz_dir"]:
    import laspy
    lo, hi = L["mid_canopy_band_m"]
    n_mid = np.zeros((nrow, ncol)); n_all = np.zeros((nrow, ncol))
    tiles = sorted(Path(L["laz_dir"]).glob("*.la[sz]"))
    for t in tiles:
        with laspy.open(t) as f:
            for pts in f.chunk_iterator(2_000_000):
                x = np.asarray(pts.x); y = np.asarray(pts.y)
                # height above ground: prefer HeightAboveGround extra dim, else z (assumes normalized tiles)
                hag = np.asarray(pts["HeightAboveGround"]) if "HeightAboveGround" in pts.point_format.dimension_names else np.asarray(pts.z)
                ci = np.floor((x - x0) / g).astype(int); ri = np.floor((y1 - y) / g).astype(int)
                ok = (ri >= 0) & (ri < nrow) & (ci >= 0) & (ci < ncol) & (hag > 0.5)
                np.add.at(n_all, (ri[ok], ci[ok]), 1)
                band = ok & (hag >= lo) & (hag < hi)
                np.add.at(n_mid, (ri[band], ci[band]), 1)
        log.info(f"{t.name} done")
    with np.errstate(invalid="ignore", divide="ignore"):
        mid = np.where(n_all > 0, n_mid / n_all, np.nan).astype("float32")
    metrics["mid_canopy_frac"] = mid
else:
    log.info("lidar.laz_dir not set; mid-canopy return fraction skipped")'''),
("md", "## Write the 30 m rasters\n\nFile names match the `sources:` keys in `config.yaml` so notebooks 01 and 02 pick them up without edits."),
("code", '''# CHM-derived cover and p95 get their own names so they never overwrite the point-based versions from 00a.
names = {"cover": "lidar2022_cover_chm_30m.tif", "cover5": "lidar2022_cover5m_chm_30m.tif", "p95": "lidar2022_p95_chm_30m.tif",
         "itd_per_ac": "lidar2022_itd_per_ac_30m.tif", "rumple": "lidar2022_rumple_30m.tif", "mid_canopy_frac": "lidar2022_midcanopy_30m.tif"}
if L["laz_dir"] is None and (RAW / "lidar2022_midcanopy_30m.tif").exists():
    names.pop("mid_canopy_frac")          # 00a already wrote it from the point cloud
written = {}
for key, fname in names.items():
    if key not in metrics or metrics[key] is None: continue
    arr = np.where(np.isfinite(metrics[key]), metrics[key], L["nodata"]).astype("float32")
    path = RAW / fname
    with rasterio.open(path, "w", driver="GTiff", height=nrow, width=ncol, count=1, dtype="float32",
                       crs=wcrs, transform=out_transform, nodata=L["nodata"], tiled=True, compress="deflate") as dst:
        dst.write(arr, 1)
    written[key] = str(path)
    v = metrics[key][np.isfinite(metrics[key])]
    log.info(f"{fname}: min {v.min():.1f}  p25 {np.percentile(v,25):.1f}  median {np.median(v):.1f}  p75 {np.percentile(v,75):.1f}  max {v.max():.1f}")
# If 00a did not run (no point cloud, CHM only), the CHM-based cover and p95 stand in under the names 01_frame reads.
import shutil
for chm_name, std_name in [("lidar2022_cover_chm_30m.tif", "lidar2022_cover_30m.tif"), ("lidar2022_cover5m_chm_30m.tif", "lidar2022_cover5m_30m.tif"), ("lidar2022_p95_chm_30m.tif", "lidar2022_p95_30m.tif")]:
    if not (RAW / std_name).exists():
        shutil.copy(RAW / chm_name, RAW / std_name); log.info(f"No point-based {std_name}; using the CHM-derived version")
pd.Series(written).to_csv(Path(cfg["paths"]["outputs"]) / "lidar_rasters_written.csv", header=False)
written'''),
("md", "## Sanity checks\n\nCover against height (should rise together), ITD against cover (ITD should saturate or fall in closed tall stands, which is the known blind spot), and a quick look at the class breaks in `config.yaml` against the distributions so the placeholders can be set sensibly before calibration."),
("code", '''df = pd.DataFrame({k: metrics[k].ravel() for k in ["cover", "p95", "itd_per_ac", "rumple"]}).dropna()
log.info(f"corr cover~p95 {df['cover'].corr(df['p95']):.2f}; itd~cover {df['itd_per_ac'].corr(df['cover']):.2f}; itd~p95 {df['itd_per_ac'].corr(df['p95']):.2f}")
q = df.quantile([0.1, 0.25, 0.5, 0.75, 0.9]).round(1)
print(q)
for ftype, hb in cfg["strata"]["height_breaks_m"].items():
    share = [(df["p95"] < hb[0]).mean(), ((df["p95"] >= hb[0]) & (df["p95"] < hb[1])).mean(), (df["p95"] >= hb[1]).mean()]
    log.info(f"{ftype} height breaks {hb} -> early/mid/late share of cells {np.round(share, 2)} (all types pooled; split by type in 02_strata)")
try:
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 3, figsize=(13, 3.5))
    ax[0].hexbin(df["p95"], df["cover"], gridsize=40, bins="log"); ax[0].set_xlabel("p95 height (m)"); ax[0].set_ylabel("cover > 2 m (%)")
    ax[1].hexbin(df["cover"], df["itd_per_ac"], gridsize=40, bins="log"); ax[1].set_xlabel("cover (%)"); ax[1].set_ylabel("ITD trees/ac")
    ax[2].imshow(metrics["p95"], cmap="viridis"); ax[2].set_title("p95 height, 30 m"); ax[2].axis("off")
    plt.tight_layout(); plt.savefig(Path(cfg["paths"]["outputs"]) / "lidar_checks.png", dpi=120); plt.show()
except Exception as e:
    log.info(f"plots skipped: {e}")
chm_ds.close()
log.info("LiDAR metrics complete; run 01_frame next")'''),
]

n = nbf.v4.new_notebook()
n["cells"] = [nbf.v4.new_markdown_cell(c[1]) if c[0] == "md" else nbf.v4.new_code_cell(c[1]) for c in cells]
n["metadata"]["kernelspec"] = {"name": "python3", "display_name": "Python 3 (arcgispro-py3)", "language": "python"}
nbf.write(n, NB / "00_lidar.ipynb")
print("wrote 00_lidar")
