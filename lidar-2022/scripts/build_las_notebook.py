"""Generate notebooks/00a_las_to_chm.ipynb. Run: python scripts/build_las_notebook.py"""
import nbformat as nbf
from pathlib import Path

NB = Path(__file__).resolve().parent.parent / "notebooks"

HEADER = '''import sys, os, time
print(sys.executable)
from pathlib import Path
os.chdir(Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd())
sys.path.insert(0, str(Path.cwd()))
import numpy as np, pandas as pd
import rasterio
from rasterio.transform import from_origin
from src.io import load_config, get_logger
from src import lidar
cfg = load_config()
log = get_logger("00a_las_to_chm")
L = cfg["lidar"]; g = cfg["crs"]["lidar_grid_m"]; wcrs = cfg["crs"]["working"]
RAW = Path(cfg["paths"]["raw"]); PROC = Path(cfg["paths"]["processed"]); OUT = Path(cfg["paths"]["outputs"])
CHM_DIR = Path(L["chm_tiles_dir"]); DTM_DIR = Path(L["dtm_tiles_dir"]); PART_DIR = Path(L["partials_dir"])
for d in (RAW, PROC, OUT, CHM_DIR, DTM_DIR, PART_DIR): d.mkdir(parents=True, exist_ok=True)
log.info(f"Project: {cfg['project']['name']} | synthetic={cfg['run']['synthetic']} | engine={L['engine']} | workers={L['workers']} | grid={g} m")'''

