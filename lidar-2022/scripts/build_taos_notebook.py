"""Generate notebooks/00b_taos.ipynb. Run: python scripts/build_taos_notebook.py"""
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
from src.io import load_config, get_logger
from src import taos
cfg = load_config()
log = get_logger("00b_taos")
T = cfg["taos"]; L = cfg["lidar"]; g = cfg["crs"]["lidar_grid_m"]; wcrs = cfg["crs"]["working"]
RAW = Path(cfg["paths"]["raw"]); OUT = Path(cfg["paths"]["outputs"])
CHM_DIR = Path(T["chm_tiles_dir"]); TAO_DIR = Path(T["out_dir"]); TAO_DIR.mkdir(parents=True, exist_ok=True)
log.info(f"Project: {cfg['project']['name']} | synthetic={cfg['run']['synthetic']} | CHM tiles {CHM_DIR} | workers={T['workers']} | polygons={T['write_polygons']}")'''

cells = [
("md", """# 00b Tree approximate objects (TAOs)

First-pass individual tree segmentation from the 1 m CHM tiles written by `00a`. For each tile: Gaussian smooth, detect tops with the height-dependent local-maximum window (same rule as `00_lidar`, so the ITD count and the TAO count agree), grow a crown around each top with marker-controlled watershed, trim crown pixels below half the top height, and write a tree table (GeoParquet points) plus crown polygons (GeoPackage). Tiles are read with a pad from their neighbours through a VRT so crowns on tile edges are segmented whole; each crown belongs to the tile that owns its top, so no tree is written twice.

Outputs feed three things: the TAO layer LITIDA takes as input (Shengli), detected-tree density per 30 m cell as the density proxy check, and crown cover, clump, and gap metrics later. The second pass, point-cloud segmentation with lidR on `LAZ_Basin_HAG`, is in `scripts/segment_trees_lidr.R` and is run where the CHM watershed is known to struggle (dense fir merges, big pines split) and at plot footprints for validation.

Resumable per tile; parallel with `taos.workers`. Basin-wide expect 10 to 35 million trees; points are cheap, polygons are not, so `taos.write_polygons: false` is the fast path when only the tree table is needed."""),
("code", HEADER),
("md", "## 1. Tiles and VRT"),
("code", '''tiles = sorted(CHM_DIR.glob("*_chm1m.tif"))
assert tiles, f"no CHM tiles in {CHM_DIR}; run 00a first"
log.info(f"{len(tiles)} CHM tiles")
vrt = None
try:
    from osgeo import gdal
    vrt = str(RAW / "chm_2022_1m_for_taos.vrt"); gdal.BuildVRT(vrt, [str(t) for t in tiles])
except ImportError:
    import shutil, subprocess
    if shutil.which("gdalbuildvrt"):
        vrt = str(RAW / "chm_2022_1m_for_taos.vrt"); subprocess.run(["gdalbuildvrt", vrt] + [str(t) for t in tiles], check=True)
    else:
        log.warning("No GDAL VRT; tiles segmented without neighbour padding (edge crowns may be clipped)")
if vrt: log.info(f"VRT for neighbour padding: {vrt}")'''),
("md", "## 2. Segment (parallel, resumable)"),
("code", '''from multiprocessing import get_context
jobs = [{"chm_path": str(t), "out_dir": str(TAO_DIR), "cfg_taos": T, "nodata": L["nodata"], "wcrs": wcrs,
         "write_polygons": T["write_polygons"], "pad_px": T["pad_px"], "vrt": vrt} for t in tiles]
results = []; t0 = time.time(); n_trees = 0
workers = max(1, int(T["workers"])) if not cfg["run"]["synthetic"] else 2
with get_context("spawn").Pool(workers) as pool:
    for i, r in enumerate(pool.imap_unordered(taos.segment_tile, jobs), 1):
        results.append(r); n_trees += r.get("trees", 0)
        if "skipped" not in r["status"]:
            log.info(f"[{i}/{len(jobs)}] {r['tile']}: {r['status']}, {r['trees']:,} trees, {r['seconds']}s")
        if i in (5, 20, 50) or i % 200 == 0:
            rate = i / max(time.time() - t0, 1)
            log.info(f"Projected remaining: {(len(jobs) - i) / max(rate, 1e-9) / 3600:,.1f} h")
res = pd.DataFrame(results); res.to_csv(OUT / "taos_processing_log.csv", index=False)
log.info(f"Done: {(res['status'].str.startswith('ok')).sum()} ok, {res['status'].str.contains('skipped').sum()} skipped, {n_trees:,} new trees, {(time.time()-t0)/60:,.1f} min")'''),
("md", "## 3. Merge the tree table and write the TAO density raster\n\nThe density raster uses the same 30 m grid origin as every other product, so it stacks with `lidar2022_itd_per_ac_30m.tif` for a direct comparison."),
("code", '''import geopandas as gpd
trees = taos.merge_trees(sorted(TAO_DIR.glob("*_trees.parquet")))
log.info(f"Merged tree table: {len(trees):,} trees; height median {trees['height_m'].median():.1f} m, crown area median {trees['crown_area_m2'].median():.1f} m2")
trees.to_parquet(OUT / "taos_trees_2022.parquet", index=False)

