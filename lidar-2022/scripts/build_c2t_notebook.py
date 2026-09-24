"""Generate notebooks/00c_cloud2trees.ipynb. Run: python scripts/build_c2t_notebook.py"""
import nbformat as nbf
from pathlib import Path

NB = Path(__file__).resolve().parent.parent / "notebooks"

HEADER = '''import sys, os, time
print(sys.executable)
from pathlib import Path
os.chdir(Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd())
sys.path.insert(0, str(Path.cwd()))
import numpy as np, pandas as pd, geopandas as gpd
import rasterio
from src.io import load_config, get_logger
from src import c2t
cfg = load_config()
log = get_logger("00c_cloud2trees")
C = cfg["c2t"]; L = cfg["lidar"]; T = cfg["taos"]; wcrs = cfg["crs"]["working"]
RAW = Path(cfg["paths"]["raw"]); OUT = Path(cfg["paths"]["outputs"]); C2T_OUT = Path(C["output_dir"]); C2T_OUT.mkdir(parents=True, exist_ok=True)
os.environ["C2T_EXT_DIR"] = str(Path(C["ext_dir"]).resolve())
log.info(f"Project: {cfg['project']['name']} | synthetic={cfg['run']['synthetic']} | c2t version {c2t.__version__} (port of {c2t.__r_source__})")'''