cells = [
("md", """# 00a LAS to CHM and point metrics

Turns the raw 2022 LAZ archive into what the rest of the chain needs. Built for the real situation: about 2 TB of LAZ on an external drive on the network, which means the network read is the bottleneck, a full pass takes hours to days, and the run has to survive being stopped.

How it deals with that:

1. **Drive inventory first, no file opens.** What is on the drive by folder and extension, so we process only the classified point-cloud tiles and not DEMs, orthos, or raw swaths that may share the drive.
2. **Header-only tile index**, then an **AOI filter** by header bounds, then a **classification sample** on a couple dozen random tiles to confirm ground (class 2) is present.
3. **One network read per tile.** Each tile is copied to `lidar.local_scratch` (a local SSD), decompressed once in memory when it fits, processed, and deleted. Optionally a height-normalized copy of every AOI tile is written back to the drive (`lidar.normalized_dir`), so any future metric can be computed without redoing the DTM step.
4. **Parallel and resumable.** `lidar.workers` tiles at a time; every tile writes its own partial (`.npz`) and CHM tile, and finished tiles are skipped on rerun. Stop the kernel at any time and rerun the same cell.
5. **Exact merge.** Partials carry counts and a per-cell height histogram, so overlapping tile edges and p95 merge exactly. The merge writes the 30 m rasters under the `sources:` names `01_frame` reads: first-return cover above 2 m and 5 m, p95, mid-canopy fraction (2 to 8 m), return density, ground density, and a solid fraction (share of above-2 m pulses with a single return; near 1 for roofs and rock, low for canopy) that `01_frame` uses to drop non-vegetation cells the frame layers missed. Building, water, and bridge classes are excluded from canopy metrics when the vendor classified them. CHM tiles go to `lidar.chm_tiles_dir` for `00_lidar` (ITD, rumple).

With `run.synthetic: true` four small fake tiles are written and the whole flow runs in seconds."""),
("code", HEADER),
("md", "## Synthetic tiles (only when `run.synthetic` is true)"),
("code", '''import laspy
las_root = Path(L["las_dir"])
if cfg["run"]["synthetic"]:
    las_root = RAW / "las_2022_synthetic"; las_root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(3); ox, oy = L["grid_origin"]
    for ti, (tx, ty) in enumerate([(0, 0), (600, 0), (0, 600), (600, 600)]):
        n = 400_000
        x = rng.uniform(ox + tx, ox + tx + 600, n); y = rng.uniform(oy + ty, oy + ty + 600, n)
        ground_z = 1900 + 0.05 * (x - ox) + 8 * np.sin((y - oy) / 150)
        stand_h = np.clip(8 + 14 * np.sin(x / 300) * np.cos(y / 250) + rng.normal(0, 2, n), 0, 45)
        is_ground = rng.random(n) < 0.35
        hag = np.where(is_ground, 0.0, stand_h * rng.beta(4, 1.5, n))
        hdr = laspy.LasHeader(point_format=6, version="1.4"); hdr.offsets = [ox, oy, 1800]; hdr.scales = [0.01, 0.01, 0.01]
        las = laspy.LasData(hdr); las.x = x; las.y = y; las.z = ground_z + hag
        las.classification = np.where(is_ground, 2, 1).astype("uint8")
        # canopy pulses mostly multi-return; a "roof" patch in the tile corner is single-return only
        roof = (x < ox + tx + 90) & (y < oy + ty + 90) & ~is_ground
        nret = np.where(is_ground, np.where(rng.random(n) < 0.5, 2, 1), np.where(rng.random(n) < 0.75, 3, 1)).astype("uint8")
        nret[roof] = 1
        las.number_of_returns = nret
        las.return_number = np.where(is_ground, nret, 1).astype("uint8")
        las.write(las_root / f"synthetic_tile_{ti}.las")
    (las_root / "not_a_tile.txt").write_text("decoy")
    log.info(f"Synthetic tiles written to {las_root}")'''),
("md", "## 1. Drive inventory\n\nOne `os.walk` over the drive. Nothing is opened. This tells us what the 2 TB actually is before any point is read."),
("code", '''t0 = time.time()
inv = lidar.drive_inventory(las_root)
inv.to_csv(OUT / "drive_inventory.csv", index=False)
by_ext = inv.groupby("ext")[["files", "gb"]].sum().sort_values("gb", ascending=False)
log.info(f"Inventory of {las_root}: {inv['files'].sum():,} files, {inv['gb'].sum():,.1f} GB in {time.time()-t0:,.0f}s")
print(by_ext.round(1).head(15))
print(inv[inv["ext"].isin([".laz", ".las"])].groupby("folder")[["files", "gb"]].sum().sort_values("gb", ascending=False).round(1).head(20))'''),
("md", "## 2. Tile index, AOI filter, classification sample\n\nHeaders only for the index (seconds per thousand tiles over the network). Tiles whose bounds do not touch the AOI are dropped. Then a random sample of tiles is opened for one chunk to confirm ground classification and return numbering."),
("code", '''tiles = sorted(p for p in las_root.rglob(L["las_glob"]))
log.info(f"{len(tiles):,} LAS/LAZ files under {las_root}")
idx = lidar.header_index(tiles)
bad = idx[idx.get("error", pd.Series(dtype=str)).notna()] if "error" in idx else idx.iloc[0:0]
if len(bad): log.warning(f"{len(bad)} tiles failed to open; see outputs/las_tile_index.csv")
idx = idx[idx.get("error", pd.Series(index=idx.index, dtype=object)).isna()].copy() if "error" in idx else idx

# AOI filter by header bounds (Basin boundary, or the CWHR population layer)
aoi_src = L["aoi"]
aoi_ok = bool(aoi_src) and not cfg["run"]["synthetic"] and (str(aoi_src).startswith("http") or Path(aoi_src).exists())
if aoi_src and not aoi_ok and not cfg["run"]["synthetic"]:
    log.warning(f"AOI {aoi_src} not found; processing every tile under las_dir")
if aoi_ok:
    from src.layers import read_layer
    aoi = read_layer(aoi_src, cfg, log=log)
    ax0, ay0, ax1, ay1 = aoi.total_bounds
    inside = (idx["xmax"] > ax0) & (idx["xmin"] < ax1) & (idx["ymax"] > ay0) & (idx["ymin"] < ay1)
    log.info(f"AOI bbox filter: {int(inside.sum()):,} of {len(idx):,} tiles intersect")
    idx = idx[inside].copy()
    try:                                     # exact polygon test on the bbox survivors
        import geopandas as gpd
        from shapely.geometry import box
        gi = gpd.GeoDataFrame(idx, geometry=[box(r.xmin, r.ymin, r.xmax, r.ymax) for r in idx.itertuples()], crs=wcrs)
        idx = idx[gi.intersects(aoi.union_all()).values].copy()
        log.info(f"AOI polygon filter: {len(idx):,} tiles")
    except Exception as e:
        log.warning(f"polygon AOI test skipped: {e}")

# CRS check: every tile must be in the working CRS (or unknown, which we assume)
epsg_work = int(wcrs.split(":")[1])
off_crs = idx[idx["epsg"].notna() & (idx["epsg"] != epsg_work)]
if len(off_crs): log.warning(f"{len(off_crs)} tiles report EPSG != {epsg_work}; reproject (las2las / arcpy) before processing: {off_crs['tile'].head().tolist()}")

# classification sample
rng = np.random.default_rng(1)
samp = idx.sample(min(L["classification_sample_tiles"], len(idx)), random_state=int(rng.integers(1e6)))
cls = pd.DataFrame([lidar.sample_classification(Path(p), ground_class=L["ground_class"]) for p in samp["path"]])
log.info(f"Classification sample ({len(cls)} tiles): ground {cls['pct_ground'].median():.1f}% median (min {cls['pct_ground'].min():.1f}), unclassified {cls['pct_unclassified'].median():.1f}%, first returns {cls['pct_first'].median():.1f}%")
if cls["pct_ground"].min() < 1:
    log.warning("Some sampled tiles have no ground class. Set lidar.engine to arcpy and classify ground first, or ask the vendor for the classified deliverable.")
idx = idx.merge(cls, on="tile", how="left")
idx.to_csv(OUT / "las_tile_index.csv", index=False)
try:
    import geopandas as gpd
    from shapely.geometry import box
    gpd.GeoDataFrame(idx, geometry=[box(r.xmin, r.ymin, r.xmax, r.ymax) for r in idx.itertuples()], crs=wcrs).to_file(OUT / "las_tile_index.gpkg", driver="GPKG")
except Exception as e:
    log.warning(f"tile index GeoPackage skipped: {e}")
total_gb = idx["gb"].sum(); total_pts = idx["points"].sum()
log.info(f"To process: {len(idx):,} tiles, {total_gb:,.1f} GB, {total_pts/1e9:,.2f} billion points, median {idx['pts_per_m2'].median():.1f} pts/m2, largest tile {idx['points'].max()/1e6:,.0f} M points")
idx[["tile", "points", "gb", "pts_per_m2", "pct_ground", "epsg"]].head(10)'''),
("md", "## 3. Output grid\n\nSnapped to `lidar.grid_origin` (set to Shengli's raster origin) and covering all selected tiles."),
("code", '''ox, oy = L["grid_origin"]
x0 = ox + np.floor((idx["xmin"].min() - ox) / g) * g
y1 = oy + np.ceil((idx["ymax"].max() - oy) / g) * g
ncol = int(np.ceil((idx["xmax"].max() - x0) / g)); nrow = int(np.ceil((y1 - idx["ymin"].min()) / g))
grid = (float(x0), float(y1), g, nrow, ncol)
out_transform = from_origin(x0, y1, g, g)
hist_gb = nrow * ncol * lidar.N_BINS * 4 / 1e9
log.info(f"Output grid: {ncol} x {nrow} cells of {g} m; merge histogram will need ~{hist_gb:.1f} GB RAM")'''),
("md", """## 4. Process tiles (parallel, resumable)

Each tile: copy to local scratch, decompress once, DTM from class 2, height above ground, 1 m CHM tile, 30 m partial with counts and a height histogram. Finished tiles (partial and CHM both present) are skipped, so this cell can be interrupted and rerun. After the first few tiles the log prints a projected total runtime; if the projection is unacceptable, the levers are `workers`, `local_scratch` (an SSD, not the C: drive if it is spinning), and running overnight.

The pool uses `spawn`, so `src/lidar.py` does the work and this cell only dispatches."""),
("code", '''from multiprocessing import get_context
norm_dir = None if cfg["run"]["synthetic"] else L.get("normalized_dir")
if norm_dir: Path(norm_dir).mkdir(parents=True, exist_ok=True)
if cfg["run"]["synthetic"]: norm_dir = str(PROC / "laz_hag_synthetic"); Path(norm_dir).mkdir(exist_ok=True)
jobs = [{"path": p, "grid": grid, "cfg_lidar": L, "wcrs": wcrs, "chm_dir": str(CHM_DIR), "partial_dir": str(PART_DIR),
         "scratch": L["local_scratch"], "in_memory_max_points": L["in_memory_max_points"], "normalized_dir": norm_dir,
         "dtm_dir": str(DTM_DIR)} for p in idx["path"]]
if L["local_scratch"]: Path(L["local_scratch"]).mkdir(parents=True, exist_ok=True)
log.info(f"Normalized LAZ copies: {norm_dir or 'off'}")
done_before = sum(1 for p in idx["path"] if (PART_DIR / (Path(p).stem + ".npz")).exists())
log.info(f"{len(jobs)} tiles queued, {done_before} already done")

results = []; t0 = time.time(); gb_done = 0.0
workers = max(1, int(L["workers"])) if not cfg["run"]["synthetic"] else 2
with get_context("spawn").Pool(workers) as pool:
    for i, r in enumerate(pool.imap_unordered(lidar.process_tile, jobs), 1):
        results.append(r)
        if r["status"] == "ok":
            gb_done += r.get("gb", 0)
            log.info(f"[{i}/{len(jobs)}] {r['tile']}: {r['points']/1e6:,.1f} M pts, ground {r['ground']/1e6:,.1f} M, copy {r['copy_seconds']}s, total {r['seconds']}s")
        elif "skipped" not in r["status"]:
            log.warning(f"[{i}/{len(jobs)}] {r['tile']}: {r['status']}")
        if i in (5, 20, 50) or i % 200 == 0:
            elapsed = time.time() - t0; rate = gb_done / max(elapsed, 1)
            remaining_gb = total_gb - gb_done - idx["gb"].iloc[:0].sum()
            log.info(f"Throughput {rate*3600:,.1f} GB/h; projected remaining {remaining_gb/max(rate,1e-9)/3600:,.1f} h")
res = pd.DataFrame(results); res.to_csv(OUT / "las_processing_log.csv", index=False)
log.info(f"Done: {(res['status']=='ok').sum()} ok, {res['status'].str.contains('skipped').sum()} skipped, {(~res['status'].isin(['ok']) & ~res['status'].str.contains('skipped')).sum()} problems, {(time.time()-t0)/3600:,.2f} h")
res["status"].value_counts()'''),
("md", "## 5. Merge partials and write the 30 m rasters"),
("code", '''partials = sorted(PART_DIR.glob("*.npz"))
log.info(f"Merging {len(partials)} partials")
metrics = lidar.merge_partials(partials, grid, percentile=L["height_percentile"])
names = {"cover": "lidar2022_cover_30m.tif", "cover5": "lidar2022_cover5m_30m.tif", "p95": "lidar2022_p95_30m.tif",
         "mid_canopy_frac": "lidar2022_midcanopy_30m.tif", "return_density": "lidar2022_return_density_30m.tif",
         "ground_density": "lidar2022_ground_density_30m.tif", "solid_frac": "lidar2022_solid_frac_30m.tif"}
for key, fname in names.items():
    arr = np.where(np.isfinite(metrics[key]), metrics[key], L["nodata"]).astype("float32")
    with rasterio.open(RAW / fname, "w", driver="GTiff", height=nrow, width=ncol, count=1, dtype="float32", crs=wcrs,
                       transform=out_transform, nodata=L["nodata"], tiled=True, compress="deflate") as dst:
        dst.write(arr, 1)
    v = metrics[key][np.isfinite(metrics[key])]
    log.info(f"{fname}: n={v.size:,} min {v.min():.2f} median {np.median(v):.2f} p90 {np.percentile(v, 90):.2f} max {v.max():.2f}")
log.info(f"CHM tiles in {CHM_DIR}: {len(list(CHM_DIR.glob('*.tif')))}; DTM tiles in {DTM_DIR}: {len(list(DTM_DIR.glob('*.tif')))}. Run 00_lidar next for ITD and rumple.")'''),
("md", "## 6. Publish to the share\n\nCopies the 30 m rasters, the CHM tiles plus a VRT, the tile index, and the processing log to `lidar.publish_dir` on the server so the products live next to the source LAZ and other machines can read them. Partials stay local (they are only for resuming)."),
("code", '''import shutil
pub = Path(L["publish_dir"]) if L.get("publish_dir") else None
if pub and not cfg["run"]["synthetic"]:
    (pub / "metrics_30m").mkdir(parents=True, exist_ok=True)
    for fname in names.values():
        shutil.copy2(RAW / fname, pub / "metrics_30m" / fname)
    n_tiles = 0
    for src_dir, sub, vrt_name in [(CHM_DIR, "chm_1m_tiles", "chm_2022_1m.vrt"), (DTM_DIR, "dtm_1m_tiles", "dtm_2022_1m.vrt")]:
        (pub / sub).mkdir(parents=True, exist_ok=True)
        for t in src_dir.glob("*.tif"):
            dst = pub / sub / t.name
            if not dst.exists() or dst.stat().st_mtime < t.stat().st_mtime:
                shutil.copy2(t, dst); n_tiles += 1
        try:
            from osgeo import gdal
            gdal.BuildVRT(str(pub / sub / vrt_name), sorted(str(p) for p in (pub / sub).glob("*.tif")))
        except ImportError:
            log.info(f"osgeo not available; build {vrt_name} with gdalbuildvrt or a Pro mosaic dataset")
    for f in ["las_tile_index.csv", "las_tile_index.gpkg", "las_processing_log.csv", "drive_inventory.csv", "las_checks.png"]:
        if (OUT / f).exists(): shutil.copy2(OUT / f, pub / f)
    shutil.copy2("config.yaml", pub / f"config_{pd.Timestamp.today():%Y%m%d}.yaml")
    log.info(f"Published metrics, {n_tiles} new CHM and DTM tiles, index, and config to {pub}")
else:
    log.info("publish_dir not set or synthetic run; nothing published")'''),
("md", """## arcpy engine (alternative, for unclassified tiles)

Runs only when `lidar.engine` is `arcpy`. Builds a LAS dataset over the selected tiles (Pro reads LAZ directly), classifies ground if the sample showed none, and writes DTM, DSM, and CHM with LAS Dataset To Raster. Then set `engine` back to `laspy` and rerun section 4 for the point metrics."""),
("code", '''if L["engine"] == "arcpy" and not cfg["run"]["synthetic"]:
    import arcpy
    arcpy.env.overwriteOutput = True
    lasd = str(PROC / "lidar2022.lasd")
    arcpy.management.CreateLasDataset(";".join(idx["path"]), lasd, "NO_RECURSION", None, arcpy.SpatialReference(epsg_work), "COMPUTE_STATS")
    if cls["pct_ground"].min() < 1:
        arcpy.ddd.ClassifyLasGround(lasd, "STANDARD", "REUSE_GROUND", None, "PROCESS_ENTIRE_FILES", "COMPUTE_STATS")
        log.info("Ground classified with ClassifyLasGround")
    ground = arcpy.management.MakeLasDatasetLayer(lasd, "ground_lyr", class_code=[L["ground_class"]])
    arcpy.conversion.LasDatasetToRaster(ground, str(PROC / "dtm_1m.tif"), "ELEVATION", "BINNING AVERAGE LINEAR", "FLOAT", "CELLSIZE", L["dtm_res_m"])
    first = arcpy.management.MakeLasDatasetLayer(lasd, "first_lyr", return_values=["1"])
    arcpy.conversion.LasDatasetToRaster(first, str(PROC / "dsm_1m.tif"), "ELEVATION", "BINNING MAXIMUM NONE", "FLOAT", "CELLSIZE", 1)
    from arcpy.sa import Raster, Con, IsNull
    chm = Con(IsNull(Raster(str(PROC / "dsm_1m.tif"))), 0, Raster(str(PROC / "dsm_1m.tif")) - Raster(str(PROC / "dtm_1m.tif")))
    chm.save(str(CHM_DIR / "chm_1m_arcpy.tif"))
    log.info(f"arcpy CHM written to {CHM_DIR}")
else:
    log.info("arcpy engine not selected")'''),
("md", "## Sanity checks"),
("code", '''df = pd.DataFrame({k: metrics[k].ravel() for k in ["cover", "p95", "mid_canopy_frac", "return_density", "ground_density"]}).dropna()
log.info(f"corr cover~p95 {df['cover'].corr(df['p95']):.2f}; mid~cover {df['mid_canopy_frac'].corr(df['cover']):.2f}")
print(df.describe().round(2))
log.info(f"Share of cells under 4 returns/m2: {(df['return_density'] < 4).mean():.1%} (F3 demo used 7-10 pts/m2)")
try:
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 3, figsize=(13, 3.5))
    ax[0].hexbin(df["p95"], df["cover"], gridsize=35, bins="log"); ax[0].set_xlabel("p95 HAG (m)"); ax[0].set_ylabel("first-return cover > 2 m (%)")
    ax[1].hexbin(df["cover"], df["mid_canopy_frac"], gridsize=35, bins="log"); ax[1].set_xlabel("cover (%)"); ax[1].set_ylabel("mid-canopy fraction 2-8 m")
    ax[2].imshow(metrics["cover"], cmap="Greens", vmin=0, vmax=100); ax[2].set_title("cover, 30 m"); ax[2].axis("off")
    plt.tight_layout(); plt.savefig(OUT / "las_checks.png", dpi=120); plt.show()
except Exception as e:
    log.info(f"plots skipped: {e}")
log.info("00a complete")'''),
]

n = nbf.v4.new_notebook()
n["cells"] = [nbf.v4.new_markdown_cell(c[1]) if c[0] == "md" else nbf.v4.new_code_cell(c[1]) for c in cells]
n["metadata"]["kernelspec"] = {"name": "python3", "display_name": "Python 3 (arcgispro-py3)", "language": "python"}
nbf.write(n, NB / "00a_las_to_chm.ipynb")
print("wrote 00a_las_to_chm")