ox, oy = L["grid_origin"]
b = trees.total_bounds
x0 = ox + np.floor((b[0] - ox) / g) * g; y1 = oy + np.ceil((b[3] - oy) / g) * g
ncol = int(np.ceil((b[2] - x0) / g)); nrow = int(np.ceil((y1 - b[1]) / g))
grid = (float(x0), float(y1), g, nrow, ncol)
dens = taos.tao_density_raster(trees, grid, RAW / "lidar2022_tao_per_ac_30m.tif", wcrs, nodata=L["nodata"], min_height_m=T["density_min_height_m"])
log.info(f"TAO density: median {np.median(dens[dens>0]):.0f} trees/ac over {int((dens>0).sum()):,} cells -> data/raw/lidar2022_tao_per_ac_30m.tif")'''),
("md", "## 4. Checks\n\nHeight distribution, crown diameter against height (should rise, roughly 0.2 to 0.4 times height for Sierra conifers), and agreement between the TAO count and the ITD count from `00_lidar` where both exist."),
("code", '''print(trees[["height_m", "crown_area_m2", "crown_diam_m"]].describe().round(1))
itd_path = RAW / "lidar2022_itd_per_ac_30m.tif"
if itd_path.exists():
    with rasterio.open(itd_path) as r:
        itd = r.read(1); itd_tr = r.transform
    # sample TAO density at ITD cell centers (grids may differ in extent)
    rr, cc = np.nonzero(itd > 0)
    xs, ys = rasterio.transform.xy(itd_tr, rr, cc)
    ci = np.floor((np.asarray(xs) - x0) / g).astype(int); ri = np.floor((y1 - np.asarray(ys)) / g).astype(int)
    ok = (ri >= 0) & (ri < nrow) & (ci >= 0) & (ci < ncol)
    a = itd[rr[ok], cc[ok]]; bb = dens[ri[ok], ci[ok]]
    log.info(f"TAO vs ITD density: corr {np.corrcoef(a, bb)[0,1]:.3f}; median ratio TAO/ITD {np.median(bb[a>0]/a[a>0]):.2f} (expect ~1; below 1 means small crowns were dropped by min_crown_area)")
try:
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 3, figsize=(13, 3.5))
    ax[0].hist(trees["height_m"], bins=40); ax[0].set_xlabel("tree height (m)")
    ax[1].hexbin(trees["height_m"], trees["crown_diam_m"], gridsize=35, bins="log"); ax[1].set_xlabel("height (m)"); ax[1].set_ylabel("crown diameter (m)")
    ax[2].imshow(dens, cmap="viridis"); ax[2].set_title("TAO trees/ac, 30 m"); ax[2].axis("off")
    plt.tight_layout(); plt.savefig(OUT / "taos_checks.png", dpi=120); plt.show()
except Exception as e:
    log.info(f"plots skipped: {e}")'''),
("md", "## 5. Publish\n\nTree table (one GeoParquet), crown polygons per tile, the density raster, and the log go to `taos.publish_dir` on the share. The tree table is what Shengli receives for LITIDA, with `tree_id`, `x`, `y`, `height_m`, `crown_area_m2`, `crown_mean_h_m`, `crown_diam_m`, and `tile`."),
("code", '''import shutil
pub = Path(T["publish_dir"]) if T.get("publish_dir") else None
if pub and not cfg["run"]["synthetic"]:
    pub.mkdir(parents=True, exist_ok=True); (pub / "crowns_by_tile").mkdir(exist_ok=True)
    shutil.copy2(OUT / "taos_trees_2022.parquet", pub / "taos_trees_2022.parquet")
    shutil.copy2(RAW / "lidar2022_tao_per_ac_30m.tif", pub / "lidar2022_tao_per_ac_30m.tif")
    n = 0
    for c in TAO_DIR.glob("*_crowns.gpkg"):
        dst = pub / "crowns_by_tile" / c.name
        if not dst.exists() or dst.stat().st_mtime < c.stat().st_mtime: shutil.copy2(c, dst); n += 1
    for f in ["taos_processing_log.csv", "taos_checks.png"]:
        if (OUT / f).exists(): shutil.copy2(OUT / f, pub / f)
    shutil.copy2("config.yaml", pub / f"config_{pd.Timestamp.today():%Y%m%d}.yaml")
    log.info(f"Published tree table, density raster, {n} crown tiles to {pub}")
else:
    log.info("publish_dir not set or synthetic run; nothing published")
log.info("00b complete")'''),
]

n = nbf.v4.new_notebook()
n["cells"] = [nbf.v4.new_markdown_cell(c[1]) if c[0] == "md" else nbf.v4.new_code_cell(c[1]) for c in cells]
n["metadata"]["kernelspec"] = {"name": "python3", "display_name": "Python 3 (arcgispro-py3)", "language": "python"}
nbf.write(n, NB / "00b_taos.ipynb")
print("wrote 00b_taos")