cells = [
("md", """# 00c cloud2trees (Python port)

Runs the Python port of the cloud2trees R package (`src/c2t`, see `docs/C2T_PORT.md`) on a subset of tiles, then attributes the 00b tree table with the back half of cloud2trees that TRPA did not have: DBH from FIA allometry (TreeMap 2022), forest type group, competition, height of maximum crown diameter, crown base height, crown biomass, and the LANL TREES export for QUIC-Fire.

Two things happen here. First, `c2t.cloud2trees()` reproduces the whole R pipeline on `c2t.input_las` with the cloud2trees defaults (0.25 or 0.5 m CHM, variable window local maxima, watershed crowns with no crown floor) so it can be compared against `00b_taos` on the same ground. Second, the `trees_*` functions run on the 00b tree table, which is the Basin product Shengli receives, so the attributes land on the TAO layer without redoing rasters. This notebook is not the Basin-scale pipeline; 00a and 00b are.

External data: TreeMap 2022, Forest Type Groups (30 m), and LANDFIRE CBD, about 7 GB, downloaded once with `c2t.get_data()` to `c2t.ext_dir`. Without them DBH, type, and biomass are skipped and the log says so."""),
("code", HEADER),
("md", "## 1. External data\n\nChecks what is present under `c2t.ext_dir`; downloads when `c2t.download_ext_data` is true. Each dataset has its own `get_*()` if only one is needed."),
("code", '''ext = c2t.find_ext_data(ext_dir=C["ext_dir"])
log.info(f"External data: {ext}")
if C.get("download_ext_data") and (not ext["treemap_dir"] or not ext["foresttype_dir"] or not ext["landfire_dir"]):
    got = c2t.get_data(savedir=C["ext_dir"], log=log)
    ext = c2t.find_ext_data(ext_dir=C["ext_dir"]); log.info(f"After download: {ext}")
have_treemap = ext["treemap_dir"] is not None; have_type = ext["foresttype_dir"] is not None; have_landfire = ext["landfire_dir"] is not None
if not have_treemap: log.warning("no TreeMap: DBH will be skipped (set c2t.download_ext_data: true or run c2t.get_treemap())")'''),
("md", "## 2. Input tiles\n\n`c2t.input_las` is a folder, a glob, or a list of tiles on the share. The synthetic run writes one fake tile so the chain runs in seconds."),
("code", '''if cfg["run"]["synthetic"]:
    from src.c2t.synthetic import synthetic_tile
    syn_dir = RAW / "c2t_synthetic"; syn_dir.mkdir(parents=True, exist_ok=True)
    truth = synthetic_tile(syn_dir / "synthetic_tile.laz")
    input_las = str(syn_dir); log.info(f"synthetic tile with {len(truth)} trees at {syn_dir}")
else:
    input_las = C["input_las"]
tiles = c2t.list_las(input_las)
assert tiles, f"no .las/.laz found at {input_las}"
hdr = pd.DataFrame([c2t.cloud.las_header(t) for t in tiles[:50]])
log.info(f"{len(tiles)} tiles; first {len(hdr)} hold {hdr['n_points'].sum():,} points; CRS {hdr['crs'].iloc[0]}")
aoi = None
if C.get("aoi"):
    from src import layers
    aoi = layers.read_layer(C["aoi"], cfg, log=log).to_crs(wcrs)   # file or TRPA REST URL, as in 00a'''),
("md", "## 3. Window function check (`itd_tuning`)\n\nUp to five 0.1 ha samples run through the linear, exponential, and logarithmic window functions. Look at the crown outlines, pick the function that follows visible crowns in dense fir and in open pine, and set `c2t.ws`."),
("code", '''pre = c2t.cloud2raster(C2T_OUT / "tuning", tiles[:2], dtm_res_m=C["dtm_res_m"], chm_res_m=C["chm_res_m"], min_height=C["min_height"],
                       max_height=C["max_height"], accuracy_level=C["accuracy_level"], noise_level=C["noise_level"], ground=C["ground"],
                       write_normalized=False, workers=1, log=log)
tune = c2t.itd_tuning(input_chm_rast=pre["chm_path"], n_samples=C.get("itd_tuning_samples", 3), min_height=C["min_height"], out_png=OUT / "c2t_itd_tuning.png", log=log)
print(tune["summary"].pivot_table(index="ws_fn", values=["n", "tpha", "height_mean_m", "crown_area_mean_m2"], aggfunc="mean").round(1))
log.info(f"itd_tuning done; using ws = {C['ws']} for the run")'''),
("md", "## 4. Full cloud2trees run on the tiles\n\nSame argument names and defaults as the R function. Optional steps are wrapped like `purrr::safely`: a failing step is logged and the run continues without its columns. Outputs land in `<output_dir>/point_cloud_processing_delivery`."),
("code", '''t0 = time.time()
ans = c2t.cloud2trees(
    C2T_OUT / "run", input_las if not cfg["run"]["synthetic"] else tiles, ws=C["ws"],
    accuracy_level=C["accuracy_level"], noise_level=C["noise_level"], ground=C["ground"], dtm_res_m=C["dtm_res_m"], chm_res_m=C["chm_res_m"],
    min_height=C["min_height"], max_height=C["max_height"], workers=C["workers"] if not cfg["run"]["synthetic"] else 1,
    estimate_tree_dbh=C["estimate_tree_dbh"] and have_treemap, dbh_model_regional=C["dbh_model_regional"], dbh_model_local=C["dbh_model_local"],
    estimate_dbh_from_cloud=C["estimate_dbh_from_cloud"], estimate_tree_competition=C["estimate_tree_competition"],
    estimate_tree_type=C["estimate_tree_type"] and have_type, estimate_tree_hmd=C["estimate_tree_hmd"], hmd_estimate_missing_hmd=C["hmd_estimate_missing_hmd"],
    estimate_tree_cbh=C["estimate_tree_cbh"], cbh_estimate_missing_cbh=C["cbh_estimate_missing_cbh"], cbh_tree_sample_n=C["cbh_tree_sample_n"],
    estimate_biomass_method=C["estimate_biomass_method"] if have_landfire else None, study_boundary=aoi, log=log)
crowns = ans["crowns_sf"]
log.info(f"cloud2trees: {len(crowns):,} trees in {(time.time()-t0)/60:.1f} min; errors: {ans['errors']}")
print(crowns.drop(columns="geometry").describe().T.round(2).head(30))'''),
("md", "## 5. Compare with 00b on the same tiles\n\nTree count, height distribution, and crown area from the c2t run against the 00b TAO table clipped to the run extent. Expect c2t to find more small trees (0.25 to 0.5 m CHM, no crown floor, 2 m minimum height against 3 m in 00b)."),
("code", '''taos_path = Path(C["taos_table"]) if C.get("taos_table") else OUT / "taos_trees_2022.parquet"
if taos_path.exists():
    taos = gpd.read_parquet(taos_path)
    b = crowns.total_bounds
    sub = taos.cx[b[0]:b[2], b[1]:b[3]]
    log.info(f"00b TAOs in the run extent: {len(sub):,} vs c2t {len(crowns):,} (ratio {len(crowns)/max(len(sub),1):.2f})")
    print(pd.DataFrame({"c2t_height": crowns["tree_height_m"].describe(), "taos_height": sub["height_m"].describe(),
                        "c2t_crown_m2": crowns["crown_area_m2"].describe(), "taos_crown_m2": sub["crown_area_m2"].describe()}).round(1))
    try:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(1, 2, figsize=(9, 3.4))
        ax[0].hist(crowns["tree_height_m"], bins=40, alpha=.6, label="c2t"); ax[0].hist(sub["height_m"], bins=40, alpha=.6, label="00b"); ax[0].set_xlabel("height (m)"); ax[0].legend()
        ax[1].hist(crowns["crown_area_m2"].clip(upper=150), bins=40, alpha=.6, label="c2t"); ax[1].hist(sub["crown_area_m2"].clip(upper=150), bins=40, alpha=.6, label="00b"); ax[1].set_xlabel("crown area (m2)")
        plt.tight_layout(); plt.savefig(OUT / "c2t_vs_taos.png", dpi=120); plt.show()
    except Exception as e:
        log.info(f"plot skipped: {e}")
else:
    sub = None; log.info(f"no 00b tree table at {taos_path}; run 00b first for the comparison")'''),
("md", "## 6. Attribute the 00b tree table\n\nThe production path: `trees_dbh`, `trees_type`, `trees_competition` on the TAO points (no point cloud needed), then `trees_hmd`, `trees_cbh`, and `trees_biomass` where crown polygons and `LAZ_Basin_HAG` are available. Restricted to `c2t.taos_aoi` (or the run extent) so it stays a subset; the Basin-wide attribution is a server run with `scripts/run_c2t.py`."),
("code", '''if sub is not None and len(sub):
    trees = c2t.from_taos(sub)
    if have_treemap and C["estimate_tree_dbh"]:
        trees = c2t.trees_dbh(trees, study_boundary=aoi, dbh_model_regional=C["dbh_model_regional"], outfolder=str(C2T_OUT / "taos_dbh"), log=log)
        log.info(f"TAO DBH: median {trees['dbh_cm'].median():.1f} cm; BA per tree median {trees['basal_area_m2'].median():.4f} m2")
    if have_type and C["estimate_tree_type"]:
        trees, _ = c2t.trees_type(trees, study_boundary=aoi, log=log)
        print(trees["forest_type_group"].value_counts().head())
    if C["estimate_tree_competition"]:
        trees = c2t.trees_competition(trees, competition_buffer_m=C.get("competition_buffer_m", 5), search_dist_max=C.get("competition_max_search_dist_m", 10))
    crowns_dir = Path(T["out_dir"])
    crown_files = sorted(crowns_dir.glob("*_crowns.gpkg")) if crowns_dir.exists() else []
    norm_dir = L.get("normalized_dir")
    if crown_files and norm_dir and Path(norm_dir).exists() and (C["estimate_tree_hmd"] or C["estimate_tree_cbh"]):
        poly = gpd.GeoDataFrame(pd.concat([gpd.read_file(f) for f in crown_files], ignore_index=True), crs=wcrs)
        poly = poly.rename(columns={"tree_id": "treeID", "height_m": "tree_height_m"})
        poly = poly[poly["treeID"].isin(trees["treeID"])]
        if C["estimate_tree_hmd"]:
            hmd = c2t.trees_hmd(poly, norm_dir, estimate_missing_hmd=C["hmd_estimate_missing_hmd"], outfolder=str(C2T_OUT / "taos_hmd"), log=log)
            trees = trees.merge(pd.DataFrame(hmd.drop(columns="geometry"))[["treeID", "max_crown_diam_height_m", "is_training_hmd"]], on="treeID", how="left")
        if C["estimate_tree_cbh"]:
            cbh = c2t.trees_cbh(poly, norm_dir, tree_sample_n=C["cbh_tree_sample_n"], estimate_missing_cbh=C["cbh_estimate_missing_cbh"], outfolder=str(C2T_OUT / "taos_cbh"), log=log)
            trees = trees.merge(pd.DataFrame(cbh.drop(columns="geometry"))[["treeID", "tree_cbh_m", "is_training_cbh"]], on="treeID", how="left")
        if have_landfire and C.get("estimate_biomass_method") and "tree_cbh_m" in trees.columns:
            trees = c2t.trees_biomass(trees, method=C["estimate_biomass_method"], study_boundary=aoi, log=log)
    else:
        log.info("crown polygons or LAZ_Basin_HAG not available here; HMD, CBH, and biomass skipped for the TAO table")
    trees = gpd.GeoDataFrame(trees, geometry="geometry", crs=wcrs)
    trees.to_parquet(OUT / "c2t_taos_attributes.parquet", index=False)
    log.info(f"wrote outputs/c2t_taos_attributes.parquet with {len(trees):,} trees and {len(trees.columns)} columns")
else:
    trees = None'''),
("md", "## 7. Per-cell rollups the threshold work needs\n\nDetected-tree density, basal area, and QMD per 30 m cell from the attributed TAO table, on the same grid origin as every other product. QMD here is from detected trees only, so it is biased high where small trees are hidden under canopy; compare against the plots before reading it as the standard's QMD."),
("code", '''if trees is not None and "basal_area_m2" in trees.columns:
    g = cfg["crs"]["lidar_grid_m"]; ox, oy = L["grid_origin"]
    ci = np.floor((trees.geometry.x - ox) / g).astype(int); ri = np.floor((oy - trees.geometry.y) / g).astype(int)
    cell = pd.DataFrame({"ci": ci, "ri": ri, "ba": trees["basal_area_m2"], "dbh": trees["dbh_cm"]})
    roll = cell.groupby(["ri", "ci"]).agg(n=("ba", "size"), ba_m2=("ba", "sum"), qmd_cm=("dbh", lambda d: np.sqrt(np.mean(np.square(d))))).reset_index()
    ha = g * g / 10000.0
    roll["tpha"] = roll["n"] / ha; roll["ba_m2_ha"] = roll["ba_m2"] / ha
    roll.to_csv(OUT / "c2t_taos_cell_rollup.csv", index=False)
    print(roll[["tpha", "ba_m2_ha", "qmd_cm"]].describe().round(1))'''),
("md", "## 8. LANL TREES export (optional)\n\nWrites the QUIC-Fire inputs (tree list, domain, fuellist, topo) for the run extent when `c2t.lanl_export` is true. Needs CBH, HMD, crown diameter, and a biomass column on the trees."),
("code", '''if C.get("lanl_export") and all(c in crowns.columns for c in ("tree_cbh_m", "max_crown_diam_height_m")) and any(c in crowns.columns for c in ("landfire_tree_kg_per_m3", "cruz_tree_kg_per_m3")):
    lanl = c2t.cloud2trees_to_lanl_trees(crowns, str(C2T_OUT / "run"), study_boundary=aoi, dtm_path=ans["dtm_rast"], topofile=C.get("lanl_topofile", "flat"),
                                         cbd_method="landfire" if "landfire_tree_kg_per_m3" in crowns.columns else "cruz", log=log)
    log.info(f"LANL TREES inputs: {lanl['treelist_path']}")
else:
    log.info("LANL export skipped (c2t.lanl_export false or CBH, HMD, biomass not all present)")'''),
("md", "## 9. Publish\n\nDelivery folder, the attributed TAO table, the tuning figure, and a config snapshot go to `c2t.publish_dir` on the share."),
("code", '''import shutil
pub = Path(C["publish_dir"]) if C.get("publish_dir") else None
if pub and not cfg["run"]["synthetic"]:
    pub.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ans["delivery_dir"], pub / "point_cloud_processing_delivery", dirs_exist_ok=True)
    for f in ["c2t_taos_attributes.parquet", "c2t_taos_cell_rollup.csv", "c2t_itd_tuning.png", "c2t_vs_taos.png"]:
        if (OUT / f).exists(): shutil.copy2(OUT / f, pub / f)
    shutil.copy2("config.yaml", pub / f"config_{pd.Timestamp.today():%Y%m%d}.yaml")
    log.info(f"Published to {pub}")
else:
    log.info("publish_dir not set or synthetic run; nothing published")
log.info("00c complete")'''),
]

n = nbf.v4.new_notebook()
n["cells"] = [nbf.v4.new_markdown_cell(c[1]) if c[0] == "md" else nbf.v4.new_code_cell(c[1]) for c in cells]
n["metadata"]["kernelspec"] = {"name": "python3", "display_name": "Python 3 (arcgispro-py3)", "language": "python"}
nbf.write(n, NB / "00c_cloud2trees.ipynb")
print("wrote 00c_cloud2trees")
